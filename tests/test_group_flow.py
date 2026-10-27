"""群聊端到端流程：@ 触发、白名单、命令不执行、私聊零回归。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from agent_client import AgentClient
from bridge import Bridge
from config import load_config
from sessions import SessionStore

BOT_QQ = 2083158752
GROUP_ID = 429949232


def _group_event(message, *, message_id=9001, user_id=10001, nickname="小明"):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "self_id": BOT_QQ,
        "group_id": GROUP_ID,
        "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": nickname},
        "message": message,
    }


def _private_event(text, *, message_id=8001, user_id=10001):
    return {
        "post_type": "message",
        "message_type": "private",
        "message_id": message_id,
        "self_id": BOT_QQ,
        "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": "owner"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _mention(text=""):
    segments = [{"type": "at", "data": {"qq": str(BOT_QQ)}}]
    if text:
        segments.append({"type": "text", "data": {"text": f" {text}"}})
    return segments


def run_flow(events, *, expected=1, wait=6.0, inspect=None, **cfg_overrides):
    """喂事件给桥接，收集发往群/私聊的消息参数。

    expected=0 表示「预期一条回复都没有」，此时会等满 wait 秒再断言，
    用于验证不该被处理的消息确实没触发任何发送。
    """

    async def scenario():
        group_msgs: list[dict] = []
        private_msgs: list[dict] = []

        async def ws_handler(ws):
            for event in events:
                await ws.send(json.dumps(event))
            async for raw in ws:
                payload = json.loads(raw)
                action = payload.get("action")
                if action == "send_group_msg":
                    group_msgs.append(payload["params"])
                elif action == "send_private_msg":
                    private_msgs.append(payload["params"])
                if expected and len(group_msgs) + len(private_msgs) >= expected:
                    return

        async with websockets.serve(ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                cfg = {
                    "onebot_ws_url": f"ws://127.0.0.1:{port}",
                    "full_access_qq": ["10001"],
                    "self_id": str(BOT_QQ),
                    "workdir": tmp,
                    "request_timeout": 10,
                    "batch_window": 0.1,
                    "image_wait_window": 0.1,
                    "persona_enabled": True,
                    "ack_delay_sec": 0,
                    "chunk_delay_sec": 0,
                    "persona_chat_delay_sec": 0,
                    "group_enabled": True,
                }
                cfg.update(cfg_overrides)
                cfg_path = Path(tmp) / "config.json"
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                loaded = load_config(cfg_path)
                store = SessionStore(Path(tmp) / "sessions.json")
                bridge = Bridge(loaded, store, AgentClient(loaded))
                task = asyncio.create_task(bridge.connect_loop())
                for _ in range(int(wait / 0.1)):
                    if len(group_msgs) + len(private_msgs) >= expected:
                        break
                    await asyncio.sleep(0.1)
                if inspect is not None:
                    inspect(store, bridge)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        return group_msgs, private_msgs

    return asyncio.run(scenario())


# ---------- @ 触发 ----------


def test_mentioned_group_message_gets_group_reply():
    group, private = run_flow([_group_event(_mention("你好"))])
    assert private == []
    assert len(group) == 1
    assert group[0]["group_id"] == GROUP_ID
    assert "假的 Codex 回复" in group[0]["message"]


def test_group_context_is_per_member():
    """群聊上下文按成员隔离：写入 g:<群号>:<QQ号>，不再碰整群键。"""
    seen: dict = {}

    def inspect(store, bridge):
        seen["keys"] = store.list_keys()
        seen["thread"] = store.get_persona_thread(f"g:{GROUP_ID}:10001")
        # 整群键可能因活动记录而存在，但绝不能持有会话线程
        seen["group_thread"] = store.get_persona_thread(f"g:{GROUP_ID}")
        seen["member"] = bridge.state_db.get_group_member(str(GROUP_ID), "10001")

    run_flow([_group_event(_mention("你好"))], inspect=inspect)
    assert seen["thread"]
    assert f"g:{GROUP_ID}:10001" in seen["keys"]
    assert seen["group_thread"] is None
    assert seen["member"] is not None
    assert seen["member"]["nickname"] == "小明"
    assert seen["member"]["message_count"] >= 1


def test_group_message_without_mention_is_ignored():
    group, private = run_flow([_group_event([{"type": "text", "data": {"text": "你好"}}])], expected=0, wait=1.5)
    assert group == []
    assert private == []


def test_at_all_does_not_trigger():
    group, _ = run_flow(
        [
            _group_event(
                [
                    {"type": "at", "data": {"qq": "all"}},
                    {"type": "text", "data": {"text": " 大家早"}},
                ]
            )
        ],
        expected=0,
        wait=1.5,
    )
    assert group == []


# ---------- 开关与白名单 ----------


def test_group_disabled_ignores_everything():
    group, private = run_flow(
        [_group_event(_mention("你好"))], expected=0, wait=1.5, group_enabled=False
    )
    assert group == []
    assert private == []


def test_group_whitelist_blocks_other_groups():
    group, _ = run_flow(
        [_group_event(_mention("你好"))],
        expected=0,
        wait=1.5,
        group_enabled=True,
        group_whitelist=["999"],
    )
    assert group == []


def test_group_whitelist_allows_listed_group():
    group, _ = run_flow(
        [_group_event(_mention("你好"))],
        group_enabled=True,
        group_whitelist=[str(GROUP_ID)],
    )
    assert len(group) == 1


# ---------- 命令不执行 ----------


def test_group_command_returns_hint_and_does_not_execute():
    group, _ = run_flow(
        [_group_event(_mention("/提醒 喝水 明天9点"))],
        group_enabled=True,
        group_command_hint="群里暂不支持命令，可以私聊我",
    )
    assert len(group) == 1
    assert group[0]["message"] == "群里暂不支持命令，可以私聊我"


def test_group_command_hint_empty_is_silent():
    group, _ = run_flow(
        [_group_event(_mention("/提醒 喝水 明天9点"))],
        expected=0,
        wait=1.5,
        group_enabled=True,
        group_command_hint="",
    )
    assert group == []


# ---------- 限流 ----------


class _RateFake:
    """只提供 _group_rate_ok 需要的属性。"""

    def __init__(self, limit):
        self.cfg = {"group_rate_limit_per_min": limit}
        self.group_reply_times: dict = {}


def test_group_rate_limit_blocks_excess_replies():
    fake = _RateFake(2)
    assert Bridge._group_rate_ok(fake, "g:1") is True
    assert Bridge._group_rate_ok(fake, "g:1") is True
    assert Bridge._group_rate_ok(fake, "g:1") is False
    # 不同群各自独立计数
    assert Bridge._group_rate_ok(fake, "g:2") is True


def test_group_rate_limit_zero_means_unlimited():
    fake = _RateFake(0)
    assert all(Bridge._group_rate_ok(fake, "g:1") for _ in range(50))


# ---------- 私聊零回归 ----------


def test_private_chat_still_works_with_group_enabled():
    group, private = run_flow(
        [_private_event("你好")], expected=1, group_enabled=True
    )
    assert group == []
    assert len(private) == 1
    assert private[0]["user_id"] == 10001


def test_group_and_private_coexist():
    group, private = run_flow(
        [
            _group_event(_mention("群里好"), message_id=6001),
            _private_event("私聊好", message_id=6002),
        ],
        expected=2,
        group_enabled=True,
    )
    assert len(group) == 1
    assert len(private) == 1
