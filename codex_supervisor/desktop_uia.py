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
