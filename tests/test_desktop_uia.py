from __future__ import annotations

from codex_supervisor.desktop_uia import _POWERSHELL_PROBE, summarize_probe


def test_probe_script_is_read_only() -> None:
    lowered = _POWERSHELL_PROBE.lower()
    forbidden = ["sendkeys", ".invoke(", "setfocus", "valuepattern", "click", "mouse_event"]
    for token in forbidden:
        assert token not in lowered


def test_summarize_probe_finds_likely_controls() -> None:
    payload = {
        "windows": [
            {
                "elements": [
                    {"name": "Inspect Brain understanding", "automationId": "", "controlType": "ControlType.ListItem"},
                    {"name": "Do anything", "automationId": "composer", "controlType": "ControlType.Edit"},
                    {"name": "Send", "automationId": "send", "controlType": "ControlType.Button"},
                ]
            }
        ]
    }
    summary = summarize_probe(payload)
    assert summary.windows == 1
    assert summary.elements == 3
    assert any("Inspect Brain understanding" in x for x in summary.likely_thread_items)
    assert any("Do anything" in x for x in summary.likely_composers)
    assert any("Send" in x for x in summary.likely_send_controls)


def test_effectful_dispatch_is_guarded_by_exact_title_before_input() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_DISPATCH

    text = _POWERSHELL_DISPATCH
    assert "Could not verify target Codex thread title" in text
    assert "$name -eq $threadTitle" in text
    assert "SendKeys" in text
    assert text.index("$name -eq $threadTitle") < text.index("SendKeys")


def test_timer_dispatch_uses_exact_verified_target_from_status_detector() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_CURRENT_CHAT_DISPATCH, _POWERSHELL_DESKTOP_STATUS

    dispatch = _POWERSHELL_CURRENT_CHAT_DISPATCH
    status = _POWERSHELL_DESKTOP_STATUS
    assert "CODEX_PET_TARGET_PID" in dispatch
    assert "CODEX_PET_TARGET_HWND" in dispatch
    assert "Get-Process -Id $targetPid" in dispatch
    assert "MainWindowHandle -ne $hwnd" in dispatch
    assert "Refusing to target the Pet window" in dispatch
    assert "hwnd=[long]$p.MainWindowHandle" in status
    assert "hwnd=$best.hwnd" in status


def test_timer_dispatch_verifies_foreground_process_before_typing() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_CURRENT_CHAT_DISPATCH

    text = _POWERSHELL_CURRENT_CHAT_DISPATCH
    assert 'GetForegroundWindow' in text
    assert 'GetWindowThreadProcessId' in text
    assert 'Test-ForegroundProcess' in text
    assert 'Windows could not foreground the already-detected Codex Desktop window' in text
    assert 'Codex Desktop lost foreground ownership before the composer click' in text
    assert text.index('Test-ForegroundProcess') < text.index('SendKeys')
    assert 'foregroundProcessVerified = $true' in text


def test_desktop_status_excludes_pet_window_and_returns_handle() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_DESKTOP_STATUS

    assert "MainWindowTitle -eq 'Codex Pet Supervisor'" in _POWERSHELL_DESKTOP_STATUS
    assert "open=$true" in _POWERSHELL_DESKTOP_STATUS
    assert "hwnd=[long]$p.MainWindowHandle" in _POWERSHELL_DESKTOP_STATUS
    assert "hwnd=$best.hwnd" in _POWERSHELL_DESKTOP_STATUS


def test_timer_dispatch_never_uses_title_only_or_appactivate() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_CURRENT_CHAT_DISPATCH

    text = _POWERSHELL_CURRENT_CHAT_DISPATCH
    assert "WScript.Shell" not in text
    assert "AppActivate" not in text
    assert "MainWindowTitle -match 'Codex|ChatGPT'" not in text
    assert "CODEX_PET_TARGET_PID" in text
    assert "CODEX_PET_TARGET_HWND" in text
    assert "chrome|msedge|firefox" in text


def test_timer_dispatch_force_foregrounds_exact_codex_pid() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_CURRENT_CHAT_DISPATCH

    text = _POWERSHELL_CURRENT_CHAT_DISPATCH
    assert "AttachThreadInput" in text
    assert "BringWindowToTop" in text
    assert "SwitchToThisWindow" in text
    assert "Test-ForegroundProcess" in text
    assert "No message was sent" in text
    assert text.index("Force-CodexForeground") < text.index("SendKeys")


def test_ansi_errors_are_cleaned_for_user_facing_dispatch() -> None:
    from codex_supervisor.desktop_uia import _clean_process_output

    dirty = "\x1b[31;1mException: bad\x1b[0m"
    assert _clean_process_output(dirty) == "Exception: bad"


def test_timer_dispatch_imports_get_current_thread_id_from_kernel32() -> None:
    from codex_supervisor.desktop_uia import _POWERSHELL_CURRENT_CHAT_DISPATCH

    text = _POWERSHELL_CURRENT_CHAT_DISPATCH
    assert '[DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();' in text
    assert '[DllImport("user32.dll")] public static extern uint GetCurrentThreadId();' not in text
