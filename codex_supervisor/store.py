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

    def set_project(self, name: str, root: str, *, make_default: bool = False) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO projects(name, root, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET root = excluded.root, updated_at = excluded.updated_at
            """,
            (name, root, now),
        )
        self._conn.commit()
        if make_default or self.default_project_name() is None:
            self.set_default_project(name)

    def get_project(self, name: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return self._row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row_to_project(row) for row in rows]

    def set_default_project(self, name: str) -> None:
        if self.get_project(name) is None:
            raise ValueError(f"Unknown project: {name}")
        self.set_json("default_project", name)

    def default_project_name(self) -> str | None:
        value = self.get_json("default_project")
        return str(value) if isinstance(value, str) and value else None

    def set_scope(self, project_name: str, name: str, relative_path: str, *, make_default: bool = False) -> None:
        if self.get_project(project_name) is None:
            raise ValueError(f"Unknown project: {project_name}")
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO scopes(project_name, name, relative_path, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(project_name, name) DO UPDATE SET
                relative_path = excluded.relative_path,
                updated_at = excluded.updated_at
            """,
            (project_name, name, relative_path, now),
        )
        self._conn.commit()
        key = f"default_scope:{project_name}"
        if make_default or self.get_json(key) is None:
            self.set_json(key, name)

    def get_scope(self, project_name: str, name: str) -> Scope | None:
        row = self._conn.execute(
            "SELECT * FROM scopes WHERE project_name = ? AND name = ?",
            (project_name, name),
        ).fetchone()
        return self._row_to_scope(row) if row else None

    def list_scopes(self, project_name: str) -> list[Scope]:
        rows = self._conn.execute(
            "SELECT * FROM scopes WHERE project_name = ? ORDER BY name COLLATE NOCASE",
            (project_name,),
        ).fetchall()
        return [self._row_to_scope(row) for row in rows]

    def set_default_scope(self, project_name: str, name: str | None) -> None:
        key = f"default_scope:{project_name}"
        if name is None:
            self.set_json(key, None)
            return
        if self.get_scope(project_name, name) is None:
            raise ValueError(f"Unknown scope {name!r} for project {project_name!r}")
        self.set_json(key, name)

    def default_scope_name(self, project_name: str) -> str | None:
        value = self.get_json(f"default_scope:{project_name}")
        return str(value) if isinstance(value, str) and value else None

    def resolve_target(self, project_name: str, scope_name: str | None = None) -> Path:
        project = self.get_project(project_name)
        if project is None:
            raise ValueError(f"Unknown project: {project_name}")
        root = Path(project.root).expanduser().resolve()
        if scope_name:
            scope = self.get_scope(project_name, scope_name)
            if scope is None:
                raise ValueError(f"Unknown scope {scope_name!r} for project {project_name!r}")
            target = (root / scope.relative_path).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Scope {scope_name!r} escapes project root") from exc
            return target
        return root

    # ---------- Jobs ----------

    def add_job(
        self,
        prompt: str,
        cwd: str = "",
        *,
        project_name: str | None = None,
        scope_name: str | None = None,
    ) -> int:
        return self.add_jobs(
            [prompt],
            cwd=cwd,
            project_name=project_name,
            scope_name=scope_name,
        )[0]

    def add_jobs(
        self,
        prompts: list[str] | tuple[str, ...],
        cwd: str = "",
        *,
        project_name: str | None = None,
        scope_name: str | None = None,
    ) -> list[int]:
        cleaned = [str(prompt).strip() for prompt in prompts if str(prompt).strip()]
        if not cleaned:
            return []
        now = int(time.time())
        ids: list[int] = []
        for prompt in cleaned:
            cur = self._conn.execute(
                """
                INSERT INTO jobs(prompt, cwd, status, created_at, updated_at, project_name, scope_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (prompt, cwd, JobStatus.QUEUED.value, now, now, project_name, scope_name),
            )
            ids.append(int(cur.lastrowid))
        self._conn.commit()
        return ids

    def adopt_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: str,
        project_name: str | None = None,
        scope_name: str | None = None,
        thread_title: str | None = None,
        goal_status: str | None = "externalPending",
    ) -> int:
        if not thread_id.strip():
            raise ValueError("Thread id is required")
        existing = self._conn.execute(
            "SELECT id FROM jobs WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if existing:
            raise ValueError(f"Thread is already managed by job {existing['id']}")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Continuation Goal cannot be empty")
        now = int(time.time())
        cur = self._conn.execute(
            """
            INSERT INTO jobs(
                prompt, cwd, status, thread_id, goal_status, created_at, updated_at,
                project_name, scope_name, thread_origin, thread_title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prompt,
                cwd,
                JobStatus.QUEUED.value,
                thread_id,
                goal_status,
                now,
                now,
                project_name,
                scope_name,
                ThreadOwnership.ADOPTED_EXTERNAL.value,
                thread_title,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def edit_job(
        self,
        job_id: int,
        *,
        prompt: str,
        project_name: str | None = None,
        scope_name: str | None = None,
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        if job.thread_id and job.ownership is not ThreadOwnership.ADOPTED_EXTERNAL:
            raise ValueError("Cannot edit a Pet-managed job that already has a Codex thread")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Goal cannot be empty")
        if project_name is not None:
            if self.get_project(project_name) is None:
                raise ValueError(f"Unknown project: {project_name}")
            if scope_name is not None and self.get_scope(project_name, scope_name) is None:
                raise ValueError(f"Unknown scope {scope_name!r} for project {project_name!r}")
            target = self.resolve_target(project_name, scope_name)
            if not target.is_dir():
                raise ValueError(f"Target folder does not exist: {target}")
        now = int(time.time())
        status = job.status
        if status in {JobStatus.BLOCKED, JobStatus.FAILED}:
            status = JobStatus.QUEUED
        if job.ownership is ThreadOwnership.ADOPTED_EXTERNAL and job.thread_id:
            self._conn.execute(
                """
                UPDATE jobs
                SET prompt = ?, project_name = ?, scope_name = ?, status = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (prompt, project_name, scope_name, status.value, now, job_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE jobs
                SET prompt = ?, project_name = ?, scope_name = ?, cwd = '', status = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (prompt, project_name, scope_name, status.value, now, job_id),
            )
        self._conn.commit()

    def get_job(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def current_job(self) -> Job | None:
        row = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN (?, ?, ?, ?, ?, ?, ?)
            ORDER BY CASE status
                WHEN ? THEN 0
                WHEN ? THEN 1
                WHEN ? THEN 2
                WHEN ? THEN 3
                WHEN ? THEN 4
                WHEN ? THEN 5
                ELSE 6
            END, id
            LIMIT 1
            """,
            (
                JobStatus.ACTIVE.value,
                JobStatus.EXTERNAL_ACTIVE.value,
                JobStatus.WAITING_QUOTA.value,
                JobStatus.READY_OWNER.value,
                JobStatus.WAITING_WRITER.value,
                JobStatus.BLOCKED.value,
                JobStatus.QUEUED.value,
                JobStatus.ACTIVE.value,
                JobStatus.EXTERNAL_ACTIVE.value,
                JobStatus.WAITING_QUOTA.value,
                JobStatus.READY_OWNER.value,
                JobStatus.WAITING_WRITER.value,
                JobStatus.BLOCKED.value,
            ),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [self._row_to_job(row) for row in rows]

    def delete_job(self, job_id: int) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        if job.thread_id and job.ownership is not ThreadOwnership.ADOPTED_EXTERNAL:
            raise ValueError("Cannot remove a Pet-managed job that already has a Codex thread")
        cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def reset_unstarted_waiting_jobs(self) -> int:
        """Return quota-waiting jobs that never reached Codex to the queue."""
        now = int(time.time())
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?
            WHERE status = ? AND thread_id IS NULL
            """,
            (JobStatus.QUEUED.value, now, JobStatus.WAITING_QUOTA.value),
        )
        self._conn.commit()
        return int(cur.rowcount)
