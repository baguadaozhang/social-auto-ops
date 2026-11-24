"""关注任务：只点「关注」，不做 Connect / 不发邀请。

两者在领英是两套限额，风险差一个量级：
- 关注（Follow）：不需要对方接受，不占邀请配额，20-30/天都算安全；
- 邀请（Connect）：硬限制，成熟号约 100/周、新号 50/周，且是滚动 7 天窗口。

本模块刻意不实现 Connect。要做邀请请另开任务，并在 config 里用独立键
（`quota.*.connect`），绝不能和 follow 共用配额。
"""
from __future__ import annotations

import json
import random
from typing import Any
from urllib.parse import quote

from ..browser import BrowserManager
from ..database import Database
from ..humanize import Humanizer
from ..platforms.linkedin import LinkedInPlatform
from ..risk_control import (
    AccountCoolingDown,
    CircuitBreakerTripped,
    QuotaExceeded,
    RiskController,
    TimeWindowClosed,
)
from .commenter import tags_in

# 行业词：话题词库（supply/quality/cost...）是为"评论什么"设计的，
# 偏经营管理；很多真实买家帖子只出现产品词（sanitary ware / faucet / shower），
# 用话题词库会把这些全判成"不相关"。关注的质量阀这里单独补一层行业词。
_INDUSTRY_HINTS = (
    "sanitary ware", "sanitaryware", "bathroom", "bath", "faucet", "tap",
    "shower", "vanity", "basin", "toilet", "wc", "urinal", "drain",
    "plumbing", "kitchen and bath", "kitchen & bath", "wellness", "spa",
    "distributor", "importer", "wholesaler", "supplier", "sourcing",
    "oem", "odm", "private label", "procurement", "trade show",
    "卫浴", "洁具", "五金", "建材", "采购", "经销", "外贸",
)


def industry_match(text: str) -> bool:
    body = (text or "").lower()
    return any(word in body for word in _INDUSTRY_HINTS)


class Follower:
    """关注任务处理器。第一版只支持领英。"""

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

    # ---------- 配额 ----------
    def daily_cap(self, platform: str) -> int:
        """当天最多关注几个：冷启动期用 warmup_follow，稳定期用配额键。"""
        warm_days = int(self.config.get("schedule.warmup_days", 7))
        if self.db.started_days_ago(platform) < warm_days:
            warm_cap = int(self.config.get("schedule.warmup_follow", 10))
            # 冷启动期取更严的那个，避免把稳定期上限当冷启动目标
            return min(warm_cap, self.risk.effective_quota(platform, "follow"))
        return self.risk.effective_quota(platform, "follow")

    def remaining_today(self, platform: str) -> int:
        used = int(self.db.get_quota(platform).get("follows", 0))
        return max(0, self.daily_cap(platform) - used)

    # ---------- 目标 ----------
    def _target_url(self, platform: str, keyword: str | None, url: str | None) -> str:
        if url:
            return url
        if keyword:
            base = self.config.get(f"platforms.{platform}.base_url", "")
            return f"{base}/search/results/content/?keywords={quote(keyword)}"
        return self.config.get(f"platforms.{platform}.feed_url", "")

    def _pick_keyword(self) -> str | None:
        keywords = list(self.config.get("schedule.comment_keywords", []) or [])
        return random.choice(keywords) if keywords else None

    def _quality_pass(self, info: dict, post_text: str) -> tuple[bool, str]:
        """三类只加其一：评论过的帖主 > 二度人脉 > 帖子命中行业话题。

        三类都不中就不加——关注一个完全不相关的人，对账号是负资产。
        """
        if not info.get("ok"):
            return False, "no_follow_button"
        url = str(info.get("url") or "").strip()
        if url and self.db.has_lead_url(url):
            return True, "commented_before"
        degree = int(info.get("degree") or 0)
        if 1 <= degree <= 2:
            return True, "2nd_degree"
        if tags_in(post_text):
            return True, "topic_match"
        if industry_match(post_text):
            return True, "industry_match"
        return False, "quality_gate"

    # ---------- 执行 ----------
    def run(
        self,
        count: int = 3,
        platform: str = "linkedin",
        keyword: str | None = None,
        url: str | None = None,
        manual: bool = False,
    ) -> dict:
        """关注 count 个目标。

        manual=True 表示人手动触发（CLI），可用于通过"首次关注需人工确认"的闸门。
        """
        result: dict[str, Any] = {
            "platform": platform,
            "requested": count,
            "success": 0,
            "skipped": 0,
            "details": [],
        }

        # 一次性闸门：confirm_before_first_follow 开着时，第一次关注必须人工触发，
        # 目的是让人先亲眼看一遍会加谁，而不是让机器直接开跑。
        gate_key = f"follow_first_done.{platform}"
        if (
            not manual
            and self.config.get("human_intervention.confirm_before_first_follow", True)
            and self.db.get_meta(gate_key) != "1"
        ):
            self.db.log(
                platform, "follow", "blocked_first_time",
                {"note": "首次关注需人工确认：手动执行一次 main.py follow --count 1"},
            )
            result["error"] = (
                "首次关注需人工确认：先手动跑一次 "
                "`python main.py follow --count 1`，通过后自动关注才会解锁"
            )
            return result

        try:
            self.risk.assert_not_cooling(platform)
            self.risk.assert_time_window()
            self.risk.check_quota(platform, "follow")
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            result["error"] = str(exc)
            return result

        cap_left = self.remaining_today(platform)
        if cap_left <= 0:
            result["error"] = f"{platform} 今日关注配额已用完（上限 {self.daily_cap(platform)}）"
            return result
        count = min(count, cap_left)

        target = self._target_url(platform, keyword or self._pick_keyword(), url)
        try:
            page = self.browser.page(platform, target)
            self.risk.check_page(page, platform)
            self.risk.check_soft_limit(page, platform)
        except CircuitBreakerTripped as exc:
            result["error"] = str(exc)
            return result

        self.humanizer.warmup(page)
        impl = LinkedInPlatform(
            self.browser, self.humanizer, self.risk, self.db, self.config
        )
        result["cards_found"] = impl.count_cards(page)

        done = 0
        index = 0
        while done < count and index < impl.count_cards(page):
            info = impl.follow_target(page, index)
            if not info.get("ok"):
                index += 1
                continue

            post_text = impl.post_text(page, index)
            ok, why = self._quality_pass(info, post_text)
            if not ok:
                result["skipped"] += 1
                result["details"].append(
                    {"index": index, "status": "skipped", "reason": why}
                )
                index += 1
                continue

            outcome = impl.follow_on_card(page, index)
            if outcome.get("success"):
                result["success"] += 1
                done += 1
            result["details"].append(
                {"index": index, "gate": why, **outcome}
            )
            index += 1

            if result["success"] and done < count:
                # 关注之间也要有间隔：同一时段连点是最典型的机器人指纹
                self.humanizer.sleep_range(20, 90)

        if manual and result["success"] > 0:
            self.db.set_meta(gate_key, "1")
            self.db.log(
                platform, "follow", "first_confirm_done",
                {"success": result["success"]},
            )
        return result
