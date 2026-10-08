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

    async def _advance_timer(self, decision) -> int | None:
        config = self.store.get_json("timer_config", {})
        if not isinstance(config, dict) or not bool(config.get("enabled")):
            return None

        state = str(config.get("state") or "armed")
        message = str(
            config.get("message")
            or "Continue the current task from where you stopped. Do not repeat completed work."
        ).strip()

        if not decision.allowed:
            config.update(
                {
                    "state": "waiting_reset",
                    "wakeAt": decision.wake_at,
                    "lastReason": decision.reason,
                    "lastError": None,
                }
            )
            self.store.set_json("timer_config", config)
            return decision.wake_at

        if state != "waiting_reset":
            if state not in {"armed", "sent"}:
                config["state"] = "armed"
                self.store.set_json("timer_config", config)
            return None

        try:
            from .desktop_uia import DesktopDispatchError, dispatch_to_current_codex_desktop

            result = await asyncio.to_thread(
                dispatch_to_current_codex_desktop,
                message=message,
            )
        except (DesktopDispatchError, OSError) as exc:
            config.update(
                {
                    "state": "dispatch_failed",
                    "lastError": str(exc),
                    "lastAttemptAt": int(time.time()),
                }
            )
            self.store.set_json("timer_config", config)
            return None

        config.update(
            {
                "state": "sent",
                "wakeAt": None,
                "lastError": None,
                "lastSentAt": int(time.time()),
                "lastDispatch": result,
            }
        )
        self.store.set_json("timer_config", config)
        return None

    async def _advance_job(self, job: Job) -> None:
        if job.ownership is ThreadOwnership.ADOPTED_EXTERNAL:
            await self._advance_external_job(job)
            return

        await self._advance_managed_job(job)

    async def _advance_external_job(self, job: Job) -> None:
        latest: dict[str, Any] | None = None
        if job.thread_id:
            try:
                latest = await self.app_server.latest_turn(job.thread_id)
            except AppServerError as exc:
                self.store.update_job(job.id, last_error=f"Could not observe Desktop task: {exc}")

        if not job.thread_title and job.thread_id:
            try:
                metadata = await self.app_server.read_thread_metadata(job.thread_id)
            except (AppServerError, AttributeError):
                metadata = None
            if isinstance(metadata, dict):
                recovered_title = str(metadata.get("name") or metadata.get("preview") or "").strip()
                if recovered_title:
                    self.store.update_job(job.id, thread_title=recovered_title)
                    refreshed = self.store.get_job(job.id)
                    if refreshed is not None:
                        job = refreshed

        if job.status is JobStatus.EXTERNAL_ACTIVE:
            await self._observe_external_active(job, latest)
            return

        baseline_id = str(latest.get("id")) if isinstance(latest, dict) and latest.get("id") else None
        baseline_status = str(latest.get("status") or "") if isinstance(latest, dict) else ""

        if baseline_id and baseline_status == "inProgress":
            self.store.update_job(
                job.id,
                status=JobStatus.EXTERNAL_ACTIVE,
                goal_status="externalRunning",
                last_error=None,
                external_baseline_turn_id=job.external_baseline_turn_id,
                external_tracked_turn_id=baseline_id,
                external_tracked_turn_status=baseline_status,
            )
            return
