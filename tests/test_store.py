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
        store.close()


def test_legacy_adopted_writer_state_migrates_to_external_ownership(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store._conn.execute(
            "UPDATE jobs SET thread_origin = 'adopted', status = ?, goal_status = 'adoptedPending' WHERE id = ?",
            (JobStatus.WAITING_WRITER.value, job_id),
        )
        store._conn.commit()
    finally:
        store.close()

    reopened = Store(db)
    try:
        job = reopened.get_job(job_id)
        assert job is not None
        assert job.thread_origin == "adopted_external"
        assert job.ownership is ThreadOwnership.ADOPTED_EXTERNAL
        assert job.status is JobStatus.QUEUED
        assert job.goal_status == "externalPending"
    finally:
        reopened.close()


def test_adopted_thread_title_and_external_tracking_persist(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            thread_title="Inspect Brain understanding",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store.update_job(
            job_id,
            external_baseline_turn_id="turn-1",
            external_tracked_turn_id="turn-2",
            external_tracked_turn_status="inProgress",
            external_dispatch_at=123,
        )
        job = store.get_job(job_id)
        assert job is not None
        assert job.thread_title == "Inspect Brain understanding"
        assert job.external_baseline_turn_id == "turn-1"
        assert job.external_tracked_turn_id == "turn-2"
        assert job.external_tracked_turn_status == "inProgress"
        assert job.external_dispatch_at == 123
    finally:
        store.close()


def test_blocked_managed_job_can_be_retried_without_replacing_thread(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.add_job("finish task", str(tmp_path))
        store.update_job(
            job_id,
            status=JobStatus.BLOCKED,
            thread_id="thread-1",
            goal_status="blocked",
            last_error="needs approval",
        )
        store.retry_blocked_job(job_id)
        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert job.thread_id == "thread-1"
        assert job.goal_status == "retryRequested"
        assert job.last_error is None
    finally:
        store.close()


def test_abandon_blocked_managed_job_unblocks_queue_without_deleting_thread(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        first = store.add_job("too narrow", str(tmp_path))
        second = store.add_job("next", str(tmp_path))
        store.update_job(first, status=JobStatus.BLOCKED, thread_id="thread-1", goal_status="blocked")
        assert store.current_job().id == first
        store.abandon_blocked_job(first)
        abandoned = store.get_job(first)
