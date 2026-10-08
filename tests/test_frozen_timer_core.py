from __future__ import annotations

import hashlib
from pathlib import Path


# Freeze the exact public beta core. Git's blob hash includes file length and
# content, so comments/whitespace cannot change silently during UI-only work.
FROZEN_PUBLIC_CORE = {
    "supervisor.py": "e386bb69d4f037d405e3a42679acd291a023fe65",
    "desktop_uia.py": "da801952d23c81c62cffe4a60ef50c2fb586c95a",
    "rate_limits.py": "810b302c31c5e2c52aa69947a98b9721b19219fa",
    "app_server.py": "c907f9482eb53e8d69fc29ec3d924bbb6c462578",
}


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def test_working_timer_core_is_frozen_during_ui_polish():
    root = Path(__file__).parents[1] / "codex_supervisor"
    for filename, expected in FROZEN_PUBLIC_CORE.items():
        actual = _git_blob_sha1((root / filename).read_bytes())
        assert actual == expected, f"Frozen timer core changed: {filename}"
