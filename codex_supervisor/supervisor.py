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

        self.store.update_job(
            job.id,
            external_baseline_turn_id=baseline_id,
            external_tracked_turn_id=None,
            external_tracked_turn_status=baseline_status or None,
        )

        auto_dispatch = bool(self.store.get_json("desktop_auto_dispatch", True))
        if job.goal_status == "externalDispatchUnconfirmed":
            self.store.update_job(
                job.id,
                status=JobStatus.READY_OWNER,
                last_error=(
                    "The previous Desktop dispatch could not be verified by a new Codex turn. "
                    "The Pet will not resend automatically because that could duplicate work. "
                    "Use Retry Selected after checking the Desktop task."
                ),
            )
            return

        if auto_dispatch and job.thread_id and job.thread_title:
            try:
                from .desktop_uia import DesktopDispatchError, dispatch_to_codex_desktop

                await asyncio.to_thread(
                    dispatch_to_codex_desktop,
                    thread_id=job.thread_id,
                    thread_title=job.thread_title,
                    message=job.prompt,
                )
            except (DesktopDispatchError, OSError) as exc:
                self.store.update_job(
                    job.id,
                    status=JobStatus.READY_OWNER,
                    goal_status="externalReady",
                    last_error=(
                        f"Automatic Desktop dispatch failed: {exc}. "
                        "Use Copy Continue/Mark Continue Sent, or disable/retry Desktop auto-dispatch."
                    ),
                )
                return

            self.store.update_job(
                job.id,
                status=JobStatus.EXTERNAL_ACTIVE,
                goal_status="externalDispatchedAuto",
                last_error=None,
                external_dispatch_at=int(time.time()),
                external_tracked_turn_id=None,
                external_tracked_turn_status=None,
            )
            return

        reason = (
            "Quota is available. This task is owned by Codex Desktop. "
            "Automatic owner dispatch is disabled or this adopted task has no verified title. "
            "Use Copy Continue/Mark Continue Sent."
        )
        self.store.update_job(
            job.id,
            status=JobStatus.READY_OWNER,
            goal_status="externalReady",
            last_error=reason,
        )

    async def _observe_external_active(self, job: Job, latest: dict[str, Any] | None) -> None:
        if not isinstance(latest, dict) or not latest.get("id"):
            return
        turn_id = str(latest.get("id"))
        turn_status = str(latest.get("status") or "")

        tracked_id = job.external_tracked_turn_id
        baseline_id = job.external_baseline_turn_id

        if tracked_id is None:
            if baseline_id and turn_id == baseline_id:
                if job.external_dispatch_at and int(time.time()) - job.external_dispatch_at >= 60:
                    self.store.update_job(
                        job.id,
                        status=JobStatus.READY_OWNER,
                        goal_status="externalDispatchUnconfirmed",
                        last_error=(
                            "Desktop dispatch was attempted, but no new Codex turn appeared within 60 seconds. "
                            "Automatic resend is disabled for this job to avoid duplicate work."
                        ),
                    )
                    self._wake_event.set()
                return
            tracked_id = turn_id
            self.store.update_job(
                job.id,
                external_tracked_turn_id=turn_id,
                external_tracked_turn_status=turn_status or None,
                goal_status="externalRunning" if turn_status == "inProgress" else job.goal_status,
            )

        if turn_id != tracked_id:
            tracked_id = turn_id
            self.store.update_job(
                job.id,
                external_tracked_turn_id=turn_id,
                external_tracked_turn_status=turn_status or None,
            )

        if turn_status == "inProgress":
            self.store.update_job(
                job.id,
                status=JobStatus.EXTERNAL_ACTIVE,
                goal_status="externalRunning",
                last_error=None,
                external_tracked_turn_status=turn_status,
            )
            return

        if turn_status == "completed":
            self.store.update_job(
                job.id,
                status=JobStatus.COMPLETE,
                goal_status="externalComplete",
                last_error=None,
                external_tracked_turn_status=turn_status,
            )
            self._wake_event.set()
            return
        if turn_status == "failed":
            self.store.update_job(
                job.id,
                status=JobStatus.FAILED,
                goal_status="externalFailed",
                last_error="The latest Codex Desktop turn failed.",
                external_tracked_turn_status=turn_status,
            )
            self._wake_event.set()
            return
        if turn_status == "interrupted":
            self.store.update_job(
                job.id,
                status=JobStatus.READY_OWNER,
                goal_status="externalReady",
                last_error="The Desktop turn was interrupted; ready to continue through the existing owner.",
                external_baseline_turn_id=turn_id,
                external_tracked_turn_id=None,
                external_tracked_turn_status=turn_status,
            )
            self._wake_event.set()

    async def _advance_managed_job(self, job: Job) -> None:
        try:
            if job.thread_id is None:
                try:
                    resolved_cwd = self.store.resolve_job_cwd(job)
                except ValueError as exc:
                    self.store.update_job(job.id, status=JobStatus.BLOCKED, last_error=str(exc))
                    return
                if not resolved_cwd.is_dir():
                    self.store.update_job(
                        job.id,
                        status=JobStatus.BLOCKED,
                        last_error=f"Working directory does not exist: {resolved_cwd}",
                    )
                    return
                self.store.update_job(job.id, cwd=str(resolved_cwd))
                thread_id = await self.app_server.start_thread(
                    cwd=str(resolved_cwd),
                    approval_policy=self.config.approval_policy,
                    approvals_reviewer=self.config.approvals_reviewer,
                    sandbox=self.config.sandbox,
                )
                self.store.update_job(
                    job.id,
                    status=JobStatus.ACTIVE,
                    thread_id=thread_id,
                    goal_status=GoalStatus.ACTIVE.value,
                    last_error=None,
                )
                await self.app_server.set_goal(thread_id, objective=job.prompt, status="active")
                return

            await self.app_server.resume_thread(job.thread_id)
            goal = await self.app_server.get_goal(job.thread_id)
            if goal:
                status = str(goal.get("status") or "")
                retry_requested = job.goal_status == "retryRequested"
                if retry_requested and status in {GoalStatus.BLOCKED.value, GoalStatus.PAUSED.value}:
                    await self.app_server.set_goal(job.thread_id, status=GoalStatus.ACTIVE.value)
                    self.store.update_job(
                        job.id,
                        status=JobStatus.ACTIVE,
                        goal_status=GoalStatus.ACTIVE.value,
                        last_error=None,
                    )
                    return
                self._apply_goal_status(job.id, status)
                if status == GoalStatus.COMPLETE.value:
                    return
                if status == GoalStatus.USAGE_LIMITED.value:
                    await self.app_server.set_goal(job.thread_id, status=GoalStatus.ACTIVE.value)
                    self.store.update_job(
                        job.id,
                        status=JobStatus.ACTIVE,
                        goal_status=GoalStatus.ACTIVE.value,
                        last_error=None,
                    )
                    return
                if status in {
                    GoalStatus.BLOCKED.value,
                    GoalStatus.PAUSED.value,
                    GoalStatus.BUDGET_LIMITED.value,
                }:
                    return

            self.store.update_job(job.id, status=JobStatus.ACTIVE, last_error=None)
        except AppServerError as exc:
            self.store.update_job(job.id, last_error=str(exc))
            raise

    def _apply_goal_status(self, job_id: int, status: str) -> None:
        if status == GoalStatus.COMPLETE.value:
            self.store.update_job(job_id, status=JobStatus.COMPLETE, goal_status=status, last_error=None)
        elif status == GoalStatus.USAGE_LIMITED.value:
            self.store.update_job(job_id, status=JobStatus.WAITING_QUOTA, goal_status=status)
        elif status in {
            GoalStatus.BLOCKED.value,
            GoalStatus.PAUSED.value,
            GoalStatus.BUDGET_LIMITED.value,
        }:
            message = {
                GoalStatus.BLOCKED.value: "Codex Goal is blocked. Check the scope/permission boundary, then use Retry Selected.",
                GoalStatus.PAUSED.value: "Codex Goal is paused. Use Retry Selected when it should continue.",
                GoalStatus.BUDGET_LIMITED.value: "Codex Goal budget was reached; this is separate from account quota.",
            }.get(status)
            self.store.update_job(job_id, status=JobStatus.BLOCKED, goal_status=status, last_error=message)
        elif status:
            self.store.update_job(job_id, status=JobStatus.ACTIVE, goal_status=status)

    async def _on_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer effectful approvals for Pet-managed jobs inside their scope.

        v0.2.12 rejected every App Server approval request, which could push a
        perfectly valid managed Goal into BLOCKED. Managed jobs now auto-approve
        ordinary command/file actions that stay inside the selected Pet scope.
        Scope escapes and network escalations fail closed and become visible in
        the UI with an exact reason. Adopted Desktop tasks never route here.
        """
        from pathlib import Path

        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            raise AppServerError(f"Unsupported server request without thread id: {method}")
        job = self._job_for_thread(thread_id)
        if job is None or job.ownership is not ThreadOwnership.MANAGED:
            raise AppServerError("Approval request does not belong to a Pet-managed task")

        def block(reason: str) -> None:
            self.store.update_job(
                job.id,
                status=JobStatus.BLOCKED,
                goal_status=GoalStatus.BLOCKED.value,
                last_error=reason,
            )
            self._wake_event.set()

        write_root = self.store.resolve_job_cwd(job).resolve()
        read_root = write_root
        if job.project_name:
            project = self.store.get_project(job.project_name)
            if project is not None:
                read_root = Path(project.root).expanduser().resolve()

        def is_within(path_value: str, root: Path) -> bool:
            try:
                Path(path_value).expanduser().resolve().relative_to(root)
                return True
            except (ValueError, OSError):
