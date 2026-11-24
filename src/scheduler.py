"""随机日程：模拟真人的不规律作息，而不是固定时间点。

思路：
1. 每天生成一份"作息表"——2-4 个不规则的活跃时段，偶尔整天空窗
2. 每个时段做的事、做多少也是随机的（只看不互动 / 评论 1-2 条 / 顺手发一条）
3. 到点由 tick 执行；错过超过容忍窗口就作废，**不补做**（真人错过就是错过）
"""
from __future__ import annotations

import json
import math
import random
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from .browser import BrowserManager
from .database import Database
from .humanize import Humanizer
from .comment_ramp import CommentRamp
from .risk_control import CircuitBreakerTripped, RiskController
from .tasks.commenter import Commenter
from .tasks.follower import Follower
from .tasks.poster import Poster


def _as_dict(raw: Any) -> dict:
    """数据库里的 actions 是 JSON 字符串，内存里是 dict，统一成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
    return {}


def _lognormal(rng: random.Random, lo: float, hi: float,
               median_ratio: float = 0.45, sigma: float = 0.65) -> float:
    """重尾分布采样，夹紧到 [lo, hi]。"""
    lo, hi = float(lo), float(hi)
    if hi <= lo:
        return lo
    mu = math.log(max(lo + (hi - lo) * median_ratio, 0.01))
    value = lo
    for _ in range(24):
        value = rng.lognormvariate(mu, sigma)
        if lo <= value <= hi:
            return value
    return min(hi, max(lo, value))


def _rand_range(config: Any, key: str, default: tuple[float, float]) -> tuple[float, float]:
    value = config.get(key, default)
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        lo, hi = default
    return (lo, hi) if lo <= hi else (hi, lo)


class DayPlanner:
    """为某一天生成随机的活跃时段（真人作息）。"""

    def __init__(self, config: Any, db: Database, platform: str):
        self.config = config
        self.db = db
        self.platform = platform

    def _windows(self) -> list[tuple[int, int]]:
        """把配置里的 HH:MM-HH:MM 转成分钟区间。"""
        out: list[tuple[int, int]] = []
        for window in self.config.get("time_window.windows", []):
            try:
                start_s, end_s = window.split("-")
                sh, sm = map(int, start_s.strip().split(":"))
                eh, em = map(int, end_s.strip().split(":"))
            except (ValueError, AttributeError):
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            if end - start >= 30:
                out.append((start, end))
        return out or [(9 * 60, 11 * 60), (14 * 60, 17 * 60)]

    def _has_pending_post(self) -> bool:
        return any(
            t["action"] == "post" and t["platform"] == self.platform
            for t in self.db.pending_tasks(limit=50)
        )

    def _in_warmup(self) -> bool:
        """是否还在冷启动期（刚上手时行为要更轻）。按平台各自计算。"""
        warmup_days = int(self.config.get("schedule.warmup_days", 14))
        return self.db.started_days_ago(self.platform) < warmup_days

    def _actions_for_session(self, is_first: bool) -> dict:
        """给一个时段随机安排做什么。"""
        cfg = self.config
        warm = self._in_warmup()
        actions: dict[str, Any] = {}

        # 每个时段都会"刷一会儿"，时长随机（重尾：多数偏短）
        browse_lo, browse_hi = _rand_range(cfg, "schedule.browse_minutes", (2, 6))
        actions["browse_minutes"] = round(
            _lognormal(random, browse_lo, browse_hi), 1
        )

        # 评论：条数由「阶段放量」(comment_ramp) 决定——
        # 测试期故意压低，先把脚本问题跑出来、别封号；凭证据一档一档往上放。
        # 把 comment_ramp.enabled 关掉就退回原来的 warmup/常规配置。
        ramp = CommentRamp(cfg, self.db)
        if ramp.enabled():
            prob = ramp.probability(self.platform)
            c_lo, c_hi = ramp.per_session(self.platform)
        else:
            prob_key = (
                "schedule.warmup_comment_probability" if warm
                else "schedule.comment_probability"
            )
            prob = float(cfg.get(prob_key, 0.3 if warm else 0.55))
            range_key = (
                "schedule.warmup_comments_per_session" if warm
                else "schedule.comments_per_session"
            )
            c_lo, c_hi = _rand_range(cfg, range_key, (0, 1) if warm else (1, 2))
        if random.random() < prob:
            # 允许取到 0：上面的概率门决定"这个时段评不评论"，
            # 命中后 0 条也是合法结果（warmup 期 [0,1] 配置才有意义）
            actions["comments"] = int(random.uniform(c_lo, c_hi + 0.999))

        # 发帖：只有队列里真有稿子时才可能安排
        if self._has_pending_post() and random.random() < float(
            cfg.get("schedule.post_probability", 0.35)
        ):
            actions["post"] = True

        # 关注：每个时段 2-4 个，和浏览/评论混在一起做。
        # 不单独跑关注批次——同一时段连发 20 个关注是最典型的机器人指纹。
        f_lo, f_hi = _rand_range(cfg, "schedule.follow_per_session", (2, 4))
        actions["follows"] = int(random.uniform(f_lo, f_hi + 0.999))

        # 偶尔这个时段只是随便看看，什么都不做
        if not is_first and random.random() < 0.25:
            actions.pop("comments", None)
            actions.pop("post", None)

        return actions

    def plan_day(self, day: date | None = None) -> list[dict]:
        """生成某天的作息表（已存在则直接返回）。"""
        day = day or date.today()
        date_str = day.isoformat()
        existing = self.db.sessions_for_date(self.platform, date_str)
        if existing:
            # 统一成 dict，避免上层拿到的是 JSON 字符串
            return [
                {**row, "actions": _as_dict(row.get("actions"))} for row in existing
            ]
        if self.db.has_plan_skip(self.platform, date_str):
            return []
        if self.db.has_plan_empty(self.platform, date_str):
            return []

        # 整天空窗：真人也有今天完全不上的日子
        if random.random() < float(self.config.get("schedule.skip_day_probability", 0.1)):
            self.db.log_plan_skip(self.platform, date_str)
            return []

        windows = self._windows()
        sessions_key = (
            "schedule.warmup_sessions_per_day" if self._in_warmup() else "schedule.sessions_per_day"
        )
        s_lo, s_hi = _rand_range(self.config, sessions_key, (1, 3))
        target = max(1, int(random.uniform(s_lo, s_hi + 0.999)))
        gap_lo, gap_hi = _rand_range(self.config, "schedule.session_gap_minutes", (90, 240))

        now = datetime.now()
        starts: list[datetime] = []
        attempts = 0
        while len(starts) < target and attempts < 300:
            attempts += 1
            start_min, end_min = random.choice(windows)
            # 避开窗口首尾各 10 分钟，看起来像"随手打开"而不是掐点
            pick = random.randint(start_min + 10, max(start_min + 10, end_min - 10))
            candidate = datetime.combine(day, dtime(pick // 60, pick % 60))
            candidate += timedelta(minutes=random.randint(-8, 8))
            if candidate < now + timedelta(minutes=1):
                continue
            # 间隔用重尾分布：多数贴近下限，偶尔拉得很开
            gap_minutes = _lognormal(random, gap_lo, gap_hi)
            if any(
                abs((candidate - other).total_seconds()) < gap_minutes * 60
                for other in starts
            ):
                continue
            starts.append(candidate)

        starts.sort()
        created: list[dict] = []
        for index, start in enumerate(starts):
            actions = self._actions_for_session(is_first=(index == 0))
            session_id = self.db.add_session(
                self.platform,
                start.isoformat(timespec="seconds"),
                actions,
                date_str,
            )
            created.append(
                {
                    "id": session_id,
                    "planned_at": start.isoformat(timespec="seconds"),
                    "actions": actions,
                }
            )
        if not created:
            # 今天剩余时间排不出时段（比如傍晚才第一次运行）。
            # 持久化一个独立的 plan_empty 标记：它只针对「当天」，不影响次日规划，
            # 但能阻止每个 tick 都重 roll skip_day 概率 + 空转 300 次尝试。
            # 与「主动空窗」(plan_skip) 分开记录，日志语义不再混淆。
            self.db.log_plan_empty(self.platform, date_str)
            return []

        self.db.log(
            self.platform, "plan", "success",
            {"date": date_str, "sessions": len(created)},
        )
        return created


def session_status(results: dict) -> str:
    """根据一个时段的执行结果判定状态。

    - 全部动作成功      -> done
    - 有失败也有成功    -> partial（2026-11-24 之前这里一律记 done，明明只做了一半）
    - 全部动作都失败    -> failed
    """
    errors = results.get("errors") or {}
    did = [k for k in results if k not in ("errors", "comment_keyword")]
    if errors and not did:
        return "failed"
    if errors:
        return "partial"
    return "done"


class Scheduler:
    """到点执行作息表里的时段。"""

    def __init__(
        self,
        config: Any,
        db: Database,
        browser: BrowserManager,
        humanizer: Humanizer,
        risk: RiskController,
    ):
        self.config = config
        self.db = db
        self.browser = browser
        self.humanizer = humanizer
        self.risk = risk

    def ensure_plan(self, platform: str) -> dict:
        """确保今天和明天的作息表都存在。"""
        planner = DayPlanner(self.config, self.db, platform)
        today = planner.plan_day(date.today())
        tomorrow = planner.plan_day(date.today() + timedelta(days=1))
        return {"today": len(today), "tomorrow": len(tomorrow)}

    def _execute(self, platform: str, session: dict) -> dict:
        """执行一个时段：每个动作各自成败，互不牵连。

        2026-11-24 的教训：那次第 2 条评论被判失败后触发熔断，异常一路抛出，
        同一时段里排的 2 个关注直接没跑，而会话状态还是 done——事后只能翻一堆
        日志才看出「其实只做了一半」。所以：
          - 每个动作单独 try，失败只记录，后面的动作照做；
          - 顺序改成「关注在评论之前」：评论是风险最高、最容易被打断的动作，
            不该让它吃掉关注这种低风险增长动作的额度。
        """
        actions = _as_dict(session.get("actions"))
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}

        def run_step(name: str, fn) -> None:
            try:
                results[name] = fn()
            except CircuitBreakerTripped as exc:
                errors[name] = f"熔断：{exc}"
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"

        browse_minutes = float(actions.get("browse_minutes") or 0)
        if browse_minutes > 0:

            def _browse() -> dict:
                page = self.browser.page(
                    platform, self.config.get(f"platforms.{platform}.feed_url", "")
                )
                self.risk.check_page(page, platform)
                self.humanizer.warmup(page)
                self.humanizer.idle_browse(page, minutes=browse_minutes)
                out: dict[str, Any] = {"browse_minutes": browse_minutes}
                # 顺带做点"不产生内容"的事：展开长文、看通知、随便滚
                if random.random() < float(
                    self.config.get("schedule.extra_activity_probability", 0.5)
                ):
                    base_url = self.config.get(f"platforms.{platform}.base_url", "")
                    feed_url = self.config.get(f"platforms.{platform}.feed_url", "")
                    out["extra_activity"] = self.humanizer.random_activity(
                        page, base_url, feed_url
                    )
                return out

            run_step("browse", _browse)

        # 关注和评论共用同一批行业关键词，不单独开关注批次——
        # 同一时段连发一大批关注是最典型的机器人指纹。
        keywords = list(self.config.get("schedule.comment_keywords", []) or [])

        follows = int(actions.get("follows") or 0)
        if follows > 0:

            def _follow() -> dict:
                return Follower(
                    self.browser, self.humanizer, self.risk, self.db, self.config
                ).run(
                    count=follows,
                    platform=platform,
                    keyword=random.choice(keywords) if keywords else None,
                )

            run_step("follow", _follow)

        comments = int(actions.get("comments") or 0)
        if comments > 0:
            # 当日额度兜底：排程只是"计划"，这里按今天真实已用量再裁一次。
            # 某个时段被跳过/延迟后，后面的时段不该把额度叠加用掉。
            ramp = CommentRamp(self.config, self.db)
            if ramp.enabled():
                allowance = ramp.daily_allowance(platform)
                if comments > allowance:
                    results["comment_clamped"] = {
                        "planned": comments,
                        "allowed": allowance,
                    }
                    comments = allowance

        # 注意这里再判一次：额度被裁到 0 时只跳过评论，不能连带跳过发帖
        if comments > 0:
            # 主动按关键词找相关话题，而不是等信息流推送
            # （信息流里大多是不相关的内容，会被话题过滤全部筛掉）
            comment_keyword = random.choice(keywords) if keywords else None

            def _comment() -> dict:
                return Commenter(
                    self.browser, self.humanizer, self.risk, self.db, self.config
                ).run(count=comments, platform=platform, keyword=comment_keyword)

            run_step("comment", _comment)
            results["comment_keyword"] = comment_keyword

        if actions.get("post"):

            def _post() -> dict:
                return Poster(
                    self.browser, self.humanizer, self.risk, self.db, self.config
                ).run_pending(max_tasks=1)

            run_step("post", _post)

        if errors:
            results["errors"] = errors
        return results

    def tick(self, platform: str, force: bool = False) -> dict:
        """执行所有到点的时段。"""
        now = datetime.now()
        grace = int(self.config.get("schedule.grace_minutes", 45))
        earliest = (now - timedelta(minutes=grace)).isoformat(timespec="seconds")

        missed = self.db.mark_missed_sessions(platform, earliest)

        if force:
            session = self.db.next_session_any(platform)
            due = [session] if session else []
        else:
            due = self.db.due_sessions(
                platform, now.isoformat(timespec="seconds"), earliest
            )

        executed: list[dict] = []
        for session in due:
            self.db.mark_session(session["id"], "running")
            try:
                results = self._execute(platform, session)
                status = session_status(results)
                self.db.mark_session(session["id"], status)
                # 把「这个时段到底做成了什么」落一条日志：
                # 以后核查不用再靠翻一堆分散的 action 日志拼图。
                self.db.log(
                    platform,
                    "session",
                    status,
                    {
                        "planned_at": session.get("planned_at"),
                        "actions": _as_dict(session.get("actions")),
                        "results": results,
                    },
                )
                executed.append(
                    {
                        "id": session["id"],
                        "planned_at": session["planned_at"],
                        "status": status,
                        "results": results,
                    }
                )
            except CircuitBreakerTripped as exc:
                self.db.mark_session(session["id"], "failed")
                executed.append({"id": session["id"], "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self.db.mark_session(session["id"], "failed")
                executed.append({"id": session["id"], "error": f"{type(exc).__name__}: {exc}"})

        nxt = self.db.next_planned_session(
            platform, now.isoformat(timespec="seconds")
        )
        return {
            "executed": executed,
            "missed": missed,
            "next": nxt["planned_at"] if nxt else None,
        }

    def daemon(self, platform: str, poll_seconds: int = 60) -> None:
        """常驻循环：每分钟检查一次有没有到点的时段。"""
        print(f"随机日程守护启动（{platform}），Ctrl+C 退出。", flush=True)
        planner = DayPlanner(self.config, self.db, platform)
        for session in planner.plan_day(date.today()):
            print(f"  今日时段 {session['planned_at'][11:16]}  "
                  f"{json.dumps(session['actions'], ensure_ascii=False)}", flush=True)
        last_next = None
        while True:
            try:
                self.ensure_plan(platform)
                result = self.tick(platform)
                for item in result["executed"]:
                    if item.get("error"):
                        print(
                            f"[{datetime.now():%H:%M:%S}] 时段 {item.get('planned_at')} 失败："
                            f"{item['error']}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[{datetime.now():%H:%M:%S}] 完成时段 {item.get('planned_at')}："
                            f"{json.dumps(item.get('results'), ensure_ascii=False)}",
                            flush=True,
                        )
                if result["next"] and result["next"] != last_next:
                    print(f"  下一个时段：{result['next']}", flush=True)
                    last_next = result["next"]
            except KeyboardInterrupt:
                print("已停止。", flush=True)
                return
            except Exception as exc:  # noqa: BLE001
                print(f"  循环异常（继续运行）：{type(exc).__name__}: {exc}", flush=True)
            time.sleep(max(15, poll_seconds + random.randint(-15, 15)))
