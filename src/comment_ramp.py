"""评论量分阶段提升（测试期低量，凭证据一档一档往上放）。

为什么不直接按天数放量：账号安不安全，不取决于「跑了多少天」，而取决于
「最近有没有出问题」。所以每一档除了时间门槛，还必须过一道健康门槛：

  - 最近 lookback_days 天里没有熔断记录
  - verify_failed 不超过阈值（允许极少数，但必须看得见）
  - 至少有 min_verified 条评论被回读确认成功（证明链路真的通，不是靠运气）

不达标就停在上一档——宁慢不冒险。当前档位是「算出来的」而不是「存在某个字段里」，
所以每次查询都是幂等的，也不存在迁移状态的问题。

2026-11-24 的教训：那天评论被判失败、其实发布成功了，还误触了 24 小时熔断。
所以「当日已用评论数」按 success + verify_failed 一起算——verify_failed 也可能
真的发出去了，核算用量时只能往多了算，否则会超发。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# 没写 config 时的兜底阶段表：保证功能可用，不至于因为少配一段就崩
DEFAULT_STAGES: list[dict[str, Any]] = [
    {
        "name": "S0 链路验证",
        "min_day": 0,
        "probability": 0.5,
        "per_session": [1, 1],
        "max_per_day": 2,
        "goal": "每条评论都能通过回读校验，确认提交链路是通的",
    },
    {
        "name": "S1 稳定性",
        "min_day": 3,
        "probability": 0.6,
        "per_session": [1, 2],
        "max_per_day": 4,
        "goal": "连续几天 verify_failed 为 0，没有限流提示",
    },
    {
        "name": "S2 常规节奏",
        "min_day": 8,
        "probability": 0.7,
        "per_session": [1, 3],
        "max_per_day": 6,
        "goal": "节奏稳定，账号无受限提示",
    },
    {
        "name": "S3 放量",
        "min_day": 15,
        "probability": 0.85,
        "per_session": [2, 4],
        "max_per_day": 10,
        "goal": "接近配额但保留余量",
    },
]

DEFAULT_GATE: dict[str, Any] = {
    "lookback_days": 3,
    "max_circuit_breaker_failures": 0,
    "max_verify_failed": 1,
    "min_verified_comments": 2,
}


@dataclass(frozen=True)
class Stage:
    index: int
    name: str
    min_day: int
    probability: float
    per_session: tuple[int, int]
    max_per_day: int
    goal: str


class CommentRamp:
    """评论量阶段控制。所有方法都读当前配置 + 数据库实时算，不自持状态。"""

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db

    # ---------- 配置 ----------
    def enabled(self) -> bool:
        return bool(self.config.get("comment_ramp.enabled", True))

    def _raw_stages(self) -> list[dict[str, Any]]:
        raw = self.config.get("comment_ramp.stages", None) or DEFAULT_STAGES
        return list(raw)

    def stages(self) -> list[Stage]:
        out: list[Stage] = []
        for i, item in enumerate(self._raw_stages()):
            lo, hi = item.get("per_session", [1, 1])
            lo, hi = int(lo), int(hi)
            if hi < lo:
                lo, hi = hi, lo
            out.append(
                Stage(
                    index=i,
                    name=str(item.get("name") or f"S{i}"),
                    min_day=int(item.get("min_day", 0)),
                    probability=float(item.get("probability", 0.5)),
                    per_session=(lo, hi),
                    max_per_day=int(item.get("max_per_day", 2)),
                    goal=str(item.get("goal") or ""),
                )
            )
        out.sort(key=lambda s: s.min_day)
        # 排序后重编号，保证 index 与「第几档」一致
        return [
            Stage(
                index=i,
                name=s.name,
                min_day=s.min_day,
                probability=s.probability,
                per_session=s.per_session,
                max_per_day=s.max_per_day,
                goal=s.goal,
            )
            for i, s in enumerate(out)
        ]

    def gate_config(self) -> dict[str, Any]:
        gate = dict(DEFAULT_GATE)
        for key in DEFAULT_GATE:
            value = self.config.get(f"comment_ramp.gate.{key}", None)
            if value is not None:
                gate[key] = value
        return gate

    # ---------- 评估 ----------
    def metrics(self, platform: str) -> dict[str, Any]:
        """健康门槛所需的三个指标（最近 lookback_days 天）。"""
        gate = self.gate_config()
        lookback = int(gate["lookback_days"])
        return {
            "lookback_days": lookback,
            "circuit_breaker_failures": self.db.count_logs(
                platform, "circuit_breaker", "failed", days=lookback
            ),
            "verify_failed": self.db.count_logs(
                platform, "comment", "verify_failed", days=lookback
            ),
            "verified_comments": self.db.count_logs(
                platform, "comment", "success", days=lookback
            ),
        }

    def gate_passed(self, platform: str) -> tuple[bool, list[str]]:
        """健康门槛是否通过；不通过时返回具体是哪几条不达标。"""
        gate = self.gate_config()
        m = self.metrics(platform)
        reasons: list[str] = []
        if m["circuit_breaker_failures"] > int(gate["max_circuit_breaker_failures"]):
            reasons.append(
                f"近 {m['lookback_days']} 天熔断 {m['circuit_breaker_failures']} 次"
                f"（上限 {gate['max_circuit_breaker_failures']}）"
            )
        if m["verify_failed"] > int(gate["max_verify_failed"]):
            reasons.append(
                f"近 {m['lookback_days']} 天 verify_failed {m['verify_failed']} 次"
                f"（上限 {gate['max_verify_failed']}）"
            )
        if m["verified_comments"] < int(gate["min_verified_comments"]):
            reasons.append(
                f"近 {m['lookback_days']} 天确认成功评论仅 {m['verified_comments']} 条"
                f"（至少要 {gate['min_verified_comments']} 条才算链路可信）"
            )
        return (not reasons), reasons

    def _start_date(self, platform: str) -> date | None:
        raw = self.db.get_meta(f"started_at.{platform}") or self.db.get_meta("started_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None

    def calendar_days(self, platform: str) -> int:
        """从首次运行到今天，跨了几个日历日。

        不用 db.started_days_ago()：那个是 (now - start).days 的截断值，
        跑满 22 小时仍算 0 天，对"第几天该提档"这种日历语义不直观。
        """
        start = self._start_date(platform)
        if start is None:
            return max(0, self.db.started_days_ago(platform))
        return max(0, (date.today() - start).days)

    def evaluate(self, platform: str) -> dict[str, Any]:
        """算出当前应该用哪一档，并说明为什么。"""
        stages = self.stages()
        if not stages:
            raise RuntimeError("comment_ramp.stages 为空，无法评估阶段")
        days = self.calendar_days(platform)
        started = self._start_date(platform)

        target = stages[0]
        for stage in stages:
            if days >= stage.min_day:
                target = stage

        ok, reasons = self.gate_passed(platform)
        current = target
        held_back = False
        if target.index > 0 and not ok:
            # 健康门槛没过：退一档，宁慢不冒险
            current = stages[target.index - 1]
            held_back = True

        return {
            "days": days,
            "started": started.isoformat() if started else "",
            "current": current,
            "target": target,
            "held_back": held_back,
            "gate_passed": ok,
            "gate_reasons": reasons,
            "metrics": self.metrics(platform),
            "next": stages[current.index + 1] if current.index + 1 < len(stages) else None,
        }

    # ---------- 给排程/执行用的参数 ----------
    def probability(self, platform: str) -> float:
        return self.evaluate(platform)["current"].probability

    def per_session(self, platform: str) -> tuple[int, int]:
        return self.evaluate(platform)["current"].per_session

    def max_per_day(self, platform: str) -> int:
        return self.evaluate(platform)["current"].max_per_day

    def daily_allowance(self, platform: str) -> int:
        """今天还能发几条评论（已按「可能已发出」核算过用量）。"""
        used = self.db.comments_posted_on(platform)
        return max(0, self.max_per_day(platform) - used)
