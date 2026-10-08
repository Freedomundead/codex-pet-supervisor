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
        ttk.Label(title, textvariable=self.status_var).pack(anchor="w", pady=(2, 0))
        ttk.Label(title, text="Created by Freedomundead • first vibe-coded open-source project", foreground="#666666").pack(anchor="w", pady=(2, 0))

        engine = ttk.LabelFrame(header, text="Pet engine", padding=(8, 5))
        engine.pack(side="right")
        self.start_button = ttk.Button(engine, text="Start", command=self._start_worker, style="Tool.TButton")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(engine, text="Stop", command=self._stop_worker, style="Tool.TButton")
        self.stop_button.pack(side="left", padx=(6, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        main_tab = ttk.Frame(self.notebook, padding=12)
        advanced_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(main_tab, text="Timer")
        self.notebook.add(advanced_tab, text="Labs")

        # ---------------- Timer tab ----------------
        main_tab.columnconfigure(0, weight=3)
        main_tab.columnconfigure(1, weight=2)
        main_tab.rowconfigure(2, weight=1)

        status_card = ttk.LabelFrame(main_tab, text="Live status", padding=12)
        status_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        status_card.columnconfigure(1, weight=1)
        ttk.Label(status_card, textvariable=self.timer_state_title_var, style="State.TLabel").grid(
            row=0, column=0, rowspan=2, sticky="nw", padx=(0, 18)
        )
        ttk.Label(status_card, textvariable=self.desktop_status_var, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(status_card, textvariable=self.desktop_activity_var).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(status_card, text="Next action", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=2, sticky="w", padx=(24, 0)
        )
        ttk.Label(status_card, textvariable=self.timer_next_action_var, wraplength=390).grid(
            row=1, column=2, sticky="w", padx=(24, 0), pady=(4, 0)
        )

        quota = ttk.LabelFrame(main_tab, text="Allowance", padding=12)
        quota.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(0, 10))
        quota.columnconfigure(1, weight=1)
        self.five_label = ttk.Label(quota, text="5-hour: —", width=34)
        self.five_label.grid(row=0, column=0, sticky="w")
        self.five_bar = ttk.Progressbar(quota, maximum=100)
        self.five_bar.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.week_label = ttk.Label(quota, text="Weekly: —", width=34)
        self.week_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.week_bar = ttk.Progressbar(quota, maximum=100)
        self.week_bar.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        self.refresh_quota_button = ttk.Button(quota, text="Refresh", command=self._refresh_quota_async, style="Tool.TButton")
        self.refresh_quota_button.grid(row=0, column=2, rowspan=2)
        ttk.Label(quota, textvariable=self.quota_summary_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(9, 0))

        lifecycle = ttk.LabelFrame(main_tab, text="Lifecycle", padding=12)
        lifecycle.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(0, 10))
        ttk.Label(lifecycle, textvariable=self.timer_state_var, font=("Segoe UI", 10, "bold"), wraplength=390).pack(anchor="w")
        ttk.Separator(lifecycle, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(lifecycle, text="Last event", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(lifecycle, textvariable=self.timer_last_event_var, wraplength=390).pack(anchor="w", pady=(3, 0))

        timer = ttk.LabelFrame(main_tab, text="Auto Continue", padding=12)
        timer.grid(row=2, column=0, columnspan=2, sticky="nsew")
        timer.columnconfigure(0, weight=1)
        timer.rowconfigure(3, weight=1)

        intro = (
            "Leave the Codex chat you want continued open. The Pet waits for a real usage-limit stop, "
            "waits for allowance to return, then sends one saved continuation message."
        )
        ttk.Label(timer, text=intro, wraplength=980).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(timer, text="Continuation message", font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="w", pady=(10, 4))
        preset_combo = ttk.Combobox(
            timer,
            textvariable=self.timer_preset_var,
            values=list(TIMER_PRESETS) + ["Custom"],
            state="readonly",
            width=24,
        )
        preset_combo.grid(row=1, column=1, sticky="w", pady=(10, 4))
        preset_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_timer_preset())
        ttk.Label(timer, text="Presets only edit the box; Save Message stores the choice.").grid(
            row=1, column=2, sticky="e", pady=(10, 4)
        )

        self.timer_message_entry = tk.Text(timer, height=6, wrap="word", undo=True, font=("Consolas", 10))
        self.timer_message_entry.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(0, 10))
        self._set_timer_message(DEFAULT_TIMER_MESSAGE)

        controls = ttk.Frame(timer)
        controls.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.arm_timer_button = ttk.Button(controls, text="Arm Timer", command=self._arm_timer, style="Primary.TButton")
        self.arm_timer_button.pack(side="left")
        self.disarm_timer_button = ttk.Button(controls, text="Disarm", command=self._disarm_timer, style="Tool.TButton")
        self.disarm_timer_button.pack(side="left", padx=(8, 0))
        self.save_timer_message_button = ttk.Button(controls, text="Save Message", command=self._save_timer_message, style="Tool.TButton")
        self.save_timer_message_button.pack(side="left", padx=(8, 0))
        self.continue_now_button = ttk.Button(controls, text="Send Test Now", command=self._send_continue_now, style="Tool.TButton")
        self.continue_now_button.pack(side="right")
