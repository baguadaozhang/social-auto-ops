"""平台适配器基类：定义统一接口与通用 DOM 定位工具。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

from ..browser import BrowserManager
from ..database import Database
from ..humanize import Humanizer
from ..risk_control import RiskController


class BasePlatform:
    name: str = "base"
    # 子类覆盖：只有登录后才存在的元素，以及登录态失效时会被重定向到的路径
    LOGGED_IN_SELECTORS: list[str] = []
    LOGIN_REDIRECTS: tuple[str, ...] = ()
    # 登录成功后平台才会下发的鉴权 cookie，是比 DOM 更可靠的判断依据
    AUTH_COOKIES: tuple[str, ...] = ()

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

    # ---------- 通用工具 ----------
    @staticmethod
    def find_first(
        page: "Page", selectors: list[str], timeout_ms: int = 2500
    ) -> "Locator | None":
        """按优先级尝试多个选择器，返回第一个可见元素。

        关键：必须给每个选择器真正的等待时间。元素可能还没渲染出来，
        如果先用 count() 判断就会立刻跳过，导致"刚点开弹窗却找不到编辑框"。
        这里把总超时按选择器数量平分，保证每个都有机会等到元素出现。
        """
        import time as _time

        total = max(0.1, timeout_ms / 1000.0)
        deadline = _time.monotonic() + total
        remaining_count = len(selectors)
        for selector in selectors:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            slice_s = max(0.4, remaining / max(1, remaining_count))
            remaining_count -= 1
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=int(slice_s * 1000))
                return locator
            except Exception:
                continue
        return None

    def debug_shot(self, page: "Page", tag: str) -> str:
        """失败时留一张截图，方便定位页面改版或流程卡点。"""
        try:
            import time as _time

            folder = self.config.path("storage.debug_dir", "./data/debug")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{self.name}_{tag}_{int(_time.time())}.png"
            page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return ""

    def page(self, url: str | None = None) -> Page:
        feed = self.config.get(f"platforms.{self.name}.feed_url", "")
        return self.browser.page(self.name, url or feed)

    def _has_auth_cookie(self, page: "Page") -> bool:
        """用鉴权 cookie 判断登录态，不受界面语言 / DOM 改版影响。"""
        if not self.AUTH_COOKIES:
            return False
        try:
            cookies = page.context.cookies()
        except Exception:
            return False
        wanted = set(self.AUTH_COOKIES)
        return any(
            cookie.get("name") in wanted and (cookie.get("value") or "").strip()
            for cookie in cookies
        )

    def is_logged_in(self, fast: bool = False) -> bool:
        """检查登录态是否有效。

        fast=True 时不做等待，只数元素数量，适合登录流程里高频轮询。
        """
        page = self.page()
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        # 停在验证页 / 登录页都视为未完成登录
        if any(part in current_url for part in self.LOGIN_REDIRECTS):
            return False
        # cookie 判断最可靠：界面是中文还是英文都不影响
        if self._has_auth_cookie(page):
            return True
        if fast:
            for selector in self.LOGGED_IN_SELECTORS:
                try:
                    if page.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
            return False
        return self.find_first(page, self.LOGGED_IN_SELECTORS) is not None

    def post(self, content: str) -> dict:
        """发帖，返回 {success, url?, error?}。"""
        raise NotImplementedError

    def _precheck(self) -> None:
        """发帖前的安全检查。"""
        self.risk.assert_not_cooling(self.name)
        self.risk.assert_time_window()
        self.risk.check_quota(self.name, "post")

    def _on_success(self, content: str, url: str = "") -> dict:
        self.db.increment_quota(self.name, "post")
        self.db.log(self.name, "post", "success", {"preview": content[:60], "url": url})
        return {"success": True, "url": url}

    def _on_failure(self, content: str, error: str) -> dict:
        self.db.log(self.name, "post", "failed", {"preview": content[:60], "error": error})
        return {"success": False, "error": error}
