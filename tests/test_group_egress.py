"""出口层场景分派：同一个函数按会话键自动走群聊或私聊接口。"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forward_utils
import media
from bridge import Bridge

GROUP = "g:429949232"
GROUP_ID = 429949232
PRIVATE = "10001"


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class FakeStore:
    def __init__(self):
        self.activity = []

    def mark_activity(self, user_id):
        self.activity.append(user_id)


class FakeBridge:
    """只提供 media / forward_utils / Bridge._send_private 需要的属性。"""

    def __init__(self, action_result=None):
        self.ws = FakeWS()
        self.store = FakeStore()
        self.web_replies = {}
        self.calls = []
        self.action_result = action_result if action_result is not None else {}

    async def _call_action(self, action, params, timeout=60):
        self.calls.append((action, params))
        return self.action_result


def _sent(bridge):
    return [json.loads(payload) for payload in bridge.ws.sent]


# ---------- send_segments ----------


def test_send_segments_to_group_uses_send_group_msg():
    bridge = FakeBridge()
    asyncio.run(
        media.send_segments(
            bridge, GROUP, [{"type": "text", "data": {"text": "在的"}}]
        )
    )
    payload = _sent(bridge)[0]
    assert payload["action"] == "send_group_msg"
    assert payload["params"]["group_id"] == GROUP_ID
    assert "user_id" not in payload["params"]


def test_send_segments_to_private_uses_send_private_msg():
    bridge = FakeBridge()
    asyncio.run(
        media.send_segments(
            bridge, PRIVATE, [{"type": "text", "data": {"text": "在的"}}]
        )
    )
    payload = _sent(bridge)[0]
    assert payload["action"] == "send_private_msg"
    assert payload["params"]["user_id"] == int(PRIVATE)


def test_legacy_name_is_same_function():
    # 旧名保留为别名，外部调用点无需改动
    assert media.send_private_segments is media.send_segments
    assert media.send_private_file is media.send_file
    assert media.send_private_record is media.send_record


def test_face_and_image_inherit_group_target():
    bridge = FakeBridge()
    asyncio.run(media.send_private_face(bridge, GROUP, "178"))
    asyncio.run(media.send_private_image(bridge, GROUP, "http://x/a.png"))
    payloads = _sent(bridge)
    assert [p["action"] for p in payloads] == ["send_group_msg", "send_group_msg"]
    assert payloads[0]["params"]["message"][0]["type"] == "face"
    assert payloads[1]["params"]["message"][0]["type"] == "image"
    assert all(p["params"]["group_id"] == GROUP_ID for p in payloads)


def test_mface_and_video_inherit_group_target():
    bridge = FakeBridge()
    asyncio.run(
        media.send_private_mface(bridge, GROUP, {"emoji_id": "1", "emoji_package_id": "2"})
    )
    asyncio.run(media.send_private_video(bridge, GROUP, "http://x/v.mp4", "v.mp4"))
    assert all(p["action"] == "send_group_msg" for p in _sent(bridge))


# ---------- send_file ----------


def test_send_file_to_group_uses_upload_group_file(tmp_path):
    source = tmp_path / "报告.txt"
    source.write_text("hi", encoding="utf-8")
    bridge = FakeBridge()
    asyncio.run(media.send_file(bridge, GROUP, str(source), "报告.txt"))
    action, params = bridge.calls[0]
    assert action == "upload_group_file"
    assert params["group_id"] == GROUP_ID
    assert params["name"] == "报告.txt"
    assert params["folder"] == "/"


def test_send_file_to_private_uses_upload_private_file(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("hi", encoding="utf-8")
    bridge = FakeBridge()
    asyncio.run(media.send_file(bridge, PRIVATE, str(source), "a.txt"))
    action, params = bridge.calls[0]
    assert action == "upload_private_file"
    assert params["user_id"] == int(PRIVATE)
    assert "folder" not in params


# ---------- send_record ----------


def test_send_record_to_group_uses_send_group_msg():
    bridge = FakeBridge()
    asyncio.run(media.send_record(bridge, GROUP, "/tmp/a.silk"))
    action, params = bridge.calls[0]
    assert action == "send_group_msg"
    assert params["group_id"] == GROUP_ID
    assert params["message"][0]["type"] == "record"


def test_send_record_to_private_uses_send_private_msg():
    bridge = FakeBridge()
    asyncio.run(media.send_record(bridge, PRIVATE, "/tmp/a.silk"))
    action, params = bridge.calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == int(PRIVATE)


# ---------- forward ----------


def test_send_forward_to_group_uses_group_forward():
    bridge = FakeBridge()
    ok = asyncio.run(
        forward_utils.send_forward(bridge, GROUP, ["a", "b", "c"], threshold=3)
    )
    assert ok is True
    action, params = bridge.calls[0]
    assert action == "send_group_forward_msg"
    assert params["group_id"] == GROUP_ID
    assert len(params["messages"]) == 3


def test_send_forward_to_private_uses_private_forward():
    bridge = FakeBridge()
    ok = asyncio.run(
        forward_utils.send_forward(bridge, PRIVATE, ["a", "b", "c"], threshold=3)
    )
    assert ok is True
    action, params = bridge.calls[0]
    assert action == "send_private_forward_msg"
    assert params["user_id"] == int(PRIVATE)


def test_send_forward_below_threshold_does_not_call():
    bridge = FakeBridge()
    assert asyncio.run(
        forward_utils.send_forward(bridge, GROUP, ["a"], threshold=3)
    ) is False
    assert bridge.calls == []


# ---------- 入站文件 ----------


def test_incoming_group_file_uses_get_group_file_url(monkeypatch, tmp_path):
    bridge = FakeBridge({"url": "http://x/f.zip"})
    captured = {}

    async def fake_download_or_copy(b, url, name):
        captured["url"] = url
        return str(tmp_path / "f.zip")

    monkeypatch.setattr(media, "_download_or_copy", fake_download_or_copy)
    asyncio.run(
        media.download_incoming_file(
            bridge, {"file_id": "abc", "name": "f.zip"}, str(GROUP_ID)
        )
    )
    action, params = bridge.calls[0]
    assert action == "get_group_file_url"
    assert params["group_id"] == GROUP_ID
    assert params["file_id"] == "abc"


def test_incoming_private_file_uses_get_private_file_url(monkeypatch, tmp_path):
    bridge = FakeBridge({"url": "http://x/f.zip"})

    async def fake_download_or_copy(b, url, name):
        return str(tmp_path / "f.zip")

    monkeypatch.setattr(media, "_download_or_copy", fake_download_or_copy)
    asyncio.run(media.download_incoming_file(bridge, {"file_id": "abc"}))
    assert bridge.calls[0][0] == "get_private_file_url"


# ---------- Bridge._send_private ----------


def test_bridge_send_text_dispatches_to_group():
    bridge = FakeBridge()
    asyncio.run(Bridge._send_private(bridge, GROUP, "在的"))
    payload = _sent(bridge)[0]
    assert payload["action"] == "send_group_msg"
    assert payload["params"]["group_id"] == GROUP_ID
    assert payload["params"]["message"] == "在的"
    assert bridge.store.activity == [GROUP]


def test_bridge_send_text_dispatches_to_private():
    bridge = FakeBridge()
    asyncio.run(Bridge._send_private(bridge, PRIVATE, "在的"))
    payload = _sent(bridge)[0]
    assert payload["action"] == "send_private_msg"
    assert payload["params"]["user_id"] == int(PRIVATE)


def test_bridge_send_text_keeps_web_reply_shortcut():
    # Web 面板用队列而不是发 QQ，这条分支不能被群聊改造破坏
    class Queue:
        def __init__(self):
            self.items = []

        async def put(self, text):
            self.items.append(text)

    bridge = FakeBridge()
    queue = Queue()
    bridge.web_replies = {"web-main": queue}
    asyncio.run(Bridge._send_private(bridge, "web-main", "你好"))
    assert queue.items == ["你好"]
    assert _sent(bridge) == []
