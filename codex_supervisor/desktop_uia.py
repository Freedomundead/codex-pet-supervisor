from __future__ import annotations

import json
import os
import shutil
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def _clean_process_output(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value or "").strip()


class DesktopProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DesktopProbeSummary:
    windows: int
    elements: int
    likely_thread_items: tuple[str, ...]
    likely_composers: tuple[str, ...]
    likely_send_controls: tuple[str, ...]


# Read-only Windows UI Automation probe. It never invokes controls, sends keys,
# changes focus, clicks, or writes into Codex. The only goal is to discover the
# accessibility tree of the user's exact Codex Desktop build before we build a
# DesktopOwnerDispatchAdapter against it.
_POWERSHELL_PROBE = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient

$processes = Get-Process | Where-Object {
    $_.MainWindowHandle -ne 0 -and (
        $_.ProcessName -match '^(Codex|ChatGPT)$' -or
        $_.MainWindowTitle -match 'Codex|ChatGPT'
    )
}

$windows = @()
foreach ($p in $processes) {
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
        if ($null -eq $root) { continue }
        $collection = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        $elements = @()
        $limit = [Math]::Min($collection.Count, 1200)
        for ($i = 0; $i -lt $limit; $i++) {
            $e = $collection.Item($i)
            try {
                $name = [string]$e.Current.Name
                $automationId = [string]$e.Current.AutomationId
                $className = [string]$e.Current.ClassName
                $controlType = [string]$e.Current.ControlType.ProgrammaticName
                if ([string]::IsNullOrWhiteSpace($name) -and [string]::IsNullOrWhiteSpace($automationId)) {
                    continue
                }
                $r = $e.Current.BoundingRectangle
                $elements += [pscustomobject]@{
                    name = $name
                    automationId = $automationId
                    className = $className
                    controlType = $controlType
                    enabled = [bool]$e.Current.IsEnabled
                    keyboardFocusable = [bool]$e.Current.IsKeyboardFocusable
                    rectangle = [pscustomobject]@{
                        left = [double]$r.Left
                        top = [double]$r.Top
                        width = [double]$r.Width
                        height = [double]$r.Height
                    }
                }
            } catch {
                # Accessibility trees are dynamic. One disappearing element must
                # not abort the whole read-only inspection.
            }
        }
        $windows += [pscustomobject]@{
            processName = $p.ProcessName
            pid = $p.Id
            windowTitle = $p.MainWindowTitle
            rootName = [string]$root.Current.Name
            elementCount = $collection.Count
            capturedCount = $elements.Count
            elements = $elements
        }
    } catch {
        $windows += [pscustomobject]@{
            processName = $p.ProcessName
            pid = $p.Id
            windowTitle = $p.MainWindowTitle
            error = $_.Exception.Message
            elements = @()
        }
    }
}

[pscustomobject]@{
    schema = 1
    windows = $windows
} | ConvertTo-Json -Depth 7 -Compress
'''


def _powershell_executable() -> str:
    for candidate in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
        path = shutil.which(candidate)
        if path:
            return path
    raise DesktopProbeError("PowerShell was not found on PATH")


def probe_codex_desktop(*, timeout_seconds: int = 20) -> dict[str, Any]:
    if os.name != "nt":
        raise DesktopProbeError("Codex Desktop UI Automation probing is supported on Windows only")
    exe = _powershell_executable()
    try:
        completed = subprocess.run(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_PROBE],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopProbeError(f"Codex Desktop accessibility probe timed out after {timeout_seconds}s") from exc
    if completed.returncode != 0:
        detail = _clean_process_output(completed.stderr or completed.stdout or "unknown PowerShell error")
        raise DesktopProbeError(f"Codex Desktop accessibility probe failed: {detail}")
    raw = completed.stdout.strip()
    if not raw:
        raise DesktopProbeError("Codex Desktop accessibility probe returned no data")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DesktopProbeError("Codex Desktop accessibility probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DesktopProbeError("Codex Desktop accessibility probe returned an unexpected payload")
    return payload


def save_probe_report(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def summarize_probe(payload: dict[str, Any]) -> DesktopProbeSummary:
    windows = payload.get("windows")
    if not isinstance(windows, list):
        windows = []
    total = 0
    threads: list[str] = []
    composers: list[str] = []
    sends: list[str] = []
    seen: set[tuple[str, str]] = set()

    thread_words = ("task", "thread", "chat", "recent", "project")
    composer_words = ("do anything", "message", "prompt", "composer", "ask")
    send_words = ("send", "submit")

    for window in windows:
        if not isinstance(window, dict):
            continue
        elements = window.get("elements")
        if not isinstance(elements, list):
            continue
        total += len(elements)
        for item in elements:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            automation_id = str(item.get("automationId") or "").strip()
            control_type = str(item.get("controlType") or "")
            key = (name, automation_id)
            if key in seen:
                continue
            seen.add(key)
            hay = f"{name} {automation_id}".lower()
            display = f"{control_type}: {name or automation_id}"
            if (("listitem" in control_type.lower() or "treeitem" in control_type.lower()) and name or any(word in hay for word in thread_words)) and len(threads) < 20:
                threads.append(display)
            if ("edit" in control_type.lower() or "document" in control_type.lower()) and (
                any(word in hay for word in composer_words) or not name
            ) and len(composers) < 20:
                composers.append(display)
            if "button" in control_type.lower() and any(word in hay for word in send_words) and len(sends) < 20:
                sends.append(display)

    return DesktopProbeSummary(
        windows=len(windows),
        elements=total,
        likely_thread_items=tuple(threads),
        likely_composers=tuple(composers),
        likely_send_controls=tuple(sends),
    )


class DesktopDispatchError(RuntimeError):
    pass


# Effectful owner-dispatch adapter. Unlike the probe above, this path deliberately
# acts on Codex Desktop. It is guarded by two checks before typing anything:
#   1. navigate to the exact adopted thread via codex://threads/<thread-id>
#   2. require the expected thread title to be visible in that Codex window's
#      accessibility tree.
# Only after both checks pass does it focus the lower composer region, paste the
# continuation text, and submit it. The caller must verify delivery by observing
# a new Codex turn through the read-only App Server APIs.
_POWERSHELL_DISPATCH = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CodexPetWin32 {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
}
"@

$threadId = [string]$env:CODEX_PET_THREAD_ID
$threadTitle = [string]$env:CODEX_PET_THREAD_TITLE
$message = [string]$env:CODEX_PET_MESSAGE
if ([string]::IsNullOrWhiteSpace($threadId)) { throw 'Missing thread id' }
if ([string]::IsNullOrWhiteSpace($threadTitle)) { throw 'Missing thread title; automatic Desktop dispatch requires a verified title' }
if ([string]::IsNullOrWhiteSpace($message)) { throw 'Missing continuation message' }

Start-Process ("codex://threads/" + $threadId)
Start-Sleep -Milliseconds 1600

$deadline = [DateTime]::UtcNow.AddSeconds(12)
$targetProcess = $null
$targetRoot = $null
$titleMatched = $false
while ([DateTime]::UtcNow -lt $deadline -and -not $titleMatched) {
    $processes = Get-Process | Where-Object {
        $_.MainWindowHandle -ne 0 -and (
            $_.ProcessName -match '^(Codex|ChatGPT)$' -or
            $_.MainWindowTitle -match 'Codex|ChatGPT'
        )
    }
    foreach ($p in $processes) {
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
            if ($null -eq $root) { continue }
            $collection = $root.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                [System.Windows.Automation.Condition]::TrueCondition
            )
            $limit = [Math]::Min($collection.Count, 1500)
            for ($i = 0; $i -lt $limit; $i++) {
                $e = $collection.Item($i)
                try {
                    $name = [string]$e.Current.Name
                    if ($name -eq $threadTitle) {
                        # An exact title can also appear in the left sidebar.
                        # Only accept a match positioned like the active-thread
                        # header: to the right of the navigation rail and near
                        # the top of the window. This prevents typing into a
                        # different thread merely because its sidebar row exists.
                        $wr = $root.Current.BoundingRectangle
                        $er = $e.Current.BoundingRectangle
                        $windowWidth = [double]$wr.Width
                        $windowHeight = [double]$wr.Height
                        $centerX = [double]$er.Left + ([double]$er.Width / 2.0)
                        $centerY = [double]$er.Top + ([double]$er.Height / 2.0)
                        $headerMinX = [double]$wr.Left + ($windowWidth * 0.24)
                        $headerMaxY = [double]$wr.Top + ($windowHeight * 0.28)
                        if ($centerX -ge $headerMinX -and $centerY -le $headerMaxY) {
                            $targetProcess = $p
                            $targetRoot = $root
                            $titleMatched = $true
                            break
                        }
                    }
                } catch {}
            }
            if ($titleMatched) { break }
        } catch {}
    }
    if (-not $titleMatched) { Start-Sleep -Milliseconds 400 }
}
if (-not $titleMatched -or $null -eq $targetProcess) {
    throw ("Could not verify target Codex thread title in Desktop: " + $threadTitle)
}

$hwnd = [IntPtr]$targetProcess.MainWindowHandle
[void][CodexPetWin32]::ShowWindow($hwnd, 9)
[void][CodexPetWin32]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 350

$rect = New-Object CodexPetWin32+RECT
if (-not [CodexPetWin32]::GetWindowRect($hwnd, [ref]$rect)) { throw 'Could not read Codex window rectangle' }
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -lt 700 -or $height -lt 500) { throw 'Codex window is too small for guarded composer dispatch' }

# Codex Desktop's renderer does not currently expose the composer through UIA on
# this Windows build. Use a window-relative point in the center of the composer
# band, never an absolute screen coordinate. The exact thread-title verification
# above prevents this from typing into an unrelated Codex task.
$x = [int]($rect.Left + ($width * 0.67))
$y = [int]($rect.Top + ($height * 0.875))
[void][CodexPetWin32]::SetCursorPos($x, $y)
[CodexPetWin32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
[CodexPetWin32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 300

$oldClipboard = $null
try { $oldClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch {}
Set-Clipboard -Value $message
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Milliseconds 500
if ($null -ne $oldClipboard) {
    try { Set-Clipboard -Value $oldClipboard } catch {}
}

[pscustomobject]@{
    ok = $true
    pid = $targetProcess.Id
    threadId = $threadId
    threadTitle = $threadTitle
    clickX = $x
    clickY = $y
} | ConvertTo-Json -Compress
'''


def dispatch_to_codex_desktop(
    *,
    thread_id: str,
    thread_title: str,
    message: str,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Ask the existing Codex Desktop owner to submit a continuation message.

    This does not touch App Server writer ownership. It navigates the Desktop app
    to the exact thread, verifies the thread title through UI Automation, then
    uses the Desktop composer as the owner-facing submission surface.
    """
    if os.name != "nt":
        raise DesktopDispatchError("Codex Desktop automatic dispatch is supported on Windows only")
    if not thread_id.strip() or not thread_title.strip() or not message.strip():
        raise DesktopDispatchError("Thread id, thread title, and message are required")
    exe = _powershell_executable()
    env = os.environ.copy()
    env["CODEX_PET_THREAD_ID"] = thread_id
    env["CODEX_PET_THREAD_TITLE"] = thread_title
    env["CODEX_PET_MESSAGE"] = message
    try:
        completed = subprocess.run(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_DISPATCH],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopDispatchError(f"Codex Desktop dispatch timed out after {timeout_seconds}s") from exc
    if completed.returncode != 0:
        detail = _clean_process_output(completed.stderr or completed.stdout or "unknown PowerShell error")
        raise DesktopDispatchError(f"Codex Desktop dispatch failed: {detail}")
    raw = completed.stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DesktopDispatchError("Codex Desktop dispatch returned invalid JSON") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise DesktopDispatchError("Codex Desktop dispatch did not report success")
    return payload

# Simple timer-mode dispatch. This deliberately does not inspect, resume, fork,
# or otherwise acquire a Codex thread. It behaves like the user returning to the
# already-open Codex Desktop conversation and sending one continuation message.
_POWERSHELL_CURRENT_CHAT_DISPATCH = r'''
$ErrorActionPreference = 'Stop'
if ($null -ne $PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CodexPetTimerWin32 {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
}
"@

$message = [string]$env:CODEX_PET_MESSAGE
$targetPidText = [string]$env:CODEX_PET_TARGET_PID
$targetHwndText = [string]$env:CODEX_PET_TARGET_HWND
if ([string]::IsNullOrWhiteSpace($message)) { throw 'Missing continuation message' }
if ([string]::IsNullOrWhiteSpace($targetPidText)) { throw 'Missing verified Codex Desktop PID' }
if ([string]::IsNullOrWhiteSpace($targetHwndText)) { throw 'Missing verified Codex Desktop window handle' }

[int]$targetPid = 0
[long]$targetHwndNumber = 0
if (-not [int]::TryParse($targetPidText, [ref]$targetPid)) { throw 'Invalid verified Codex Desktop PID' }
if (-not [long]::TryParse($targetHwndText, [ref]$targetHwndNumber)) { throw 'Invalid verified Codex Desktop window handle' }
$hwnd = [IntPtr]$targetHwndNumber
if (-not [CodexPetTimerWin32]::IsWindow($hwnd)) { throw 'The verified Codex Desktop window no longer exists. No message was sent.' }

try { $targetProcess = Get-Process -Id $targetPid -ErrorAction Stop } catch { throw 'The verified Codex Desktop process is no longer running. No message was sent.' }
if ([IntPtr]$targetProcess.MainWindowHandle -ne $hwnd) { throw 'Codex Desktop changed windows after detection. No message was sent; try again.' }
if ($targetProcess.MainWindowTitle -eq 'Codex Pet Supervisor') { throw 'Refusing to target the Pet window.' }
if ($targetProcess.ProcessName -match '^(python|pythonw|powershell|pwsh|WindowsTerminal|chrome|msedge|firefox)$') { throw ('Refusing unsafe target process: ' + $targetProcess.ProcessName) }
if (-not [CodexPetTimerWin32]::IsWindowVisible($hwnd)) { throw 'The verified Codex Desktop window is not visible. No message was sent.' }

function Test-ForegroundProcess([int]$expectedPid) {
    $fg = [CodexPetTimerWin32]::GetForegroundWindow()
    if ($fg -eq [IntPtr]::Zero) { return $false }
    [uint32]$fgPid = 0
    [void][CodexPetTimerWin32]::GetWindowThreadProcessId($fg, [ref]$fgPid)
    return ([int]$fgPid -eq $expectedPid)
}

function Force-CodexForeground([IntPtr]$targetWindow, [int]$expectedPid) {
    [void][CodexPetTimerWin32]::ShowWindow($targetWindow, 9)
    Start-Sleep -Milliseconds 100
    $fg = [CodexPetTimerWin32]::GetForegroundWindow()
    [uint32]$fgPid = 0
    [uint32]$fgThread = 0
    if ($fg -ne [IntPtr]::Zero) { $fgThread = [CodexPetTimerWin32]::GetWindowThreadProcessId($fg, [ref]$fgPid) }
    [uint32]$ownerPid = 0
    $targetThread = [CodexPetTimerWin32]::GetWindowThreadProcessId($targetWindow, [ref]$ownerPid)
    $currentThread = [CodexPetTimerWin32]::GetCurrentThreadId()
    $attachedForeground = $false
    $attachedTarget = $false
    try {
        if ($fgThread -ne 0 -and $fgThread -ne $currentThread) { $attachedForeground = [CodexPetTimerWin32]::AttachThreadInput($currentThread, $fgThread, $true) }
        if ($targetThread -ne 0 -and $targetThread -ne $currentThread) { $attachedTarget = [CodexPetTimerWin32]::AttachThreadInput($currentThread, $targetThread, $true) }
        [void][CodexPetTimerWin32]::BringWindowToTop($targetWindow)
        [void][CodexPetTimerWin32]::SetForegroundWindow($targetWindow)
        [void][CodexPetTimerWin32]::SetActiveWindow($targetWindow)
        [void][CodexPetTimerWin32]::SetFocus($targetWindow)
    } finally {
        if ($attachedTarget) { [void][CodexPetTimerWin32]::AttachThreadInput($currentThread, $targetThread, $false) }
        if ($attachedForeground) { [void][CodexPetTimerWin32]::AttachThreadInput($currentThread, $fgThread, $false) }
    }
    Start-Sleep -Milliseconds 250
    if (-not (Test-ForegroundProcess $expectedPid)) { [CodexPetTimerWin32]::SwitchToThisWindow($targetWindow, $true); Start-Sleep -Milliseconds 350 }
    return (Test-ForegroundProcess $expectedPid)
}

if (-not (Force-CodexForeground $hwnd $targetPid)) { throw ('Windows could not foreground the already-detected Codex Desktop window (PID ' + $targetPid + '). No message was sent.') }

$r = New-Object CodexPetTimerWin32+RECT
if (-not [CodexPetTimerWin32]::GetWindowRect($hwnd, [ref]$r)) { throw 'Could not read the Codex Desktop window rectangle. No message was sent.' }
$width = [double]($r.Right - $r.Left)
$height = [double]($r.Bottom - $r.Top)
if ($width -lt 500 -or $height -lt 350) { throw 'Codex Desktop window is too small for composer dispatch. No message was sent.' }

$x = [int]($r.Left + ($width * 0.68))
$y = [int]($r.Top + ($height * 0.875))
if (-not (Test-ForegroundProcess $targetPid)) { throw 'Codex Desktop lost foreground ownership before the composer click. No message was sent.' }
[void][CodexPetTimerWin32]::SetCursorPos($x, $y)
[CodexPetTimerWin32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
[CodexPetTimerWin32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 250
if (-not (Test-ForegroundProcess $targetPid)) { throw 'Codex Desktop lost foreground ownership after the composer click. No message was sent.' }

$oldClipboard = $null
