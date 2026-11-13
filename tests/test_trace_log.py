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


def test_trace_body_becomes_multiline(caplog):
    """body 作为后续多行原样写入，首行仍在记录开头。"""
    with caplog.at_level(logging.INFO):
        trace("send", "已发送 %d 字", 5, body="第一行\n第二行")
    lines = caplog.text.strip().splitlines()
    assert lines[-3].endswith("[send] 已发送 5 字")
    assert lines[-2] == "第一行"
    assert lines[-1] == "第二行"


def test_trace_body_keeps_real_newlines(caplog):
    """多行正文在日志里是真实换行，而不是转义字符。"""
    with caplog.at_level(logging.INFO):
        trace("reply", "模型输出", body="a\nb\nc")
    text = caplog.text
    assert "\\n" not in text          # 没有字面反斜杠 n
    assert text.rstrip().endswith("a\nb\nc")


def test_trace_body_strips_trailing_newline(caplog):
    """正文末尾多余空行不写进日志。"""
    with caplog.at_level(logging.INFO):
        trace("reply", "输出", body="内容\n\n")
    assert caplog.text.rstrip().endswith("内容")


def test_trace_without_body_stays_single_line(caplog):
    with caplog.at_level(logging.INFO):
        trace("recv", "收到")
    assert len(caplog.text.strip().splitlines()) == 1
