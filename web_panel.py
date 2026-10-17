"""统一 Web 工作台：聊天 + 日志 + 提醒/待办/任务管理 + 配置查看。

Web 和 QQ 共用同一套命令与处理管线：`POST /api/chat` 走 `bridge.process_web_message`，
因此 QQ 能用的命令在 Web 里也能用。

**作用域（scope）**：一个字符串，同时是 `state_db` 的 `user_id` 与 `SessionStore` 的会话键。
请求里传来的 scope 必须命中服务端认可的白名单，否则 403 —— 不能凭一个字符串就读写
任意 QQ 的数据。变更类接口进一步把归属条件下推进 SQL（`WHERE id = ? AND user_id = ?`），
做成原子单语句，既防止改到别的 scope，也避免「先查再改」的竞态。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from calendar_todos import normalize_todo_text
from scheduler import add_reminder_from_text

_BEARER = HTTPBearer(auto_error=False)

# 日志行形如：2026-10-16 20:25:40,844 INFO 配置：...
_LEVEL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d{3}\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b"
)
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_KIND_ORDER = {"qq": 0, "group": 1, "web": 2, "other": 9}

DEFAULT_TAIL_BYTES = 64 * 1024
MAX_TAIL_BYTES = 1024 * 1024
MAX_LOG_LINES = 5000


class ChatIn(BaseModel):
    text: str
    session_id: str = "main"


class ReminderIn(BaseModel):
    text: str
    scope: str = ""
    user_id: str = ""  # 兼容旧字段，见 _owner 说明


class TodoIn(BaseModel):
    text: str
    scope: str = ""
    user_id: str = ""


class TaskIn(BaseModel):
    text: str
    scope: str = ""
    user_id: str = ""


class SearchIn(BaseModel):
    query: str
    limit: int = 5


class IdScopeIn(BaseModel):
    id: int
    scope: str = ""
    user_id: str = ""


def _kind_of(key: str) -> str:
    text = str(key)
    if text.startswith("web-"):
        return "web"
    if text.startswith("g:"):
        return "group"
    if text.isdigit():
        return "qq"
    return "other"


def _label_of(key: str) -> str:
    text = str(key)
    kind = _kind_of(text)
    if kind == "web":
        return f"Web · {text[4:]}"
    if kind == "group":
        return f"群 {text[2:]}"
    if kind == "qq":
        return f"QQ {text}"
    return text


def _line_level(line: str, fallback: int) -> int:
    """从日志行里取级别；取不到就沿用上一行的级别。

    traceback 这类续行没有级别前缀，若不给它继承上一行，按 WARNING 过滤会把堆栈撕碎。
    """
    match = _LEVEL_RE.match(line)
    if match:
        return _LEVEL_ORDER.get(match.group(1), fallback)
    head = line[:64].upper()
    for name in ("CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if name in head:
            return _LEVEL_ORDER["WARNING" if name == "WARN" else name]
    return fallback


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class WebPanel:
    def __init__(self, bridge: Any):
        self.bridge = bridge
        self.token = str(bridge.cfg.get("web_token") or "dev-token-change-me")
        self.app = FastAPI(title="QQ Bridge Workbench")
        self._scope_cache: set[str] | None = None
        self._scope_cache_ts = 0.0
        self._rebuild_lock = asyncio.Lock()
        self._register_routes()

    def _require_auth(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),  # noqa: B008
    ) -> None:
        if credentials is not None and credentials.credentials == self.token:
            return
        if self._local_request_allowed(request):
            return
        raise HTTPException(status_code=401, detail="Unauthorized")

    def _local_request_allowed(self, request: Request) -> bool:
        """来自本机回环的请求可免填 token。

        只在 `web_local_no_token` 显式打开时生效。绑定 127.0.0.1 时这意味着
        同机的浏览器与进程都能直接访问 —— 单用户桌面上的取舍。远程访问仍要 token。
        """
        if not bool(self.bridge.cfg.get("web_local_no_token", False)):
            return False
        client = getattr(request, "client", None)
        host = str(getattr(client, "host", "") or "")
        return host in ("127.0.0.1", "::1", "localhost")

    # ---- 作用域 ----

    def _default_scope(self) -> str:
        owners = self.bridge.cfg.get("full_access_qq") or []
        return str(owners[0]) if owners else "web-main"

    def _scope_list(self) -> list[dict[str, str]]:
        """合并四路来源，顺序固定让前端下拉稳定。

        - `full_access_qq`：主人账号，永远排最前
        - `state_db` 里实际出现过数据的 user_id
        - `SessionStore` 里 `web-` 开头的会话键
        - 兜底恒含 `web-main`
        """
        found: dict[str, dict[str, str]] = {}

        def add(key: Any) -> None:
            text = str(key or "")
            if text and text not in found:
                found[text] = {"key": text, "kind": _kind_of(text), "label": _label_of(text)}

        for owner in self.bridge.cfg.get("full_access_qq") or []:
            add(owner)
        store = getattr(self.bridge, "store", None)
        if store is not None:
            try:
                for key in store.list_keys():
                    if str(key).startswith("web-"):
                        add(key)
            except Exception:
                pass
        state_db = getattr(self.bridge, "state_db", None)
        if state_db is not None:
            try:
                for user_id in state_db.distinct_user_ids():
                    add(user_id)
            except Exception:
                pass
        add("web-main")
        return sorted(
            found.values(), key=lambda item: (_KIND_ORDER.get(item["kind"], 9), item["key"])
        )

    def _allowed_scopes(self) -> set[str]:
        now = time.monotonic()
        if self._scope_cache is not None and now - self._scope_cache_ts < 2.0:
            return self._scope_cache
        keys = {item["key"] for item in self._scope_list()}
        keys.add(self._default_scope())
        keys.add("web-main")
        self._scope_cache, self._scope_cache_ts = keys, now
        return keys

    def _invalidate_scopes(self) -> None:
        self._scope_cache = None

    def _owner(self, scope: str = "") -> str:
        """把请求里的 scope 校验成服务端认可的取值；空 = 默认作用域。

        不再「传什么就返回什么」—— 那是越权读写任意 QQ 数据的入口。
        """
        text = str(scope or "")
        if not text:
            return self._default_scope()
        if text in self._allowed_scopes():
            return text
        raise HTTPException(status_code=403, detail=f"未知作用域：{text}")

    def _resolve(self, scope: str, legacy: str = "") -> str:
        """`scope` 为主，`user_id` 作为废弃别名兜底。"""
        return self._owner(scope or legacy)

    # ---- 日志 ----

    def _log_path(self) -> Path:
        raw = str(self.bridge.cfg.get("log_file") or "bridge.log")
        path = Path(raw)
        if path.is_absolute():
            return path
        base = str(self.bridge.cfg.get("data_dir") or "")
        if base:
            return Path(base) / raw
        root = getattr(self.bridge, "bridge_dir", None) or Path.cwd()
        return Path(root) / "data" / raw

    def _read_log(
        self,
        *,
        since: int,
        tail_bytes: int,
        max_lines: int,
        level: str,
        query: str,
        client_file_id: str,
    ) -> dict[str, Any]:
        """基于 seek 的尾部读取 + 按字节偏移的增量返回。

        三个正确性要点：
        1. offset 永远停在**完整行边界**，末行写到一半就不推进，下一轮重读，不丢不重
        2. 起点 > 0 时丢掉首行 —— 字节截断会切在多字节字符中间，解码后是乱码
        3. 过滤只影响返回，不影响 offset 推进，否则被过滤的行每轮都要重扫
        """
        path = self._log_path()
        result: dict[str, Any] = {
            "lines": [],
            "offset": 0,
            "size": 0,
            "file_id": "",
            "truncated": False,
            "rotated": False,
            "exists": False,
            "path": str(path),
            "level": level,
            "q": query,
        }
        try:
            stat = path.stat()
        except OSError:
            return result
        if not path.is_file():
            return result

        size = int(stat.st_size)
        file_id = f"{getattr(stat, 'st_dev', 0)}:{getattr(stat, 'st_ino', 0)}"
        result.update({"exists": True, "size": size, "file_id": file_id})

        incremental = (
            since > 0 and since <= size and (not client_file_id or client_file_id == file_id)
        )
        rotated = False
        truncated = False
        if incremental:
            start = since
        else:
            rotated = since > 0  # 轮转或文件被截短，旧偏移已失效
            start = max(0, size - tail_bytes)
            truncated = start > 0

        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()

        cut = data.rfind(b"\n")
        if cut < 0:
            # 还没有完整的一行，进度不推进
            result.update(
                {
                    "offset": size if start == 0 else start,
                    "truncated": truncated,
                    "rotated": rotated,
                }
            )
            return result

        consumed = start + cut + 1
        raw_lines = data[:cut].decode("utf-8", errors="replace").split("\n")
        if not incremental and start > 0:
            # 尾部 seek 会落在一行中间，首行是半截残片（还可能切断多字节字符），丢掉。
            # 增量模式的 since 永远停在行边界上，首行是完整的，不能丢。
            raw_lines = raw_lines[1:]

        if len(raw_lines) > max_lines:
            truncated = True
            if incremental and not rotated:
                # 增量模式保留最前面的，并据此回收偏移，保证一行都不丢
                raw_lines = raw_lines[:max_lines]
                consumed = start + sum(len(x.encode("utf-8")) + 1 for x in raw_lines)
            else:
                raw_lines = raw_lines[-max_lines:]

        result.update(
            {
                "lines": self._filter_lines(raw_lines, level, query),
                "offset": consumed,
                "truncated": truncated,
                "rotated": rotated,
            }
        )
        return result

    @staticmethod
    def _filter_lines(lines: list[str], level: str, query: str) -> list[str]:
        threshold = _LEVEL_ORDER.get(str(level).upper(), 0) if level else 0
        needle = str(query or "").lower()
        kept: list[str] = []
        last_level = _LEVEL_ORDER["INFO"]
        for line in lines:
            last_level = _line_level(line, last_level)
            if threshold and last_level < threshold:
                continue
            if needle and needle not in line.lower():
                continue
            kept.append(line)
        return kept

    # ---- 路由 ----

    def _register_routes(self) -> None:
        app = self.app
        bridge = self.bridge

        @app.get("/")
        async def index() -> FileResponse:
            static = Path(__file__).resolve().parent / "web_panel" / "static" / "index.html"
            return FileResponse(str(static))

        @app.get("/api/status", dependencies=[Depends(self._require_auth)])
        async def api_status() -> dict[str, Any]:
            seen = getattr(bridge, "seen_ids", None)
            buffers = getattr(bridge, "buffers", None) or {}
            running = queued = 0
            task_manager = getattr(bridge, "task_manager", None)
            if task_manager is not None:
                try:
                    for task in task_manager.list_tasks(None):
                        if task.get("status") == "running":
                            running += 1
                        elif task.get("status") == "queued":
                            queued += 1
                except Exception:
                    pass
            return {
                "status": "ok",
                "bot": "qq-agent-bridge",
                "ws_connected": getattr(bridge, "ws", None) is not None,
                "owner": self._default_scope(),
                "self_id": str(getattr(bridge, "self_id", "") or ""),
                "seen_ids": len(seen) if seen is not None else 0,
                "active_buffers": len(buffers),
                "rate_limit_scopes": len(getattr(bridge, "group_reply_times", None) or {}),
                "tasks_running": running,
                "tasks_queued": queued,
                "scopes": len(self._scope_list()),
                "server_time": datetime.now().isoformat(timespec="seconds"),
            }

        @app.get("/api/scopes", dependencies=[Depends(self._require_auth)])
        async def api_scopes() -> dict[str, Any]:
            return {"scopes": self._scope_list(), "default": self._default_scope()}

        # ---- 提醒 ----

        @app.get("/api/reminders", dependencies=[Depends(self._require_auth)])
        async def api_reminders(scope: str = "", user_id: str = "") -> dict[str, Any]:
            owner = self._resolve(scope, user_id)
            return {"scope": owner, "reminders": bridge.state_db.list_reminders(owner)}

        @app.post("/api/reminders", dependencies=[Depends(self._require_auth)])
        async def api_reminder_create(body: ReminderIn) -> dict[str, Any]:
            owner = self._resolve(body.scope, body.user_id)
            message = add_reminder_from_text(bridge.state_db, owner, body.text)
            self._invalidate_scopes()
            return {"scope": owner, "message": message}

        @app.post("/api/reminders/cancel", dependencies=[Depends(self._require_auth)])
        async def api_reminder_cancel(body: IdScopeIn) -> dict[str, Any]:
            return self._mutate(
                body, bridge.state_db.cancel_reminder, "cancel_reminder"
            )

        @app.post("/api/reminders/done", dependencies=[Depends(self._require_auth)])
        async def api_reminder_done(body: IdScopeIn) -> dict[str, Any]:
            return self._mutate(
                body, bridge.state_db.mark_reminder_done, "mark_reminder_done"
            )

        # ---- 待办 ----

        @app.get("/api/todos", dependencies=[Depends(self._require_auth)])
        async def api_todos(scope: str = "", user_id: str = "") -> dict[str, Any]:
            owner = self._resolve(scope, user_id)
            return {"scope": owner, "todos": bridge.state_db.list_todos(owner)}

        @app.post("/api/todos", dependencies=[Depends(self._require_auth)])
        async def api_todo_create(body: TodoIn) -> dict[str, Any]:
            owner = self._resolve(body.scope, body.user_id)
            cleaned, due_at = normalize_todo_text(body.text)
            if not cleaned:
                raise HTTPException(status_code=400, detail="待办内容不能为空")
            todo_id = bridge.state_db.add_todo(owner, cleaned, due_at)
            self._invalidate_scopes()
            return {"scope": owner, "id": todo_id, "message": f"已添加待办：{cleaned}"}

        @app.post("/api/todos/complete", dependencies=[Depends(self._require_auth)])
        async def api_todo_complete(body: IdScopeIn) -> dict[str, Any]:
            return self._mutate(body, bridge.state_db.complete_todo, "complete_todo")

        @app.post("/api/todos/delete", dependencies=[Depends(self._require_auth)])
        async def api_todo_delete(body: IdScopeIn) -> dict[str, Any]:
            return self._mutate(body, bridge.state_db.delete_todo, "delete_todo")

        # ---- 任务 ----

        @app.get("/api/tasks", dependencies=[Depends(self._require_auth)])
        async def api_tasks(scope: str = "", user_id: str = "") -> dict[str, Any]:
            owner = self._resolve(scope, user_id)
            return {"scope": owner, "tasks": bridge.task_manager.list_tasks(owner)}

        @app.post("/api/tasks", dependencies=[Depends(self._require_auth)])
        async def api_task_create(body: TaskIn) -> dict[str, Any]:
            owner = self._resolve(body.scope, body.user_id)
            task_id = bridge.task_manager.submit(owner, body.text, kind="task")
            self._invalidate_scopes()
            return {"scope": owner, "id": task_id, "message": f"任务已加入后台队列 #{task_id}"}

        @app.post("/api/tasks/cancel", dependencies=[Depends(self._require_auth)])
        async def api_task_cancel(body: IdScopeIn) -> dict[str, Any]:
            owner = self._resolve(body.scope, body.user_id)
            result = bridge.task_manager.cancel_task(body.id, user_id=owner)
            if not result.get("ok") and result.get("reason") == "not_found":
                raise HTTPException(status_code=404, detail="未找到该任务")
            return {"scope": owner, **result}

        # ---- 知识库 ----

        @app.post("/api/knowledge/search", dependencies=[Depends(self._require_auth)])
        async def api_knowledge_search(body: SearchIn) -> dict[str, Any]:
            try:
                from rag import search_knowledge

                return {"results": search_knowledge(bridge.state_db, body.query, body.limit)}
            except Exception:
                return {"results": [], "notice": "知识库尚未构建，可在下方点「重建知识库」"}

        @app.post("/api/knowledge/rebuild", dependencies=[Depends(self._require_auth)])
        async def api_knowledge_rebuild() -> dict[str, Any]:
            if self._rebuild_lock.locked():
                raise HTTPException(status_code=409, detail="知识库重建正在进行中")
            dirs = list(bridge.cfg.get("rag_dirs", []) or [])
            async with self._rebuild_lock:
                started = time.monotonic()
                try:
                    from rag import RagIndex

                    indexed = await RagIndex(bridge.state_db).rebuild(dirs)
                except Exception as exc:
                    logging.warning("知识库重建失败：%s", exc)
                    return {"ok": False, "reason": "rebuild_failed", "message": str(exc)}
            elapsed = round(time.monotonic() - started, 2)
            logging.info("Web 触发知识库重建：%s 块，%.2f 秒", indexed, elapsed)
            return {
                "ok": True,
                "indexed": indexed,
                "dirs": [str(d) for d in dirs],
                "seconds": elapsed,
            }

        # ---- 日志 ----

        @app.get("/api/logs", dependencies=[Depends(self._require_auth)])
        async def api_logs(
            since: int = 0,
            tail_bytes: int = DEFAULT_TAIL_BYTES,
            max_lines: int = 500,
            level: str = "",
            q: str = "",
            file_id: str = "",
            limit: int = 0,
        ) -> dict[str, Any]:
            return self._read_log(
                since=max(0, since),
                tail_bytes=_clamp(tail_bytes, 1024, MAX_TAIL_BYTES),
                max_lines=_clamp(limit or max_lines, 1, MAX_LOG_LINES),
                level=str(level or "").strip().upper(),
                query=str(q or ""),
                client_file_id=str(file_id or ""),
            )

        # ---- 配置 ----

        @app.get("/api/config", dependencies=[Depends(self._require_auth)])
        async def api_config() -> dict[str, Any]:
            sensitive_keys = ("api_key", "token", "password", "secret")
            return {
                k: v
                for k, v in bridge.cfg.items()
                if not any(s in k.lower() for s in sensitive_keys)
            }

        # ---- 聊天 / 命令 ----

        @app.post("/api/chat", dependencies=[Depends(self._require_auth)])
        async def api_chat(body: ChatIn) -> dict[str, Any]:
            web_user_id = f"web-{body.session_id}"
            replies = await bridge.process_web_message(web_user_id, body.text)
            self._invalidate_scopes()
            return {"scope": web_user_id, "replies": replies}

        # 命令与聊天走同一个入口，让 Web 和 QQ 行为完全一致。
        @app.post("/api/command", dependencies=[Depends(self._require_auth)])
        async def api_command(body: ChatIn) -> dict[str, Any]:
            web_user_id = f"web-{body.session_id}"
            replies = await bridge.process_web_message(web_user_id, body.text)
            self._invalidate_scopes()
            return {"scope": web_user_id, "replies": replies}

    def _mutate(self, body: IdScopeIn, action: Any, name: str) -> dict[str, Any]:
        """变更类接口的统一出口：作用域条件随 SQL 一起下发，原子生效。

        「id 不存在」与「id 属于别的 scope」一律返回同样的 404 —— 不通过响应差异
        泄露他域数据的存在性。
        """
        owner = self._resolve(body.scope, body.user_id)
        if action(body.id, user_id=owner):
            return {"ok": True, "scope": owner, "id": body.id, "reason": name}
        raise HTTPException(status_code=404, detail="未找到该记录（或不属于当前作用域）")


async def start_web_panel(bridge: Any) -> None:
    panel = WebPanel(bridge)
    config = uvicorn.Config(
        panel.app,
        host=str(bridge.cfg.get("web_host", "127.0.0.1")),
        port=int(bridge.cfg.get("web_port", 8080)),
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()
