from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from typing import Any


PACKAGE_NAME = "codex-allowance-supervisor"
FALLBACK_VERSION = "0.4.1"
LATEST_RELEASE_API = "https://api.github.com/repos/Freedomundead/codex-pet-supervisor/releases/latest"


class UpdateCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str | None


def current_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return FALLBACK_VERSION


def _version_tuple(value: str) -> tuple[int, ...]:
    text = value.strip().lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if not match:
        raise UpdateCheckError(f"Unsupported release version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def fetch_latest_release(*, timeout_seconds: int = 5) -> dict[str, Any]:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-pet-supervisor-update-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise UpdateCheckError(f"Could not check GitHub Releases: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError("GitHub returned an unexpected release response")
    return payload


def check_for_update(*, timeout_seconds: int = 5) -> UpdateInfo:
    payload = fetch_latest_release(timeout_seconds=timeout_seconds)
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateCheckError("Latest GitHub release has no tag")
    latest = tag.lstrip("vV")
    current = current_version()
    release_url_raw = payload.get("html_url")
    release_url = str(release_url_raw) if isinstance(release_url_raw, str) and release_url_raw else None
    return UpdateInfo(
        current_version=current,
        latest_version=latest,
        update_available=_version_tuple(latest) > _version_tuple(current),
        release_url=release_url,
    )
