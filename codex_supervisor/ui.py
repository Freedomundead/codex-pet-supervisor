from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .app_server import CodexAppServer
from .models import JobStatus, ThreadOwnership
from .rate_limits import decide_availability, normalize_rate_limits
from .store import Store


DEFAULT_TIMER_MESSAGE = "Continue the current task from where you stopped. Do not repeat completed work."

TIMER_PRESETS = {
    "Default": DEFAULT_TIMER_MESSAGE,
    "Finish the task": (
        "Continue the current task from where you stopped. Preserve completed changes and verified results. "
        "Do not restart or repeat completed work. Continue until the task is genuinely complete or another usage limit stops you."
    ),
    "Verify, then continue": (
        "Continue from the exact unfinished step. First verify the current state so completed work is not repeated, "
        "then continue the remaining task."
    ),
    "Minimal": "Continue from where you stopped.",
}


def split_sequence_lines(text: str) -> list[str]:
    """Return one queued Goal per non-empty line, preserving order."""
    return [line.strip() for line in text.splitlines() if line.strip()]


class SupervisorUI:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path).resolve())
        self.root = tk.Tk()
        self.root.title("Codex Pet Supervisor")
        self.root.geometry("1080x760")
        self.root.minsize(900, 650)
        self.worker: subprocess.Popen[str] | None = None
        self.project_var = tk.StringVar()
        self.scope_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Pet idle")
        self.current_var = tk.StringVar(value="No active job")
        self.quota_summary_var = tk.StringVar(value="Quota not loaded")
        self.target_path_var = tk.StringVar(value="Target: —")
        self.queue_summary_var = tk.StringVar(value="Queue: 0 jobs")
        self.desktop_auto_dispatch_var = tk.BooleanVar(value=False)
        self.timer_enabled_var = tk.BooleanVar(value=False)
        self.timer_state_var = tk.StringVar(value="Timer off")
        self.timer_state_title_var = tk.StringVar(value="OFF")
        self.timer_next_action_var = tk.StringVar(value="Arm the timer when you want the Pet to watch for a real usage-limit reset.")
        self.timer_last_event_var = tk.StringVar(value="No continuation has been sent yet.")
        self.timer_preset_var = tk.StringVar(value="Default")
        self.queue_mode_var = tk.StringVar(value="single")
        self.preview_summary_var = tk.StringVar(value="Preview: nothing yet")
        self._preview_prompts: list[str] = []
        self.desktop_status_var = tk.StringVar(value="Codex Desktop: checking…")
        self.desktop_activity_var = tk.StringVar(value="Activity: unknown")
        self.advanced_status_var = tk.StringVar(value="Labs are experimental and optional")
        self._timer_loaded = False
        self._quota_refreshing = False
        self._current_job_status: JobStatus | None = None
        self._desktop_status_refreshing = False
        self._last_five_remaining: int | None = None
        self._last_quota_change_at: float | None = None
        self._build()
        self._refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _store(self) -> Store:
        return Store(self.db_path)

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("State.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 7))
        style.configure("Tool.TButton", padding=(10, 5))
        style.configure("Treeview", rowheight=27)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 7))

        outer = ttk.Frame(self.root, padding=(16, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="🐾", font=("Segoe UI Emoji", 31)).pack(side="left", padx=(0, 12))
        title = ttk.Frame(header)
        title.pack(side="left", fill="x", expand=True)
        ttk.Label(title, text="Codex Pet Supervisor", style="Title.TLabel").pack(anchor="w")
