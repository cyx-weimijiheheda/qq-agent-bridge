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
import json
import logging
import os
import re
import sqlite3
import tempfile
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
from config import DEFAULTS, _env_raw
from scheduler import add_reminder_from_text
from targets import group_key, group_member_key, parse_group_member_key

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


class ContextClearIn(BaseModel):
    key: str


class ContextGroupClearIn(BaseModel):
    group_id: str


class SettingsUpdateIn(BaseModel):
    values: dict[str, Any]


_SECRET_SETTING_PARTS = ("api_key", "token", "password", "secret")
_RESTART_SETTINGS = {
    "onebot_ws_url", "web_enabled", "web_host", "web_port", "web_token",
    "data_dir", "memory_db", "state_db_file", "sessions_file", "log_file",
    "workdir", "timetable_file", "persona_prompt_file", "plugins_dir",
    "plugins_enabled", "keep_proxy", "tts_backend", "tts_local_url", "tts_timeout_sec",
    "voice_cache_dir", "messages_dir", "to_qq_dir", "to_qq_sent_dir",
    "from_qq_log_file", "main_agent_inbox_file", "agent_status_file",
    "legacy_agent_status_file", "pending_file", "outbox_dir", "outbox_sent_dir",
}


def _setting_description_map() -> dict[str, str]:
    """从配置手册读取说明，设置页与全量配置文档共用同一份文案。"""
    path = Path(__file__).resolve().parent / "CONFIG.md"
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].startswith("`") and cells[0].endswith("`"):
                result[cells[0][1:-1]] = cells[2]
    except OSError:
        pass
    return result


def _setting_category(key: str) -> str:
    groups = (
        ("群聊", ("group_",)),
        ("连接与权限", ("onebot_", "self_id", "full_access_", "web_enabled", "web_host", "web_port", "web_token", "web_local")),
        ("模型与推理", ("api_", "model", "chat_", "task_", "search_", "request_timeout", "fallback_")),
        ("目录与文件", ("data_dir", "workdir", "memory_db", "state_db_file", "sessions_file", "log_", "_file", "_dir", "_path")),
        ("人设与提示词", ("persona_", "chat_style_", "media_", "emoticon_", "proactive_topic_", "extra_skill_")),
        ("工具与记忆", ("tools_", "shell_", "file_tools_", "web_search_", "knowledge_", "skills_", "memory_", "github_")),
        ("图片与视觉", ("vision_", "image_search_")),
        ("PDF 与视频", ("pdf_", "video_")),
        ("主动消息", ("proactive_",)),
        ("语音", ("tts_", "voice_", "silk_")),
    )
    for label, prefixes in groups:
        if any(key.startswith(prefix) or key.endswith(prefix) for prefix in prefixes):
            return label
    return "其他参数"


def _setting_is_secret(key: str) -> bool:
    return any(part in key.lower() for part in _SECRET_SETTING_PARTS)


def _setting_is_restart_only(key: str) -> bool:
    return key in _RESTART_SETTINGS or key.endswith(("_file", "_dir", "_path"))


def _validate_setting_value(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} 必须是整数")
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
        if len(value) > 100_000:
            raise ValueError(f"{key} 内容过长")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是 JSON 数组")
        if len(value) > 10_000:
            raise ValueError(f"{key} 数组过长")
    elif isinstance(default, dict) and not isinstance(value, dict):
        raise ValueError(f"{key} 必须是 JSON 对象")
    if key == "group_reply_trigger_level" and not 0 <= value <= 100:
        raise ValueError("group_reply_trigger_level 必须在 0–100 之间")
    if key == "group_rate_limit_per_min" and value < 0:
        raise ValueError("group_rate_limit_per_min 不能小于 0")
    if key == "web_port" and not 1 <= value <= 65535:
        raise ValueError("web_port 必须在 1–65535 之间")
    return value


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
        self._settings_lock = asyncio.Lock()
        self._register_routes()

    def _settings_file_path(self) -> Path:
        raw = str(self.bridge.cfg.get("_config_path") or "").strip()
        if not raw:
            raise RuntimeError("找不到当前配置文件路径")
        return Path(raw).expanduser()

    def _read_saved_settings(self) -> dict[str, Any]:
        path = self._settings_file_path()
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return raw

    def _settings_records(self) -> list[dict[str, Any]]:
        raw = self._read_saved_settings()
        descriptions = _setting_description_map()
        records = []
        for key, default in DEFAULTS.items():
            env_override = _env_raw(key) is not None
            effective = self.bridge.cfg.get(key, default)
            value = effective if env_override else raw.get(key, default)
            source = "环境变量" if env_override else ("配置文件" if key in raw else "内置默认值")
            if key == "persona_prompt" and key not in raw and effective:
                value = effective
                source = "人设文件"
            secret = _setting_is_secret(key)
            records.append({
                "key": key,
                "value": None if secret else value,
                "default": None if secret else default,
                "type": "null" if default is None else type(default).__name__,
                "category": _setting_category(key),
                "description": descriptions.get(key, ""),
                "secret": secret,
                "is_set": bool(value) if secret else True,
                "env_override": env_override,
                "restart_required": _setting_is_restart_only(key),
                "source": source,
            })
        return records

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, mode)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

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

    # ---- 上下文 ----

    def _session_store(self) -> Any:
        return getattr(self.bridge, "store", None)

    def _context_meta(self, key: str) -> dict[str, Any]:
        """会话键 → 线程信息；读不到就当作没有上下文。"""
        store = self._session_store()
        thread = ""
        turns = 0
        started = None
        archived: list[str] = []
        if store is not None:
            try:
                thread = str(store.get_persona_thread(key) or "")
                archived = list(store.get_archived_persona_threads(key))
                if thread:
                    turns = int(store.get_persona_turns(key) or 0)
                    started = store.get_persona_started_ts(key)
            except Exception:
                thread, turns, started = "", 0, None
        return {
            "key": key,
            "thread_id": thread,
            "has_context": bool(thread or archived),
            "archived_count": len(archived),
            "turns": turns,
            "started_ts": started,
        }

    def _context_entries(self) -> dict[str, Any]:
        """按私聊 / 群共享与历史成员线程 / Web 分组列出所有上下文。

        群成员来自 `state_db.group_members`；群级线程使用 `g:<群号>`，旧版成员线程
        `g:<群号>:<QQ号>` 仍列出，便于检查和永久清理迁移前的历史。
        """
        private: list[dict[str, Any]] = []
        web: list[dict[str, Any]] = []
        for item in self._scope_list():
            key = item["key"]
            if item["kind"] == "web":
                entry = self._context_meta(key)
                if entry["has_context"]:
                    entry.update({"label": item["label"], "kind": "web"})
                    web.append(entry)
            elif item["kind"] in ("qq", "other"):
                entry = self._context_meta(key)
                if entry["has_context"]:
                    entry.update({"label": item["label"], "kind": item["kind"]})
                    private.append(entry)

        groups: dict[str, list[dict[str, Any]]] = {}
        state_db = getattr(self.bridge, "state_db", None)
        if state_db is not None:
            try:
                for row in state_db.list_group_context_groups():
                    group_id = str(row.get("group_id") or "")
                    if not group_id:
                        continue
                    entry = self._context_meta(group_key(group_id))
                    entry.update(
                        {
                            "group_id": group_id,
                            "user_id": "",
                            "nickname": "",
                            "label": "群共享上下文",
                            "shared": True,
                            "message_count": int(row.get("message_count") or 0),
                            "last_seen_ts": float(row.get("last_seen_ts") or 0),
                            "has_context": bool(entry["has_context"] or row.get("message_count")),
                        }
                    )
                    groups.setdefault(group_id, []).append(entry)
            except Exception:
                logging.warning("读取群上下文目录失败", exc_info=True)
            try:
                rows = state_db.list_group_members()
            except Exception:
                rows = []
            for row in rows:
                group_id = str(row.get("group_id") or "")
                user_id = str(row.get("user_id") or "")
                if not group_id or not user_id:
                    continue
                key = group_member_key(group_id, user_id)
                entry = self._context_meta(key)
                if not entry["has_context"] and not int(row.get("message_count") or 0):
                    continue
                nickname = str(row.get("nickname") or "")
                entry.update(
                    {
                        "group_id": group_id,
                        "user_id": user_id,
                        "nickname": nickname,
                        "label": f"{user_id}" + (f" · {nickname}" if nickname else ""),
                        "last_seen_ts": float(row.get("last_seen_ts") or 0),
                        "message_count": int(row.get("message_count") or 0),
                    }
                )
                groups.setdefault(group_id, []).append(entry)

        # 会话存储里存在、但成员表没登记的键（旧数据或登记失败）
        store = self._session_store()
        if store is not None:
            try:
                keys = store.list_keys()
            except Exception:
                keys = []
            for key in keys:
                parsed = parse_group_member_key(key)
                if parsed:
                    group_id, user_id = parsed
                    if any(item["key"] == key for item in groups.get(group_id, [])):
                        continue
                    entry = self._context_meta(key)
                    if not entry["has_context"]:
                        continue
                    entry.update(
                        {
                            "group_id": group_id,
                            "user_id": user_id,
                            "nickname": "",
                            "label": user_id,
                            "last_seen_ts": 0.0,
                            "message_count": 0,
                        }
                    )
                    groups.setdefault(group_id, []).append(entry)
                elif str(key).startswith("g:"):
                    group_id = str(key)[2:]
                    if any(item["key"] == key for item in groups.get(group_id, [])):
                        continue
                    entry = self._context_meta(key)
                    if not entry["has_context"]:
                        continue
                    entry.update(
                        {
                            "group_id": group_id,
                            "user_id": "",
                            "nickname": "",
                            "label": "群共享上下文",
                            "last_seen_ts": 0.0,
                            "message_count": 0,
                            "shared": True,
                        }
                    )
                    groups.setdefault(group_id, []).append(entry)

        group_list: list[dict[str, Any]] = []
        for group_id, members in groups.items():
            members.sort(key=lambda item: (-item.get("last_seen_ts", 0), item["key"]))
            group_list.append(
                {
                    "group_id": group_id,
                    "label": f"群 {group_id}",
                    "members": members,
                    "contexts": sum(1 for item in members if item["has_context"]),
                }
            )
        group_list.sort(key=lambda item: item["group_id"])
        private.sort(key=lambda item: item["key"])
        web.sort(key=lambda item: item["key"])
        return {
            "groups": group_list,
            "private": private,
            "web": web,
            "total": len(private) + len(web) + sum(len(g["members"]) for g in group_list),
        }

    def _context_map(self) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        data = self._context_entries()
        for item in data["private"] + data["web"]:
            mapped[item["key"]] = item
        for group in data["groups"]:
            for item in group["members"]:
                mapped[item["key"]] = item
        return mapped

    def _history_path(self) -> Path:
        raw = str(self.bridge.cfg.get("memory_db") or "memory.db")
        path = Path(raw)
        if path.is_absolute():
            return path
        base = str(self.bridge.cfg.get("data_dir") or "")
        if base:
            return Path(base) / path
        return Path(__file__).resolve().parent / path

    @staticmethod
    def _display_user_text(content: str) -> str:
        """会话历史里存的是整段提示词，只取真正的用户消息部分。"""
        text = str(content or "")
        marker = "用户消息："
        index = text.rfind(marker)
        if index >= 0:
            text = text[index + len(marker) :]
        for stop in ("\n\n【群聊场景】", "\n长期记忆", "\n语音规则"):
            cut = text.find(stop)
            if cut >= 0:
                text = text[:cut]
        return text.strip()

    def _read_context_messages(self, thread_id: str, limit: int = 60) -> list[dict[str, Any]]:
        """从 agno 的 SQLite 里读出某个线程的 user/assistant 消息。"""
        if not thread_id:
            return []
        path = self._history_path()
        if not path.exists():
            return []
        rows: list[Any] = []
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT run_index, created_at, run_data FROM agno_runs"
                    " WHERE session_id = ? ORDER BY run_index",
                    (thread_id,),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logging.warning("读取上下文历史失败：%s", exc)
            return []

        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["run_data"] or "{}")
            except (TypeError, ValueError):
                continue
            for msg in data.get("messages") or []:
                role = str(msg.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    content = self._display_user_text(content)
                if not content:
                    continue
                messages.append({"role": role, "content": content, "ts": row["created_at"]})
        # 某些版本会把历史重复写进每个 run，去掉相邻重复
        deduped: list[dict[str, Any]] = []
        for msg in messages:
            if (
                deduped
                and deduped[-1]["role"] == msg["role"]
                and deduped[-1]["content"] == msg["content"]
            ):
                continue
            deduped.append(msg)
        limit = max(1, min(int(limit), 200))
        return deduped[-limit:]

    def _delete_context_history(self, thread_id: str) -> int:
        """永久删除一个 Agno 会话的运行历史及会话行，返回删除的历史 run 数。"""
        if not thread_id:
            return 0
        path = self._history_path()
        if not path.exists():
            return 0
        conn = sqlite3.connect(str(path), timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            targets = ["agno_runs"] if "agno_runs" in tables else []
            targets.extend(
                name
                for name in tables
                if name.startswith("agno_")
                and "session" in name.lower()
                and name not in targets
            )
            deleted_runs = 0
            for table in targets:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                }
                if "session_id" not in columns:
                    continue
                cursor = conn.execute(
                    f"DELETE FROM {quoted} WHERE session_id = ?", (thread_id,)
                )
                if table == "agno_runs":
                    deleted_runs = max(0, int(cursor.rowcount))
            conn.commit()
            return deleted_runs
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                "contexts": len(self._context_map()),
                "server_time": datetime.now().isoformat(timespec="seconds"),
            }

        @app.get("/api/scopes", dependencies=[Depends(self._require_auth)])
        async def api_scopes() -> dict[str, Any]:
            return {"scopes": self._scope_list(), "default": self._default_scope()}

        # ---- 上下文 ----

        @app.get("/api/contexts", dependencies=[Depends(self._require_auth)])
        async def api_contexts() -> dict[str, Any]:
            return self._context_entries()

        @app.get("/api/contexts/messages", dependencies=[Depends(self._require_auth)])
        async def api_context_messages(key: str = "", limit: int = 60) -> dict[str, Any]:
            entry = self._context_map().get(str(key or ""))
            if entry is None:
                raise HTTPException(status_code=403, detail="未知上下文")
            thread_id = str(entry.get("thread_id") or "")
            store = self._session_store()
            thread_ids = ([thread_id] if thread_id else [])
            if store is not None:
                thread_ids.extend(store.get_archived_persona_threads(str(key)))
            messages = []
            for saved_thread in dict.fromkeys(thread_ids):
                messages.extend(self._read_context_messages(saved_thread, 200))
            messages.sort(key=lambda item: str(item.get("ts") or ""))
            messages = messages[-min(max(int(limit), 1), 200):]
            if str(key).startswith("g:") and parse_group_member_key(key) is None:
                # 群原始消息来自 group_context；模型线程只取助手回复，避免重复列出 @ 消息。
                messages = [item for item in messages if item.get("role") == "assistant"]
                group_id = str(key)[2:]
                state_db = getattr(self.bridge, "state_db", None)
                if state_db is not None:
                    try:
                        group_messages = state_db.list_group_context_messages(
                            group_id, limit=min(max(int(limit), 1), 200)
                        )
                        messages.extend(
                            {
                                "role": "user",
                                "content": (
                                    f"{row['nickname'] or row['user_id']}（QQ {row['user_id']}）："
                                    f"{row['text']}"
                                ),
                                "ts": row["created_at"],
                            }
                            for row in group_messages
                            if str(row.get("text") or "").strip()
                        )
                        messages.sort(key=lambda item: str(item.get("ts") or ""))
                        messages = messages[-min(max(int(limit), 1), 200) :]
                    except Exception:
                        logging.warning("读取群消息上下文失败：%s", group_id, exc_info=True)
            return {
                "key": entry["key"],
                "label": entry.get("label") or entry["key"],
                "thread_id": thread_id,
                "has_context": bool(entry.get("has_context")),
                "turns": entry.get("turns", 0),
                "messages": messages,
            }

        @app.post("/api/contexts/clear", dependencies=[Depends(self._require_auth)])
        async def api_context_clear(body: ContextClearIn) -> dict[str, Any]:
            key = str(body.key or "").strip()
            if key not in self._context_map():
                raise HTTPException(status_code=403, detail="未知上下文")
            store = self._session_store()
            if store is None:
                return {
                    "ok": False,
                    "reason": "no_store",
                    "message": "当前没有会话存储，无法清除",
                }
            try:
                thread_id = store.archive_persona_thread(key)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"清除失败：{exc}") from exc
            self._invalidate_scopes()
            return {
                "ok": True,
                "key": key,
                "cleared": bool(thread_id),
                "message": "已清除当前会话；历史已保留，可用“删除”永久删除。" if thread_id else "当前没有活动会话。",
            }

        @app.post("/api/contexts/delete", dependencies=[Depends(self._require_auth)])
        async def api_context_delete(body: ContextClearIn) -> dict[str, Any]:
            key = str(body.key or "").strip()
            if key not in self._context_map():
                raise HTTPException(status_code=403, detail="未知上下文")
            store = self._session_store()
            if store is None:
                raise HTTPException(status_code=503, detail="当前没有会话存储")
            try:
                thread_ids = await asyncio.to_thread(store.delete_persona_context, key)
                deleted_runs = 0
                for thread_id in thread_ids:
                    deleted_runs += await asyncio.to_thread(self._delete_context_history, thread_id)
                deleted_group_messages = 0
                deleted_group_members = 0
                member_scope = parse_group_member_key(key)
                if member_scope:
                    state_db = getattr(self.bridge, "state_db", None)
                    if state_db is not None:
                        result = await asyncio.to_thread(
                            state_db.delete_group_member_context,
                            member_scope[0], member_scope[1],
                        )
                        deleted_group_messages = result["messages"]
                        deleted_group_members = result["members"]
                elif key.startswith("g:"):
                    state_db = getattr(self.bridge, "state_db", None)
                    if state_db is not None:
                        deleted_group_messages = await asyncio.to_thread(state_db.delete_group_context, key[2:])
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
            self._invalidate_scopes()
            return {"ok": True, "message": f"已永久删除 {len(thread_ids)} 个线程的模型历史（{deleted_runs} 条）、{deleted_group_messages} 条群消息和 {deleted_group_members} 条群成员目录记录。"}

        @app.post("/api/contexts/compress", dependencies=[Depends(self._require_auth)])
        async def api_context_compress(body: ContextClearIn) -> dict[str, Any]:
            key = str(body.key or "").strip()
            entry = self._context_map().get(key)
            if entry is None:
                raise HTTPException(status_code=403, detail="未知上下文")
            store = self._session_store()
            if store is None:
                raise HTTPException(status_code=503, detail="当前没有会话存储")
            thread_ids = ([str(store.get_persona_thread(key) or "")] if store.get_persona_thread(key) else [])
            thread_ids.extend(store.get_archived_persona_threads(key))
            old_messages = []
            for thread_id in dict.fromkeys(thread_ids):
                old_messages.extend(self._read_context_messages(thread_id, 200))
            if key.startswith("g:") and parse_group_member_key(key) is None:
                state_db = getattr(self.bridge, "state_db", None)
                if state_db is not None:
                    group_rows = state_db.list_group_context_messages(key[2:], limit=200)
                    old_messages.extend({"role": "user", "content": f"{r.get('nickname') or r.get('user_id')}：{r.get('text')}", "ts": r.get("created_at")} for r in group_rows if str(r.get("text") or "").strip())
            old_messages.sort(key=lambda item: str(item.get("ts") or ""))
            transcript = "\n".join(f"{m['role']}: {m['content']}" for m in old_messages)[-30000:]
            if not transcript:
                raise HTTPException(status_code=400, detail="没有可压缩的历史内容")
            from call_policy import resolve_policy, run_with_policy

            bridge = self.bridge
            summary_thread = None
            seed_thread = None
            try:
                summary, summary_thread = await run_with_policy(
                    bridge,
                    "请把以下聊天上下文压缩成供后续对话使用的摘要。保留人物关系、偏好、约定、进行中的事项和必要事实；过滤寒暄与重复。只输出摘要。\n\n" + transcript,
                    None, resolve_policy(bridge.cfg, "summary"), full_access=False,
                    group=key.startswith("g:"), persona=True,
                )
                if not summary.strip():
                    raise RuntimeError("模型没有生成摘要")
                _, seed_thread = await run_with_policy(
                    bridge,
                    "以下是此前对话的压缩摘要。请将其作为本会话已有上下文，后续自然接续，不要复述摘要：\n\n" + summary.strip(),
                    None, resolve_policy(bridge.cfg, "summary"), full_access=False,
                    group=key.startswith("g:"), persona=True,
                )
                if not seed_thread:
                    raise RuntimeError("无法建立摘要会话")
                store.set_persona_thread(key, seed_thread)
                failures = []
                for old_id in dict.fromkeys(thread_ids):
                    try:
                        await asyncio.to_thread(self._delete_context_history, old_id)
                    except Exception:
                        failures.append(old_id)
                store.set_archived_persona_threads(key, failures)
            except Exception as exc:
                if summary_thread:
                    await asyncio.to_thread(self._delete_context_history, summary_thread)
                if seed_thread:
                    await asyncio.to_thread(self._delete_context_history, seed_thread)
                raise HTTPException(status_code=500, detail=f"压缩失败：{exc}") from exc
            return {"ok": True, "message": "上下文已压缩为摘要，新会话已建立。" + (f"有 {len(failures)} 个旧线程未能清理。" if failures else "")}

        @app.post("/api/contexts/clear_group", dependencies=[Depends(self._require_auth)])
        async def api_context_clear_group(body: ContextGroupClearIn) -> dict[str, Any]:
            group_id = str(body.group_id or "").strip()
            entries = self._context_map()
            keys = [
                key
                for key, item in entries.items()
                if str(item.get("group_id") or "") == group_id
            ]
            if not keys:
                raise HTTPException(status_code=404, detail="没有这个群的上下文")
            store = self._session_store()
            if store is None:
                return {
                    "ok": False,
                    "reason": "no_store",
                    "message": "当前没有会话存储，无法清除",
                }
            cleared = 0
            deleted_runs = 0
            deleted_group_messages = 0
            for key in keys:
                try:
                    thread_ids = await asyncio.to_thread(store.delete_persona_context, key)
                    for thread_id in thread_ids:
                        deleted_runs += await asyncio.to_thread(self._delete_context_history, thread_id)
                    if thread_ids:
                        cleared += 1
                except Exception:
                    logging.warning("清除上下文失败：%s", key, exc_info=True)
            state_db = getattr(self.bridge, "state_db", None)
            if state_db is not None:
                try:
                    deleted_group_messages = await asyncio.to_thread(
                        state_db.delete_group_context, group_id, include_members=True
                    )
                except Exception:
                    logging.warning("删除群消息上下文失败：%s", group_id, exc_info=True)
            self._invalidate_scopes()
            return {
                "ok": True,
                "group_id": group_id,
                "cleared": cleared,
                "total": len(keys),
                "deleted_runs": deleted_runs,
                "deleted_group_messages": deleted_group_messages,
                "message": (
                    f"已清除 {cleared}/{len(keys)} 个上下文，"
                    f"永久删除模型历史 {deleted_runs} 条、群消息 {deleted_group_messages} 条"
                ),
            }

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

        @app.get("/api/settings", dependencies=[Depends(self._require_auth)])
        async def api_settings() -> dict[str, Any]:
            try:
                entries = self._settings_records()
            except Exception as exc:
                logging.warning("读取设置失败：%s", exc)
                raise HTTPException(status_code=500, detail="无法读取配置文件") from exc
            categories = list(dict.fromkeys(item["category"] for item in entries))
            return {"entries": entries, "categories": categories}

        @app.post("/api/settings", dependencies=[Depends(self._require_auth)])
        async def api_settings_update(body: SettingsUpdateIn) -> dict[str, Any]:
            if not body.values:
                raise HTTPException(status_code=400, detail="没有要保存的设置")
            async with self._settings_lock:
                try:
                    updates: dict[str, Any] = {}
                    for key, value in body.values.items():
                        if key not in DEFAULTS:
                            raise ValueError(f"不支持的配置项：{key}")
                        if _env_raw(key) is not None:
                            raise ValueError(f"{key} 由环境变量覆盖，请修改环境变量")
                        updates[key] = _validate_setting_value(key, value)

                    current_file = self._read_saved_settings()
                    updated_file = dict(current_file)
                    updated_file.update(updates)
                    if "api_key" in updates:
                        from agent_client import _load_api_key

                        try:
                            _load_api_key({**bridge.cfg, **updates})
                        except ValueError as exc:
                            raise ValueError("API 密钥不能为空；请填写可用密钥") from exc
                    path = self._settings_file_path()
                    await asyncio.to_thread(self._write_json_atomic, path, updated_file)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except Exception as exc:
                    logging.warning("写入配置失败：%s", exc)
                    raise HTTPException(status_code=500, detail="配置文件写入失败") from exc

                restart_keys = [key for key in updates if _setting_is_restart_only(key)]
                live_updates = {
                    key: value for key, value in updates.items()
                    if key not in restart_keys
                }
                bridge.cfg.update(live_updates)
                if "self_id" in live_updates:
                    bridge.self_id = str(live_updates["self_id"] or "")
                agent = getattr(bridge, "agent", None)
                if agent is not None and live_updates:
                    if {"api_base_url", "api_key", "model", "user_agent"} & live_updates.keys():
                        from agent_client import _load_api_key

                        agent._api_key = _load_api_key(bridge.cfg)
                        agent._base_url = str(
                            bridge.cfg.get("api_base_url")
                            or os.environ.get("OPENAI_BASE_URL")
                            or "https://api.openai.com/v1"
                        ).rstrip("/")
                        agent._default_model = str(
                            bridge.cfg.get("model")
                            or os.environ.get("OPENAI_MODEL")
                            or "gpt-4o-mini"
                        )
                        from agent_client import BROWSER_UA

                        agent._user_agent = str(bridge.cfg.get("user_agent") or BROWSER_UA)
                    if hasattr(agent, "_agents"):
                        agent._agents.clear()
                    if any(key.startswith("memory_") for key in live_updates) and hasattr(agent, "_memory_manager"):
                        agent._memory_manager = None
                if "log_level" in live_updates:
                    logging.getLogger().setLevel(str(live_updates["log_level"]).upper())
                self._invalidate_scopes()
            return {
                "ok": True,
                "saved": list(updates),
                "applied": list(live_updates),
                "restart_required": restart_keys,
                "message": (
                    f"已保存 {len(updates)} 项设置；{len(live_updates)} 项立即生效。"
                    + (f"另有 {len(restart_keys)} 项需要重启后生效。" if restart_keys else "")
                ),
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
