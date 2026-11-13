"""带轨迹标签的日志助手。

日志行统一在正文最前面加一个 `[tag]`，网页端按 tag 上色，把一条消息的
完整链路串起来：

    [recv]    收到消息
    [route]   走向判定（私聊 / 群聊 / 忽略）
    [media]   图片、视频、文件、表情的收集结果
    [prompt]  提示词组装（长度、注入项）
    [model]   模型调用开始与结束（含耗时、模型名）
    [tool]    工具调用
    [reply]   模型输出（长度、片段数）
    [send]    实际发送（目标、场景、长度）
    [limit]   限流、去重、白名单等拦截
    [task]    任务与阶段
    [web]     网页面板
    [cfg]     配置加载
    [probe]   主动消息与自检

使用 `trace()` 而不是裸 `logging.info()`，是为了让网页能把同一条消息的
链路自动归组；不带标签的老日志仍然正常显示，只是没有 tag 着色。
"""

from __future__ import annotations

import logging

TRACE_TAGS = (
    "recv",
    "route",
    "media",
    "prompt",
    "model",
    "tool",
    "reply",
    "send",
    "limit",
    "task",
    "web",
    "cfg",
    "probe",
)


def trace(tag: str, message: str, *args: object, level: int = logging.INFO) -> None:
    """写一条带轨迹标签的日志。

    tag 建议取 TRACE_TAGS 里的值；未知 tag 也能用，网页端会退化成默认颜色。
    `*args` 与 logging 的 %-格式化一致，不要在 message 里预先拼字符串，
    这样未命中任何处理器的场景下也不会白白格式化。
    """
    logging.getLogger().log(level, "[%s] " + message, tag, *args, stacklevel=2)
