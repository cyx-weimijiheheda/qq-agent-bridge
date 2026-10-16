import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import task_manager
from state_db import StateDB
from task_manager import TaskManager


def test_submit_and_list(tmp_path):
    db = StateDB(tmp_path / "state.db")
    tm = TaskManager(None, db)
    tid = tm.submit("10001", "帮我写报告", kind="task")
    assert tid > 0
    tasks = tm.list_tasks("10001")
    assert tasks[0]["status"] == "queued"
    assert tasks[0]["prompt"] == "帮我写报告"


def test_cancel_queued_task(tmp_path):
    db = StateDB(tmp_path / "state.db")
    tm = TaskManager(None, db)
    tid = tm.submit("10001", "帮我写报告")
    assert tm.cancel(tid) is True
    tasks = tm.list_tasks("10001")
    assert tasks[0]["status"] == "cancelled"
    assert tm.cancel(tid) is True  # 幂等


def test_process_next_runs_and_sends_result(tmp_path, monkeypatch):
    db = StateDB(tmp_path / "state.db")

    class FakeBridge:
        def __init__(self):
            self.sent = []
            self.cfg = {}

        async def _send_private(self, user_id, text):
            self.sent.append((user_id, text))

    bridge = FakeBridge()
    tid = TaskManager(bridge, db).submit("10001", "帮我写报告")

    async def fake_run(bridge_, prompt, thread_id, policy):
        assert prompt == "帮我写报告"
        return "报告写好了", f"task-{tid}"

    monkeypatch.setattr(task_manager, "run_with_policy", fake_run)
    tm = TaskManager(bridge, db)
    done = asyncio.run(tm.process_next())
    assert done is True
    task = tm.list_tasks("10001")[0]
    assert task["status"] == "done"
    assert task["result"] == "报告写好了"
    assert bridge.sent == [("10001", "✅ 任务完成：报告写好了")]


def test_process_next_marks_failed_on_error(tmp_path, monkeypatch):
    db = StateDB(tmp_path / "state.db")

    class FakeBridge:
        def __init__(self):
            self.cfg = {}

        async def _send_private(self, user_id, text):
            pass

    bridge = FakeBridge()
    tm = TaskManager(bridge, db)
    tm.submit("10001", "会失败的任务")

    async def fake_run(bridge_, prompt, thread_id, policy):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_manager, "run_with_policy", fake_run)
    asyncio.run(tm.process_next())
    task = tm.list_tasks("10001")[0]
    assert task["status"] == "failed"
    assert "boom" in task["error"]


def test_cancel_task_running_reports_not_cancellable(tmp_path):
    """worker 会把状态覆盖回 done/failed，所以 running 的任务取消不了 —— 要如实说。"""
    db = StateDB(tmp_path / "state.db")
    tm = TaskManager(None, db)
    task_id = db.add_task("10001", "task", "跑着的任务", status="running")

    result = tm.cancel_task(task_id)
    assert result["ok"] is False
    assert result["reason"] == "running_not_cancellable"
    assert result["message"]
    assert db.get_task(task_id)["status"] == "running"


def test_cancel_task_scoped_and_missing(tmp_path):
    db = StateDB(tmp_path / "state.db")
    tm = TaskManager(None, db)
    task_id = tm.submit("10001", "排队任务")

    assert tm.cancel_task(task_id, user_id="99999")["reason"] == "not_found"
    assert tm.cancel_task(424242)["reason"] == "not_found"

    assert tm.cancel_task(task_id, user_id="10001")["ok"] is True
    # 幂等：再次取消视为已终结
    assert tm.cancel_task(task_id)["reason"] == "already_final"
    assert tm.cancel(task_id) is True  # 旧接口语义保留
