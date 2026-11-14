"""提醒时间解析：内容与时间要干净分离。"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import next_run_from_text, parse_remind_when


def test_minute_suffix_is_part_of_time():
    """「13点20分」里的「分」属于时间，不该残留在提醒内容里。"""
    when, repeat = parse_remind_when("数学分析考试 明天下午13点20分")
    assert when == "明天下午13点20分"
    assert repeat == "none"
    content = "数学分析考试 明天下午13点20分".replace(when, "").strip()
    assert content == "数学分析考试"


def test_time_without_minute_suffix():
    when, _ = parse_remind_when("喝水 明天9点")
    assert when == "明天9点"


def test_next_run_uses_parsed_time():
    now = datetime(2026, 11, 14, 18, 50)
    when, _ = parse_remind_when("数学分析考试 明天下午13点20分")
    assert next_run_from_text(when, now) == datetime(2026, 11, 15, 13, 20)
