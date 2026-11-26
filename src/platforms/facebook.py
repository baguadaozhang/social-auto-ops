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

# ---------- 主页发帖：Business Suite 编辑器 ----------
# 2026-11-26 实测：business.facebook.com/latest/composer 打开后约 8-15 秒才渲染，
# 编辑框是 role=combobox（不是 textbox），aria-label 为「在对话框中输入内容…」；
# 「发布」是普通 div[role=button]，未输入内容时 aria-disabled=true。
_BS_COMPOSER = "https://business.facebook.com/latest/composer?asset_id={asset}"
_BS_EDITOR = [
    "div[role='combobox'][aria-label*='输入内容']",
    "div[contenteditable='true'][role='combobox']",
    "div[contenteditable='true'][aria-label*='输入内容']",
]
_BS_PUBLISH = [
    "div[role='button']:text-is('发布')",
    "div[role='button']:text-is('Post')",
    "button:text-is('发布')",
    "button:text-is('Post')",
]
_BS_CANCEL = [
    "div[role='button']:text-is('取消')",
    "div[role='button']:text-is('Cancel')",
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

    # ---------- 主页发帖（Business Suite） ----------
    def composer_url(self) -> str:
        """Business Suite 发帖页。asset_id 是主页在 Business Suite 里的资产编号，
        与 Page ID 不是同一个数（2026-11-26 实测：Page 61591663326933 → asset 1212447438618991）。"""
        asset = str(self.config.get("platforms.facebook.business_asset_id", "") or "").strip()
        if not asset:
            return "https://business.facebook.com/latest/composer"
        return _BS_COMPOSER.format(asset=asset)

    @staticmethod
    def _bs_editor(page, timeout_ms: int = 25000):
        """等编辑器渲染出来。Business Suite 是 SPA，首屏要 8–15 秒。"""
        import time as _time

        deadline = _time.monotonic() + timeout_ms / 1000.0
        while _time.monotonic() < deadline:
            editor = FacebookPlatform.find_first(page, _BS_EDITOR, timeout_ms=1200)
            if editor is not None:
                return editor
            page.wait_for_timeout(500)
        return None

    @staticmethod
    def mark_button(page, labels: list[str], attr: str = "data-dsh-btn"):
        """按「去掉零宽字符后的文本」在页面上标记一个按钮，再返回它的 locator。

        Business Suite 的按钮里常带 \\u200b，`:text-is('发布')` 匹配不上；
        这里直接在 DOM 里找可见的 role=button 并打标记，定位更稳。
        """
        marked = page.evaluate(
            """
            ([labels, attr]) => {
              const clean = s => (s || '').replace(/\\u200b/g, '').trim();
              document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr));
              const cands = [...document.querySelectorAll("div[role='button'],button")]
                .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
              const btn = cands.find(b => labels.includes(clean(b.innerText)));
              if (!btn) return false;
              btn.setAttribute(attr, '1');
              return true;
            }
            """,
            [labels, attr],
        )
        if not marked:
            return None
        try:
            locator = page.locator(f"[{attr}='1']").first
            return locator if locator.count() else None
        except Exception:
            return None

    @staticmethod
    def _bs_publish_button(page):
        """返回「发布」按钮；未启用时 aria-disabled=true。"""
        return FacebookPlatform.mark_button(page, ["发布", "Post"], "data-dsh-publish")

    def _bs_click(self, page, locator) -> bool:
        """Business Suite 的按钮是 div[role=button]，human_click 的坐标点击更稳。"""
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            self.humanizer.human_click(locator)
            return True
        except Exception:
            try:
                locator.click(timeout=4000)
                return True
            except Exception:
                return False

    def post_page(self, content: str) -> dict:
        """以「公共主页」身份发帖（Business Suite 编辑器）。

        与 post() 的区别：post() 走 facebook.com 首页的「你在想什么」，那是
        个人时间线；本方法走 Business Suite，发布位置固定是主页。
        """
        try:
            self._precheck()
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            return self._on_failure(content, str(exc))

        page = None
        try:
            page = self.page(self.composer_url())
            try:
                page.bring_to_front()
            except Exception:
                pass
            page_wait(page, 1500)
            self.risk.check_page(page, self.name)

            editor = self._bs_editor(page)
            if editor is None:
                self.debug_shot(page, "bs_no_editor")
                return self._on_failure(
                    content, "主页发帖编辑器未出现（Business Suite 可能未登录或改版）"
                )

            try:
                editor.click(timeout=4000)
            except Exception:
                pass
            page_wait(page, 600)
            self.humanizer.human_type(editor, content)
            page_wait(page, 900)

            publish = self._bs_publish_button(page)
            if publish is None:
                self.debug_shot(page, "bs_no_publish")
                return self._on_failure(content, "未找到「发布」按钮")

            # 等按钮从 disabled 变可用：内容没进编辑器时它一直是 disabled
            import time as _time

            deadline = _time.monotonic() + 8.0
            while _time.monotonic() < deadline:
                if (publish.get_attribute("aria-disabled") or "").lower() != "true":
                    break
                page.wait_for_timeout(400)
            else:
                self.debug_shot(page, "bs_publish_disabled")
                return self._on_failure(content, "「发布」按钮一直是灰的，内容可能没输入成功")

            self.humanizer.pause_before_submit()
            self.risk.check_page(page, self.name)
            if not self._bs_click(page, publish):
                return self._on_failure(content, "点击「发布」失败")

            # 校验：编辑器消失 或 出现「已发布」提示，才算成功
            ok = False
            deadline = _time.monotonic() + 20.0
            while _time.monotonic() < deadline:
                page.wait_for_timeout(700)
                try:
                    if "已发布" in page.inner_text("body"):
                        ok = True
                        break
                except Exception:
                    pass
                if self._bs_editor(page, timeout_ms=800) is None:
                    ok = True
                    break
            if not ok:
                self.debug_shot(page, "bs_publish_unverified")
                return self._on_failure(content, "点发布后既没看到提示也没关掉编辑器，判定未成功")

            url = ""
            try:
                url = page.url
            except Exception:
                pass
            return self._on_success(content, url)

        except CircuitBreakerTripped as exc:
            return self._on_failure(content, str(exc))
        except Exception as exc:
            if page is not None:
                self.debug_shot(page, "bs_exception")
            return self._on_failure(content, f"主页发帖异常: {type(exc).__name__}: {exc}")

    def post(self, content: str) -> dict:
        # 默认以主页身份发（Business Suite）；只有显式配成 personal 才走旧路径
        mode = str(self.config.get("platforms.facebook.post_via", "business_suite"))
        if mode != "personal":
            return self.post_page(content)

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
