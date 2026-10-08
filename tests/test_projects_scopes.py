from pathlib import Path

from codex_supervisor.models import JobStatus
from codex_supervisor.store import Store


def test_queued_project_job_follows_project_pointer_until_start(tmp_path):
    db = tmp_path / "state.db"
    v9 = tmp_path / "DEMO_V1"
    v10 = tmp_path / "DEMO_V2"
    (v9 / "Brain").mkdir(parents=True)
    (v10 / "Brain").mkdir(parents=True)

    store = Store(db)
    try:
        store.set_project("SBC", str(v9), make_default=True)
        store.set_scope("SBC", "brain", "Brain", make_default=True)
        job_id = store.add_job("audit brain", project_name="SBC", scope_name="brain")
        job = store.get_job(job_id)
        assert job is not None
        assert job.cwd == ""
        assert store.resolve_job_cwd(job) == (v9 / "Brain").resolve()

        store.set_project("SBC", str(v10))
        job = store.get_job(job_id)
        assert job is not None
        assert store.resolve_job_cwd(job) == (v10 / "Brain").resolve()
    finally:
        store.close()


def test_started_job_remains_pinned_when_project_pointer_changes(tmp_path):
    db = tmp_path / "state.db"
    v9 = tmp_path / "DEMO_V1"
    v10 = tmp_path / "DEMO_V2"
    (v9 / "Brain").mkdir(parents=True)
    (v10 / "Brain").mkdir(parents=True)

    store = Store(db)
    try:
        store.set_project("SBC", str(v9), make_default=True)
        store.set_scope("SBC", "brain", "Brain", make_default=True)
        job_id = store.add_job("audit brain", project_name="SBC", scope_name="brain")
        pinned = str((v9 / "Brain").resolve())
        store.update_job(job_id, cwd=pinned, thread_id="thread-1", status=JobStatus.ACTIVE)
        store.set_project("SBC", str(v10))

        job = store.get_job(job_id)
        assert job is not None
        assert store.resolve_job_cwd(job) == Path(pinned)
    finally:
        store.close()


def test_scope_cannot_escape_project_root(tmp_path):
    db = tmp_path / "state.db"
    root = tmp_path / "SBC"
    root.mkdir()
    store = Store(db)
    try:
        store.set_project("SBC", str(root), make_default=True)
        store.set_scope("SBC", "bad", "..")
        try:
            store.resolve_target("SBC", "bad")
        except ValueError as exc:
            assert "escapes project root" in str(exc)
        else:
            raise AssertionError("escaping scope should fail")
    finally:
        store.close()


def test_delete_only_jobs_without_native_thread(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        queued = store.add_job("queued", str(tmp_path))
        assert store.delete_job(queued) is True

        waiting = store.add_job("waiting", str(tmp_path))
        store.update_job(waiting, status=JobStatus.WAITING_QUOTA)
        assert store.delete_job(waiting) is True

        active = store.add_job("active", str(tmp_path))
        store.update_job(active, status=JobStatus.ACTIVE, thread_id="thread-1")
        try:
            store.delete_job(active)
        except ValueError as exc:
            assert "Codex thread" in str(exc)
        else:
            raise AssertionError("job with native Codex thread should not be deleted")
    finally:
        store.close()


def test_stop_reset_only_requeues_waiting_jobs_without_threads(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        unstarted = store.add_job("waiting", str(tmp_path))
        started = store.add_job("started", str(tmp_path))
        store.update_job(unstarted, status=JobStatus.WAITING_QUOTA)
        store.update_job(started, status=JobStatus.WAITING_QUOTA, thread_id="thread-1")

        assert store.reset_unstarted_waiting_jobs() == 1
        assert store.get_job(unstarted).status is JobStatus.QUEUED
        assert store.get_job(started).status is JobStatus.WAITING_QUOTA
    finally:
        store.close()
