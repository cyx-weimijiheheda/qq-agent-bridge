import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent_client import AgentClient, AgentError


class FakeRunOutput:
    def __init__(self, content: str = "回复", session_id: str = "s-1"):
        self.content = content
        self.session_id = session_id

    def get_content_as_string(self) -> str:
        return str(self.content)


class FakeAgent:
    def __init__(
        self,
        delay: float = 0.0,
        error: Exception | None = None,
        content: str = "回复",
    ):
        self.delay = delay
        self.error = error
        self.content = content
        self.calls: list[dict] = []
        self.model = SimpleNamespace(extra_headers=None)

    async def arun(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return FakeRunOutput(content=self.content, session_id="s-1")


def make_client(tmp_path, monkeypatch, **cfg_overrides):
    cfg = {
        "api_key": "test-key",
        "api_base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-flash",
        "workdir": str(tmp_path),
        "memory_db": str(tmp_path / "agno_test.db"),
        "request_timeout": 10,
    }
    cfg.update(cfg_overrides)
    client = AgentClient(cfg)
    return client


def test_agent_client_is_agno_alias():
    assert AgentClient is AgentClient
    assert AgentError is AgentError


def test_agno_error_fields():
    err = AgentError("超时", timeout=True, thread_id="t-1")
    assert err.timeout is True
    assert err.thread_id == "t-1"


def test_agent_client_run_returns_content_and_session(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent()
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    text, thread_id = asyncio.run(client.run("你好"))
    assert text == "回复"
    assert thread_id == "s-1"
    assert fake_agent.calls[0]["prompt"] == "你好"
    # 会话头应带上 x-opencode-session
    assert "x-opencode-session" in fake_agent.model.extra_headers


def test_agent_client_generates_session_id_when_missing(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent()
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    _, thread_id = asyncio.run(client.run("你好"))
    assert thread_id == "s-1"  # FakeAgent 固定返回 s-1
    assert fake_agent.calls[0]["session_id"].startswith("session-")


def test_agent_client_reuses_given_session_id(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent()
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    _, thread_id = asyncio.run(client.run("你好", thread_id="my-session"))
    assert fake_agent.calls[0]["session_id"] == "my-session"
    assert thread_id == "s-1"  # FakeAgent 固定返回 s-1


def test_agent_client_passes_images(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent()
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    asyncio.run(client.run("看图", image_paths=["/tmp/a.png"]))
    images = fake_agent.calls[0]["images"]
    assert len(images) == 1
    assert images[0].filepath == "/tmp/a.png"


def test_agent_client_timeout_raises_with_flag_and_thread_id(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent(delay=1.0)
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    with pytest.raises(AgentError) as exc_info:
        asyncio.run(client.run("你好", thread_id="t-1", timeout=0.05))
    assert exc_info.value.timeout is True
    assert exc_info.value.thread_id == "t-1"


def test_agent_client_wraps_agent_errors(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent(error=RuntimeError("model exploded"))
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    with pytest.raises(AgentError) as exc_info:
        asyncio.run(client.run("你好", thread_id="t-1"))
    assert "model exploded" in str(exc_info.value)
    assert exc_info.value.thread_id == "t-1"
    assert exc_info.value.timeout is False


def test_agent_client_raises_when_empty_reply(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent(content="   ")
    monkeypatch.setattr(client, "_get_agent", lambda *a, **kw: fake_agent)
    with pytest.raises(AgentError):
        asyncio.run(client.run("你好", thread_id="t-1"))


def test_agent_client_close(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    assert asyncio.run(client.close()) is None


def test_agent_client_build_grok_search_tool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool = client._build_grok_search_tool()
    assert tool is not None
    assert tool.name == "web_search"
    assert "image_search" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["query"]


def test_agent_client_build_cn_search_tool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool = client._build_cn_search_tool()
    assert tool is not None
    assert tool.name == "cn_web_search"
    assert tool.parameters["required"] == ["query"]


def test_agent_client_build_grok_image_tool(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    tool = client._build_grok_image_tool()
    assert tool is not None
    assert tool.name == "image_search"
    assert tool.parameters["required"] == ["query"]


# ---------- 工具权限分档 ----------


def _tool_names(client, full_access):
    return [type(tool).__name__ for tool in client._build_tools(full_access=full_access)]


def test_build_tools_restricted_drops_shell_and_file(tmp_path, monkeypatch):
    """受限档必须不含 shell 与文件工具 —— 这是群里限制非白名单成员的唯一手段。"""
    client = make_client(tmp_path, monkeypatch)
    try:
        from agno.tools.file import FileTools  # noqa: F401
        from agno.tools.shell import ShellTools  # noqa: F401

        importable = True
    except Exception:
        importable = False

    full = _tool_names(client, True)
    restricted = _tool_names(client, False)

    assert "ShellTools" not in restricted
    assert "FileTools" not in restricted
    assert set(restricted) <= set(full)
    if importable:
        assert "ShellTools" in full
        assert "FileTools" in full


def test_get_agent_caches_per_tool_profile(tmp_path, monkeypatch):
    """缓存键必须区分工具档位，否则受限请求会拿到完整工具的那个实例。"""
    client = make_client(tmp_path, monkeypatch)
    full = client._get_agent("m", None, 10.0, full_access=True, group=False)
    restricted = client._get_agent("m", None, 10.0, full_access=False, group=False)
    assert full is not restricted
    assert client._get_agent("m", None, 10.0, full_access=True, group=False) is full


def test_get_agent_group_disables_memory_by_default(tmp_path, monkeypatch):
    """群聊默认不碰长期记忆，避免群内容污染主人记忆库。"""
    client = make_client(tmp_path, monkeypatch, memory_enabled=True)
    calls = {"n": 0}

    def fake_memory_manager():
        calls["n"] += 1
        return object()

    monkeypatch.setattr(client, "_get_memory_manager", fake_memory_manager)

    client._get_agent("m", None, 10.0, full_access=True, group=True)
    assert calls["n"] == 0

    client._get_agent("m", None, 10.0, full_access=True, group=False)
    assert calls["n"] == 1


def test_get_agent_group_memory_can_be_enabled(tmp_path, monkeypatch):
    client = make_client(
        tmp_path, monkeypatch, memory_enabled=True, group_memory_enabled=True
    )
    calls = {"n": 0}

    def fake_memory_manager():
        calls["n"] += 1
        return object()

    monkeypatch.setattr(client, "_get_memory_manager", fake_memory_manager)
    client._get_agent("m", None, 10.0, full_access=True, group=True)
    assert calls["n"] == 1


def test_run_passes_tool_profile_down(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    fake_agent = FakeAgent()
    captured = {}

    def fake_get_agent(model, effort, timeout, *, full_access=True, group=False):
        captured["model"] = model
        captured["full_access"] = full_access
        captured["group"] = group
        return fake_agent

    monkeypatch.setattr(client, "_get_agent", fake_get_agent)
    asyncio.run(client.run("你好", "t-1", full_access=False, group=True))
    assert captured["full_access"] is False
    assert captured["group"] is True


def test_run_defaults_to_full_access_private(tmp_path, monkeypatch):
    """私聊调用点不传档位时必须落到完整工具 + 私聊场景。"""
    client = make_client(tmp_path, monkeypatch)
    captured = {}

    def fake_get_agent(model, effort, timeout, *, full_access=True, group=False):
        captured["full_access"] = full_access
        captured["group"] = group
        return FakeAgent()

    monkeypatch.setattr(client, "_get_agent", fake_get_agent)
    asyncio.run(client.run("你好", "t-1"))
    assert captured["full_access"] is True
    assert captured["group"] is False