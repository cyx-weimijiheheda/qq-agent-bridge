import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import targets


def test_private_key_is_bare_id():
    # 私聊键必须与历史数据一致，不能加任何前缀
    assert targets.private_key("10001") == "10001"
    assert targets.private_key(10001) == "10001"


def test_group_key_has_prefix():
    assert targets.group_key("429949232") == "g:429949232"
    assert targets.group_key(429949232) == "g:429949232"


def test_same_number_in_two_scenes_does_not_collide():
    # QQ 号与群号数值空间可能重叠，加前缀后必须是两个不同的键
    assert targets.private_key(10001) != targets.group_key(10001)


def test_is_group_and_scene_of():
    assert targets.is_group("g:10001") is True
    assert targets.is_group("10001") is False
    assert targets.scene_of("g:10001") == "group"
    assert targets.scene_of("10001") == "private"


def test_parse_key_round_trip():
    for raw in ("10001", "g:429949232"):
        scene, raw_id = targets.parse_key(raw)
        rebuilt = (
            targets.group_key(raw_id) if scene == "group" else targets.private_key(raw_id)
        )
        assert rebuilt == raw


def test_parse_key_splits_scene_and_id():
    assert targets.parse_key("g:429949232") == ("group", "429949232")
    assert targets.parse_key("10001") == ("private", "10001")


def test_parse_key_accepts_non_numeric_private_keys():
    # web 面板的会话键形如 "web-xxx"，应原样视作私聊场景
    assert targets.parse_key("web-main") == ("private", "web-main")


def test_to_id_converts_numeric():
    assert targets.to_id("429949232") == 429949232
    assert targets.to_id(429949232) == 429949232


def test_to_id_keeps_non_numeric():
    assert targets.to_id("web-main") == "web-main"
    assert targets.to_id(None) is None


def test_group_member_key_round_trip():
    key = targets.group_member_key("429949232", "3197142392")
    assert key == "g:429949232:3197142392"
    assert targets.parse_group_member_key(key) == ("429949232", "3197142392")


def test_parse_group_member_key_rejects_group_and_private_keys():
    assert targets.parse_group_member_key("g:429949232") is None
    assert targets.parse_group_member_key("3197142392") is None
    assert targets.parse_group_member_key("g::123") is None
