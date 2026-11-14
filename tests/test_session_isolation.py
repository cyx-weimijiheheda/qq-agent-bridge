"""私聊上下文隔离：不同 QQ 号不能互相串上下文。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from targets import group_key, group_member_key, private_key


def test_private_and_group_keys_do_not_collide():
    """QQ 号与群号数值可能相同，加前缀后必须是两个键。"""
    assert private_key("10001") != group_key("10001")
    assert private_key("10001") == "10001"
    assert group_key("10001") == "g:10001"


def test_group_member_key_is_distinct_per_member():
    a = group_member_key("429949232", "10001")
    b = group_member_key("429949232", "10002")
    assert a != b
    assert a == "g:429949232:10001"


def test_session_store_keeps_threads_apart(tmp_path):
    """两个 QQ 号各自持有独立线程。"""
    from sessions import SessionStore

    store = SessionStore(tmp_path / "sessions.json")
    store.set_persona_thread("3197142392", "session-A")
    store.set_persona_thread("3997107559", "session-B")

    assert store.get_persona_thread("3197142392") == "session-A"
    assert store.get_persona_thread("3997107559") == "session-B"
    assert store.thread_owner("session-A") == "3197142392"
    assert store.thread_owner("session-B") == "3997107559"


def test_shared_thread_is_reset_not_leaked(tmp_path):
    """线程被两个会话键共用时，后来的那个要被重置，避免读到别人的上下文。"""
    from handlers import _ensure_thread_owner
    from sessions import SessionStore

    store = SessionStore(tmp_path / "sessions.json")
    store.set_persona_thread("3197142392", "session-A")
    # 模拟数据被改错：另一个用户也指向同一线程
    store.set_persona_thread("3997107559", "session-A")

    class Bridge:
        pass

    bridge = Bridge()
    bridge.store = store

    assert _ensure_thread_owner(bridge, "3197142392", "persona_thread_id") == "session-A"
    assert _ensure_thread_owner(bridge, "3997107559", "persona_thread_id") is None
    # 原主人不受影响
    assert store.get_persona_thread("3197142392") == "session-A"
