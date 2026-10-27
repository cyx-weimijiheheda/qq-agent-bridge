from __future__ import annotations

from typing import Any

from targets import parse_key, to_id


def build_forward_nodes(
    messages: list[str], sender_name: str = "AI 助手", sender_uin: str = "10000"
) -> list[dict[str, Any]]:
    nodes = []
    for msg in messages:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": sender_name,
                    "uin": sender_uin,
                    "content": [{"type": "text", "data": {"text": msg}}],
                },
            }
        )
    return nodes


def should_forward(segments: list[str], threshold: int = 5) -> bool:
    return len(segments) >= threshold


async def send_forward(
    bridge: Any,
    target: str,
    messages: list[str],
    threshold: int = 5,
    sender_name: str = "AI 助手",
    sender_uin: str = "10000",
) -> bool:
    """合并转发；群聊走 send_group_forward_msg，私聊走 send_private_forward_msg。

    返回 False 表示未发送（未达阈值或接口失败），调用方据此回退为逐条发送。
    """
    if not should_forward(messages, threshold=threshold):
        return False
    try:
        scene, raw_id = parse_key(target)
        nodes = build_forward_nodes(
            messages, sender_name=sender_name, sender_uin=sender_uin
        )
        if scene == "group":
            action = "send_group_forward_msg"
            params: dict[str, Any] = {"group_id": to_id(raw_id), "messages": nodes}
        else:
            action = "send_private_forward_msg"
            params = {"user_id": to_id(raw_id), "messages": nodes}
        await bridge._call_action(action, params, timeout=30.0)
        return True
    except Exception:
        return False
