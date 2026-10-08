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

        ttk.Label(
            main_tab,
            text="Safe rule: the timer never needs a project/scope. Test Send Test Now once after installing a new Pet version.",
            wraplength=1000,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(9, 0))

        # ---------------- Labs tab ----------------
        labs_notice = ttk.LabelFrame(advanced_tab, text="Labs • experimental", padding=10)
        labs_notice.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(
            labs_notice,
            text="Timer mode is the supported product. The tools below are experiments for contributors and may change; they are not required for Auto Continue.",
            wraplength=960,
        ).pack(anchor="w")

        advanced_tab.columnconfigure(0, weight=1)
        advanced_tab.columnconfigure(1, weight=1)
        advanced_tab.rowconfigure(3, weight=1)

        target = ttk.LabelFrame(advanced_tab, text="Workspace for managed Goals", padding=10)
        target.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(target, text="Project").grid(row=0, column=0, sticky="w")
        self.project_combo = ttk.Combobox(target, textvariable=self.project_var, state="readonly", width=28)
        self.project_combo.grid(row=0, column=1, sticky="ew", padx=6)
        self.project_combo.bind("<<ComboboxSelected>>", lambda _e: self._project_selected())
        ttk.Button(target, text="Set / Update Project", command=self._set_project).grid(row=0, column=2, padx=4)
        ttk.Label(target, text="Scope").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.scope_combo = ttk.Combobox(target, textvariable=self.scope_var, state="readonly", width=28)
        self.scope_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        self.scope_combo.bind("<<ComboboxSelected>>", lambda _e: self._scope_selected())
        ttk.Button(target, text="Set / Update Scope", command=self._set_scope).grid(row=1, column=2, padx=4, pady=(8, 0))
        ttk.Label(target, textvariable=self.target_path_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(target, text="Read: project context  •  Write/commands: selected scope").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        target.columnconfigure(1, weight=1)

        builder = ttk.LabelFrame(advanced_tab, text="Goal builder", padding=10)
        builder.grid(row=2, column=0, sticky="nsew", padx=(0, 5), pady=(0, 10))
        ttk.Label(builder, text="Write one Goal, or one Goal per non-empty line.").pack(anchor="w")
        self.prompt = tk.Text(builder, height=7, wrap="word", undo=True)
        self.prompt.pack(fill="both", expand=True, pady=(7, 7))
        mode = ttk.Frame(builder)
        mode.pack(fill="x")
        ttk.Radiobutton(mode, text="One Goal", variable=self.queue_mode_var, value="single").pack(side="left")
        ttk.Radiobutton(mode, text="One Goal per line", variable=self.queue_mode_var, value="lines").pack(side="left", padx=(12, 0))
        ttk.Button(mode, text="Preview", command=self._preview_goal_builder).pack(side="right")
        ttk.Label(builder, textvariable=self.preview_summary_var).pack(anchor="w", pady=(7, 3))
        self.preview_list = tk.Listbox(builder, height=5, selectmode="extended", exportselection=False)
        self.preview_list.pack(fill="both", expand=True)
        build_actions = ttk.Frame(builder)
        build_actions.pack(fill="x", pady=(7, 0))
        ttk.Button(build_actions, text="Queue Selected", command=self._queue_selected_preview).pack(side="left")
        ttk.Button(build_actions, text="Queue All", command=self._queue_all_preview).pack(side="left", padx=(7, 0))

        tools = ttk.LabelFrame(advanced_tab, text="Task tools", padding=10)
        tools.grid(row=2, column=1, sticky="nsew", padx=(5, 0), pady=(0, 10))
        ttk.Label(tools, textvariable=self.current_var, wraplength=470).pack(anchor="w")
        ttk.Separator(tools, orient="horizontal").pack(fill="x", pady=8)
        row1 = ttk.Frame(tools)
        row1.pack(fill="x")
        ttk.Button(row1, text="Adopt Existing", command=self._adopt_existing_task).pack(side="left")
        ttk.Button(row1, text="Edit", command=self._edit_selected).pack(side="left", padx=(6, 0))
        ttk.Button(row1, text="Remove", command=self._remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(row1, text="Refresh", command=self._refresh_all).pack(side="right")
        row2 = ttk.Frame(tools)
        row2.pack(fill="x", pady=(7, 0))
        ttk.Button(row2, text="Probe Desktop", command=self._probe_desktop_async).pack(side="left")
        ttk.Button(row2, text="Copy Continue", command=self._copy_external_continue).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="Mark Sent", command=self._mark_external_sent).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="Mark Complete", command=self._mark_external_complete).pack(side="left", padx=(6, 0))
        row3 = ttk.Frame(tools)
        row3.pack(fill="x", pady=(7, 0))
        ttk.Button(row3, text="Retry", command=self._retry_selected).pack(side="left")
        ttk.Button(row3, text="Abandon", command=self._abandon_selected).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            row3,
            text="Auto-dispatch adopted Desktop tasks (experimental)",
            variable=self.desktop_auto_dispatch_var,
            command=self._desktop_auto_dispatch_changed,
        ).pack(side="right")
        ttk.Label(tools, textvariable=self.advanced_status_var).pack(anchor="w", pady=(9, 0))

        queue_frame = ttk.LabelFrame(advanced_tab, text="Queue", padding=8)
        queue_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        ttk.Label(queue_frame, textvariable=self.queue_summary_var).pack(anchor="w", pady=(0, 6))
        cols = ("pos", "status", "target", "goal")
        self.tree = ttk.Treeview(queue_frame, columns=cols, show="headings", height=8)
        self.tree.heading("pos", text="#")
        self.tree.heading("status", text="Status")
        self.tree.heading("target", text="Project / Scope")
        self.tree.heading("goal", text="Goal")
        self.tree.column("pos", width=50, stretch=False, anchor="center")
        self.tree.column("status", width=120, stretch=False)
        self.tree.column("target", width=230)
        self.tree.column("goal", width=520)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(queue_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

    def run(self) -> None:
        # Populate the allowance surface immediately. This metadata read does not
        # spend a Codex work turn or consume the user's coding allowance.
        self.root.after(250, self._refresh_quota_async)
        self.root.after(350, self._refresh_desktop_status_async)
        self.root.after(1500, self._tick)
        self.root.after(5000, self._desktop_tick)
        self.root.mainloop()

    def _tick(self) -> None:
        self._refresh_all(light=True)
        self.root.after(2000, self._tick)

    def _desktop_tick(self) -> None:
        self._refresh_desktop_status_async()
        self.root.after(5000, self._desktop_tick)

    def _refresh_desktop_status_async(self) -> None:
        if self._desktop_status_refreshing:
            return
        self._desktop_status_refreshing = True

        def worker() -> None:
            try:
                from .desktop_uia import get_codex_desktop_status
                result = get_codex_desktop_status()
            except Exception as exc:
                result = {"open": False, "error": str(exc)}

            def apply() -> None:
                self._desktop_status_refreshing = False
                if result.get("open"):
                    title = str(result.get("windowTitle") or "Codex")
                    self.desktop_status_var.set(f"Codex Desktop: open  •  {title}")
                elif result.get("error"):
                    self.desktop_status_var.set("Codex Desktop: status unavailable")
                else:
                    self.desktop_status_var.set("Codex Desktop: not open")
                self._update_activity_label()

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _update_activity_label(self) -> None:
        now = time.time()
        if self._last_quota_change_at and now - self._last_quota_change_at <= 75:
            self.desktop_activity_var.set("Activity: allowance changed recently — Codex is/was actively working")
        else:
            self.desktop_activity_var.set("Activity: no recent allowance change detected")

    def _refresh_all(self, light: bool = False) -> None:
        store = self._store()
        try:
            projects = store.list_projects()
            names = [p.name for p in projects]
            self.project_combo["values"] = names
            default_project = store.default_project_name()
            if self.project_var.get() not in names:
                self.project_var.set(default_project or (names[0] if names else ""))
            self.desktop_auto_dispatch_var.set(bool(store.get_json("desktop_auto_dispatch", True)))
            timer_config = store.get_json("timer_config", {})
            if not isinstance(timer_config, dict):
                timer_config = {}
            if not self._timer_loaded:
                self.timer_enabled_var.set(bool(timer_config.get("enabled")))
                message = timer_config.get("message")
                if isinstance(message, str) and message.strip():
                    self._set_timer_message(message)
                self._timer_loaded = True
            else:
                self.timer_enabled_var.set(bool(timer_config.get("enabled")))
            self._render_timer(timer_config)
            self._refresh_scopes(store)
            self._refresh_jobs(store)
            self._render_quota(store.get_json("rate_limits"), store.get_json("last_quota_decision"))
        finally:
            store.close()
        if self.worker and self.worker.poll() is not None:
            self.status_var.set(f"Pet stopped (exit {self.worker.returncode})")
            self.worker = None
        elif self.worker:
            if self._current_job_status is JobStatus.WAITING_QUOTA:
                self.status_var.set("Pet waiting for quota")
            elif self._current_job_status is JobStatus.READY_OWNER:
                self.status_var.set("Pet ready for Codex Desktop owner")
            elif self._current_job_status is JobStatus.EXTERNAL_ACTIVE:
                self.status_var.set("Pet monitoring Desktop-owned task")
            elif self._current_job_status is JobStatus.WAITING_WRITER:
                self.status_var.set("Pet migrating legacy adopted task")
            elif self._current_job_status is JobStatus.BLOCKED:
                self.status_var.set("Pet blocked — see Current for reason")
            elif self._current_job_status is JobStatus.FAILED:
                self.status_var.set("Pet task failed — see Current for reason")
            else:
                timer_store = self._store()
                try:
                    timer_config = timer_store.get_json("timer_config", {})
