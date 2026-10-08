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
                finally:
                    timer_store.close()
                timer_state = str(timer_config.get("state") or "") if isinstance(timer_config, dict) else ""
                timer_enabled = bool(timer_config.get("enabled")) if isinstance(timer_config, dict) else False
                if timer_enabled and timer_state == "waiting_reset":
                    self.status_var.set("Pet waiting for Codex allowance reset")
                elif timer_enabled and timer_state == "armed":
                    self.status_var.set("Pet watching for the next usage limit")
                elif timer_enabled and timer_state == "sent":
                    self.status_var.set("Pet dispatched Continue; watching for the next limit")
                elif timer_enabled and timer_state == "dispatch_failed":
                    self.status_var.set("Pet could not send Continue — see Timer")
                else:
                    self.status_var.set("Pet running")
        elif not light:
            self.status_var.set("Pet idle")
        self._sync_worker_buttons()

    def _refresh_scopes(self, store: Store) -> None:
        project = self.project_var.get()
        scopes = store.list_scopes(project) if project else []
        names = [""] + [s.name for s in scopes]
        self.scope_combo["values"] = names
        default_scope = store.default_scope_name(project) if project else None
        current_scope = self.scope_var.get()
        default_value = default_scope if default_scope in names else ""
        if current_scope not in names or (not current_scope and default_value):
            self.scope_var.set(default_value)
        self._update_target_path(store)

    def _refresh_jobs(self, store: Store) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        current = store.current_job()
        self._current_job_status = current.status if current else None
        jobs = store.list_jobs()
        runnable_statuses = {
            JobStatus.QUEUED,
            JobStatus.ACTIVE,
            JobStatus.WAITING_QUOTA,
            JobStatus.READY_OWNER,
            JobStatus.EXTERNAL_ACTIVE,
            JobStatus.WAITING_WRITER,
            JobStatus.BLOCKED,
        }
        position_by_id: dict[int, int] = {}
        position = 1
        for job in jobs:
            if job.status in runnable_statuses:
                position_by_id[job.id] = position
                position += 1
        for job in jobs:
            goal = job.prompt.replace("\n", " ").strip()
            if len(goal) > 120:
                goal = goal[:117] + "..."
            display_position = position_by_id.get(job.id, "—")
            self.tree.insert(
                "",
                "end",
                iid=str(job.id),
                values=(display_position, job.status.value, store.job_target_label(job), goal),
            )
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        active_count = len(position_by_id)
        self.queue_summary_var.set(f"Queue: {active_count} active / queued  •  {len(jobs)} total")
        if current:
            current_position = position_by_id.get(current.id, "—")
            detail = (
                f"Position {current_position}  {current.status.value}  |  "
                f"{store.job_target_label(current)}  |  {current.prompt[:180]}"
            )
            if current.last_error:
                detail += f"\n{current.last_error}"
            self.current_var.set(detail)
        else:
            self.current_var.set("No active or queued job")

    def _render_quota(self, payload, decision) -> None:
        windows = normalize_rate_limits(payload) if isinstance(payload, dict) else ()
        five = next((w for w in windows if w.duration_mins == 300), None)
        week = next((w for w in windows if w.duration_mins == 10080), None)
        if five is not None:
            remaining = int(five.remaining_percent)
            if self._last_five_remaining is not None and remaining < self._last_five_remaining:
                self._last_quota_change_at = time.time()
            self._last_five_remaining = remaining
            self._update_activity_label()
        self._render_window(self.five_label, self.five_bar, five, "5-hour")
        self._render_window(self.week_label, self.week_bar, week, "Weekly")
        if isinstance(decision, dict):
            reason = decision.get("reason", "unknown")
            wake = decision.get("wakeAt")
            wake_text = self._format_reset(wake) if isinstance(wake, int) else "—"
            self.quota_summary_var.set(f"{reason} | next wake: {wake_text}")
        else:
            self.quota_summary_var.set("Quota snapshot not loaded yet")

    def _render_window(self, label: ttk.Label, bar: ttk.Progressbar, window, name: str) -> None:
        if window is None:
            label.configure(text=f"{name}: —")
            bar["value"] = 0
            return
        bar["value"] = window.remaining_percent
        label.configure(
            text=f"{name}: {window.remaining_percent}% left  |  reset {self._format_reset(window.resets_at)}"
        )

    @staticmethod
    def _format_reset(value: int | None) -> str:
        if not value:
            return "—"
        remaining = max(0, value - int(time.time()))
        h, rem = divmod(remaining, 3600)
        m, s = divmod(rem, 60)
        clock = datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
        return f"{clock} ({h:02d}:{m:02d}:{s:02d})"

    def _update_target_path(self, store: Store) -> None:
        project = self.project_var.get()
        if not project:
            self.target_path_var.set("Target: —")
            return
        scope = self.scope_var.get() or None
        try:
            target = store.resolve_target(project, scope)
        except ValueError as exc:
            self.target_path_var.set(f"Target unavailable: {exc}")
            return
        self.target_path_var.set(f"Target: {target}")

    def _render_timer(self, config: dict) -> None:
        enabled = bool(config.get("enabled"))
        state = str(config.get("state") or ("armed" if enabled else "idle"))

        if not enabled:
            self.timer_state_title_var.set("OFF")
            self.timer_state_var.set("Timer off")
            self.timer_next_action_var.set("Arm the timer when you want the Pet to watch for a real usage-limit reset.")
        elif state == "waiting_reset":
            wake = config.get("wakeAt")
            wake_text = self._format_reset(wake) if isinstance(wake, int) else "the next allowance reset"
            self.timer_state_title_var.set("WAITING")
            self.timer_state_var.set(f"Waiting for allowance reset: {wake_text}.")
            self.timer_next_action_var.set("Do nothing. The Pet will re-check allowance at reset and send exactly once when it is available.")
        elif state == "sent":
            sent = config.get("lastSentAt")
            sent_text = datetime.fromtimestamp(sent).strftime("%Y-%m-%d %H:%M:%S") if isinstance(sent, int) else "recently"
            self.timer_state_title_var.set("SENT")
            self.timer_state_var.set(f"Continue dispatched at {sent_text}.")
            self.timer_next_action_var.set("Keep working normally. The Pet is watching for the next real usage-limit stop.")
        elif state == "dispatch_failed":
            self.timer_state_title_var.set("ATTENTION")
            self.timer_state_var.set(f"Send failed: {config.get('lastError') or 'unknown Desktop error'}")
            self.timer_next_action_var.set("Open the intended Codex chat and use Send Test Now. Re-arm only after the send path is healthy.")
        else:
            self.timer_state_title_var.set("ARMED")
            self.timer_state_var.set("Armed. Quota is available; waiting until Codex actually hits a usage limit.")
            self.timer_next_action_var.set("Use Codex normally. The Pet will stay idle until a real denied-allowance event appears.")

        sent = config.get("lastSentAt")
        last_error = config.get("lastError")
        if last_error:
            self.timer_last_event_var.set(f"Last dispatch error: {last_error}")
        elif isinstance(sent, int):
            self.timer_last_event_var.set(
                "Last continuation sent: " + datetime.fromtimestamp(sent).strftime("%Y-%m-%d %H:%M:%S")
            )
        elif isinstance(config.get("armedAt"), int):
            self.timer_last_event_var.set(
                "Timer armed: " + datetime.fromtimestamp(config["armedAt"]).strftime("%Y-%m-%d %H:%M:%S")
            )
        else:
            self.timer_last_event_var.set("No continuation has been sent yet.")

    def _apply_timer_preset(self) -> None:
        name = self.timer_preset_var.get()
        message = TIMER_PRESETS.get(name)
        if message is not None:
            self._set_timer_message(message)
            self.status_var.set(f"Preset loaded: {name} — save when ready")

    def _preview_goal_builder(self) -> None:
        text = self.prompt.get("1.0", "end").strip()
        if self.queue_mode_var.get() == "lines":
            prompts = split_sequence_lines(text)
        else:
            prompts = [text] if text else []
        self._preview_prompts = prompts
        self.preview_list.delete(0, "end")
        for index, prompt in enumerate(prompts, start=1):
            compact = " ".join(prompt.split())
            if len(compact) > 120:
                compact = compact[:117] + "..."
            self.preview_list.insert("end", f"{index}. {compact}")
        self.preview_summary_var.set(
            f"Preview: {len(prompts)} Goal{'s' if len(prompts) != 1 else ''}"
            if prompts else "Preview: nothing to queue"
        )
        if prompts:
            self.preview_list.selection_set(0, "end")

    def _queue_selected_preview(self) -> None:
        if not self._preview_prompts:
            self._preview_goal_builder()
        selected = list(self.preview_list.curselection())
        prompts = [self._preview_prompts[i] for i in selected if i < len(self._preview_prompts)]
        if not prompts:
            messagebox.showinfo("Goal builder", "Select at least one preview line to queue.", parent=self.root)
            return
        self._queue_prompts(prompts)
        self._preview_prompts = []
        self.preview_list.delete(0, "end")
        self.preview_summary_var.set("Preview: nothing yet")

    def _queue_all_preview(self) -> None:
        if not self._preview_prompts:
            self._preview_goal_builder()
        if not self._preview_prompts:
            return
        self._queue_prompts(list(self._preview_prompts))
        self._preview_prompts = []
        self.preview_list.delete(0, "end")
        self.preview_summary_var.set("Preview: nothing yet")

    def _get_timer_message(self) -> str:
        return self.timer_message_entry.get("1.0", "end-1c").strip()

    def _set_timer_message(self, message: str) -> None:
        self.timer_message_entry.delete("1.0", "end")
        self.timer_message_entry.insert("1.0", message)

    def _save_timer_message(self) -> None:
        message = self._get_timer_message()
        if not message:
            messagebox.showerror("Auto Continue Timer", "Enter a continuation message.", parent=self.root)
            return
        store = self._store()
        try:
            config = store.get_json("timer_config", {})
            if not isinstance(config, dict):
                config = {}
            config["message"] = message
            store.set_json("timer_config", config)
        finally:
            store.close()
        self.status_var.set("Continuation message saved")
        self._refresh_all()

    def _arm_timer(self) -> None:
        message = self._get_timer_message()
        if not message:
            messagebox.showerror("Auto Continue Timer", "Enter a continuation message.", parent=self.root)
            return
        store = self._store()
        try:
            decision = store.get_json("last_quota_decision", {})
            waiting = isinstance(decision, dict) and decision.get("allowed") is False
            config = {
                "enabled": True,
                "message": message,
                "state": "waiting_reset" if waiting else "armed",
                "wakeAt": decision.get("wakeAt") if waiting and isinstance(decision, dict) else None,
                "lastError": None,
                "armedAt": int(time.time()),
            }
            store.set_json("timer_config", config)
        finally:
            store.close()
        self.timer_enabled_var.set(True)
        if not (self.worker and self.worker.poll() is None):
            self._start_worker()
        self.status_var.set("Auto Continue Timer armed")
        self._refresh_all()

    def _disarm_timer(self) -> None:
        store = self._store()
        try:
            config = store.get_json("timer_config", {})
            if not isinstance(config, dict):
                config = {}
            config.update({"enabled": False, "state": "idle", "wakeAt": None})
            store.set_json("timer_config", config)
        finally:
            store.close()
        self.timer_enabled_var.set(False)
        self.timer_state_var.set("Timer off")
        self.status_var.set("Auto Continue Timer disarmed")

    def _send_continue_now(self) -> None:
        message = self._get_timer_message()
        if not message:
            messagebox.showerror("Auto Continue Timer", "Enter a continuation message.", parent=self.root)
            return
        self.continue_now_button.configure(state="disabled")
        self.status_var.set("Sending Continue to the open Codex Desktop chat…")

        def worker() -> None:
            try:
                from .desktop_uia import dispatch_to_current_codex_desktop
                result = dispatch_to_current_codex_desktop(message=message)
                store = self._store()
                try:
                    config = store.get_json("timer_config", {})
                    if not isinstance(config, dict):
                        config = {}
                    config.update({
                        "message": message,
                        "state": "sent",
                        "lastSentAt": int(time.time()),
                        "lastDispatch": result,
                        "lastError": None,
                    })
                    store.set_json("timer_config", config)
                finally:
                    store.close()
                self.root.after(0, lambda: self.status_var.set("Continue dispatched to Codex Desktop"))
            except Exception as exc:
                error_text = str(exc)
                def fail(message: str = error_text) -> None:
                    self.status_var.set("Continue send failed")
                    messagebox.showerror("Auto Continue Timer", message, parent=self.root)
                self.root.after(0, fail)
            finally:
                self.root.after(0, lambda: self.continue_now_button.configure(state="normal"))
                self.root.after(0, self._refresh_all)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_worker_buttons(self) -> None:
        running = bool(self.worker and self.worker.poll() is None)
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _project_selected(self) -> None:
        project = self.project_var.get()
        if not project:
            return
        store = self._store()
        try:
            store.set_default_project(project)
            default_scope = store.default_scope_name(project)
            self.scope_var.set(default_scope or "")
            self._refresh_scopes(store)
        finally:
            store.close()
        self._refresh_all()

    def _scope_selected(self) -> None:
        project = self.project_var.get()
        if not project:
            return
        store = self._store()
        try:
            scope = self.scope_var.get() or None
            store.set_default_scope(project, scope)
            self._update_target_path(store)
        finally:
            store.close()

    def _ask_named_folder(
        self,
        *,
        title: str,
        name_label: str,
        folder_label: str,
        initial_name: str = "",
        initial_folder: str = "",
        browse_title: str,
        browse_root: str | None = None,
    ) -> tuple[str, str] | None:
        """Open one compact dialog for both a logical name and a real folder.

        v0.2.2/v0.2.3 used two separate dialogs (name first, then a native
        folder picker). That was technically functional but easy to mistake for
        a manual-path workflow. Keep both fields visible in one surface instead.
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        name_var = tk.StringVar(value=initial_name)
        folder_var = tk.StringVar(value=initial_folder)
        result: dict[str, tuple[str, str] | None] = {"value": None}

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=name_label).grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(body, textvariable=name_var, width=46)
        name_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 10))

        ttk.Label(body, text=folder_label).grid(row=2, column=0, sticky="w")
        folder_entry = ttk.Entry(body, textvariable=folder_var, width=46)
        folder_entry.grid(row=3, column=0, sticky="ew", pady=(3, 0))

        def browse() -> None:
            initial = folder_var.get().strip()
            initialdir = initial if initial and Path(initial).is_dir() else browse_root
            options = {"title": browse_title, "parent": dialog}
            if initialdir and Path(initialdir).is_dir():
                options["initialdir"] = str(initialdir)
            selected = filedialog.askdirectory(**options)
            if selected:
                folder_var.set(str(Path(selected).resolve()))

        ttk.Button(body, text="Browse…", command=browse).grid(row=3, column=1, padx=(8, 0), sticky="ew")

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))

        def accept() -> None:
            name = name_var.get().strip()
            folder_text = folder_var.get().strip()
            if not name:
                messagebox.showerror(title, "Name is required.", parent=dialog)
                return
            if not folder_text:
                messagebox.showerror(title, "Select a folder.", parent=dialog)
                return
            folder = Path(folder_text).expanduser().resolve()
            if not folder.is_dir():
                messagebox.showerror(title, f"Folder does not exist:\n{folder}", parent=dialog)
                return
            result["value"] = (name, str(folder))
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=accept).pack(side="right", padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: accept())
        name_entry.focus_set()
        self.root.wait_window(dialog)
        return result["value"]

    def _set_project(self) -> None:
        current_name = self.project_var.get().strip()
        current_root = ""
        store = self._store()
        try:
            existing = store.get_project(current_name) if current_name else None
            if existing is not None:
                current_root = existing.root
        finally:
            store.close()

        selected = self._ask_named_folder(
            title="Project",
            name_label="Project name (example: My Project)",
            folder_label="Project folder",
            initial_name=current_name,
            initial_folder=current_root,
            browse_title="Select project folder",
        )
        if selected is None:
            return
        name, folder = selected

        store = self._store()
        try:
            store.set_project(name, folder, make_default=True)
        finally:
            store.close()
        self.project_var.set(name)
        self._refresh_all()

    def _set_scope(self) -> None:
        project = self.project_var.get()
        if not project:
            messagebox.showerror("Scope", "Set a project first.", parent=self.root)
            return

        store = self._store()
        try:
            project_obj = store.get_project(project)
            assert project_obj is not None
            root = Path(project_obj.root).resolve()
            current_name = self.scope_var.get().strip()
            existing = store.get_scope(project, current_name) if current_name else None
            current_folder = str((root / existing.relative_path).resolve()) if existing else str(root)
        finally:
            store.close()

        selected = self._ask_named_folder(
            title="Scope",
            name_label="Scope name (example: Feature Area)",
            folder_label="Folder inside the project",
            initial_name=current_name,
            initial_folder=current_folder,
            browse_title=f"Select scope folder inside {project}",
            browse_root=str(root),
        )
        if selected is None:
            return
        name, selected_folder = selected
        target = Path(selected_folder).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            messagebox.showerror("Scope", "Scope must stay inside the project root.", parent=self.root)
            return

        store = self._store()
        try:
            store.set_scope(project, name, str(relative) if str(relative) else ".", make_default=True)
            store.set_default_scope(project, name)
        finally:
            store.close()
        self.scope_var.set(name)
        self._refresh_all()

    def _queue_prompts(self, prompts: list[str]) -> None:
        prompts = [prompt.strip() for prompt in prompts if prompt.strip()]
        if not prompts:
            return
        project = self.project_var.get() or None
        scope = self.scope_var.get() or None
        if project is None:
            messagebox.showerror("Add Goal", "Set a project first.", parent=self.root)
            return
        store = self._store()
        try:
            target = store.resolve_target(project, scope)
            if not target.is_dir():
                raise ValueError(f"Target folder does not exist: {target}")
            store.add_jobs(prompts, project_name=project, scope_name=scope)
        except ValueError as exc:
            messagebox.showerror("Add Goal", str(exc), parent=self.root)
            return
        finally:
            store.close()
        self.prompt.delete("1.0", "end")
        self.status_var.set(f"Queued {len(prompts)} Goal{'s' if len(prompts) != 1 else ''}")
        self._refresh_all()

    def _add_job(self) -> None:
        prompt = self.prompt.get("1.0", "end").strip()
        if prompt:
            self._queue_prompts([prompt])

    def _add_sequence(self) -> None:
        prompts = split_sequence_lines(self.prompt.get("1.0", "end"))
        if not prompts:
            return
        self._queue_prompts(prompts)

    def _adopt_existing_task(self) -> None:
        """Attach one existing Codex thread without enumerating global history."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Adopt Existing Codex Task")
        dialog.transient(self.root)
        dialog.geometry("980x620")
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=(
                "Find the existing Codex task by folder and title. "
                "Codex Desktop remains the owner; the Pet supervises quota and completion."
            ),
        ).pack(anchor="w")

        search_box = ttk.LabelFrame(body, text="Find existing task", padding=10)
        search_box.pack(fill="x", pady=(10, 8))
        task_search_var = tk.StringVar()
        folder_var = tk.StringVar()

        ttk.Label(search_box, text="Task title contains").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(search_box, textvariable=task_search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))

        ttk.Label(search_box, text="Folder filter").grid(row=1, column=0, sticky="w", pady=(8, 0))
        folder_entry = ttk.Entry(search_box, textvariable=folder_var)
        folder_entry.grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))

        def browse_folder() -> None:
            selected = filedialog.askdirectory(parent=dialog, title="Choose Codex task working folder")
            if selected:
                folder_var.set(str(Path(selected).resolve()))

        ttk.Button(search_box, text="Browse…", command=browse_folder).grid(
            row=1, column=2, pady=(8, 0)
        )
        search_box.columnconfigure(1, weight=1)

        # Prefer the selected project root. A Codex desktop thread usually records
        # the project root as cwd even when the user's logical Pet scope is deeper.
        store = self._store()
        try:
            project_name = self.project_var.get().strip()
            if project_name:
                project = store.get_project(project_name)
                if project:
                    folder_var.set(str(Path(project.root).resolve()))
        finally:
            store.close()

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("title", "cwd", "updated")
        threads_tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=11)
        threads_tree.heading("title", text="Codex task")
        threads_tree.heading("cwd", text="Working folder")
        threads_tree.heading("updated", text="Updated")
        threads_tree.column("title", width=360)
        threads_tree.column("cwd", width=440)
        threads_tree.column("updated", width=130, stretch=False)
        threads_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=threads_tree.yview)
        sb.pack(side="right", fill="y")
        threads_tree.configure(yscrollcommand=sb.set)

        ttk.Label(body, text="Continuation Goal").pack(anchor="w")
        continuation = tk.Text(body, height=4, wrap="word")
        continuation.pack(fill="x", pady=(4, 8))
        continuation.insert(
            "1.0",
            "Continue and complete the unfinished task in this existing Codex thread. "
            "Preserve completed work and continue from the next unfinished step. "
            "Do not restart or repeat work that is already complete.",
        )

        status_var = tk.StringVar(
            value="Enter part of the task title, then click Find Matching Tasks."
        )
        ttk.Label(body, textvariable=status_var).pack(anchor="w")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        find_button = ttk.Button(actions, text="Find Matching Tasks")
        find_button.pack(side="left")
        adopt_button = ttk.Button(actions, text="Adopt Selected", state="disabled")
        adopt_button.pack(side="right")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 8))

        thread_map: dict[str, dict] = {}
        loading = {"active": False}

        def populate(items: list[dict]) -> None:
            for row in threads_tree.get_children():
                threads_tree.delete(row)
            thread_map.clear()
            for item in items:
                thread_id = str(item.get("id") or "")
                if not thread_id:
                    continue
                title = str(item.get("name") or item.get("preview") or thread_id)
                cwd = str(item.get("cwd") or "")
                updated_raw = item.get("updatedAt")
                try:
                    updated = datetime.fromtimestamp(int(updated_raw)).strftime("%Y-%m-%d %H:%M")
                except (TypeError, ValueError, OSError):
                    updated = "—"
                thread_map[thread_id] = item
                threads_tree.insert("", "end", iid=thread_id, values=(title, cwd, updated))

            rows = threads_tree.get_children()
            if rows:
                status_var.set(f"Found {len(rows)} matching task(s).")
                threads_tree.selection_set(rows[0])
                adopt_button.configure(state="normal")
            else:
                status_var.set(
                    "No matching tasks found. Check the folder and use a shorter title fragment."
                )
                adopt_button.configure(state="disabled")

        def set_loading(active: bool) -> None:
            loading["active"] = active
            find_button.configure(state="disabled" if active else "normal")
            if active:
                adopt_button.configure(state="disabled")

        def find_matching() -> None:
            if loading["active"]:
                return
            title = task_search_var.get().strip()
            folder = folder_var.get().strip()
            if not title and not folder:
                messagebox.showerror(
                    "Adopt Existing Task",
                    "Enter a task-title fragment or choose a working folder.",
                    parent=dialog,
                )
                return
            if folder and not Path(folder).is_dir():
                messagebox.showerror(
                    "Adopt Existing Task", f"Folder does not exist: {folder}", parent=dialog
                )
                return
            set_loading(True)
            status_var.set("Searching a small filtered set of Codex tasks…")

            def worker() -> None:
                try:
                    items = asyncio.run(
                        self._find_existing_threads(
                            search_term=title or None,
                            cwd=folder or None,
                        )
                    )
                    self.root.after(0, lambda items=items: (set_loading(False), populate(items)))
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda exc=exc: (
                            set_loading(False),
                            status_var.set(f"Task lookup failed: {exc}"),
                        ),
                    )

            threading.Thread(target=worker, daemon=True).start()

        def adopt() -> None:
            selected = threads_tree.selection()
            if not selected:
                return
            thread_id = selected[0]
            item = thread_map.get(thread_id)
            if not item:
                return
            objective = continuation.get("1.0", "end").strip()
            if not objective:
                messagebox.showerror(
                    "Adopt Existing Task", "Continuation Goal cannot be empty.", parent=dialog
                )
                return
            cwd = str(item.get("cwd") or "").strip()
            thread_title = str(item.get("name") or item.get("preview") or "").strip()
            if not cwd:
                messagebox.showerror(
                    "Adopt Existing Task", "Selected thread has no working folder.", parent=dialog
                )
                return
            project = self.project_var.get() or None
            scope = self.scope_var.get() or None
            store = self._store()
            try:
                store.adopt_thread(
                    thread_id=thread_id,
                    prompt=objective,
                    cwd=cwd,
                    project_name=project,
                    scope_name=scope,
                    thread_title=thread_title or None,
                )
            except ValueError as exc:
                messagebox.showerror("Adopt Existing Task", str(exc), parent=dialog)
                return
            finally:
                store.close()
            dialog.destroy()
            self.status_var.set("Existing Codex task linked (external owner)")
            self._refresh_all()

        find_button.configure(command=find_matching)
        adopt_button.configure(command=adopt)
        search_entry.bind("<Return>", lambda _e: find_matching())
        search_entry.focus_set()

    async def _find_existing_threads(
        self, *, search_term: str | None, cwd: str | None
    ) -> list[dict]:
        client = CodexAppServer()

        async def operation() -> list[dict]:
            await client.start()
            try:
                return await client.list_threads(
                    limit=20,
                    search_term=search_term,
                    cwd=cwd,
                    state_db_only=True,
                )
            finally:
                await client.close()

        try:
            return await asyncio.wait_for(operation(), timeout=12)
        except TimeoutError as exc:
            await client.close()
            raise RuntimeError(
                "Filtered Codex task lookup timed out after 12 seconds"
            ) from exc

    def _desktop_auto_dispatch_changed(self) -> None:
        store = self._store()
        try:
            store.set_json("desktop_auto_dispatch", bool(self.desktop_auto_dispatch_var.get()))
        finally:
            store.close()
        self.advanced_status_var.set(
            "Desktop adopted-task dispatch enabled"
            if self.desktop_auto_dispatch_var.get()
            else "Desktop adopted-task dispatch disabled"
        )

    def _retry_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.ownership is ThreadOwnership.ADOPTED_EXTERNAL:
            # External tasks are re-armed for the Desktop owner without ever
            # acquiring their writer lock.
            store = self._store()
            try:
                store.update_job(
                    job.id,
                    status=JobStatus.QUEUED,
                    goal_status="externalPending",
                    last_error=None,
                    external_tracked_turn_id=None,
                    external_tracked_turn_status=None,
                )
            finally:
                store.close()
            self.status_var.set("Adopted Desktop task re-armed")
            self._refresh_all()
            return

        store = self._store()
        try:
            try:
                store.retry_blocked_job(job.id)
            except ValueError as exc:
                messagebox.showerror("Retry Selected", str(exc), parent=self.root)
                return
        finally:
            store.close()
        self.status_var.set("Managed task queued for retry")
        self._refresh_all()

    def _abandon_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.ownership is ThreadOwnership.ADOPTED_EXTERNAL:
            messagebox.showinfo(
                "Abandon Selected",
                "Adopted Desktop tasks can be detached with Remove Selected; the Desktop thread remains untouched.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Abandon Selected",
            "Stop supervising this blocked managed Goal so the queue can move on?\n\n"
            "The persisted Codex thread will be left unchanged.",
            parent=self.root,
        ):
            return
        store = self._store()
        try:
            try:
                store.abandon_blocked_job(job.id)
            except ValueError as exc:
                messagebox.showerror("Abandon Selected", str(exc), parent=self.root)
                return
        finally:
            store.close()
        self.status_var.set("Blocked managed task abandoned")
        self._refresh_all()

    def _edit_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        job_id = int(selected[0])
        store = self._store()
        try:
            job = store.get_job(job_id)
            if job is None:
                return
            if job.thread_id and job.ownership is not ThreadOwnership.ADOPTED_EXTERNAL:
                messagebox.showerror(
                    "Edit Goal",
                    "Cannot edit a Pet-managed Goal after its Codex thread has started.",
                    parent=self.root,
                )
                return
            projects = store.list_projects()
            project_names = [item.name for item in projects]
        finally:
            store.close()

        dialog = tk.Toplevel(self.root)
        dialog.title("Edit queued Goal")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("720x430")

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        project_var = tk.StringVar(value=job.project_name or self.project_var.get())
        scope_var = tk.StringVar(value=job.scope_name or "")

        ttk.Label(body, text="Project").grid(row=0, column=0, sticky="w")
        project_combo = ttk.Combobox(body, textvariable=project_var, state="readonly", values=project_names)
        project_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(body, text="Scope").grid(row=1, column=0, sticky="w", pady=(8, 0))
        scope_combo = ttk.Combobox(body, textvariable=scope_var, state="readonly")
        scope_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(body, text="Goal").grid(row=2, column=0, sticky="nw", pady=(10, 0))
        goal_text = tk.Text(body, height=12, wrap="word")
        goal_text.grid(row=2, column=1, sticky="nsew", padx=(8, 0), pady=(10, 0))
        goal_text.insert("1.0", job.prompt)

        body.columnconfigure(1, weight=1)
        body.rowconfigure(2, weight=1)

        def refresh_scopes() -> None:
            project_name = project_var.get()
            local_store = self._store()
            try:
                names = [""] + [item.name for item in local_store.list_scopes(project_name)]
            finally:
                local_store.close()
            scope_combo["values"] = names
            if scope_var.get() not in names:
                scope_var.set("")

        project_combo.bind("<<ComboboxSelected>>", lambda _e: refresh_scopes())
        refresh_scopes()
        if job.scope_name in scope_combo["values"]:
            scope_var.set(job.scope_name or "")

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def save() -> None:
            prompt = goal_text.get("1.0", "end").strip()
            project_name = project_var.get() or None
            scope_name = scope_var.get() or None
            local_store = self._store()
            try:
                local_store.edit_job(
                    job_id,
                    prompt=prompt,
                    project_name=project_name,
                    scope_name=scope_name,
                )
            except ValueError as exc:
                messagebox.showerror("Edit Goal", str(exc), parent=dialog)
                return
            finally:
                local_store.close()
            dialog.destroy()
            self.status_var.set("Queued Goal updated")
            self._refresh_all()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save Changes", command=save).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        goal_text.focus_set()

    def _selected_job(self):
        selected = self.tree.selection()
        store = self._store()
        try:
            if selected:
                return store.get_job(int(selected[0]))
            # Convenience: external-owner controls act on the current queue head
            # when the user has not explicitly selected a row.
            return store.current_job()
        finally:
            store.close()


    def _probe_desktop_async(self) -> None:
        """Read-only inspect the exact Codex Desktop accessibility surface."""
        self.status_var.set("Probing Codex Desktop accessibility tree…")

        def worker() -> None:
            try:
                from .desktop_uia import (
                    DesktopProbeError,
                    probe_codex_desktop,
                    save_probe_report,
                    summarize_probe,
                )
                payload = probe_codex_desktop()
                report_path = Path(self.db_path).parent / "desktop-probe.json"
                report = save_probe_report(payload, report_path)
                summary = summarize_probe(payload)

                def finish() -> None:
                    lines = [
                        f"Codex windows found: {summary.windows}",
                        f"Accessibility elements captured: {summary.elements}",
                        f"Likely thread/task controls: {len(summary.likely_thread_items)}",
                        f"Likely composer controls: {len(summary.likely_composers)}",
                        f"Likely Send controls: {len(summary.likely_send_controls)}",
                        "",
                        f"Report: {report}",
                        "",
                        "The probe was read-only; it did not click, type, focus, or send anything.",
                    ]
                    self.status_var.set("Desktop probe complete")
                    messagebox.showinfo("Probe Desktop", "\n".join(lines), parent=self.root)

                self.root.after(0, finish)
            except Exception as exc:
                def fail() -> None:
                    self.status_var.set("Desktop probe failed")
                    messagebox.showerror("Probe Desktop", str(exc), parent=self.root)
                self.root.after(0, fail)

        threading.Thread(target=worker, daemon=True).start()

    def _copy_external_continue(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.ownership is not ThreadOwnership.ADOPTED_EXTERNAL:
            messagebox.showinfo(
                "Copy Continue",
                "This control is only for externally owned adopted Codex tasks.",
                parent=self.root,
            )
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(job.prompt)
        self.root.update_idletasks()
        self.status_var.set("Continuation instruction copied to clipboard")

    def _mark_external_sent(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        store = self._store()
        try:
            try:
                store.mark_external_continue_sent(job.id)
            except ValueError as exc:
                messagebox.showerror("Mark Continue Sent", str(exc), parent=self.root)
                return
        finally:
            store.close()
        self.status_var.set("Desktop owner marked as continuing")
        self._refresh_all()

    def _mark_external_complete(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.ownership is not ThreadOwnership.ADOPTED_EXTERNAL:
            messagebox.showinfo(
                "Mark Complete",
                "Pet-managed Goals complete from their native Codex Goal status.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Mark Complete",
            "Mark this externally owned Codex task complete in the Pet queue?\n\n"
            "This does not delete or modify the Codex Desktop thread.",
            parent=self.root,
        ):
            return
        store = self._store()
        try:
            store.mark_external_complete(job.id)
        finally:
            store.close()
        self.status_var.set("External task marked complete")
        self._refresh_all()

    def _remove_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        job_id = int(selected[0])
        store = self._store()
        try:
            try:
                removed = store.delete_job(job_id)
            except ValueError as exc:
                messagebox.showerror("Remove", str(exc), parent=self.root)
                return
        finally:
            store.close()
        if removed:
            self._refresh_all()

    def _start_worker(self) -> None:
        if self.worker and self.worker.poll() is None:
            return
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "codex_supervisor", "--db", self.db_path, "run"],
