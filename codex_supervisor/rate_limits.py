from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from .models import QuotaDecision, QuotaWindow


def _iter_snapshots(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()

    candidates: list[dict[str, Any]] = []
    single = payload.get("rateLimits")
    if isinstance(single, dict):
        candidates.append(single)

    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, snapshot in by_id.items():
            if isinstance(snapshot, dict):
                copy = dict(snapshot)
                copy.setdefault("limitId", limit_id)
                candidates.append(copy)

    for snapshot in candidates:
        key = (
            snapshot.get("limitId"),
            _window_key(snapshot.get("primary")),
            _window_key(snapshot.get("secondary")),
        )
        if key in seen:
            continue
        seen.add(key)
        yield snapshot


def _window_key(window: Any) -> tuple[Any, ...] | None:
    if not isinstance(window, dict):
        return None
    return (
        window.get("usedPercent"),
        window.get("windowDurationMins"),
        window.get("resetsAt"),
    )


def normalize_rate_limits(payload: dict[str, Any]) -> tuple[QuotaWindow, ...]:
    windows: list[QuotaWindow] = []
    for snapshot in _iter_snapshots(payload):
        limit_id = str(snapshot.get("limitId") or "default")
        limit_name_raw = snapshot.get("limitName")
        limit_name = str(limit_name_raw) if limit_name_raw is not None else None
        for slot in ("primary", "secondary"):
            raw = snapshot.get(slot)
            if not isinstance(raw, dict):
                continue
            used = raw.get("usedPercent")
            if not isinstance(used, (int, float)):
                continue
            duration = raw.get("windowDurationMins")
            reset = raw.get("resetsAt")
            windows.append(
                QuotaWindow(
                    limit_id=limit_id,
                    limit_name=limit_name,
                    slot=slot,
                    used_percent=max(0, min(100, int(used))),
                    duration_mins=int(duration) if isinstance(duration, (int, float)) else None,
                    resets_at=int(reset) if isinstance(reset, (int, float)) else None,
                )
            )
    return tuple(windows)


def classify_duration(duration_mins: int | None) -> str:
    if duration_mins == 300:
        return "5-hour"
    if duration_mins == 10080:
        return "weekly"
    if duration_mins is None:
        return "unknown"
    return f"{duration_mins}-minute"


def decide_availability(payload: dict[str, Any], *, now: int | None = None) -> QuotaDecision:
    current = int(time.time()) if now is None else now
    windows = normalize_rate_limits(payload)

    ordinary_allowed = payload.get("ordinaryUsageAllowed")
    exhausted = [window for window in windows if window.exhausted]

    if ordinary_allowed is False:
        exhausted_resets = [
            w.resets_at for w in exhausted if w.resets_at and w.resets_at > current
        ]
        all_resets = [w.resets_at for w in windows if w.resets_at and w.resets_at > current]
        wake_at = max(exhausted_resets) if exhausted_resets else (min(all_resets) if all_resets else None)
        return QuotaDecision(
            allowed=False,
            reason="backend_denied_ordinary_usage",
            wake_at=wake_at,
            windows=windows,
        )

    if ordinary_allowed is not True:
        return QuotaDecision(
            allowed=False,
            reason="ordinary_usage_availability_unknown",
            wake_at=None,
            windows=windows,
        )

    if not windows:
        return QuotaDecision(
            allowed=False,
            reason="no_quota_windows_returned",
            wake_at=None,
            windows=windows,
        )

    if exhausted:
        future_resets = [w.resets_at for w in exhausted if w.resets_at and w.resets_at > current]
        wake_at = max(future_resets) if future_resets else None
        labels = ", ".join(sorted({classify_duration(w.duration_mins) for w in exhausted}))
        return QuotaDecision(
            allowed=False,
            reason=f"quota_exhausted:{labels}",
            wake_at=wake_at,
            windows=windows,
        )

    return QuotaDecision(
        allowed=True,
        reason="quota_available",
        wake_at=None,
        windows=windows,
    )
