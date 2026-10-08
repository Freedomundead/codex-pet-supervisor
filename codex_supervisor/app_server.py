from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .models import RpcNotification


class AppServerError(RuntimeError):
    pass


@dataclass(slots=True)
class _PendingRequest:
    future: asyncio.Future[dict[str, Any]]


NotificationHandler = Callable[[RpcNotification], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def discover_codex_executable() -> str:
    """Resolve a usable Codex executable without requiring manual PATH changes.

    Resolution order:
    1. CODEX_EXECUTABLE environment override.
    2. Existing PATH entries (codex/codex.cmd/codex.exe).
    3. Codex Windows desktop-app versioned bin directory.
    4. npm's per-user codex.cmd launcher.
    """

    explicit = os.environ.get("CODEX_EXECUTABLE")
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise AppServerError(
            f"CODEX_EXECUTABLE points to a missing file: {candidate}"
        )

    for name in ("codex", "codex.cmd", "codex.exe"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        desktop_bin = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if desktop_bin.is_dir():
            candidates = [p for p in desktop_bin.glob("*/codex.exe") if p.is_file()]
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return str(candidates[0])

    app_data = os.environ.get("APPDATA")
    if app_data:
        npm_launcher = Path(app_data) / "npm" / "codex.cmd"
        if npm_launcher.is_file():
            return str(npm_launcher)

    raise AppServerError(
        "Codex executable was not found. Install Codex CLI/app, add it to PATH, "
        "or set CODEX_EXECUTABLE to the full path of codex.exe/codex.cmd."
    )


class CodexAppServer:
    """Minimal JSON-RPC client for `codex app-server`."""

    def __init__(self, command: tuple[str, ...] | None = None) -> None:
        self.command = command
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._handlers: list[NotificationHandler] = []
        self._server_request_handler: ServerRequestHandler | None = None
        self._next_id = 1
        self._write_lock = asyncio.Lock()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._handlers.append(handler)

    def set_server_request_handler(self, handler: ServerRequestHandler | None) -> None:
        self._server_request_handler = handler

    async def start(self) -> None:
        if self._process is not None:
            return
        command = self.command
        if command is None:
            command = (discover_codex_executable(), "app-server")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AppServerError(
                f"Codex executable could not be launched: {command[0]}"
            ) from exc

        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex-allowance-supervisor", "version": "0.3.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        await self.notify("initialized")

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin:
                process.stdin.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send_message(payload)

    async def _send_message(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("App Server is not running")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("App Server is not running")

        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = _PendingRequest(future=future)

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send_message(payload)

        try:
            response = await future
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            raise AppServerError(f"{method} failed: {response['error']}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def read_rate_limits(self) -> dict[str, Any]:
        return await self.request("account/rateLimits/read")

    async def start_thread(
        self,
        *,
        cwd: str,
        approval_policy: str,
        approvals_reviewer: str,
        sandbox: str,
    ) -> str:
        result = await self.request(
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": approval_policy,
                "approvalsReviewer": approvals_reviewer,
                "sandbox": sandbox,
                "ephemeral": False,
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or not thread.get("id"):
            raise AppServerError(f"thread/start returned no thread id: {result}")
        return str(thread["id"])

    async def list_threads(
        self,
        *,
        limit: int = 20,
        search_term: str | None = None,
        cwd: str | list[str] | None = None,
        state_db_only: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": min(max(int(limit), 1), 20),
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
            "useStateDbOnly": state_db_only,
        }
        if search_term:
            params["searchTerm"] = search_term
        if cwd:
            params["cwd"] = cwd
        result = await self.request("thread/list", params)
        data = result.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def read_thread_metadata(self, thread_id: str) -> dict[str, Any] | None:
        result = await self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        thread = result.get("thread")
        return thread if isinstance(thread, dict) else None

    async def latest_turn(self, thread_id: str) -> dict[str, Any] | None:
        result = await self.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
        )
        data = result.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    async def resume_thread(self, thread_id: str) -> None:
        await self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})

    async def set_goal(
        self, thread_id: str, *, objective: str | None = None, status: str = "active"
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "status": status}
        if objective is not None:
            params["objective"] = objective
        return await self.request("thread/goal/set", params)

    async def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        result = await self.request("thread/goal/get", {"threadId": thread_id})
        goal = result.get("goal")
        return goal if isinstance(goal, dict) else None

    async def start_turn(self, thread_id: str, text: str) -> str | None:
        result = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text, "textElements": []}],
                "turnTrigger": "codex-allowance-supervisor",
            },
        )
        turn = result.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return None

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                error = AppServerError("Codex App Server exited")
                for pending in self._pending.values():
                    if not pending.future.done():
                        pending.future.set_exception(error)
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            if isinstance(message.get("id"), int) and ("result" in message or "error" in message):
                pending = self._pending.get(message["id"])
                if pending and not pending.future.done():
                    pending.future.set_result(message)
                continue

            method = message.get("method")
            params = message.get("params")

            if isinstance(method, str) and isinstance(message.get("id"), (int, str)):
                request_id = message["id"]
                if self._server_request_handler is not None and isinstance(params, dict):
                    try:
                        result = await self._server_request_handler(method, params)
                        await self._send_message(
                            {"jsonrpc": "2.0", "id": request_id, "result": result}
                        )
                    except Exception as exc:
                        await self._send_message(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "error": {"code": -32000, "message": str(exc)},
                            }
                        )
                else:
                    await self._send_message(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": "codex-allowance-supervisor has no handler for this server request",
                            },
                        }
                    )
                continue

            if isinstance(method, str) and isinstance(params, dict):
                note = RpcNotification(method=method, params=params)
                for handler in tuple(self._handlers):
                    await handler(note)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
