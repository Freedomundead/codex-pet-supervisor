from __future__ import annotations

import hashlib
from pathlib import Path


FROZEN_V036 = {
    "supervisor.py": "753c6a4b651874fa4c9f8eb0b7b1fb129e8070523a69252ec9f8bec770506978",
    "desktop_uia.py": "1e56235d95a129968c642111d4c37c3085c7f0de0ac636480140fac93ba56a7f",
    "rate_limits.py": "6a3933ce0a70318b33e9bfc3f3726051dae16c6d1d007b3e5b27ddc9fbbef185",
    "app_server.py": "95b5ed6ce6b1ec93a576d12ab2a3575394b24bf47451d910831657bae82617d9",
}


def test_working_v036_timer_core_is_frozen_during_ui_polish():
    root = Path(__file__).parents[1] / "codex_supervisor"
    for filename, expected in FROZEN_V036.items():
        actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        assert actual == expected, f"Frozen timer core changed: {filename}"
