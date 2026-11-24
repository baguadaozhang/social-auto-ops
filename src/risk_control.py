"""风控层：配额限制、活跃时段、熔断检测。

这是系统的安全网——宁可少做，不可硬上。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page

from .database import ACTION_COLUMN, Database, now_iso


class QuotaExceeded(Exception):
    """当日配额已用完。"""


class TimeWindowClosed(Exception):
    """当前不在允许操作的时段。"""


class CircuitBreakerTripped(Exception):
    """检测到验证码或账号受限，已熔断。"""


class AccountCoolingDown(Exception):
    """账号处于熔断冷却期。"""


class RiskController:
    """所有高风险操作的守门人。"""

    def __init__(self, config: Any, db: Database):
        self.config = config
        self.db = db

    # ---------- 配额 ----------
    def effective_quota(self, platform: str, action: str) -> int:
        """考虑周末减半后的当日有效配额。"""
        base = int(self.config.get(f"quota.{platform}.{action}", 0))
        if self.config.get("time_window.weekdays_only", True) and datetime.now().weekday() >= 5:
            mult = float(self.config.get("time_window.weekend_multiplier", 0.5))
            return max(1, int(base * mult))
        return base

    def check_quota(self, platform: str, action: str) -> dict:
        """检查配额，超额抛异常；返回剩余额度。"""
        limit = self.effective_quota(platform, action)
        used = self.db.get_quota(platform).get(ACTION_COLUMN[action], 0)
        remaining = limit - used
        if remaining <= 0:
            raise QuotaExceeded(
                f"{platform} 今日 {action} 配额已用完 ({used}/{limit})"
            )
        return {"limit": limit, "used": used, "remaining": remaining}

    # ---------- 时间窗口 ----------
    def check_time_window(self) -> bool:
        """检查当前是否在允许时段内。"""
        if not self.config.get("time_window.enabled", True):
            return True
        now = datetime.now()
        if self.config.get("time_window.weekdays_only", True) and now.weekday() >= 5:
            return False
        jitter = int(self.config.get("time_window.jitter_minutes", 40))
        current = now.hour * 60 + now.minute
        for window in self.config.get("time_window.windows", []):
            try:
                start_s, end_s = window.split("-")
                sh, sm = map(int, start_s.strip().split(":"))
                eh, em = map(int, end_s.strip().split(":"))
                start, end = sh * 60 + sm, eh * 60 + em
            except (ValueError, AttributeError):
                continue
            if (start - jitter) <= current <= (end + jitter):
                return True
        return False

    def assert_time_window(self) -> None:
        if not self.check_time_window():
            raise TimeWindowClosed("当前不在活跃时间窗口内，任务已推迟")

    def next_available_time(self, after: datetime | None = None) -> datetime:
        """计算下一次可操作的时间：下一个窗口开始，或跳过周末。"""
        now = after or datetime.now()
        starts: list[int] = []
        for window in self.config.get("time_window.windows", []):
            try:
                start_s = window.split("-")[0].strip()
                sh, sm = map(int, start_s.split(":"))
                starts.append(sh * 60 + sm)
            except (ValueError, AttributeError):
                continue
        starts.sort()
        if not starts:
            starts = [9 * 60]

        weekdays_only = bool(self.config.get("time_window.weekdays_only", True))
        current = now.hour * 60 + now.minute

        for start in starts:
            if start <= current:
                continue
            candidate = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            if weekdays_only and candidate.weekday() >= 5:
                continue
            return candidate

        # 今天没有剩余窗口，顺延到明天（必要时跳过周末）
        candidate = (now + timedelta(days=1)).replace(
            hour=starts[0] // 60, minute=starts[0] % 60, second=0, microsecond=0
        )
        if weekdays_only:
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
        return candidate

    # ---------- 熔断 ----------
    def is_cooling_down(self, platform: str) -> bool:
        state = self.db.account_status(platform)
        until = state.get("circuit_break_until")
        if not until:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(until)
        except ValueError:
            return False

    def assert_not_cooling(self, platform: str) -> None:
        if self.is_cooling_down(platform):
            state = self.db.account_status(platform)
            raise AccountCoolingDown(
                f"{platform} 处于熔断冷却期，直到 {state['circuit_break_until']}"
            )

    # 只扫描这些"系统级"区域。
    # 注意：不能扫 [role='dialog']——发帖弹窗里就是用户自己输入的正文，
    # 正文提到"验证码"会造成误熔断。
    _ALERT_SELECTORS = [
        "[role='alert']",
        ".artdeco-toast-item",
        "[data-testid='toast']",
        "[data-testid='error-message']",
        "div[aria-live='assertive']",
    ]
    # URL 命中这些片段，基本可确定触发了验证/拦截
    _URL_SIGNALS = ("checkpoint", "challenge", "captcha", "authwall")

    def check_page(self, page: "Page", platform: str) -> None:
        """检查是否有风控信号：URL 拦截页、标题、系统提示区。

        不扫描整页正文，否则用户动态里出现"验证码"会误熔断。
        """
        keywords = [k.lower() for k in self.config.get("circuit_breaker.keywords", [])]
        if not keywords:
            return

        # 1) URL 信号最可靠
        try:
            url = (page.url or "").lower()
            if any(signal in url for signal in self._URL_SIGNALS):
                self.trip(platform, f"URL 命中风控页: {url[:80]}")
                return
        except Exception:
            pass

        # 2) 页面标题（验证码页标题通常是 "Security verification" 之类）
        texts: list[str] = []
        try:
            texts.append(page.title() or "")
        except Exception:
            pass

        # 3) 系统提示区域
        for selector in self._ALERT_SELECTORS:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 3)
                for i in range(count):
                    content = locator.nth(i).inner_text(timeout=1500) or ""
                    if content:
                        texts.append(content)
            except Exception:
                continue

        blob = "\n".join(texts).lower()
        for kw in keywords:
            if kw in blob:
                self.trip(platform, kw)
                return

    # 软性限流的提示区域：领英的"发得太快 / 评论到上限"通常出现在
    # 编辑器旁的 inline feedback、页面 banner 或 toast 里，与验证码那类硬信号分开扫。
    _SOFT_SIGNAL_SELECTORS = [
        "div[aria-live='assertive']",
        "div[aria-live='polite']",
        "[class*='inline-feedback']",
        "[role='alert']",
        ".artdeco-toast-item",
        "[class*='error'][role='presentation']",
    ]
    # 出现这些文案说明已经触到频率限制（中英双语，均为小写匹配）
    _SOFT_LIMIT_PHRASES = (
        "you're posting too fast",
        "you are posting too fast",
        "posting too fast",
        "comment limit",
        "too many comments",
        "too many actions",
        "reached the limit",
        "try again later",
        "temporarily restricted",
        "评论过于频繁",
        "操作过于频繁",
        "评论数量已达上限",
        "稍后再试",
    )

    def check_soft_limit(self, page: "Page", platform: str) -> None:
        """扫描软性限流提示，命中即熔断（冷却时长复用 cooldown_hours）。

        与 check_page 的分工：
        - check_page 抓"验证码 / 账号受限"，那是已经出事；
        - 这里抓"发得太快 / 评论到上限"，那是还没出事但方向不对，
          继续操作才会被真正限制，所以命中就停一天。
        只扫反馈类容器，不扫整页正文——普通帖子提到 "try again later"
        不应该让账号停摆一整天。
        """
        texts: list[str] = []
        for selector in self._SOFT_SIGNAL_SELECTORS:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 3)
                for i in range(count):
                    content = locator.nth(i).inner_text(timeout=800) or ""
                    if content.strip():
                        texts.append(content)
            except Exception:
                continue
        if not texts:
            return
        blob = "\n".join(texts).lower()
        for phrase in self._SOFT_LIMIT_PHRASES:
            if phrase in blob:
                self.trip(platform, f"软限流提示: {phrase}")

    def trip(self, platform: str, reason: str, hours: int | None = None) -> None:
        """触发熔断：标记账号状态 + 设置冷却截止时间。

        hours 留空用全局 circuit_breaker.cooldown_hours（默认 24）。
        证据强弱不同，冷却时长也该不同：像「评论提交后看不到」这种单次、
        证据偏弱的信号用短冷却，别为一个可能的误判赔掉一整天
        （2026-11-24 就是这么白停了一天的）。
        """
        if hours is None:
            hours = int(self.config.get("circuit_breaker.cooldown_hours", 24))
        until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.db.set_account_status(platform, "limited", until)
        self.db.log(platform, "circuit_breaker", "failed", {"reason": reason, "until": until})
        raise CircuitBreakerTripped(
            f"检测到风控信号「{reason}」，{platform} 已熔断至 {until}"
        )

    def mark_healthy(self, platform: str) -> None:
        self.db.set_account_status(platform, "healthy", None)

    # ---------- 人工接管判定 ----------
    def should_human_takeover(self, action: str) -> bool:
        """约每 N 次操作随机接管 1 次；高危动作可配置为每次确认。

        接管是真随机的（每次 1/N 概率），而不是确定性的"逢 N 必接管"——
        固定周期本身是可被观察的行为模式。operation_count 已随"逢 N"逻辑移除。
        """
        n = int(self.config.get("human_intervention.takeover_every_n", 10))
        if n <= 0:
            return False
        if random.random() < 1.0 / n:
            return True
        if action == "post" and self.config.get("human_intervention.confirm_before_post", True):
            return True
        if (
            action == "follow"
            and self.config.get("human_intervention.confirm_before_first_follow", True)
        ):
            return True
        return False

    def human_todo_required(self, platform: str, action: str) -> bool:
        """按人工占比决定是否把本次任务转人工。"""
        ratio = float(self.config.get("human_intervention.human_ratio", 0.25))
        return random.random() < ratio
