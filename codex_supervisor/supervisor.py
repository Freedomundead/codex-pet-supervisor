from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .app_server import AppServerError, CodexAppServer
from .models import GoalStatus, Job, JobStatus, RpcNotification, ThreadOwnership
from .rate_limits import decide_availability
from .store import Store


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    poll_seconds: int = 15
    reset_grace_seconds: int = 15
    approval_policy: str = "on-request"
    approvals_reviewer: str = "auto_review"
    sandbox: str = "workspace-write"


class Supervisor:
    def __init__(self, store: Store, app_server: CodexAppServer, config: SupervisorConfig) -> None:
        self.store = store
        self.app_server = app_server
        self.config = config
        self._wake_event = asyncio.Event()
        self._rate_limits: dict[str, Any] | None = None
        self.app_server.add_notification_handler(self._on_notification)
        if hasattr(self.app_server, "set_server_request_handler"):
            self.app_server.set_server_request_handler(self._on_server_request)

    async def run_forever(self) -> None:
        await self.app_server.start()
        try:
            await self._refresh_rate_limits()
            while True:
                decision = decide_availability(self._rate_limits or {})
                self.store.set_json(
                    "last_quota_decision",
                    {"allowed": decision.allowed, "reason": decision.reason, "wakeAt": decision.wake_at},
                )

                # Timer mode is intentionally independent from queue/thread ownership.
                # It only watches account allowance and, after a denied -> allowed
                # transition, sends one continuation message into the already-open
                # Codex Desktop conversation.
                timer_wake = await self._advance_timer(decision)

                job = self.store.current_job()
                if job is None:
                    if timer_wake is not None:
                        await self._wait_until_reset(timer_wake)
                        await self._refresh_rate_limits()
                    else:
                        await self._wait(self.config.poll_seconds)
                        timer_config = self.store.get_json("timer_config", {})
                        if isinstance(timer_config, dict) and bool(timer_config.get("enabled")):
                            try:
                                await self._refresh_rate_limits()
                            except AppServerError:
                                pass
                    continue

                if not decision.allowed:
                    if job.status is not JobStatus.BLOCKED:
                        self.store.update_job(job.id, status=JobStatus.WAITING_QUOTA)
                    await self._wait_until_reset(decision.wake_at)
                    await self._refresh_rate_limits()
                    continue

                await self._advance_job(job)
                await self._wait(self.config.poll_seconds)
        finally:
            await self.app_server.close()

    async def run_once(self) -> None:
        await self.app_server.start()
        try:
            await self._refresh_rate_limits()
            decision = decide_availability(self._rate_limits or {})
            self.store.set_json(
                "last_quota_decision",
                {"allowed": decision.allowed, "reason": decision.reason, "wakeAt": decision.wake_at},
            )
            await self._advance_timer(decision)
            job = self.store.current_job()
            if job is None:
                return
            if not decision.allowed:
                self.store.update_job(job.id, status=JobStatus.WAITING_QUOTA)
                return
            await self._advance_job(job)
        finally:
            await self.app_server.close()
