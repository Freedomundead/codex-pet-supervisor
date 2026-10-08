from codex_supervisor.models import JobStatus, ThreadOwnership
from codex_supervisor.store import Store


def test_queue_order_and_state_survive_reopen(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    first = store.add_job("first", "/tmp/a")
    second = store.add_job("second", "/tmp/b")
    store.update_job(first, status=JobStatus.ACTIVE, thread_id="thread-1")
    store.close()

    reopened = Store(db)
    try:
        current = reopened.current_job()
        assert current is not None
        assert current.id == first
        assert current.thread_id == "thread-1"
        reopened.update_job(first, status=JobStatus.COMPLETE)
        next_job = reopened.current_job()
        assert next_job is not None
        assert next_job.id == second
    finally:
        reopened.close()


def test_batch_queue_preserves_order_and_edit_is_allowed_before_thread(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        root = tmp_path / "project"
        (root / "Brain").mkdir(parents=True)
        store.set_project("project", str(root), make_default=True)
        store.set_scope("project", "brain", "Brain", make_default=True)
        ids = store.add_jobs(["first", "second", "third"], project_name="project", scope_name="brain")
        assert [job.prompt for job in store.list_jobs()] == ["first", "second", "third"]
        store.edit_job(ids[1], prompt="second edited", project_name="project", scope_name="brain")
        assert store.get_job(ids[1]).prompt == "second edited"
        store.update_job(ids[0], thread_id="thread-1", status=JobStatus.ACTIVE)
        try:
            store.edit_job(ids[0], prompt="not allowed", project_name="project", scope_name="brain")
        except ValueError as exc:
            assert "already has a Codex thread" in str(exc)
        else:
            raise AssertionError("started job was editable")
    finally:
        store.close()


def test_adopted_thread_is_persisted_and_can_be_detached(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        root = tmp_path / "project"
        root.mkdir()
        store.set_project("project", str(root), make_default=True)
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue existing work",
            cwd=str(root),
            project_name="project",
        )
        job = store.get_job(job_id)
        assert job is not None
        assert job.thread_id == "existing-thread"
        assert job.thread_origin == "adopted_external"
        assert job.ownership is ThreadOwnership.ADOPTED_EXTERNAL
        assert job.goal_status == "externalPending"
        assert store.delete_job(job_id) is True
        assert store.get_job(job_id) is None
    finally:
        store.close()


def test_external_owner_manual_dispatch_lifecycle(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store.update_job(job_id, status=JobStatus.READY_OWNER, goal_status="externalReady")
        store.mark_external_continue_sent(job_id)
        job = store.get_job(job_id)
        assert job.status is JobStatus.EXTERNAL_ACTIVE
        assert job.goal_status == "externalDispatched"
        store.mark_external_complete(job_id)
        job = store.get_job(job_id)
        assert job.status is JobStatus.COMPLETE
        assert job.goal_status == "externalComplete"
    finally:
