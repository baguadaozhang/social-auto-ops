"""Facebook 平台适配器 - 第一版仅实现发帖。"""
from __future__ import annotations

from .base import BasePlatform
from ..humanize import page_wait
from ..risk_control import (
    CircuitBreakerTripped,
    QuotaExceeded,
    TimeWindowClosed,
    AccountCoolingDown,
)

_START_SELECTORS = [
    "div[aria-label*=\"What's on your mind\"]",
    "div[aria-label*='你在想什么']",
    "[data-testid='status-attachment-menu-btn']",
    "div[role='button']:has-text(\"What's on your mind\")",
    "span:has-text('在想什么')",
]
_EDITOR_SELECTORS = [
    "div[contenteditable='true'][role='textbox']",
    "[data-testid='status-composer']",
    "div[contenteditable='true']",
]
_SUBMIT_SELECTORS = [
    "[data-testid='react-composer-post-button']",
    "button:has-text('Post')",
    "button:has-text('发布')",
    "div[aria-label='Post'][role='button']",
]
class FacebookPlatform(BasePlatform):
    name = "facebook"

    # 必须是"只有登录后才有"的元素；div[role='navigation'] 在登录页也存在，不能用
    LOGGED_IN_SELECTORS = [
        "[aria-label='Your profile']",
        "div[aria-label='你的个人主页']",
        "[data-testid='left_nav_section_heading']",
        "a[href*='/friends']",
    ]
    # c_user 是 Facebook 登录后的用户 ID cookie
    AUTH_COOKIES = ("c_user",)
    LOGIN_REDIRECTS = ("/login", "/checkpoint", "/recover")

    def post(self, content: str) -> dict:
        try:
            self._precheck()
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            return self._on_failure(content, str(exc))

        try:
            # 浏览器启动 / 页面加载失败也要作为任务失败处理，不能中断整个队列
            page = self.page()
            self.risk.check_page(page, self.name)
            self.humanizer.warmup(page)

            starter = self.find_first(page, _START_SELECTORS)
            if starter is None:
                return self._on_failure(content, "未找到发帖入口，页面结构可能已改版")
            self.humanizer.human_click(starter)
            page_wait(page, 1200)

            editor = self.find_first(page, _EDITOR_SELECTORS)
            if editor is None:
                return self._on_failure(content, "未找到内容编辑框")
            self.humanizer.human_type(editor, content)

            self.humanizer.pause_before_submit()
            self.risk.check_page(page, self.name)

            submit = self.find_first(page, _SUBMIT_SELECTORS)
            if submit is None:
                return self._on_failure(content, "未找到发布按钮")
            self.humanizer.human_click(submit)
            page_wait(page, 2500)

            # 校验：编辑框还留着未提交的文字才算失败；
            # 不做校验会把"按钮没点上"误记为成功并白扣配额
            try:
                editor_probe = page.locator(
                    "div[contenteditable='true'][role='textbox']"
                )
                if (
                    editor_probe.count() > 0
                    and editor_probe.first.is_visible()
                    and (editor_probe.first.inner_text() or "").strip()
                ):
                    return self._on_failure(
                        content, "点击发布后编辑框未清空，可能未发布成功"
                    )
            except Exception:
                pass

            url = ""
            try:
                url = page.url
            except Exception:
                pass
            return self._on_success(content, url)

        except CircuitBreakerTripped as exc:
            return self._on_failure(content, str(exc))
        except Exception as exc:
            return self._on_failure(content, f"发帖异常: {type(exc).__name__}: {exc}")
