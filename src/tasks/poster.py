"""发帖任务编排：队列消费 + 人工介入分流 + 会话节奏控制。"""
from __future__ import annotations

import json
from typing import Any

from ..browser import BrowserManager
from ..database import Database, now_iso
from ..humanize import Humanizer
from ..platforms.base import BasePlatform
from ..platforms.facebook import FacebookPlatform
from ..platforms.linkedin import LinkedInPlatform
from ..risk_control import (
    AccountCoolingDown,
    QuotaExceeded,
    RiskController,
    TimeWindowClosed,
)


class Poster:
    """发帖任务处理器。"""

    def __init__(
        self,
        browser: BrowserManager,
        humanizer: Humanizer,
        risk: RiskController,
        db: Database,
        config: Any,
    ):
        self.browser = browser
        self.humanizer = humanizer
        self.risk = risk
        self.db = db
        self.config = config
        self._platforms: dict[str, BasePlatform] = {}

    def platform(self, name: str) -> BasePlatform:
        if name not in self._platforms:
            cls = {"linkedin": LinkedInPlatform, "facebook": FacebookPlatform}.get(name)
            if cls is None:
                raise ValueError(f"不支持的平台: {name}")
            self._platforms[name] = cls(
                self.browser, self.humanizer, self.risk, self.db, self.config
            )
        return self._platforms[name]

    def enqueue(
        self,
        platform: str,
        content: str,
        run_at: str | None = None,
        media: list[str] | None = None,
    ) -> int:
        """把发帖内容（可附带图片/视频）加入队列。"""
        if platform not in ("linkedin", "facebook"):
            raise ValueError(f"不支持的平台: {platform}")
        payload: dict[str, Any] = {"content": content}
        if media:
            payload["media"] = [str(m) for m in media if m]
        task_id = self.db.add_task(platform, "post", payload, run_at=run_at)
        self.db.log(platform, "enqueue", "success", {"task_id": task_id}, task_id)
        return task_id

    def _divert_to_human(self, task: dict, reason: str) -> None:
        """转人工：生成待办，任务状态改为 waiting_human。"""
        payload = json.loads(task.get("payload") or "{}")
        todo_id = self.db.add_human_todo(
            platform=task["platform"],
            action=task["action"],
            detail={
                "content": payload.get("content", ""),
                "media": payload.get("media") or [],
                "reason": reason,
                "task_id": task["id"],
            },
            task_id=task["id"],
        )
        self.db.update_task(task["id"], "waiting_human")
        self.db.log(
            task["platform"], "post", "diverted_to_human",
            {"todo_id": todo_id, "reason": reason}, task["id"],
        )

    def run_pending(self, max_tasks: int = 20) -> dict:
        """消费待执行任务队列。"""
        tasks = [t for t in self.db.pending_tasks(limit=max_tasks) if t["action"] == "post"]
        result: dict[str, Any] = {
            "processed": 0, "success": 0, "failed": 0,
            "waiting_human": 0, "deferred": 0, "details": [],
        }
        # 同一平台每次 run 最多发 1 条，避免连发被判定为垃圾信息
        max_per_platform = int(self.config.get("run.max_posts_per_platform_per_run", 1))
        attempted: dict[str, int] = {}

        for index, task in enumerate(tasks):
            payload = json.loads(task.get("payload") or "{}")
            platform_name = task["platform"]
            if attempted.get(platform_name, 0) >= max_per_platform:
                result["deferred"] += 1
                result["details"].append(
                    {"task_id": task["id"], "status": "kept_for_next_run"}
                )
                continue
            # 已人工批准的任务不再重复转人工，否则会形成死循环
            if not payload.get("human_approved"):
                if self.risk.should_human_takeover("post") or self.risk.human_todo_required(
                    task["platform"], "post"
                ):
                    self._divert_to_human(task, "人工介入机制触发")
                    result["waiting_human"] += 1
                    result["details"].append(
                        {"task_id": task["id"], "status": "waiting_human"}
                    )
                    continue

            # 安全前置检查
            try:
                self.risk.assert_not_cooling(task["platform"])
                self.risk.assert_time_window()
                self.risk.check_quota(task["platform"], "post")
            except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
                # 推迟到下一个可用时间点，避免每次 run 都重复捡起同一条
                next_at = self.risk.next_available_time().isoformat(timespec="seconds")
                self.db.defer_task(task["id"], next_at, str(exc))
                result["deferred"] += 1
                result["details"].append(
                    {
                        "task_id": task["id"],
                        "status": "deferred",
                        "resume_at": next_at,
                        "reason": str(exc),
                    }
                )
                continue

            content = payload.get("content", "")
            if not content:
                self.db.update_task(task["id"], "failed", error="内容为空")
                result["failed"] += 1
                continue

            platform = self.platform(task["platform"])
            media = [m for m in (payload.get("media") or []) if m]
            if media and not getattr(platform, "supports_media", False):
                msg = f"{platform_name} 暂不支持图片/视频发布"
                self.db.update_task(task["id"], "failed", error=msg)
                result["failed"] += 1
                result["details"].append(
                    {"task_id": task["id"], "status": "failed", "error": msg}
                )
                continue
            attempted[platform_name] = attempted.get(platform_name, 0) + 1
            outcome = platform.post(content, media=media) if media else platform.post(content)
            self.db.update_task(
                task["id"],
                "done" if outcome.get("success") else "failed",
                error=outcome.get("error"),
            )
            result["processed"] += 1
            if outcome.get("success"):
                result["success"] += 1
            else:
                result["failed"] += 1
            result["details"].append({"task_id": task["id"], **outcome})

            # 只有成功发布后才需要"人一样的间隔"；
            # 失败时立即继续，避免环境问题（如未装浏览器）把 run 挂住几分钟
            if outcome.get("success") and index < len(tasks) - 1:
                self.humanizer.between_tasks()

        return result

    def complete_human_todo(self, todo_id: int, dispatch: bool = False) -> bool:
        """处理人工待办。

        dispatch=False：你自己已经手动发了，任务直接结单（不占机器配额）。
        dispatch=True ：你只是批准，交回机器发布（标记 human_approved，避免再次转人工）。
        """
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM human_todos WHERE id = ?", (todo_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            conn.execute(
                "UPDATE human_todos SET status = 'done', completed_at = ? WHERE id = ?",
                (now_iso(), todo_id),
            )
            if row["task_id"]:
                if dispatch:
                    task_row = conn.execute(
                        "SELECT payload FROM tasks WHERE id = ?", (row["task_id"],)
                    ).fetchone()
                    payload = json.loads(task_row["payload"] or "{}") if task_row else {}
                    payload["human_approved"] = True
                    conn.execute(
                        """UPDATE tasks
                           SET status = 'pending', payload = ?, error = NULL, updated_at = ?
                           WHERE id = ?""",
                        (
                            json.dumps(payload, ensure_ascii=False),
                            now_iso(),
                            row["task_id"],
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE tasks
                           SET status = 'done', error = NULL, updated_at = ?
                           WHERE id = ?""",
                        (now_iso(), row["task_id"]),
                    )
        self.db.log(
            row["platform"],
            "human_todo",
            "approved_for_dispatch" if dispatch else "completed_by_human",
            {"todo_id": todo_id},
            row["task_id"],
        )
        return True
