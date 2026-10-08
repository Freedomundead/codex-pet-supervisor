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
