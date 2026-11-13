"""轨迹日志助手的行为契约。"""

import logging

from trace_log import TRACE_TAGS, trace


def test_trace_prefixes_tag_and_formats_args(caplog):
    """标签写在正文最前面，参数按 %-格式展开。"""
    with caplog.at_level(logging.INFO):
        trace("recv", "群 %s 收到：%s", "123456", "你好")
    assert "[recv] 群 123456 收到：你好" in caplog.text


def test_trace_respects_level(caplog):
    """level 参数生效，WARNING 不应被当成 INFO 过滤掉。"""
    with caplog.at_level(logging.WARNING):
        trace("limit", "触发限流：%s", "群 1", level=logging.WARNING)
    assert "[limit] 触发限流：群 1" in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


def test_trace_records_caller_module(caplog):
    """模块名归属调用方，而不是 trace_log 自身，网页端按模块着色才有意义。"""
    with caplog.at_level(logging.INFO):
        trace("model", "调用结束")
    assert caplog.records[0].module == "test_trace_log"


def test_trace_tags_are_documented():
    """标签表供网页端着色使用，至少覆盖一次消息的主链路。"""
    for tag in ("recv", "route", "media", "prompt", "model", "reply", "send", "limit"):
        assert tag in TRACE_TAGS


def test_encode_body_keeps_line_intact(caplog):
    """多行正文编码后不产生真实换行，日志仍然一行一条。"""
    import json as _json

    from trace_log import encode_body

    body = "第一行\n第二行\n\n第四行"
    encoded = encode_body(body)
    assert "\n" not in encoded
    assert encoded.startswith(" {{BODY:")
    raw = encoded[len(" {{BODY:") : -2]
    assert _json.loads(raw) == body


def test_trace_with_body_appends_marker(caplog):
    """trace(..., body=) 把正文挂在行尾，正文里的换行被转义。"""
    import json as _json

    with caplog.at_level(logging.INFO):
        trace("send", "已发送 %d 字", 5, body="a\nb")
    line = caplog.text.strip().splitlines()[-1]
    assert "[send] 已发送 5 字 {{BODY:" in line
    assert "\n" not in line
    payload = _json.loads(line[line.index("{{BODY:") + len("{{BODY:") : -2])
    assert payload == "a\nb"


def test_trace_without_body_has_no_marker(caplog):
    with caplog.at_level(logging.INFO):
        trace("recv", "收到")
    assert "{{BODY:" not in caplog.text
