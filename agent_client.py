"""模型核心：OpenAI 兼容接口 + 工具调用 + 会话/长期记忆。

设计目标：
- 所有模型调用走统一的 Agent 抽象（任意 OpenAI 兼容接口）。
- 自主工具：按配置启用 Shell / File / DuckDuckGo 工具，工具被限制在工作目录内。
- 长期记忆：用 SQLite 持久化会话（session_id），并可选启用自动记忆沉淀
  自动沉淀跨会话长期记忆，替代/桥接旧的 sessions.json 线程轮换。
- 保持与旧调用方兼容：`run(prompt, thread_id, ...) -> (reply, new_thread_id)`，
  `AgentError` 与旧的 `AgentError` 字段一致（message / timeout / thread_id）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.media import Image
from agno.memory import MemoryManager
from agno.models.openai import OpenAIChat

from message_utils import persona_system_prompt
from trace_log import estimate_tokens
from trace_log import trace as _trace
from vision_client import BROWSER_UA

# 所有外联都按“直连优先”设计。部分 HTTP 客户端会读取系统代理，
# 常驻代理（例如失效的 127.0.0.1:7890）一旦存在，会让模型请求/记忆更新报
# Connection error / ProxyError。这里在创建任何 HTTP 客户端前统一清掉代理变量，
# 避免默认客户端或 deepcopy 出来的客户端重新捡起代理。
# 若网络环境必须走代理，可设置 QQBOT_KEEP_PROXY=1（或 config 中 keep_proxy=true）。
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _purge_proxy_env(keep_proxy: bool = False) -> None:
    if keep_proxy:
        return
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    # 同时声明 NO_PROXY，覆盖那些不认 trust_env=False 的库/子进程。
    os.environ["NO_PROXY"] = "*"


class AgentError(Exception):
    def __init__(self, message: str, timeout: bool = False, thread_id: str | None = None):
        super().__init__(message)
        self.timeout = timeout
        self.thread_id = thread_id


def _normalize_chat_path(path: str | None) -> str:
    """把配置里的对话路径规整成以 / 开头的纯路径。

    只接受路径部分；误填了完整 URL 时取它的 path，避免拼出双域名。
    """
    raw = str(path or "").strip()
    if not raw:
        return "/chat/completions"
    if "://" in raw:
        from urllib.parse import urlsplit

        raw = urlsplit(raw).path or "/chat/completions"
    if not raw.startswith("/"):
        raw = "/" + raw
    return re.sub(r"/{2,}", "/", raw).rstrip("/") or "/chat/completions"


class _PathRewriteClient(httpx.AsyncClient):
    """把 OpenAI SDK 写死的 /chat/completions 改写到网关实际使用的路径。

    OpenAI SDK 在 chat.completions.create 里把路径硬编码为 /chat/completions，
    agno 也固定调用那个方法，所以只能在这一层拦。只改这一条路径，其它请求
    （文件上传、/models 等）原样透传。
    """

    def __init__(self, *args: Any, chat_path: str = "/chat/completions", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._chat_path = _normalize_chat_path(chat_path)

    def _rewrite(self, url: Any) -> Any:
        if self._chat_path == "/chat/completions":
            return url
        text = str(url)
        if text.endswith("/chat/completions"):
            return httpx.URL(text[: -len("/chat/completions")] + self._chat_path)
        return url

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        request.url = self._rewrite(request.url)
        return await super().send(request, **kwargs)


TOOL_SAFETY_INSTRUCTIONS = (
    "工具使用铁律：\n"
    "1. 所有文件读写和命令执行都必须限制在工作目录内，"
    "禁止读写工作目录之外的任何路径，禁止读取 ~/.ssh、钥匙串、凭据文件等敏感信息。\n"
    "2. 禁止执行关机、重启、kill、sudo、rm -rf 等危险或系统级命令；"
    "不要擅自安装软件包，不要修改系统配置。\n"
    "3. 需要写文件时优先使用文件工具；命令工具只用于运行、测试或查看工作目录内的内容。\n"
    "4. 任务完成后用 [FILE:绝对路径|文件名] 标记发送文件，不要空口说“已发送”。\n"
    "5. 联网搜索优先用本地知识库；只有确实需要实时网络内容时才调用联网搜索工具；"
    "对方要图/找素材时把返回的 URL 用 [IMAGE:URL] 标记发送。\n"
)


def _load_api_key(cfg: dict[str, Any]) -> str:
    key = str(
        cfg.get("api_key")
        or cfg.get("vision_api_key")
        or cfg.get("mimo_vision_api_key")
        or ""
    ).strip()
    if key:
        return key
    for env_name in ("QQBOT_API_KEY", "OPENAI_API_KEY", "OPENCODE_GO_API_KEY"):
        env_key = os.environ.get(env_name, "").strip()
        if env_key:
            return env_key
    # 可选：从任意 JSON 凭据文件里读 OPENAI_API_KEY（QQBOT_AUTH_FILE）
    auth_file = os.environ.get("QQBOT_AUTH_FILE", "").strip()
    if auth_file:
        try:
            data = json.loads(Path(auth_file).expanduser().read_text(encoding="utf-8"))
            key = str(data.get("OPENAI_API_KEY") or data.get("api_key") or "").strip()
            if key:
                return key
        except Exception:
            pass
    raise ValueError(
        "未配置模型 API key：请设置 config.json 里的 api_key，"
        "或环境变量 QQBOT_API_KEY / OPENAI_API_KEY"
    )


class AgentClient:
    """模型调用客户端：任意 OpenAI 兼容接口 + 工具 + 长期记忆。"""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self._api_key = _load_api_key(cfg)
        # 任意 OpenAI 兼容端点：config.json 的 api_base_url 优先，
        # 其次环境变量 OPENAI_BASE_URL，最后兜底官方地址。
        self._base_url = str(
            cfg.get("api_base_url")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self._default_model = str(
            cfg.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        )
        self._role_map = {
            "system": "system",
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
            "model": "assistant",
        }
        self._user_agent = str(cfg.get("user_agent") or BROWSER_UA)
        self._session_headers_cache: dict[str, str] = {}
        self._bridge_dir = Path(__file__).resolve().parent
        self._workdir = Path(cfg.get("workdir") or self._bridge_dir.parent)
        db_file_value = Path(str(cfg.get("memory_db", "memory.db")))
        if not db_file_value.is_absolute():
            db_file_value = self._bridge_dir / db_file_value
        db_file_value.parent.mkdir(parents=True, exist_ok=True)
        self.db = SqliteDb(db_file=str(db_file_value))
        # 默认清掉 Shell/系统注入的代理变量，确保直连客户端不会被默认实现
        # 的默认客户端或 deepcopy 出来的客户端重新捡起代理。
        # 需要走代理时把 config 里的 keep_proxy 打开（或设 QQBOT_KEEP_PROXY=1）。
        _purge_proxy_env(bool(cfg.get("keep_proxy", False)))
        # api_chat_path 非默认时，用改写客户端把 /chat/completions 换成网关实际路径
        self._chat_path = _normalize_chat_path(cfg.get("api_chat_path"))
        self._force_stream = bool(cfg.get("api_force_stream", False))
        self._http_client = _PathRewriteClient(
            trust_env=False, chat_path=self._chat_path
        )
        self._agents: dict[tuple[str, str, float, bool, bool], Agent] = {}
        self._memory_manager: MemoryManager | None = None

    # ---- 会话头：x-opencode-session（每个会话一个稳定 ID） ----

    def _session_headers(self, session_id: str | None = None) -> dict[str, str]:
        """按会话返回稳定的 x-opencode-session 头。

        - 有 thread_id（会话）时：同一会话用同一个 ID；
        - 无 thread_id 时：进程内固定一个 ID，保证同一进程的请求稳定。
        """
        key = str(session_id).strip() if session_id else "_fixed"
        if "opencode" not in self._base_url:
            # 这个头是 opencode-go 的会话归属约定，其它提供商不需要
            return {}
        value = self._session_headers_cache.get(key)
        if value is None:
            value = f"session-{uuid.uuid4().hex}"
            self._session_headers_cache[key] = value
        return {"x-opencode-session": value}

    # ---- 模型 / Agent 构造 ----

    def _make_model(
        self,
        model: str,
        reasoning_effort: str | None,
        timeout: float,
        max_retries: int | None = None,
    ) -> OpenAIChat:
        kwargs: dict[str, Any] = {
            "id": model,
            "api_key": self._api_key,
            "base_url": self._base_url,
            "timeout": max(timeout, 30.0),
            "max_retries": max_retries if max_retries is not None else 1,
            "default_headers": {"User-Agent": self._user_agent},
            "extra_headers": self._session_headers(),
            "role_map": self._role_map,
            "http_client": self._http_client,
            # 记忆管理器会 deepcopy 模型并丢弃 http_client 字段；
            # 把 http_client 也放进 client_params，deepcopy 后仍能复用 trust_env=False 的自定义客户端。
            "client_params": {"http_client": self._http_client},
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        return OpenAIChat(**kwargs)

    def _get_state_db(self) -> Any:
        from state_db import StateDB

        path = Path(str(self.cfg.get("state_db_file", "state.db")))
        if not path.is_absolute():
            path = self._bridge_dir / path
        return StateDB(path)

    def _build_reminder_tools(self, user_id: str, full_access: bool) -> list[Any]:
        """提醒与待办工具。

        此前只有 /提醒 这类命令能创建提醒，命令靠前缀拦截，模型看不到。
        用户用自然语言说「提醒我明天考试」，模型只能口头答应，实际什么都没存。
        这两个工具让模型能真正落库。

        只给可信账号：写库操作不该对群友开放。
        """
        if not full_access or not user_id:
            return []
        try:
            from agno.tools import Function

            db = self._get_state_db()

            def create_reminder(content: str, when: str) -> str:
                from scheduler import add_reminder_from_text

                raw = f"{content} {when}".strip()
                return add_reminder_from_text(db, user_id, raw)

            def list_reminders() -> str:
                rows = db.list_reminders(user_id)
                if not rows:
                    return "（暂无提醒）"
                lines = []
                for row in rows:
                    repeat = f"（{row['repeat']}）" if row["repeat"] != "none" else ""
                    lines.append(
                        f"#{row['id']} {row['text']} @ {row['remind_at']}{repeat} [{row['status']}]"
                    )
                return "\n".join(lines)

            def cancel_reminder(reminder_id: int) -> str:
                ok = db.cancel_reminder(reminder_id, user_id=user_id)
                return f"已取消提醒 #{reminder_id}" if ok else f"没找到提醒 #{reminder_id}"

            def create_todo(content: str, due: str = "") -> str:
                todo_id = db.add_todo(user_id, content, due)
                suffix = f"，截止 {due}" if due else ""
                return f"已记下待办 #{todo_id}：{content}{suffix}"

            def list_todos() -> str:
                rows = db.list_todos(user_id)
                if not rows:
                    return "（暂无待办）"
                return "\n".join(
                    f"#{row['id']} {row['text']} [{'已完成' if row['done'] else '进行中'}]"
                    for row in rows
                )

            def complete_todo(todo_id: int) -> str:
                ok = db.complete_todo(todo_id, user_id=user_id)
                return f"待办 #{todo_id} 已完成" if ok else f"没找到待办 #{todo_id}"

            return [
                Function(
                    name="create_reminder",
                    description=(
                        "创建定时提醒，到点会自动发消息。用户说「提醒我……」时用它。"
                        "when 必须写相对或绝对时间，如「明天13点20分」「明天9点」「每天8点」。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "提醒内容，如「数学分析考试」"},
                            "when": {"type": "string", "description": "提醒时间，如「明天13点20分」"},
                        },
                        "required": ["content", "when"],
                    },
                    entrypoint=create_reminder,
                ),
                Function(
                    name="list_reminders",
                    description="列出当前所有提醒。用户问「我有哪些提醒」时用它。",
                    parameters={"type": "object", "properties": {}, "required": []},
                    entrypoint=list_reminders,
                ),
                Function(
                    name="cancel_reminder",
                    description="按编号取消提醒。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "reminder_id": {"type": "integer", "description": "提醒编号"}
                        },
                        "required": ["reminder_id"],
                    },
                    entrypoint=cancel_reminder,
                ),
                Function(
                    name="create_todo",
                    description="记一条待办。用户说「记一下要做什么」时用它。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "待办内容"},
                            "due": {"type": "string", "description": "截止时间，可留空"},
                        },
                        "required": ["content"],
                    },
                    entrypoint=create_todo,
                ),
                Function(
                    name="list_todos",
                    description="列出当前所有待办。",
                    parameters={"type": "object", "properties": {}, "required": []},
                    entrypoint=list_todos,
                ),
                Function(
                    name="complete_todo",
                    description="把某条待办标记为已完成。",
                    parameters={
                        "type": "object",
                        "properties": {"todo_id": {"type": "integer", "description": "待办编号"}},
                        "required": ["todo_id"],
                    },
                    entrypoint=complete_todo,
                ),
            ]
        except Exception as exc:
            logging.warning("提醒/待办工具初始化失败：%s", exc)
            return []

    def _build_knowledge_tool(self) -> Any | None:
        if not self.cfg.get("knowledge_tool_enabled", True):
            return None
        try:
            from agno.tools import Function

            db = self._get_state_db()

            def search_knowledge(query: str) -> str:
                from rag import search_knowledge as _search

                return _search(db, query)

            return Function(
                name="search_knowledge",
                description="搜索本地知识库（已索引的文档），返回相关片段；命令形如：/知识库 搜 关键词。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要搜索的关键词或短语",
                        }
                    },
                    "required": ["query"],
                },
                entrypoint=search_knowledge,
            )
        except Exception as exc:
            logging.warning("知识库工具初始化失败：%s", exc)
            return None

    def _build_grok_search_tool(self) -> Any | None:
        if not self.cfg.get("web_search_enabled", True):
            return None
        try:
            from agno.tools import Function

            from trending import format_trending_context, run_search_from_cfg

            def web_search(query: str, image_search: bool = False) -> str:
                try:
                    data = run_search_from_cfg(
                        self.cfg, query, image_search=bool(image_search)
                    )
                    return format_trending_context(data, include_images=bool(image_search))
                except Exception as exc:
                    return f"搜索失败：{exc}"

            return Function(
                name="web_search",
                description=(
                    "搜索实时网页、热搜、热梗和梗图，返回带来源的文本；"
                    "当需要找图/梗图时把 image_search 设为 true。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或问题",
                        },
                        "image_search": {
                            "type": "boolean",
                            "description": "是否同时搜索图片/梗图 URL，默认 false",
                        },
                    },
                    "required": ["query"],
                },
                entrypoint=web_search,
            )
        except Exception as exc:
            logging.warning("grok 搜索工具初始化失败：%s", exc)
            return None

    def _build_grok_image_tool(self) -> Any | None:
        if not self.cfg.get("web_search_enabled", True):
            return None
        try:
            from agno.tools import Function

            from image_utils import download_images
            from trending import format_trending_context, run_search_from_cfg

            def image_search(query: str) -> str:
                try:
                    data = run_search_from_cfg(self.cfg, query, image_search=True)
                    image_urls = data.get("image_urls") or []
                    dest_dir = Path(self.cfg.get("workdir", Path(__file__).parent.parent)) / "tmp" / "grok_images"
                    local_images = download_images(image_urls, dest_dir, 3, 20)
                    if not local_images:
                        from cn_search import search_cn_images

                        domestic_urls = search_cn_images(query, 15)
                        local_images = download_images(domestic_urls, dest_dir, 3, 20)
                    if local_images:
                        return (
                            "已下载可发送的图片（用 [IMAGE:本地路径] 发送）：\n"
                            + "\n".join(f"- {path}" for path in local_images)
                        )
                    return format_trending_context(data, include_images=True)
                except Exception as exc:
                    return f"图片搜索失败：{exc}"

            return Function(
                name="image_search",
                description=(
                    "搜索并返回图片/梗图/画作/表情包 URL；当对方要图、找素材、找梗图时使用。"
                    "返回的 URL 可以直接用 [IMAGE:URL] 标记发送。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要搜索的图片/画作/梗图关键词",
                        }
                    },
                    "required": ["query"],
                },
                entrypoint=image_search,
            )
        except Exception as exc:
            logging.warning("grok 图片搜索工具初始化失败：%s", exc)
            return None

    def _build_cn_search_tool(self) -> Any | None:
        if not self.cfg.get("web_search_enabled", True):
            return None
        try:
            from agno.tools import Function

            from cn_search import search_domestic

            def cn_web_search(query: str) -> str:
                try:
                    return search_domestic(query)
                except Exception as exc:
                    return f"国内直连搜索失败：{exc}"

            return Function(
                name="cn_web_search",
                description=(
                    "国内直连中文搜索（Bing 中国，无需代理/外网），适合中文资料、"
                    "热梗、新闻、产品口碑等国内内容。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "中文搜索关键词",
                        }
                    },
                    "required": ["query"],
                },
                entrypoint=cn_web_search,
            )
        except Exception as exc:
            logging.warning("国内搜索工具初始化失败：%s", exc)
            return None

    def _build_skill_tools(self) -> list[Any]:
        if not self.cfg.get("skills_tool_enabled", True):
            return []
        try:
            from skills_registry import load_skills

            return load_skills(self.cfg)
        except Exception as exc:
            logging.warning("技能工具加载失败：%s", exc)
            return []

    def _build_tools(self, full_access: bool = True, user_id: str = "") -> list[Any]:
        """组装工具集。

        full_access=False 时**不装配** ShellTools 与 FileTools —— 模型实例里
        根本没有这些工具，调用必然失败。这是群聊里限制非白名单成员唯一可靠的
        拦截方式。搜索 / 知识库 / 技能属于只读信息查询，两档都保留。
        """
        tools: list[Any] = []
        if self.cfg.get("tools_enabled", True):
            if full_access and self.cfg.get("shell_enabled", True):
                try:
                    from agno.tools.shell import ShellTools

                    tools.append(ShellTools(base_dir=self._workdir))
                except Exception as exc:
                    logging.warning("ShellTools 初始化失败：%s", exc)
            if full_access and self.cfg.get("file_tools_enabled", True):
                try:
                    from agno.tools.file import FileTools

                    tools.append(
                        FileTools(
                            base_dir=self._workdir,
                            enable_delete_file=False,
                            enable_save_file=True,
                            enable_read_file=True,
                            enable_list_files=True,
                            enable_search_files=True,
                            enable_search_content=True,
                            expose_base_directory=True,
                        )
                    )
                except Exception as exc:
                    logging.warning("FileTools 初始化失败：%s", exc)
            tools.extend(self._build_reminder_tools(user_id, full_access))
            cn_tool = self._build_cn_search_tool()
            if cn_tool is not None:
                tools.append(cn_tool)
            grok_tool = self._build_grok_search_tool()
            if grok_tool is not None:
                tools.append(grok_tool)
            grok_image_tool = self._build_grok_image_tool()
            if grok_image_tool is not None:
                tools.append(grok_image_tool)
            knowledge_tool = self._build_knowledge_tool()
            if knowledge_tool is not None:
                tools.append(knowledge_tool)
            tools.extend(self._build_skill_tools())
        return tools

    def _get_memory_manager(self) -> MemoryManager | None:
        if not self.cfg.get("memory_enabled", True):
            return None
        if self._memory_manager is None:
            memory_model = self._make_model(
                self._default_model,
                str(self.cfg.get("memory_effort", "low")),
                float(self.cfg.get("memory_timeout", 180)),
                max_retries=0,
            )
            self._memory_manager = MemoryManager(model=memory_model, db=self.db)
        return self._memory_manager

    async def _arun_streaming(
        self,
        agent: Agent,
        prompt: str,
        session_id: str,
        images: list[Image] | None,
    ) -> Any:
        """走流式接口再收成完整结果。

        有些网关（中转/代理）只接受 stream=true，非流式直接返回
        streaming_required。这里取最后一个完整 RunOutput，语义与
        agent.arun() 一致，失败信息也保持同样的抛法。
        """
        last_output: Any = None
        async for event in agent.arun(
            prompt,
            session_id=session_id,
            images=images,
            stream=True,
            yield_run_output=True,
        ):
            # yield_run_output=True 时最后会给出完整 RunOutput；其余是增量事件
            if getattr(event, "event", "") == "RunCompleted":
                last_output = event
            elif hasattr(event, "content") and hasattr(event, "session_id"):
                last_output = event
        if last_output is None:
            raise AgentError("模型没有返回结果", thread_id=session_id)
        return last_output

    def _get_agent(
        self,
        model: str,
        reasoning_effort: str | None,
        timeout: float,
        *,
        full_access: bool = True,
        group: bool = False,
        persona: bool = False,
        user_id: str = "",
    ) -> Agent:
        key = (model, reasoning_effort or "", timeout, full_access, group, persona, user_id)
        agent = self._agents.get(key)
        if agent is not None:
            return agent
        # 群聊默认关闭长期记忆：否则群成员的对话会写进主人共享的记忆库，
        # 主人的 memory.md 也会被注入到群里。
        memory_on = bool(self.cfg.get("memory_enabled", True))
        if group and not bool(self.cfg.get("group_memory_enabled", False)):
            memory_on = False
        agent = Agent(
            model=self._make_model(model, reasoning_effort, timeout),
            db=self.db,
            memory_manager=self._get_memory_manager() if memory_on else None,
            enable_agentic_memory=memory_on,
            update_memory_on_run=memory_on
            and bool(self.cfg.get("memory_auto_update", True)),
            add_memories_to_context=memory_on
            and bool(self.cfg.get("memory_in_chat", True)),
            add_history_to_context=True,
            num_history_runs=int(
                self.cfg.get("group_history_turns", 8)
                if group
                else self.cfg.get("history_turns", 20)
            ),
            tools=self._build_tools(full_access, user_id),
            # 人设放 system instructions：agno 会把 user prompt 写进会话历史，
            # 整套人设若每轮都塞进 user prompt，下一轮就会在“对话”里看见提示词。
            instructions=(
                TOOL_SAFETY_INSTRUCTIONS + "\n\n" + persona_system_prompt(self.cfg)
                if persona
                else TOOL_SAFETY_INSTRUCTIONS
            ),
            markdown=False,
        )
        self._agents[key] = agent
        return agent

    # ---- 主调用 ----

    async def run(
        self,
        prompt: str,
        thread_id: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        image_paths: list[str] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        bypass_proxy: bool = False,
        full_access: bool = True,
        group: bool = False,
        persona: bool = False,
        user_id: str = "",
    ) -> tuple[str, str | None]:
        del model_provider  # 服务商由 api_base_url 决定，参数保留仅为接口兼容
        del bypass_proxy  # 内置 HTTP 客户端默认不信任代理环境变量
        model_name = str(model or self._default_model).strip()
        effective_timeout = float(
            timeout if timeout is not None else self.cfg.get("request_timeout", 900)
        )
        effort = str(reasoning_effort or "").strip() or None
        session_id = str(thread_id or "").strip() or f"session-{uuid.uuid4().hex}"
        agent = self._get_agent(
            model_name,
            effort,
            effective_timeout,
            full_access=full_access,
            group=group,
            persona=persona,
            user_id=user_id or "",
        )
        images = [Image(filepath=str(p)) for p in (image_paths or [])]
        # 每个会话一个稳定的 x-opencode-session：同一 thread 的多次请求用同一 ID
        agent.model.extra_headers = self._session_headers(session_id)
        started_at = time.monotonic()
        _trace(
            "model",
            "开始调用 model=%s 流式=%s timeout=%.0fs thread=%s 场景=%s 工具=%s 提示词约 %d tok 图片=%d",
            model_name,
            "是" if self._force_stream else "否",
            effective_timeout,
            session_id,
            "群聊" if group else "私聊",
            "完整" if full_access else "受限",
            estimate_tokens(prompt or ""),
            len(image_paths or []),
        )
        try:
            if self._force_stream:
                run_output = await asyncio.wait_for(
                    self._arun_streaming(
                        agent, prompt, session_id, images or None
                    ),
                    timeout=effective_timeout,
                )
            else:
                run_output = await asyncio.wait_for(
                    agent.arun(
                        prompt,
                        session_id=session_id,
                        images=images or None,
                    ),
                    timeout=effective_timeout,
                )
        except TimeoutError as exc:
            raise AgentError(
                "模型处理超时，已终止任务",
                timeout=True,
                thread_id=session_id,
            ) from exc
        except Exception as exc:
            raise AgentError(f"模型处理失败：{exc}", thread_id=session_id) from exc

        try:
            content = run_output.get_content_as_string() or ""
        except Exception:
            content = str(run_output.content or "")
        content = (content or "").strip()
        # 用网关回报的真实用量，不做字符估算
        metrics = getattr(run_output, "metrics", None)
        in_tok = int(getattr(metrics, "input_tokens", 0) or 0)
        out_tok = int(getattr(metrics, "output_tokens", 0) or 0)
        cache_tok = int(getattr(metrics, "cache_read_tokens", 0) or 0)
        if in_tok or out_tok:
            _trace(
                "model",
                "调用结束 耗时 %.1fs 输入 %d tok 输出 %d tok 缓存命中 %d tok thread=%s",
                time.monotonic() - started_at,
                in_tok,
                out_tok,
                cache_tok,
                session_id,
            )
        else:
            # 网关没回 usage 时才退回字符估算，并在日志里标明是估算
            _trace(
                "model",
                "调用结束 耗时 %.1fs 输出约 %d tok（网关未返回 usage）thread=%s",
                time.monotonic() - started_at,
                estimate_tokens(content),
                session_id,
            )
        if not content:
            raise AgentError("模型没有返回结果", thread_id=session_id)
        new_session_id = str(run_output.session_id or session_id)
        return content, new_session_id

    async def close(self) -> None:
        await self._http_client.aclose()
