import asyncio

from codex_supervisor.app_server import AppServerError
from codex_supervisor.models import JobStatus, RpcNotification
from codex_supervisor.store import Store
from codex_supervisor.supervisor import Supervisor, SupervisorConfig


class FakeAppServer:
    def __init__(self, goal_status="active", latest_turn=None):
        self.goal_status = goal_status
        self.handlers = []
        self.calls = []
        self.server_request_handler = None
        self.latest_turn_value = latest_turn

    def add_notification_handler(self, handler):
        self.handlers.append(handler)

    def set_server_request_handler(self, handler):
        self.server_request_handler = handler

    async def latest_turn(self, thread_id):
        self.calls.append(("latest_turn", thread_id))
        return self.latest_turn_value

    async def start(self):
        self.calls.append(("start", None))

    async def close(self):
        self.calls.append(("close", None))

    async def read_rate_limits(self):
        return {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 9999999999},
                "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 9999999999},
            },
        }

    async def start_thread(self, **kwargs):
        self.calls.append(("start_thread", kwargs))
        return "thread-1"

    async def resume_thread(self, thread_id):
        self.calls.append(("resume_thread", thread_id))

    async def set_goal(self, thread_id, *, objective=None, status="active"):
        self.calls.append(("set_goal", {"thread_id": thread_id, "objective": objective, "status": status}))
        self.goal_status = status
        return {"goal": {"threadId": thread_id, "status": status}}

    async def get_goal(self, thread_id):
        self.calls.append(("get_goal", thread_id))
        return {"threadId": thread_id, "status": self.goal_status}


def test_new_job_uses_native_goal_without_duplicate_continue_turn(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.add_job("finish task", str(tmp_path))
        fake = FakeAppServer()
        supervisor = Supervisor(store, fake, SupervisorConfig())
        asyncio.run(supervisor.run_once())

        job = store.get_job(job_id)
        assert job is not None
        assert job.thread_id == "thread-1"
        assert job.status is JobStatus.ACTIVE
        assert [name for name, _ in fake.calls].count("set_goal") == 1
        assert all(name != "start_turn" for name, _ in fake.calls)
    finally:
        store.close()


def test_usage_limited_goal_is_reactivated_when_account_quota_is_available(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.add_job("finish task", str(tmp_path))
        store.update_job(job_id, status=JobStatus.WAITING_QUOTA, thread_id="thread-1", goal_status="usageLimited")
        fake = FakeAppServer(goal_status="usageLimited")
        supervisor = Supervisor(store, fake, SupervisorConfig())
        asyncio.run(supervisor.run_once())

        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.ACTIVE
        assert job.goal_status == "active"
        goal_calls = [payload for name, payload in fake.calls if name == "set_goal"]
        assert goal_calls == [{"thread_id": "thread-1", "objective": None, "status": "active"}]
    finally:
        store.close()


def test_budget_limited_goal_is_not_overridden_as_account_quota(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.add_job("finish task", str(tmp_path))
        store.update_job(job_id, status=JobStatus.ACTIVE, thread_id="thread-1")
        fake = FakeAppServer(goal_status="budgetLimited")
        supervisor = Supervisor(store, fake, SupervisorConfig())
        asyncio.run(supervisor.run_once())

        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.BLOCKED
        assert all(name != "set_goal" for name, _ in fake.calls)
    finally:
        store.close()


def test_adopted_external_thread_never_acquires_writer(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue from the unfinished step",
            cwd=str(tmp_path),
        )
        store.set_json("desktop_auto_dispatch", False)
        fake = FakeAppServer()
        supervisor = Supervisor(store, fake, SupervisorConfig())
        asyncio.run(supervisor.run_once())

        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.READY_OWNER
        assert job.goal_status == "externalReady"
        assert "owned by Codex Desktop" in (job.last_error or "")
        forbidden = {"resume_thread", "get_goal", "set_goal", "start_thread"}
        assert forbidden.isdisjoint({name for name, _ in fake.calls})
    finally:
        store.close()


def test_external_owner_mark_sent_is_monitor_only(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store.update_job(job_id, status=JobStatus.READY_OWNER, goal_status="externalReady")
        store.mark_external_continue_sent(job_id)
        store.set_json("desktop_auto_dispatch", False)

        fake = FakeAppServer()
        supervisor = Supervisor(store, fake, SupervisorConfig())
        asyncio.run(supervisor.run_once())

        job = store.get_job(job_id)
        assert job.status is JobStatus.EXTERNAL_ACTIVE
        forbidden = {"resume_thread", "get_goal", "set_goal", "start_thread"}
        assert forbidden.isdisjoint({name for name, _ in fake.calls})
    finally:
        store.close()


def test_external_owner_rearms_after_quota_cycle(tmp_path):
    class ExhaustedFake(FakeAppServer):
        async def read_rate_limits(self):
            return {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 9999999999},
                    "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 9999999999},
                },
            }

    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store.update_job(job_id, status=JobStatus.EXTERNAL_ACTIVE, goal_status="externalDispatched")
        store.set_json("desktop_auto_dispatch", False)

        exhausted = ExhaustedFake()
        asyncio.run(Supervisor(store, exhausted, SupervisorConfig()).run_once())
        assert store.get_job(job_id).status is JobStatus.WAITING_QUOTA

        available = FakeAppServer()
        asyncio.run(Supervisor(store, available, SupervisorConfig()).run_once())
        job = store.get_job(job_id)
        assert job.status is JobStatus.READY_OWNER
        assert job.goal_status == "externalReady"
        forbidden = {"resume_thread", "get_goal", "set_goal", "start_thread"}
        assert forbidden.isdisjoint({name for name, _ in available.calls})
    finally:
        store.close()



def test_external_owner_completion_is_detected_from_turn_status(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            thread_title="Inspect Brain understanding",
            prompt="continue",
            cwd=str(tmp_path),
        )
        store.set_json("desktop_auto_dispatch", False)
        store.update_job(
            job_id,
            status=JobStatus.EXTERNAL_ACTIVE,
            goal_status="externalDispatched",
            external_baseline_turn_id="old-turn",
            external_tracked_turn_id="new-turn",
            external_tracked_turn_status="inProgress",
        )
        fake = FakeAppServer(latest_turn={"id": "new-turn", "status": "completed"})
        asyncio.run(Supervisor(store, fake, SupervisorConfig()).run_once())
        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.COMPLETE
        assert job.goal_status == "externalComplete"
        forbidden = {"resume_thread", "get_goal", "set_goal", "start_thread"}
        assert forbidden.isdisjoint({name for name, _ in fake.calls})
    finally:
        store.close()


def test_external_owner_in_progress_is_detected_without_dispatch(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        job_id = store.adopt_thread(
            thread_id="existing-thread",
            thread_title="Inspect Brain understanding",
            prompt="continue",
            cwd=str(tmp_path),
        )
        fake = FakeAppServer(latest_turn={"id": "turn-live", "status": "inProgress"})
        asyncio.run(Supervisor(store, fake, SupervisorConfig()).run_once())
        job = store.get_job(job_id)
        assert job is not None
        assert job.status is JobStatus.EXTERNAL_ACTIVE
        assert job.external_tracked_turn_id == "turn-live"
        forbidden = {"resume_thread", "get_goal", "set_goal", "start_thread"}
        assert forbidden.isdisjoint({name for name, _ in fake.calls})
    finally:
        store.close()


def test_managed_command_and_file_approvals_inside_scope_are_accepted(tmp_path):
    root = tmp_path / "project"
    brain = root / "Brain"
    brain.mkdir(parents=True)
    store = Store(tmp_path / "state.db")
    try:
        store.set_project("SBC", str(root), make_default=True)
        store.set_scope("SBC", "brain", "Brain", make_default=True)
        job_id = store.add_job("edit Brain", project_name="SBC", scope_name="brain")
        store.update_job(job_id, status=JobStatus.ACTIVE, thread_id="thread-1")
        fake = FakeAppServer()
        supervisor = Supervisor(store, fake, SupervisorConfig())

        command = asyncio.run(supervisor._on_server_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "cwd": str(brain), "command": "python test.py"},
        ))
        file_change = asyncio.run(supervisor._on_server_request(
            "item/fileChange/requestApproval",
            {"threadId": "thread-1", "grantRoot": str(brain)},
