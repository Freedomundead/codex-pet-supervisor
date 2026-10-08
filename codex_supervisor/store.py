from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import Job, JobStatus, Project, Scope, ThreadOwnership


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,
                cwd TEXT NOT NULL,
                status TEXT NOT NULL,
                thread_id TEXT,
                goal_status TEXT,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id);

            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scopes (
                project_name TEXT NOT NULL,
                name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(project_name, name),
                FOREIGN KEY(project_name) REFERENCES projects(name) ON DELETE CASCADE
            );
            """
        )
        self._ensure_column("jobs", "project_name", "TEXT")
        self._ensure_column("jobs", "scope_name", "TEXT")
        self._ensure_column("jobs", "thread_origin", "TEXT NOT NULL DEFAULT 'pet'")
        self._ensure_column("jobs", "thread_title", "TEXT")
        self._ensure_column("jobs", "external_baseline_turn_id", "TEXT")
        self._ensure_column("jobs", "external_tracked_turn_id", "TEXT")
        self._ensure_column("jobs", "external_tracked_turn_status", "TEXT")
        self._ensure_column("jobs", "external_dispatch_at", "INTEGER")
        self._conn.execute(
            "UPDATE jobs SET thread_origin = ? WHERE thread_origin = 'adopted'",
            (ThreadOwnership.ADOPTED_EXTERNAL.value,),
        )
        self._conn.execute(
            """
            UPDATE jobs
            SET status = ?, goal_status = 'externalPending', last_error = NULL
            WHERE thread_origin = ? AND status = ?
            """,
            (
                JobStatus.QUEUED.value,
                ThreadOwnership.ADOPTED_EXTERNAL.value,
                JobStatus.WAITING_WRITER.value,
            ),
        )
        self._conn.execute(
            "UPDATE jobs SET goal_status = 'externalPending' "
            "WHERE thread_origin = ? AND goal_status = 'adoptedPending'",
            (ThreadOwnership.ADOPTED_EXTERNAL.value,),
        )
        self._conn.commit()

    def _ensure_column(self, table: str, name: str, decl: str) -> None:
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---------- Projects / scopes ----------
