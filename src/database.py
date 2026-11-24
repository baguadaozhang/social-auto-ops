"""SQLite 数据层：任务队列、操作日志、账号状态、人工待办。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# action → account_state 表的配额列名，全项目唯一一份定义
ACTION_COLUMN = {"post": "posts", "comment": "comments", "follow": "follows"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    action      TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    run_at      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    platform    TEXT,
    action      TEXT,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_state (
    platform              TEXT PRIMARY KEY,
    date                  TEXT NOT NULL,
    posts                 INTEGER NOT NULL DEFAULT 0,
    comments              INTEGER NOT NULL DEFAULT 0,
    follows               INTEGER NOT NULL DEFAULT 0,
    last_active_at        TEXT,
    status                TEXT NOT NULL DEFAULT 'healthy',
    circuit_break_until   TEXT
);

CREATE TABLE IF NOT EXISTS human_todos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    platform     TEXT,
    action       TEXT,
    detail       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    date        TEXT NOT NULL,
    planned_at  TEXT NOT NULL,
    actions     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'planned',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, run_at);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
CREATE INDEX IF NOT EXISTS idx_todos_status ON human_todos(status);
CREATE INDEX IF NOT EXISTS idx_sessions_plan ON sessions(platform, date, status);
"""


class Database:
    """轻量 SQLite 封装。所有方法自检出连接，线程内使用。"""

    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        self.prune_logs()

    def _connect(self) -> sqlite3.Connection:
        # WAL + busy_timeout：daemon 常驻读写与手动 CLI 并发时不至于撞锁失败
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def session(self):
        """自动提交/回滚并保证关闭连接，避免 sqlite 连接泄漏。"""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 任务队列 ----------
    def add_task(
        self,
        platform: str,
        action: str,
        payload: dict | None = None,
        run_at: str | None = None,
        status: str = "pending",
    ) -> int:
        ts = now_iso()
        with self.session() as conn:
            cur = conn.execute(
                """INSERT INTO tasks
                   (platform, action, payload, status, run_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    platform, action,
                    json.dumps(payload or {}, ensure_ascii=False),
                    status, run_at, ts, ts,
                ),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def pending_tasks(self, limit: int = 50) -> list[dict]:
        with self.session() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status = 'pending'
                     AND (run_at IS NULL OR run_at <= ?)
                   ORDER BY id LIMIT ?""",
                (now_iso(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id: int, status: str, error: str | None = None) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, now_iso(), task_id),
            )

    def defer_task(self, task_id: int, run_at: str, reason: str = "") -> None:
        """把任务推迟到指定时间，保持 pending 但暂不被捞取。"""
        with self.session() as conn:
            conn.execute(
                """UPDATE tasks
                   SET status = 'pending', run_at = ?, error = ?, updated_at = ?
                   WHERE id = ?""",
                (run_at, reason, now_iso(), task_id),
            )

    # ---------- 操作日志 ----------
    def log(
        self,
        platform: str,
        action: str,
        status: str,
        detail: dict | None = None,
        task_id: int | None = None,
    ) -> int:
        with self.session() as conn:
            cur = conn.execute(
                """INSERT INTO logs (task_id, platform, action, status, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_id, platform, action, status,
                    json.dumps(detail or {}, ensure_ascii=False), now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def recent_comment_texts(self, days: int = 7) -> set[str]:
        """取最近 N 天成功发过的评论文案，用于去重。

        uncertain（编辑框已清空但未确认渲染）也计入：它大概率已发出，
        宁可少发一条，也不能对同一帖子重发。
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.session() as conn:
            rows = conn.execute(
                """SELECT detail FROM logs
                   WHERE action = 'comment' AND status IN ('success', 'uncertain')
                     AND created_at >= ?""",
                (cutoff,),
            ).fetchall()
        texts: set[str] = set()
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (ValueError, TypeError):
                continue
            # 同时记录最终文本和原句：加了变体/错字后也要能识别"这句用过"
            for key in ("text", "base_text"):
                value = (detail.get(key) or "").strip()
                if value:
                    texts.add(value)
        return texts

    def recent_logs(self, limit: int = 20) -> list[dict]:
        with self.session() as conn:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_logs(self, days: int = 30) -> int:
        """清理 N 天前的操作日志，避免长期运行后日志表无限膨胀。"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        try:
            with self.session() as conn:
                cur = conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,))
                return cur.rowcount
        except Exception:
            return 0

    # ---------- 账号状态与配额 ----------
    def ensure_account(self, platform: str) -> dict:
        today = date.today().isoformat()
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM account_state WHERE platform = ?", (platform,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO account_state (platform, date) VALUES (?, ?)",
                    (platform, today),
                )
                row = conn.execute(
                    "SELECT * FROM account_state WHERE platform = ?", (platform,)
                ).fetchone()
            elif row["date"] != today:
                conn.execute(
                    """UPDATE account_state
                       SET date = ?, posts = 0, comments = 0, follows = 0
                       WHERE platform = ?""",
                    (today, platform),
                )
                row = conn.execute(
                    "SELECT * FROM account_state WHERE platform = ?", (platform,)
                ).fetchone()

            # 熔断冷却期已过则自动恢复健康状态，避免状态一直卡在 limited
            until = row["circuit_break_until"]
            if row["status"] == "limited" and until:
                try:
                    if datetime.now() >= datetime.fromisoformat(until):
                        conn.execute(
                            """UPDATE account_state
                               SET status = 'healthy', circuit_break_until = NULL
                               WHERE platform = ?""",
                            (platform,),
                        )
                        row = conn.execute(
                            "SELECT * FROM account_state WHERE platform = ?", (platform,)
                        ).fetchone()
                except ValueError:
                    pass
        return dict(row)

    def get_quota(self, platform: str) -> dict[str, int]:
        state = self.ensure_account(platform)
        return {
            "posts": int(state["posts"]),
            "comments": int(state["comments"]),
            "follows": int(state["follows"]),
        }

    def increment_quota(self, platform: str, action: str, amount: int = 1) -> None:
        column = ACTION_COLUMN.get(action)
        if column is None:
            raise ValueError(f"未知动作: {action}")
        self.ensure_account(platform)
        with self.session() as conn:
            conn.execute(
                f"""UPDATE account_state
                    SET {column} = {column} + ?, last_active_at = ?
                    WHERE platform = ?""",
                (amount, now_iso(), platform),
            )

    def set_account_status(
        self, platform: str, status: str, circuit_break_until: str | None = None
    ) -> None:
        self.ensure_account(platform)
        with self.session() as conn:
            conn.execute(
                """UPDATE account_state
                   SET status = ?, circuit_break_until = ? WHERE platform = ?""",
                (status, circuit_break_until, platform),
            )

    def account_status(self, platform: str) -> dict:
        return self.ensure_account(platform)

    # ---------- 元数据 ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.session() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.session() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _has_platform_activity(self, platform: str) -> bool:
        """该平台是否已有真实操作痕迹（发帖/评论/关注，或已有日志）。

        用于 started_days_ago 的迁移判断：老库只有全局 started_at 时，
        只有「真正跑过」的平台才继承它；全新平台（如后接入的 facebook）
        必须从自己的第 0 天开始冷启动，而不是照抄别的平台的历史。
        """
        with self.session() as conn:
            row = conn.execute(
                "SELECT 1 FROM account_state WHERE platform = ? "
                "AND last_active_at IS NOT NULL LIMIT 1",
                (platform,),
            ).fetchone()
            if row is not None:
                return True
            row = conn.execute(
                "SELECT 1 FROM logs WHERE platform = ? LIMIT 1", (platform,)
            ).fetchone()
            return row is not None

    def started_days_ago(self, platform: str | None = None) -> int:
        """距离首次运行已经多少天，用于判断是否还在冷启动期。

        分平台记录（started_at.{platform}），后接入的平台有自己的冷启动期；
        旧的全局 started_at 只迁移给「有操作痕迹」的平台，否则按新平台冷启动。
        """
        key = f"started_at.{platform}" if platform else "started_at"
        raw = self.get_meta(key)
        if not raw and platform:
            # 迁移：旧版本只有一个全局 started_at。只有真正跑过的平台沿用，
            # 全新平台不能继承别人的历史（否则会跳过自己的冷启动期）。
            legacy = self.get_meta("started_at")
            if legacy and self._has_platform_activity(platform):
                raw = legacy
                self.set_meta(key, legacy)
        if not raw:
            self.set_meta(key, now_iso())
            return 0
        try:
            start = datetime.fromisoformat(raw)
        except ValueError:
            # 数据损坏时保守处理：视为已过冷启动期。
            # 绝不能重置为今天——那会让账号行为量突然骤降，反而显眼。
            return 10 ** 6
        return max(0, (datetime.now() - start).days)

    # ---------- 每日会话日程 ----------
    def add_session(
        self, platform: str, planned_at: str, actions: dict, date_str: str
    ) -> int:
        with self.session() as conn:
            cur = conn.execute(
                """INSERT INTO sessions
                   (platform, date, planned_at, actions, status, created_at)
                   VALUES (?, ?, ?, ?, 'planned', ?)""",
                (
                    platform, date_str, planned_at,
                    json.dumps(actions, ensure_ascii=False), now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def sessions_for_date(self, platform: str, date_str: str) -> list[dict]:
        with self.session() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE platform = ? AND date = ?
                   ORDER BY planned_at""",
                (platform, date_str),
            ).fetchall()
        return [dict(r) for r in rows]

    def next_planned_session(self, platform: str, after_iso: str) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE platform = ? AND status = 'planned' AND planned_at >= ?
                   ORDER BY planned_at LIMIT 1""",
                (platform, after_iso),
            ).fetchone()
        return dict(row) if row else None

    def due_sessions(
        self, platform: str, now_iso_str: str, deadline_iso: str
    ) -> list[dict]:
        """到点且还没超过容忍窗口的会话。"""
        with self.session() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE platform = ? AND status = 'planned'
                     AND planned_at <= ? AND planned_at >= ?
                   ORDER BY planned_at""",
                (platform, now_iso_str, deadline_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_missed_sessions(self, platform: str, threshold_iso: str) -> int:
        """超过容忍窗口还没执行的，标记为 missed（真人错过就错过，不补做）。

        顺带回收"僵尸"时段：进程在执行中途被杀（断电/强杀/重启）后，
        session 会永远卡在 running，不会被任何查询看到，这里一并归为 failed。
        """
        with self.session() as conn:
            cur = conn.execute(
                """UPDATE sessions SET status = 'missed'
                   WHERE platform = ? AND status = 'planned' AND planned_at < ?""",
                (platform, threshold_iso),
            )
            missed = cur.rowcount
            cur2 = conn.execute(
                """UPDATE sessions SET status = 'failed'
                   WHERE platform = ? AND status = 'running' AND planned_at < ?""",
                (platform, threshold_iso),
            )
            return missed + cur2.rowcount

    def mark_session(self, session_id: int, status: str) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
            )

    def log_plan_skip(self, platform: str, date_str: str) -> None:
        """记录"今天不上"的决定，避免每次 tick 都重新规划。"""
        self.log(platform, "plan_skip", "success", {"date": date_str})

    def has_plan_skip(self, platform: str, date_str: str) -> bool:
        with self.session() as conn:
            row = conn.execute(
                """SELECT 1 FROM logs
                   WHERE platform = ? AND action = 'plan_skip' AND detail LIKE ?
                   LIMIT 1""",
                (platform, f'%"{date_str}"%'),
            ).fetchone()
        return row is not None

    def log_plan_empty(self, platform: str, date_str: str) -> None:
        """记录"当天排不出时段"，与主动空窗(plan_skip)区分，避免每个 tick 重 roll。"""
        self.log(platform, "plan_empty", "success", {"date": date_str})

    def has_plan_empty(self, platform: str, date_str: str) -> bool:
        with self.session() as conn:
            row = conn.execute(
                """SELECT 1 FROM logs
                   WHERE platform = ? AND action = 'plan_empty' AND detail LIKE ?
                   LIMIT 1""",
                (platform, f'%"{date_str}"%'),
            ).fetchone()
        return row is not None

    def next_session_any(self, platform: str) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE platform = ? AND status = 'planned'
                   ORDER BY planned_at LIMIT 1""",
                (platform,),
            ).fetchone()
        return dict(row) if row else None

    # ---------- 人工待办 ----------
    def add_human_todo(
        self,
        platform: str,
        action: str,
        detail: dict | None = None,
        task_id: int | None = None,
    ) -> int:
        with self.session() as conn:
            cur = conn.execute(
                """INSERT INTO human_todos
                   (task_id, platform, action, detail, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (
                    task_id, platform, action,
                    json.dumps(detail or {}, ensure_ascii=False), now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def pending_todos(self) -> list[dict]:
        with self.session() as conn:
            rows = conn.execute(
                "SELECT * FROM human_todos WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_todo(self, todo_id: int, status: str = "done") -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE human_todos SET status = ?, completed_at = ? WHERE id = ?",
                (status, now_iso(), todo_id),
            )

    def pending_todo_count(self, platform: str, action: str) -> int:
        with self.session() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM human_todos
                   WHERE platform = ? AND action = ? AND status = 'pending'""",
                (platform, action),
            ).fetchone()
        return int(row["n"]) if row else 0
