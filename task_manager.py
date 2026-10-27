from __future__ import annotations

import asyncio
import logging
from typing import Any

from call_policy import resolve_policy, run_with_policy
from state_db import StateDB


class TaskManager:
    """后台任务队列：持久化任务、顺序执行、状态回写、完成推送。"""

    def __init__(self, bridge: Any, db: StateDB):
        self.bridge = bridge
        self.db = db

    def submit(self, user_id: str, prompt: str, kind: str = "task") -> int:
        return self.db.add_task(user_id, kind, prompt, status="queued")

    def list_tasks(self, user_id: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_tasks(user_id)

    def cancel_task(self, task_id: int, user_id: str | None = None) -> dict[str, Any]:
        """取消任务，返回能如实反映结果的详情。

        注意：**正在执行的任务取消不了** —— worker 持有该行，结束时会把状态覆盖回
        done/failed。这里如实报告，而不是假装成功。
        """
        task = self.db.get_task(task_id)
        if task is None or (user_id is not None and str(task["user_id"]) != str(user_id)):
            return {"ok": False, "reason": "not_found", "id": task_id}
        status = str(task["status"])
        if status in ("done", "failed", "cancelled"):
            return {"ok": True, "reason": "already_final", "id": task_id, "status": status}
        if status == "running":
            return {
                "ok": False,
                "reason": "running_not_cancellable",
                "id": task_id,
                "status": status,
                "message": f"任务 #{task_id} 正在执行，无法取消；等它自己跑完吧。",
            }
        self.db.update_task(task_id, status="cancelled", finished_at=_now_iso())
        return {"ok": True, "reason": "cancelled", "id": task_id, "status": "cancelled"}

    def cancel(self, task_id: int) -> bool:
        return bool(self.cancel_task(task_id)["ok"])

    async def process_next(self) -> bool:
        queued = [t for t in self.db.list_tasks() if t["status"] == "queued"]
        if not queued:
            return False
        task = queued[0]
        task_id = int(task["id"])
        self.db.update_task(task_id, status="running")
        prompt = str(task["prompt"])
        user_id = str(task["user_id"])
        try:
            reply, thread_id = await run_with_policy(
                self.bridge,
                prompt,
                None,
                resolve_policy(self.bridge.cfg, "task"),
            )
            self.db.update_task(
                task_id,
                status="done",
                result=reply,
                thread_id=str(thread_id or ""),
                finished_at=_now_iso(),
            )
            await self.bridge._send_private(user_id, f"✅ 任务完成：{reply}")
        except Exception as exc:
            logging.exception("后台任务 %s 执行失败", task_id)
            self.db.update_task(
                task_id,
                status="failed",
                error=str(exc),
                finished_at=_now_iso(),
            )
            await self.bridge._send_private(user_id, f"❌ 任务失败：{exc}")
        return True

    async def worker_loop(self, interval: float = 0.5) -> None:
        while True:
            try:
                if not await self.process_next():
                    await asyncio.sleep(interval)
            except Exception:
                logging.exception("任务 worker 循环出错")
                await asyncio.sleep(interval)


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now().isoformat(timespec="seconds")
