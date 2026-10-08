from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .app_server import CodexAppServer
from .rate_limits import classify_duration, decide_availability, normalize_rate_limits
from .store import Store
from .supervisor import Supervisor, SupervisorConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-supervisor")
    parser.add_argument("--db", default="data/supervisor.db", help="SQLite state path")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Add a prompt to the persistent queue")
    add.add_argument("prompt")
    add.add_argument("--cwd", default=None, help="Direct working directory (bypasses project pointer)")
    add.add_argument("--project", default=None)
    add.add_argument("--scope", default=None)

    remove = sub.add_parser("remove", help="Remove a job that has not created a Codex thread")
    remove.add_argument("job_id", type=int)

    sub.add_parser("list", help="List queued/current/completed jobs")
    sub.add_parser("quota", help="Read current Codex quota from App Server")
    sub.add_parser("run-once", help="Advance the current job once")
    sub.add_parser("run", help="Run the persistent allowance supervisor")
    sub.add_parser("ui", help="Open the compact Pet control panel")
    probe = sub.add_parser("desktop-probe", help="Read-only inspect Codex Desktop accessibility controls")
    probe.add_argument("--output", default="data/desktop-probe.json")

    project = sub.add_parser("project", help="Manage project pointers")
    psub = project.add_subparsers(dest="project_command", required=True)
    pset = psub.add_parser("set")
    pset.add_argument("name")
    pset.add_argument("root")
    pset.add_argument("--default", action="store_true")
    pdefault = psub.add_parser("default")
    pdefault.add_argument("name")
    psub.add_parser("list")
    psub.add_parser("show")

    scope = sub.add_parser("scope", help="Manage named project scopes")
    ssub = scope.add_subparsers(dest="scope_command", required=True)
    sset = ssub.add_parser("set")
    sset.add_argument("name")
    sset.add_argument("relative_path")
    sset.add_argument("--project", default=None)
    sset.add_argument("--default", action="store_true")
    sdefault = ssub.add_parser("default")
    sdefault.add_argument("name")
    sdefault.add_argument("--project", default=None)
    slist = ssub.add_parser("list")
    slist.add_argument("--project", default=None)
    return parser


def _store(path: str) -> Store:
    return Store(Path(path).resolve())


async def _quota() -> int:
    client = CodexAppServer()
    await client.start()
    try:
        payload = await client.read_rate_limits()
    finally:
        await client.close()

    decision = decide_availability(payload)
    print(json.dumps({"allowed": decision.allowed, "reason": decision.reason, "wakeAt": decision.wake_at}, indent=2))
    for window in normalize_rate_limits(payload):
        print(
            f"{window.limit_id}:{window.slot} "
            f"{classify_duration(window.duration_mins)} "
            f"used={window.used_percent}% remaining={window.remaining_percent}% "
            f"resetsAt={window.resets_at}"
        )
    return 0


async def _run(db: str, once: bool) -> int:
    store = _store(db)
    try:
        supervisor = Supervisor(store, CodexAppServer(), SupervisorConfig())
        if once:
            await supervisor.run_once()
        else:
            await supervisor.run_forever()
    finally:
        store.close()
    return 0


def _selected_project(store: Store, explicit: str | None) -> str:
    name = explicit or store.default_project_name()
    if not name:
        raise ValueError("No project selected. Run: codex-supervisor project set NAME PATH --default")
    return name


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "add":
        store = _store(args.db)
        try:
            if args.cwd:
                path = Path(args.cwd).expanduser().resolve()
                if not path.is_dir():
                    parser.error(f"Working directory does not exist: {path}")
                job_id = store.add_job(args.prompt, str(path))
            else:
                try:
                    project = _selected_project(store, args.project)
                    scope = args.scope if args.scope is not None else store.default_scope_name(project)
                    target = store.resolve_target(project, scope)
                except ValueError as exc:
                    parser.error(str(exc))
                if not target.is_dir():
                    parser.error(f"Target folder does not exist: {target}")
                job_id = store.add_job(args.prompt, project_name=project, scope_name=scope)
            print(job_id)
        finally:
            store.close()
        return

    if args.command == "remove":
        store = _store(args.db)
        try:
            try:
                removed = store.delete_job(args.job_id)
            except ValueError as exc:
                parser.error(str(exc))
            if not removed:
                parser.error(f"Unknown job: {args.job_id}")
        finally:
            store.close()
        return

    if args.command == "list":
        store = _store(args.db)
        try:
            for job in store.list_jobs():
                print(
                    f"{job.id}\t{job.status.value}\tthread={job.thread_id or '-'}\t"
                    f"goal={job.goal_status or '-'}\t{store.job_target_label(job)}\t{job.prompt}"
                )
        finally:
            store.close()
        return

    if args.command == "project":
        store = _store(args.db)
        try:
            if args.project_command == "set":
                root = Path(args.root).expanduser().resolve()
                if not root.is_dir():
                    parser.error(f"Project folder does not exist: {root}")
                store.set_project(args.name, str(root), make_default=args.default)
                print(f"{args.name} -> {root}")
            elif args.project_command == "default":
                try:
                    store.set_default_project(args.name)
                except ValueError as exc:
                    parser.error(str(exc))
                print(args.name)
            elif args.project_command == "list":
                default = store.default_project_name()
                for item in store.list_projects():
                    marker = "*" if item.name == default else " "
                    print(f"{marker} {item.name}\t{item.root}")
            elif args.project_command == "show":
                name = store.default_project_name()
                if not name:
                    print("No default project")
                else:
                    item = store.get_project(name)
                    scope = store.default_scope_name(name)
                    print(f"project={name}\nroot={item.root if item else '-'}\nscope={scope or '-'}")
        finally:
            store.close()
        return

    if args.command == "scope":
        store = _store(args.db)
        try:
            try:
                project = _selected_project(store, args.project)
            except ValueError as exc:
                parser.error(str(exc))
            if args.scope_command == "set":
                project_obj = store.get_project(project)
                assert project_obj is not None
                root = Path(project_obj.root).resolve()
                target = (root / args.relative_path).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    parser.error("Scope path must remain inside the project root")
                if not target.is_dir():
                    parser.error(f"Scope folder does not exist: {target}")
                store.set_scope(project, args.name, args.relative_path, make_default=args.default)
                print(f"{project}/{args.name} -> {args.relative_path}")
            elif args.scope_command == "default":
                try:
                    store.set_default_scope(project, args.name)
                except ValueError as exc:
                    parser.error(str(exc))
                print(f"{project}/{args.name}")
            elif args.scope_command == "list":
                default = store.default_scope_name(project)
                for item in store.list_scopes(project):
                    marker = "*" if item.name == default else " "
                    print(f"{marker} {item.name}\t{item.relative_path}")
        finally:
            store.close()
        return

    if args.command == "quota":
        raise SystemExit(asyncio.run(_quota()))

    if args.command == "run-once":
        raise SystemExit(asyncio.run(_run(args.db, True)))

    if args.command == "run":
        try:
            raise SystemExit(asyncio.run(_run(args.db, False)))
        except KeyboardInterrupt:
            raise SystemExit(130)

    if args.command == "desktop-probe":
        from .desktop_uia import DesktopProbeError, probe_codex_desktop, save_probe_report, summarize_probe
        try:
            payload = probe_codex_desktop()
            report = save_probe_report(payload, args.output)
            summary = summarize_probe(payload)
        except DesktopProbeError as exc:
            parser.error(str(exc))
        print(f"windows={summary.windows} captured_elements={summary.elements}")
        print(f"report={report}")
        for label, items in (("thread", summary.likely_thread_items), ("composer", summary.likely_composers), ("send", summary.likely_send_controls)):
            for item in items[:10]:
                print(f"{label}: {item}")
        return

    if args.command == "ui":
        from .ui import run_ui
        run_ui(str(Path(args.db).resolve()))
        return
