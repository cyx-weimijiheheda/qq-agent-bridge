import asyncio
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bridge import Bridge
from sessions import SessionStore
from state_db import StateDB
from task_manager import TaskManager
from web_panel import WebPanel


class FakeBridge:
    def __init__(self, tmp_path):
        self.cfg = {
            "web_token": "test-token",
            "web_host": "127.0.0.1",
            "web_port": 18080,
            "full_access_qq": ["10001"],
            "data_dir": str(tmp_path),
            "log_file": str(tmp_path / "bridge.log"),
            "rag_dirs": [],
        }
        self.state_db = StateDB(tmp_path / "state.db")
        self.task_manager = TaskManager(self, self.state_db)
        self.store = SessionStore(tmp_path / "sessions.json")
        self.ws = None
        self.web_replies = {}
        self.bridge_dir = tmp_path
        self.self_id = "10001"
        self.seen_ids = deque()
        self.buffers = {}
        self.buffer_tasks = {}
        self.group_reply_times = {}

    async def process_web_message(self, user_id, text, timeout=900):
        return [f"echo:{text}"]


def make_panel(tmp_path, **cfg_overrides):
    bridge = FakeBridge(tmp_path)
    bridge.cfg.update(cfg_overrides)
    return WebPanel(bridge), bridge


def authed(client):
    return {"Authorization": "Bearer test-token"}


def write_log(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_root_serves_page_without_token(tmp_path):
    panel = WebPanel(FakeBridge(tmp_path))
    client = TestClient(panel.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "QQ AI 助手" in r.text


def test_requires_token(tmp_path):
    panel = WebPanel(FakeBridge(tmp_path))
    client = TestClient(panel.app)
    r = client.get("/api/status")
    assert r.status_code == 401
    r = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_status_authed(tmp_path):
    panel = WebPanel(FakeBridge(tmp_path))
    client = TestClient(panel.app)
    r = client.get("/api/status", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["bot"] == "qq-agent-bridge"


def test_chat_endpoint(tmp_path):
    panel = WebPanel(FakeBridge(tmp_path))
    client = TestClient(panel.app)
    headers = {"Authorization": "Bearer test-token"}
    r = client.post("/api/chat", json={"text": "你好"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["replies"] == ["echo:你好"]


def test_reminders_post(tmp_path):
    bridge = FakeBridge(tmp_path)
    panel = WebPanel(bridge)
    client = TestClient(panel.app)
    headers = {"Authorization": "Bearer test-token"}
    r = client.post("/api/reminders", json={"text": "喝水 明天9点"}, headers=headers)
    assert r.status_code == 200
    assert "已设置提醒" in r.json()["message"]


def test_web_chat_uses_bridge_pipeline(tmp_path):
    class FakeCodex:
        async def run(
            self,
            prompt,
            thread_id=None,
            timeout=None,
            reasoning_effort=None,
            image_paths=None,
            model=None,
            model_provider=None,
            bypass_proxy=False,
            full_access=True,
            group=False,
            persona=False,
        ):
            return "网页回复", "thread-web"

    cfg = {
        "full_access_qq": ["10001"],
        "workdir": str(tmp_path),
        "persona_enabled": False,
        "plugins_enabled": False,
        "batch_window": 0.1,
        "image_wait_window": 0.1,
        "chunk_delay_sec": 0,
        "ack_delay_sec": 0,
        "forward_threshold": 99,
    }
    store = SessionStore(tmp_path / "sessions.json")
    bridge = Bridge(cfg, store, FakeCodex())
    replies = asyncio.run(bridge.process_web_message("web-main", "你好"))
    assert any("网页回复" in r for r in replies)


# ── 作用域 ───────────────────────────────────────────


def test_scopes_merges_sources(tmp_path):
    panel, bridge = make_panel(tmp_path)
    bridge.state_db.add_todo("web-x", "从 Web 建的")
    store = SessionStore(tmp_path / "sessions.json")
    store.mark_activity("web-c1")
    bridge.store = store

    data = TestClient(panel.app).get("/api/scopes", headers=authed(None)).json()
    keys = [s["key"] for s in data["scopes"]]
    assert data["default"] == "10001"
    assert keys[0] == "10001"                      # 主人恒排第一
    for expected in ("web-main", "web-x", "web-c1"):
        assert expected in keys


def test_unknown_scope_is_rejected(tmp_path):
    panel, _ = make_panel(tmp_path)
    r = TestClient(panel.app).get("/api/todos?scope=evil", headers=authed(None))
    assert r.status_code == 403


def test_legacy_user_id_param_still_works(tmp_path):
    panel, bridge = make_panel(tmp_path)
    bridge.state_db.add_todo("10001", "旧参数")
    r = TestClient(panel.app).get("/api/todos?user_id=10001", headers=authed(None))
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "10001"
    assert len(body["todos"]) == 1


# ── 越权（变更类必须校验归属）─────────────────────────


def test_complete_todo_cross_scope_is_blocked(tmp_path):
    panel, bridge = make_panel(tmp_path)
    todo_id = bridge.state_db.add_todo("10001", "别人的待办")
    # web-main 也是合法作用域，但这条待办不属于它
    r = TestClient(panel.app).post(
        "/api/todos/complete", json={"id": todo_id, "scope": "web-main"}, headers=authed(None)
    )
    assert r.status_code == 404
    assert bridge.state_db.get_todo(todo_id)["done"] == 0


def test_delete_todo_cross_scope_is_blocked(tmp_path):
    panel, bridge = make_panel(tmp_path)
    todo_id = bridge.state_db.add_todo("10001", "别人的待办")
    r = TestClient(panel.app).post(
        "/api/todos/delete", json={"id": todo_id, "scope": "web-main"}, headers=authed(None)
    )
    assert r.status_code == 404
    assert bridge.state_db.get_todo(todo_id) is not None


def test_cancel_reminder_cross_scope_is_blocked(tmp_path):
    panel, bridge = make_panel(tmp_path)
    rid = bridge.state_db.create_reminder("10001", "喝水", "2099-01-01T09:00:00")
    r = TestClient(panel.app).post(
        "/api/reminders/cancel", json={"id": rid, "scope": "web-main"}, headers=authed(None)
    )
    assert r.status_code == 404
    assert bridge.state_db.get_reminder(rid)["status"] == "active"


def test_cancel_task_cross_scope_is_blocked(tmp_path):
    panel, bridge = make_panel(tmp_path)
    task_id = bridge.task_manager.submit("10001", "别人的任务")
    r = TestClient(panel.app).post(
        "/api/tasks/cancel", json={"id": task_id, "scope": "web-main"}, headers=authed(None)
    )
    assert r.status_code == 404
    assert bridge.state_db.get_task(task_id)["status"] == "queued"


# ── 变更路由 ─────────────────────────────────────────


def test_reminder_cancel_marks_cancelled_not_done(tmp_path):
    """取消与完成必须是两个不同的状态，否则「取消」按钮是假的。"""
    panel, bridge = make_panel(tmp_path)
    cancelled_id = bridge.state_db.create_reminder("10001", "取消我", "2099-01-01T09:00:00")
    done_id = bridge.state_db.create_reminder("10001", "完成我", "2099-01-01T10:00:00")
    client = TestClient(panel.app)

    assert client.post(
        "/api/reminders/cancel", json={"id": cancelled_id}, headers=authed(None)
    ).json()["ok"] is True
    assert client.post(
        "/api/reminders/done", json={"id": done_id}, headers=authed(None)
    ).json()["ok"] is True

    assert bridge.state_db.get_reminder(cancelled_id)["status"] == "cancelled"
    assert bridge.state_db.get_reminder(done_id)["status"] == "done"


def test_todo_complete_and_delete(tmp_path):
    panel, bridge = make_panel(tmp_path)
    todo_id = bridge.state_db.add_todo("10001", "写周报")
    client = TestClient(panel.app)

    r = client.post("/api/todos/complete", json={"id": todo_id}, headers=authed(None))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert bridge.state_db.get_todo(todo_id)["done"] == 1

    r = client.post("/api/todos/delete", json={"id": todo_id}, headers=authed(None))
    assert r.status_code == 200
    assert bridge.state_db.get_todo(todo_id) is None


def test_task_cancel_running_reports_not_cancellable(tmp_path):
    """正在执行的任务取消不了，接口必须如实说，而不是假装成功。"""
    panel, bridge = make_panel(tmp_path)
    task_id = bridge.state_db.add_task("10001", "task", "跑着的任务", status="running")
    r = TestClient(panel.app).post(
        "/api/tasks/cancel", json={"id": task_id}, headers=authed(None)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "running_not_cancellable"
    assert bridge.state_db.get_task(task_id)["status"] == "running"


def test_task_cancel_queued_succeeds(tmp_path):
    panel, bridge = make_panel(tmp_path)
    task_id = bridge.task_manager.submit("10001", "排队中的任务")
    r = TestClient(panel.app).post(
        "/api/tasks/cancel", json={"id": task_id}, headers=authed(None)
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert bridge.state_db.get_task(task_id)["status"] == "cancelled"


def test_new_routes_require_token(tmp_path):
    panel, _ = make_panel(tmp_path)
    r = TestClient(panel.app).post("/api/todos/delete", json={"id": 1})
    assert r.status_code == 401


# ── 日志协议 ─────────────────────────────────────────


def _log_client(tmp_path, content=""):
    panel, bridge = make_panel(tmp_path)
    log_path = Path(bridge.cfg["log_file"])
    if content:
        write_log(log_path, content)
    return TestClient(panel.app), bridge, log_path


def test_logs_tail_then_incremental(tmp_path):
    client, _, log_path = _log_client(tmp_path, "a\nb\nc\n")
    first = client.get("/api/logs", headers=authed(None)).json()
    assert first["lines"] == ["a", "b", "c"]
    assert first["offset"] == log_path.stat().st_size
    assert first["exists"] is True

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("d\n")
    second = client.get(
        "/api/logs",
        params={"since": first["offset"], "file_id": first["file_id"]},
        headers=authed(None),
    ).json()
    assert second["lines"] == ["d"]
    assert second["rotated"] is False


def test_logs_partial_last_line_is_not_consumed(tmp_path):
    """末行写到一半时不能推进偏移，否则那半行会被吞掉。"""
    client, _, log_path = _log_client(tmp_path, "完整行\n半截")
    data = client.get("/api/logs", headers=authed(None)).json()
    assert data["lines"] == ["完整行"]
    assert data["offset"] == len("完整行\n".encode())

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("行\n")
    data = client.get(
        "/api/logs",
        params={"since": data["offset"], "file_id": data["file_id"]},
        headers=authed(None),
    ).json()
    assert data["lines"] == ["半截行"]


def test_logs_level_filter_keeps_offset_advancing(tmp_path):
    client, _, log_path = _log_client(
        tmp_path,
        "2026-01-01 00:00:00,000 INFO 正常\n"
        "2026-01-01 00:00:01,000 WARNING 注意\n"
        "2026-01-01 00:00:02,000 ERROR 出错\n",
    )
    data = client.get("/api/logs", params={"level": "WARNING"}, headers=authed(None)).json()
    assert len(data["lines"]) == 2
    assert "WARNING" in data["lines"][0]
    assert "ERROR" in data["lines"][1]
    # 被过滤掉的行也必须计入偏移，否则每轮都要重扫
    assert data["offset"] == log_path.stat().st_size


def test_logs_query_filter(tmp_path):
    client, _, _ = _log_client(tmp_path, "alpha\nbeta target\ngamma\n")
    data = client.get("/api/logs", params={"q": "target"}, headers=authed(None)).json()
    assert data["lines"] == ["beta target"]


def test_logs_rotated_offset_resets(tmp_path):
    client, _, log_path = _log_client(tmp_path, "新的文件内容\n")
    data = client.get(
        "/api/logs",
        params={"since": 999999, "file_id": "old:inode"},
        headers=authed(None),
    ).json()
    assert data["rotated"] is True
    assert data["offset"] == log_path.stat().st_size


def test_logs_missing_file(tmp_path):
    client, _, _ = _log_client(tmp_path)
    data = client.get("/api/logs", headers=authed(None)).json()
    assert data["exists"] is False
    assert data["lines"] == []
    assert data["offset"] == 0


def test_logs_multibyte_tail_has_no_garbage(tmp_path):
    """tail_bytes 可能切在汉字中间，首行残片必须丢掉。"""
    lines = "".join(f"第{i}行中文内容\n" for i in range(200))
    client, _, log_path = _log_client(tmp_path, lines)
    data = client.get(
        "/api/logs", params={"tail_bytes": 1024}, headers=authed(None)
    ).json()
    assert data["truncated"] is True
    assert data["lines"], "应至少返回一行"
    for line in data["lines"]:
        assert line.endswith("行中文内容"), f"出现残行：{line!r}"


# ── 状态条 ───────────────────────────────────────────


def test_status_exposes_extended_fields(tmp_path):
    panel, bridge = make_panel(tmp_path)
    bridge.task_manager.submit("10001", "排队任务")
    data = TestClient(panel.app).get("/api/status", headers=authed(None)).json()
    assert data["status"] == "ok"           # 既有字段不变
    assert data["bot"] == "qq-agent-bridge"
    assert data["self_id"] == "10001"
    assert data["tasks_queued"] == 1
    assert data["active_buffers"] == 0
    assert data["scopes"] >= 2


# ── 本机免 token ─────────────────────────────────────


def test_loopback_bypasses_token_when_enabled(tmp_path):
    panel, _ = make_panel(tmp_path, web_local_no_token=True)
    client = TestClient(panel.app, client=("127.0.0.1", 50000))
    assert client.get("/api/status").status_code == 200


def test_loopback_requires_token_when_disabled(tmp_path):
    """默认关闭 —— 安全默认值，不能悄悄放开。"""
    panel, _ = make_panel(tmp_path)
    client = TestClient(panel.app, client=("127.0.0.1", 50000))
    assert client.get("/api/status").status_code == 401


def test_remote_client_always_requires_token(tmp_path):
    panel, _ = make_panel(tmp_path, web_local_no_token=True)
    client = TestClient(panel.app, client=("203.0.113.9", 50000))
    assert client.get("/api/status").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/api/status", headers=headers).status_code == 200


# ── 上下文管理 ───────────────────────────────────────


def test_contexts_lists_group_members_and_clears(tmp_path):
    panel, bridge = make_panel(tmp_path)
    bridge.state_db.touch_group_member("429949232", "10001", "小明")
    bridge.store.set_persona_thread("g:429949232:10001", "thread-g-1")
    bridge.store.increment_persona_turns("g:429949232:10001")
    bridge.store.set_persona_thread("10001", "thread-p-1")
    client = TestClient(panel.app)

    data = client.get("/api/contexts", headers=authed(None)).json()
    group = next(item for item in data["groups"] if item["group_id"] == "429949232")
    member = next(
        item for item in group["members"] if item["key"] == "g:429949232:10001"
    )
    assert member["has_context"] is True
    assert member["turns"] == 1
    assert member["nickname"] == "小明"
    assert any(item["key"] == "10001" for item in data["private"])

    r = client.post(
        "/api/contexts/clear", json={"key": member["key"]}, headers=authed(None)
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["cleared"] is True
    # 只重置会话指针，成员登记仍然保留
    assert bridge.store.get_persona_thread(member["key"]) is None
    assert bridge.state_db.get_group_member("429949232", "10001") is not None


def test_context_clear_unknown_key_is_rejected(tmp_path):
    panel, _ = make_panel(tmp_path)
    r = TestClient(panel.app).post(
        "/api/contexts/clear", json={"key": "g:1:99999"}, headers=authed(None)
    )
    assert r.status_code == 403


def test_context_messages_unknown_key_is_rejected(tmp_path):
    panel, _ = make_panel(tmp_path)
    r = TestClient(panel.app).get(
        "/api/contexts/messages?key=evil", headers=authed(None)
    )
    assert r.status_code == 403


def test_clear_group_clears_every_member_and_legacy_key(tmp_path):
    panel, bridge = make_panel(tmp_path)
    for user_id in ("10001", "10002"):
        bridge.state_db.touch_group_member("g1", user_id)
        bridge.store.set_persona_thread(f"g:g1:{user_id}", f"t-{user_id}")
    # 旧版整群共享键也应能一起清掉
    bridge.store.set_persona_thread("g:g1", "t-legacy")
    r = TestClient(panel.app).post(
        "/api/contexts/clear_group", json={"group_id": "g1"}, headers=authed(None)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cleared"] == 3
    for key in ("g:g1:10001", "g:g1:10002", "g:g1"):
        assert bridge.store.get_persona_thread(key) is None


def test_context_messages_reads_agno_history(tmp_path):
    import json
    import sqlite3

    panel, bridge = make_panel(tmp_path)
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE agno_runs"
        " (session_id TEXT, run_index INTEGER, created_at REAL, run_data TEXT)"
    )
    run_data = {
        "messages": [
            {"role": "system", "content": "系统提示词"},
            {
                "role": "user",
                "content": "对话风格……\n用户消息：\n你好\n\n【群聊场景】这是群聊",
            },
            {"role": "assistant", "content": "回复一"},
        ]
    }
    conn.execute(
        "INSERT INTO agno_runs VALUES (?, ?, ?, ?)",
        ("thread-1", 0, 1.0, json.dumps(run_data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    bridge.cfg["memory_db"] = str(db_path)
    bridge.store.set_persona_thread("10001", "thread-1")

    data = TestClient(panel.app).get(
        "/api/contexts/messages?key=10001", headers=authed(None)
    ).json()
    assert data["has_context"] is True
    assert data["messages"] == [
        {"role": "user", "content": "你好", "ts": 1.0},
        {"role": "assistant", "content": "回复一", "ts": 1.0},
    ]


def test_loopback_still_accepts_correct_token(tmp_path):
    panel, _ = make_panel(tmp_path, web_local_no_token=False)
    client = TestClient(panel.app, client=("127.0.0.1", 50000))
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/api/status", headers=headers).status_code == 200
