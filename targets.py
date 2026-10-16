"""会话寻址：把「私聊 / 群聊」编码进统一的字符串键。

私聊键就是 QQ 号本身，与历史数据（sessions.json、state.db）里的键一字不差；
群聊键带 `g:` 前缀。QQ 号与群号都是十进制数字，数值空间可能重叠，
但加前缀之后 `"10001"` 与 `"g:10001"` 是两个不同的键，
因而各自对应独立的会话线程、锁与活动记录，不会互相命中。

这样既有函数只需把入参当会话键解释，无需改签名即可同时支持两种场景。
"""

from __future__ import annotations

from typing import Any

GROUP_PREFIX = "g:"


def private_key(user_id: Any) -> str:
    """私聊会话键：保持裸 QQ 号，兼容既有数据。"""
    return str(user_id)


def group_key(group_id: Any) -> str:
    """群聊会话键：加前缀，避免与同号 QQ 用户相互覆盖。"""
    return f"{GROUP_PREFIX}{group_id}"


def is_group(key: Any) -> bool:
    return str(key).startswith(GROUP_PREFIX)


def scene_of(key: Any) -> str:
    """返回 private / group。"""
    return "group" if is_group(key) else "private"


def parse_key(key: Any) -> tuple[str, str]:
    """拆成 (场景, 原始 ID)。"""
    text = str(key)
    if text.startswith(GROUP_PREFIX):
        return "group", text[len(GROUP_PREFIX) :]
    return "private", text


def to_id(value: Any) -> int | str:
    """转成 OneBot 参数里的 ID：能当数字就当数字，否则原样返回。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
