from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING_QUOTA = "waiting_quota"
    WAITING_WRITER = "waiting_writer"
    READY_OWNER = "ready_owner"
    EXTERNAL_ACTIVE = "external_active"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


class ThreadOwnership(StrEnum):
    MANAGED = "managed"
    ADOPTED_EXTERNAL = "adopted_external"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usageLimited"
    BUDGET_LIMITED = "budgetLimited"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    prompt: str
    cwd: str
    status: JobStatus
    thread_id: str | None
    goal_status: str | None
    last_error: str | None
    created_at: int
    updated_at: int
    project_name: str | None = None
    scope_name: str | None = None
    thread_origin: str = "pet"
    thread_title: str | None = None
    external_baseline_turn_id: str | None = None
    external_tracked_turn_id: str | None = None
    external_tracked_turn_status: str | None = None
    external_dispatch_at: int | None = None

    @property
    def ownership(self) -> ThreadOwnership:
        if self.thread_origin in {"adopted", ThreadOwnership.ADOPTED_EXTERNAL.value}:
            return ThreadOwnership.ADOPTED_EXTERNAL
        return ThreadOwnership.MANAGED

    @property
    def is_pinned(self) -> bool:
        return bool(self.thread_id or self.cwd)


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    root: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class Scope:
    project_name: str
    name: str
    relative_path: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    limit_id: str
    limit_name: str | None
    slot: str
    used_percent: int
    duration_mins: int | None
    resets_at: int | None

    @property
    def remaining_percent(self) -> int:
        return max(0, 100 - self.used_percent)

    @property
    def exhausted(self) -> bool:
        return self.used_percent >= 100


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    reason: str
    wake_at: int | None
    windows: tuple[QuotaWindow, ...]


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, Any]
