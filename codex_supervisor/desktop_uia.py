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
