"""行为拟真层：让每一次自动化操作都包裹在真人使用痕迹里。

核心原则：任务不是开场就干，而是像人一样先逛一逛、看一看、想一想。
所有方法都对页面异常做容错——拟真失败不应导致任务失败。
"""
from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page, Locator

# 常见错别字映射，用于输入时偶发打错再改正
_TYPO_MAP = {
    "a": "s", "s": "a", "d": "f", "f": "d",
    "e": "w", "w": "e", "r": "t", "t": "r",
    "i": "o", "o": "i", "n": "m", "m": "n",
    "在": "再", "再": "在", "的": "地", "了": "啦",
    "营": "赢", "销": "消", "你": "您",
}


def page_wait(page: "Page", center_ms: float, jitter: float = 0.3) -> None:
    """以 center 为中心、±jitter 随机抖动的页面等待。

    固定毫秒数的等待（每次恰好 2200ms）本身就是机器特征，
    所有"等页面渲染"的调用统一走这里。
    """
    lo = center_ms * (1.0 - jitter)
    hi = center_ms * (1.0 + jitter)
    page.wait_for_timeout(random.uniform(lo, hi))


class Humanizer:
    """把机械操作包装得更像真人。"""

    def __init__(self, config: Any, rng: random.Random | None = None):
        self.config = config
        self.enabled = bool(config.get("humanize.enabled", True))
        self.intensity = float(config.get("humanize.intensity", 0.8))
        self.rng = rng or random.Random()
        # 在 Python 端跟踪每个页面的鼠标坐标，避免每个轨迹点都跨进程 evaluate
        self._positions: dict[int, tuple[float, float]] = {}

    # ---------- 基础时间控制 ----------
    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        time.sleep(seconds)

    def sleep_range(self, min_s: float, max_s: float, scale: bool = True) -> float:
        """在范围内随机睡眠。scale=True 时按强度缩放时长。"""
        lo, hi = float(min_s), float(max_s)
        if scale and self.enabled:
            lo *= self.intensity
            hi *= self.intensity
        if hi < lo:
            lo, hi = hi, lo
        seconds = self.rng.uniform(lo, hi)
        self.sleep(seconds)
        return seconds

    def lognormal_between(
        self, lo: float, hi: float, median_ratio: float = 0.45, sigma: float = 0.65
    ) -> float:
        """重尾分布采样：大多数偏短，偶尔出现明显长尾。

        真人的行为间隔不是均匀分布，短间隔远多于长间隔；
        均匀随机会显得"太整齐"，这里改用对数正态。
        """
        lo, hi = float(lo), float(hi)
        if hi <= lo:
            return lo
        mu = math.log(max(lo + (hi - lo) * median_ratio, 0.01))
        value = lo
        for _ in range(24):
            value = self.rng.lognormvariate(mu, sigma)
            if lo <= value <= hi:
                return value
        return min(hi, max(lo, value))

    def micro_pause(self) -> None:
        """0.2-0.9 秒的微停顿，模仿人的反应延迟。"""
        if self.enabled:
            self.sleep(self.rng.uniform(0.2, 0.9) * self.intensity)

    def reading_pause(self, text: str) -> float:
        """按内容长度计算阅读时间：字数 ÷ 15 字/秒 ± 40% 抖动。"""
        if not self.enabled:
            return 0.0
        cps = float(self.config.get("humanize.reading_chars_per_second", 15))
        jitter = float(self.config.get("humanize.reading_jitter", 0.4))
        chars = len(text.strip())
        base = chars / max(cps, 1)
        seconds = base * self.rng.uniform(1 - jitter, 1 + jitter)
        seconds = max(0.8, min(seconds, 120.0))
        self.sleep(seconds)
        return seconds

    # ---------- 鼠标轨迹 ----------
    def bezier_points(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        steps: int | None = None,
    ) -> list[tuple[float, float]]:
        """生成二次贝塞尔曲线上的点，中段有弧度偏移，不走直线。

        t 用 smoothstep 缓动（慢-快-慢），模拟真人"起步加速、接近目标减速"
        的手部运动；均匀 t 的物理速度曲线恰好相反（两头快中间慢），是机器特征。
        """
        if steps is None:
            steps = self.rng.randint(14, 26)
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        # 控制点：中点沿垂直方向偏移，偏移量随距离变化
        distance = math.hypot(dx, dy)
        bend = self.rng.uniform(-0.28, 0.28) * min(distance, 400)
        cx = (sx + ex) / 2 - dy / max(distance, 1) * bend
        cy = (sy + ey) / 2 + dx / max(distance, 1) * bend
        points: list[tuple[float, float]] = []
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3.0 - 2.0 * t)  # smoothstep 缓动
            u = 1 - t
            x = u * u * sx + 2 * u * t * cx + t * t * ex
            y = u * u * sy + 2 * u * t * cy + t * t * ey
            points.append((x, y))
        return points

    def _start_position(self, page: Page, key: int) -> tuple[float, float]:
        """轨迹起点：已知位置优先；新页面用视口中心附近，而不是 (0,0)。

        从左上角拉一条横跨屏幕的轨迹是稳定可复现的自动化签名。
        """
        pos = self._positions.get(key)
        if pos is not None:
            return pos
        try:
            size = page.evaluate(
                "() => [window.innerWidth, window.innerHeight]"
            )
            return (
                float(size[0]) * self.rng.uniform(0.35, 0.65),
                float(size[1]) * self.rng.uniform(0.35, 0.65),
            )
        except Exception:
            return (720.0, 450.0)

    def move_mouse_to(self, page: Page, x: float, y: float) -> None:
        """以贝塞尔曲线移动鼠标到目标坐标，每个轨迹点都有小间隔。"""
        if not self.enabled:
            page.mouse.move(x, y)
            return
        key = id(page)
        try:
            sx, sy = self._start_position(page, key)
            points = self.bezier_points((sx, sy), (x, y))
            pause_at = set(self.rng.sample(range(len(points)), k=min(2, len(points))))
            for idx, (px, py) in enumerate(points):
                page.mouse.move(px, py)
                if idx in pause_at:
                    self.sleep(self.rng.uniform(0.05, 0.18))
                else:
                    # 无间隔的瞬时跳点会让 mousemove 事件密度异常
                    self.sleep(self.rng.uniform(0.004, 0.014))
            self._positions[key] = (x, y)
            # 防泄漏：页面句柄只增不减时定期清空（id 复用后也会重新取起点）
            if len(self._positions) > 16:
                self._positions.clear()
                self._positions[key] = (x, y)
        except Exception:
            # 轨迹拟真失败不阻断任务
            try:
                page.mouse.move(x, y)
                self._positions[key] = (x, y)
            except Exception:
                pass

    def human_click(self, locator: Locator) -> None:
        """带偏移和曲线轨迹的点击：不点元素正中。"""
        page = locator.page
        box = locator.bounding_box()
        if box is None or not self.enabled:
            locator.click()
            return
        w, h = box["width"], box["height"]
        # 目标点：中心附近随机偏移，但保证在元素内部
        # 元素很小的时候夹紧偏移，避免点到元素外面
        max_dx = max(0.0, min(w / 2 - 2, 8))
        max_dy = max(0.0, min(h / 2 - 2, 8))
        offset_x = self.rng.uniform(-max_dx, max_dx)
        offset_y = self.rng.uniform(-max_dy, max_dy)
        tx = box["x"] + w / 2 + offset_x
        ty = box["y"] + h / 2 + offset_y
        self.move_mouse_to(page, tx, ty)
        self.micro_pause()
        page.mouse.click(tx, ty)

    # ---------- 滚动 ----------
    def scroll_page(
        self, page: Page, screens: float = 1.0, direction: str = "down"
    ) -> None:
        """带惯性的小步滚动，非一次性跳转。"""
        if not self.enabled:
            page.mouse.wheel(0, 600 * screens * (1 if direction == "down" else -1))
            return
        try:
            viewport = page.evaluate("() => window.innerHeight")
        except Exception:
            viewport = 800
        total = viewport * screens
        step = self.rng.uniform(90, 220)
        sign = 1 if direction == "down" else -1
        moved = 0.0
        while moved < total:
            delta = min(step, total - moved) * sign
            try:
                page.mouse.wheel(0, delta)
            except Exception:
                break
            moved += abs(delta)
            # 滚动途中随机微停顿，偶尔往回滚一点
            self.sleep(self.rng.uniform(0.08, 0.35))
            if self.rng.random() < 0.15:
                try:
                    page.mouse.wheel(0, -delta * self.rng.uniform(0.2, 0.5))
                except Exception:
                    pass
                self.micro_pause()

    def warmup(self, page: Page) -> None:
        """会话热身：先滚动几屏、随机停留，30% 概率多看一个无关内容。"""
        if not self.enabled or not self.config.get("humanize.warmup.enabled", True):
            return
        lo = int(self.config.get("humanize.warmup.scroll_screens_min", 2))
        hi = int(self.config.get("humanize.warmup.scroll_screens_max", 4))
        screens = self.rng.uniform(lo, hi)
        self.scroll_page(page, screens=screens, direction="down")
        pause_lo = float(self.config.get("humanize.warmup.pause_min_s", 15))
        pause_hi = float(self.config.get("humanize.warmup.pause_max_s", 60))
        self.sleep_range(pause_lo, pause_hi)
        prob = float(self.config.get("humanize.warmup.unrelated_click_probability", 0.3))
        if self.rng.random() < prob:
            # 模拟"点开一条无关内容看一眼"：悬停 + 延长阅读，不跳转页面以免丢失状态
            self._hover_random_post(page)
            self.sleep_range(8, 25)
            self.scroll_page(page, screens=0.6, direction="up")

    def _hover_random_post(self, page: Page) -> None:
        """悬停在一个随机的可见内容块上。"""
        try:
            candidates = page.locator("div[role='article'], .feed-shared-update-v2, div[data-testid='post']")
            count = min(candidates.count(), 8)
            if count > 0:
                idx = self.rng.randrange(count)
                box = candidates.nth(idx).bounding_box()
                if box:
                    self.move_mouse_to(
                        page,
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
        except Exception:
            pass

    # ---------- 输入拟真 ----------
    def human_type(self, locator: Locator, text: str) -> None:
        """逐字符输入，间隔随机；偶尔打错一个字符再退格改正。"""
        page = locator.page
        min_ms = int(self.config.get("humanize.typing.min_delay_ms", 60))
        max_ms = int(self.config.get("humanize.typing.max_delay_ms", 280))
        typo_prob = float(self.config.get("humanize.typing.typo_probability", 0.08))

        self.human_click(locator)
        self.micro_pause()

        if not self.enabled:
            locator.fill(text)
            return

        # 先想一下再开始打字
        self.sleep(self.rng.uniform(0.5, 2.0))

        for ch in text:
            if ch == "\n":
                page.keyboard.press("Enter")
            else:
                if self.rng.random() < typo_prob and ch in _TYPO_MAP:
                    wrong = _TYPO_MAP[ch]
                    try:
                        self._type_char(page, wrong)
                        self.sleep(self.rng.uniform(0.2, 0.6))
                        page.keyboard.press("Backspace")
                    except Exception:
                        pass
                try:
                    self._type_char(page, ch)
                except Exception:
                    locator.fill(text)
                    return
            # 重尾分布：大多数击键间隔短，偶尔明显停顿（真人打字节奏），
            # 且随 intensity 整体缩放，与 sleep_range 的缩放行为一致
            delay_ms = self.lognormal_between(
                min_ms * self.intensity,
                max_ms * self.intensity,
                median_ratio=0.35,
                sigma=0.5,
            )
            self.sleep(delay_ms / 1000.0)

    @staticmethod
    def _type_char(page: Page, ch: str) -> None:
        """ASCII 走按键事件；非 ASCII（中文等）走 insert_text，否则中文可能输不进去。"""
        if ch.isascii():
            page.keyboard.type(ch)
        else:
            page.keyboard.insert_text(ch)

    # ---------- 纯浏览 ----------
    def idle_browse(self, page: Page, minutes: float | None = None) -> None:
        """每天 5-10 分钟纯浏览：只滚动、偶尔点赞，不产生互动内容。"""
        if not self.enabled:
            return
        if minutes is None:
            minutes = float(self.config.get("humanize.idle_browse_minutes", 5))
        end = time.time() + minutes * 60
        while time.time() < end:
            self.scroll_page(page, screens=self.rng.uniform(1, 3))
            self.reading_pause("一段不相关的动态内容" * self.rng.randint(1, 5))
            if self.rng.random() < 0.2:
                self._low_value_like(page)
        self.scroll_page(page, screens=self.rng.uniform(1, 2), direction="up")

    # ---------- 非互动行为（扩充行为足迹） ----------
    def expand_read_more(self, page: Page) -> bool:
        """随机点开一条动态的"…更多"，展开读完——这是真人最常见的动作之一。"""
        try:
            buttons = page.locator(
                "button:has-text('… 更多'), button:has-text('…更多'), "
                "button:has-text('see more'), button:has-text('展开')"
            )
            count = min(buttons.count(), 6)
            if count <= 0:
                return False
            target = buttons.nth(self.rng.randrange(count))
            if not target.is_visible():
                return False
            self.human_click(target)
            self.sleep(self.rng.uniform(2.5, 8.0))
            return True
        except Exception:
            return False

    def view_notifications(self, page: Page, base_url: str, feed_url: str) -> bool:
        """去通知页扫一眼再回来。真人不会只盯着信息流。"""
        try:
            page.goto(
                base_url.rstrip("/") + "/notifications/",
                wait_until="domcontentloaded",
            )
            page_wait(page, 1800)
            self.scroll_page(page, screens=self.rng.uniform(0.4, 1.2))
            self.sleep(self.rng.uniform(3, 9))
            page.goto(feed_url, wait_until="domcontentloaded")
            page_wait(page, 1500)
            return True
        except Exception:
            return False

    def random_activity(self, page: Page, base_url: str, feed_url: str) -> str:
        """随机做一件"不产生内容"的事，返回做了什么。"""
        roll = self.rng.random()
        if roll < 0.45 and self.expand_read_more(page):
            return "expand_more"
        if roll < 0.75 and self.view_notifications(page, base_url, feed_url):
            return "notifications"
        self.scroll_page(page, screens=self.rng.uniform(0.5, 2.0))
        return "scroll"

    def _low_value_like(self, page: Page) -> None:
        """低价值点赞：偶尔点一下，降低纯刷的机械感。"""
        try:
            buttons = page.locator(
                "button[aria-label*='Like'], button[aria-label*='赞'], "
                "[data-testid='like-button']"
            )
            count = min(buttons.count(), 5)
            if count > 0:
                self.human_click(buttons.nth(self.rng.randrange(count)))
                self.micro_pause()
        except Exception:
            pass

    # ---------- 会话节奏 ----------
    def between_tasks(self) -> None:
        """任务之间发呆：用重尾分布，大多数是短的，偶尔很长。"""
        lo = float(self.config.get("humanize.idle_between_tasks_min_s", 60))
        hi = float(self.config.get("humanize.idle_between_tasks_max_s", 300))
        if not self.enabled:
            return
        self.sleep(self.lognormal_between(lo, hi))

    def pause_before_submit(self) -> None:
        """发布前停顿 1-3 秒，模拟再看一眼。"""
        self.sleep_range(1, 3)
