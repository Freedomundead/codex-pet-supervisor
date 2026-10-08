from __future__ import annotations

import io
import json

import pytest

from codex_supervisor import update_check


class _Response:
    def __init__(self, payload: dict):
        self._bytes = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._bytes


def test_version_tuple_accepts_v_prefix():
    assert update_check._version_tuple("v0.4.2") == (0, 4, 2)


def test_update_available_when_latest_release_is_newer(monkeypatch):
    monkeypatch.setattr(update_check, "current_version", lambda: "0.4.1")
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(
            {
                "tag_name": "v0.4.2",
                "html_url": "https://github.com/Freedomundead/codex-pet-supervisor/releases/tag/v0.4.2",
            }
        ),
    )
    info = update_check.check_for_update()
    assert info.current_version == "0.4.1"
    assert info.latest_version == "0.4.2"
    assert info.update_available is True
    assert info.release_url.endswith("/v0.4.2")


def test_no_update_when_versions_match(monkeypatch):
    monkeypatch.setattr(update_check, "current_version", lambda: "0.4.1")
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda request, timeout: _Response({"tag_name": "v0.4.1", "html_url": "https://example.test/release"}),
    )
    assert update_check.check_for_update().update_available is False


def test_network_failure_is_wrapped(monkeypatch):
    def fail(request, timeout):
        raise OSError("offline")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fail)
    with pytest.raises(update_check.UpdateCheckError, match="Could not check GitHub Releases"):
        update_check.check_for_update()
