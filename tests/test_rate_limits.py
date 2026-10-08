from codex_supervisor.rate_limits import decide_availability, normalize_rate_limits


def test_normalizes_primary_and_secondary_without_assuming_position_meaning():
    payload = {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 2000},
            "secondary": {"usedPercent": 90, "windowDurationMins": 300, "resetsAt": 1500},
        },
        "rateLimitsByLimitId": None,
    }
    windows = normalize_rate_limits(payload)
    assert {w.duration_mins for w in windows} == {300, 10080}


def test_waits_for_latest_exhausted_window_reset():
    payload = {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1500},
            "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 9000},
        },
    }
    decision = decide_availability(payload, now=1000)
    assert decision.allowed is False
    assert decision.wake_at == 9000
    assert "5-hour" in decision.reason
    assert "weekly" in decision.reason


def test_backend_denial_is_authoritative():
    payload = {
        "ordinaryUsageAllowed": False,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1500},
        },
    }
    decision = decide_availability(payload, now=1000)
    assert decision.allowed is False
    assert decision.reason == "backend_denied_ordinary_usage"


def test_unknown_backend_permission_fails_closed():
    payload = {
        "ordinaryUsageAllowed": None,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1500},
        },
    }
    decision = decide_availability(payload, now=1000)
    assert decision.allowed is False
    assert decision.reason == "ordinary_usage_availability_unknown"


def test_backend_denial_waits_for_latest_visibly_exhausted_constraint():
    payload = {
        "ordinaryUsageAllowed": False,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1500},
            "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 9000},
        },
    }
    decision = decide_availability(payload, now=1000)
    assert decision.allowed is False
    assert decision.wake_at == 9000
