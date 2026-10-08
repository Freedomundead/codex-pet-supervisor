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
