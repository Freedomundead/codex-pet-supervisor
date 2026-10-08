from pathlib import Path

import pytest

import codex_supervisor.app_server as app_server


def test_explicit_codex_executable(monkeypatch, tmp_path):
    exe = tmp_path / "codex.exe"
    exe.write_text("")
    monkeypatch.setenv("CODEX_EXECUTABLE", str(exe))
    monkeypatch.setattr(app_server.shutil, "which", lambda _: None)

    assert app_server.discover_codex_executable() == str(exe)


def test_discovers_windows_desktop_codex(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_EXECUTABLE", raising=False)
    monkeypatch.setattr(app_server.shutil, "which", lambda _: None)
    local = tmp_path / "Local"
    old = local / "OpenAI" / "Codex" / "bin" / "old" / "codex.exe"
    new = local / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("")
    new.write_text("")
    old.touch()
    new.touch()
    # Ensure deterministic newest selection without depending on filesystem clock resolution.
    old_stat = old.stat()
    new_stat = new.stat()
    import os
    os.utime(old, (old_stat.st_atime, old_stat.st_mtime - 10))
    os.utime(new, (new_stat.st_atime, new_stat.st_mtime + 10))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("APPDATA", raising=False)

    assert Path(app_server.discover_codex_executable()) == new


def test_discovers_npm_launcher(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_EXECUTABLE", raising=False)
    monkeypatch.setattr(app_server.shutil, "which", lambda _: None)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    appdata = tmp_path / "Roaming"
    launcher = appdata / "npm" / "codex.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off")
    monkeypatch.setenv("APPDATA", str(appdata))

    assert Path(app_server.discover_codex_executable()) == launcher


def test_missing_codex_fails_with_actionable_error(monkeypatch):
    monkeypatch.delenv("CODEX_EXECUTABLE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(app_server.shutil, "which", lambda _: None)

    with pytest.raises(app_server.AppServerError, match="CODEX_EXECUTABLE"):
        app_server.discover_codex_executable()
