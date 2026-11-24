"""LinkedIn 平台适配器 - 第一版仅实现发帖。"""
from __future__ import annotations

import time

from .base import BasePlatform
from ..humanize import page_wait
from ..risk_control import (
    CircuitBreakerTripped,
    QuotaExceeded,
    TimeWindowClosed,
    AccountCoolingDown,
)

# LinkedIn DOM 经常改版，且中文/英文界面文案不同，每个元素都提供多个候选
_START_SELECTORS = [
    # 中文界面：信息流顶部「发动态」
    "div[role='button']:has-text('发动态')",
    "button:has-text('发动态')",
    "button[aria-label*='发动态']",
    # 英文界面
    "button[aria-label*='Start a post']",
    "div[role='button'][aria-label*='Start a post']",
    "button:has-text('Start a post')",
    # 结构类选择器
    ".share-box-feed-entry__trigger",
    "[data-testid='start-post-button']",
]
# 编辑框：领英新版用 TipTap，class 是混淆的，只能靠结构特征定位
_EDITOR_SELECTORS = [
    "div.tiptap.ProseMirror[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
    ".ql-editor[contenteditable='true']",
    "[data-testid='post-editor']",
    "div[contenteditable='true']",
]
# 发布按钮文案随界面语言变化：中文「动态」/「发布」，英文「Post」。
# 注意空内容时按钮是 disabled 的，必须输入完成后才可点。
_SUBMIT_SELECTORS = [
    "button:text-is('发布')",
    "button:text-is('动态')",
    "button:text-is('Post')",
    ".share-actions__primary-action",
]
# TipTap 编辑器存在性，用于校验弹窗是否还开着
_EDITOR_PROBE = "div.tiptap.ProseMirror[contenteditable='true']"
# 用 JS 找"可见且已启用"的发布按钮坐标，规避混淆 class 与同名干扰
_SUBMIT_POINT_JS = """
() => {
  const ed = document.querySelector("div.tiptap.ProseMirror[contenteditable='true']")
          || document.querySelector("div[contenteditable='true']");
  if (!ed) return null;
  const edRect = ed.getBoundingClientRect();
  const cands = [];
  for (const b of document.querySelectorAll('button')) {
    const t = (b.innerText || '').trim();
    if (!/^(动态|发布|Post|Share)$/.test(t)) continue;
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
    if (disabled) continue;
    cands.push({
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      text: t,
      dist: Math.abs(r.left - edRect.left) + Math.abs(r.top - edRect.top),
    });
  }
  if (!cands.length) return null;
  cands.sort((a, b) => a.dist - b.dist);
  return cands[0];
}
"""
_COMMENT_BTN_SELECTORS = [
    "button[aria-label='评论']",
    "button[aria-label*='评论']",
    "button[aria-label='Comment']",
    "button[aria-label*='Comment']",
]
_COMMENT_EDITOR_SELECTORS = [
    "div[aria-label*='评论的文本编辑器'][contenteditable='true']",
    "div[aria-label*='comment'][contenteditable='true']",
    "div.tiptap.ProseMirror[contenteditable='true']",
]
# 动作栏的"评论"是纯图标按钮（无文字），提交按钮是蓝底带「评论」文字，用 text-is 区分
_COMMENT_SUBMIT_SELECTORS = [
    "button:text-is('评论')",
    "button:text-is('发布')",
    "button:text-is('Comment')",
    "button:text-is('Post')",
]
# 评论框可能渲染在卡片之外，从"离当前卡片最近的评论框"向上找同容器内的提交按钮。
# 注意：不能取"全页最后一个编辑器"——上一条帖子残留的空评论框会被误选，
# 把评论打到别的帖子下面。
_COMMENT_EDITOR_NEAR_CARD_JS = """
(card) => {
  const cr = card.getBoundingClientRect();
  const all = [...document.querySelectorAll(
    "div[aria-label*='评论的文本编辑器'][contenteditable='true'], " +
    "div[aria-label*='comment'][contenteditable='true'], " +
    "div.tiptap.ProseMirror[contenteditable='true']"
  )];
  // 返回的下标必须是「全量 DOM 顺序」的下标，与 Python 侧 .nth(idx) 一一对应。
  // 只在计算距离时跳过零尺寸编辑器，绝不先 filter 再取下标（否则会错位 N 个）。
  let best = -1, bestDist = Infinity;
  all.forEach((e, i) => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    const d = Math.abs(r.top - cr.bottom) + Math.abs(r.left - cr.left);
    if (d < bestDist) { bestDist = d; best = i; }
  });
  return best;
}
"""
_COMMENT_EDITOR_PAGE_SELECTOR = (
    "div[aria-label*='评论的文本编辑器'][contenteditable='true'], "
    "div[aria-label*='comment'][contenteditable='true'], "
    "div.tiptap.ProseMirror[contenteditable='true']"
)
_COMMENT_SUBMIT_POINT_JS = """
(ed) => {
  let node = ed;
  for (let i = 0; i < 9 && node; i++) {
    for (const b of node.querySelectorAll('button')) {
      const t = (b.innerText || '').trim();
      const r = b.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && /^(评论|发布|Comment|Post)$/.test(t)) {
        return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: t};
      }
    }
    node = node.parentElement;
  }
  return null;
}
"""


class LinkedInPlatform(BasePlatform):
    name = "linkedin"

    # 必须是"只有登录后才有"的元素，避免公开页误判
    LOGGED_IN_SELECTORS = [
        ".global-nav__me",
        "[data-testid='global-nav-me']",
        "img.global-nav__me-photo",
        "button[aria-label*='Me']",
        "button[aria-label='我']",
        "a[href*='/messaging/']",
        "[data-testid='global-nav']",
    ]
    # li_at 是领英登录成功后下发的鉴权 cookie
    AUTH_COOKIES = ("li_at",)
    # 被重定向到这些路径说明登录态失效
    LOGIN_REDIRECTS = ("/login", "/authwall", "/checkpoint", "/uas/login")

    def post(self, content: str) -> dict:
        """发帖流程：安全检查 → 热身 → 打开编辑器 → 拟真输入 → 发布。"""
        try:
            self._precheck()
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            return self._on_failure(content, str(exc))

        try:
            # 浏览器启动 / 页面加载失败也要作为任务失败处理，不能中断整个队列
            page = self.page()
            # 页面级风控扫描
            self.risk.check_page(page, self.name)

            # 会话热身：先像人一样逛一会儿
            self.humanizer.warmup(page)

            # 找到发帖入口
            starter = self.find_first(page, _START_SELECTORS, timeout_ms=6000)
            if starter is None:
                self.debug_shot(page, "no_starter")
                return self._on_failure(content, "未找到发帖入口，页面结构可能已改版")
            self.humanizer.human_click(starter)
            page_wait(page, 1500)

            # 找到编辑框（弹窗需要时间渲染，给足等待预算）
            editor = self.find_first(page, _EDITOR_SELECTORS, timeout_ms=8000)
            if editor is None:
                self.debug_shot(page, "no_editor")
                return self._on_failure(content, "未找到内容编辑框")
            self.humanizer.human_type(editor, content)

            # 发布前再看一眼
            self.humanizer.pause_before_submit()
            self.risk.check_page(page, self.name)

            # 优先用选择器；中文新版按钮文案是「动态」且需输入后才启用，故用 JS 兜底
            submit = self.find_first(page, _SUBMIT_SELECTORS, timeout_ms=1500)
            if submit is not None:
                self.humanizer.human_click(submit)
            else:
                point = page.evaluate(_SUBMIT_POINT_JS)
                if not point:
                    self.debug_shot(page, "no_submit")
                    return self._on_failure(content, "未找到可用的发布按钮（可能是内容没输入成功）")
                self.humanizer.move_mouse_to(page, point["x"], point["y"])
                self.humanizer.micro_pause()
                page.mouse.click(point["x"], point["y"])
            page_wait(page, 3000)

            # 校验：编辑弹窗消失才算发布成功
            still_open = False
            try:
                still_open = page.locator(_EDITOR_PROBE).count() > 0 and \
                    page.locator(_EDITOR_PROBE).first.is_visible()
            except Exception:
                pass
            if still_open:
                return self._on_failure(content, "点击发布后弹窗未关闭，可能未发布成功")

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

    # ---------- 评论 ----------
    def _card_locator(self, page, index: int):
        return page.locator("div[role='listitem']").nth(index)

    def _first_in(self, root, selectors: list[str], prefer_last: bool = False):
        """在某个容器内按优先级找第一个（或最后一个）可见元素。"""
        for selector in selectors:
            try:
                locator = root.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.last if prefer_last else locator.first
                if target.count() > 0 and target.is_visible():
                    return target
            except Exception:
                continue
        return None

    _PROMOTED_MARKS = ("推广", "赞助内容", "赞助", "Promoted", "Sponsored")

    def _is_promoted(self, card) -> bool:
        """判断卡片是否是推广广告。广告帖不评论。"""
        try:
            head = card.inner_text()[:160]
        except Exception:
            return False
        return any(mark in head for mark in self._PROMOTED_MARKS)

    def _editor_near_card(self, page, card):
        """页面级兜底：找离当前卡片最近的评论编辑器（而不是全页最后一个）。

        之前评论过的帖子常残留空评论框，取"最后一个"会把评论打到别的帖子下。
        """
        try:
            idx = card.evaluate(_COMMENT_EDITOR_NEAR_CARD_JS)
        except Exception:
            return None
        if idx is None or idx < 0:
            return None
        locator = page.locator(_COMMENT_EDITOR_PAGE_SELECTOR).nth(idx)
        try:
            if locator.count() > 0 and locator.is_visible():
                return locator
        except Exception:
            pass
        return None

    def comment_on_card(
        self, page, index: int, text: str, base_text: str | None = None
    ) -> dict:
        """在第 index 张帖子卡片下发表评论。

        流程：滚动到卡片 → 展开评论框 → 拟真输入 → 点「评论」提交 → 校验。
        """
        try:
            cards = page.locator("div[role='listitem']")
            if index >= cards.count():
                return {"success": False, "error": f"卡片索引 {index} 超出范围"}
            card = self._card_locator(page, index)

            # 滚动到可视区域（真人也会先看到才会评论）
            try:
                card.scroll_into_view_if_needed(timeout=5000)
                page_wait(page, 850, jitter=0.4)
            except Exception:
                pass

            # 评论框可能已展开，避免重复点击把它收起
            editor = self._first_in(card, _COMMENT_EDITOR_SELECTORS)
            if editor is None:
                btn = self._first_in(card, _COMMENT_BTN_SELECTORS)
                if btn is None:
                    self.debug_shot(page, f"no_comment_btn_{index}")
                    return {"success": False, "error": "未找到评论按钮"}
                self.humanizer.human_click(btn)
                page_wait(page, 2200)
                editor = self._first_in(card, _COMMENT_EDITOR_SELECTORS)
                # 有些版式下评论框渲染在卡片容器之外，
                # 兜底时找"离当前卡片最近"的编辑器，避免选到上一条帖子的残留评论框
                if editor is None:
                    editor = self._editor_near_card(page, card)
            if editor is None:
                self.debug_shot(page, f"no_comment_editor_{index}")
                return {"success": False, "error": "未找到评论输入框"}

            # 拟真输入
            self.humanizer.human_type(editor, text)
            self.humanizer.pause_before_submit()

            # 提交：卡片内 → 从当前编辑器向上找就近按钮 → 就近编辑器兜底
            submit = self._first_in(card, _COMMENT_SUBMIT_SELECTORS)
            if submit is not None:
                self.humanizer.human_click(submit)
            else:
                point = None
                try:
                    point = editor.evaluate(_COMMENT_SUBMIT_POINT_JS)
                except Exception:
                    point = None
                if point:
                    self.humanizer.move_mouse_to(page, point["x"], point["y"])
                    self.humanizer.micro_pause()
                    page.mouse.click(point["x"], point["y"])
                else:
                    near = self._editor_near_card(page, card) or editor
                    try:
                        point = near.evaluate(_COMMENT_SUBMIT_POINT_JS)
                    except Exception:
                        point = None
                    if point:
                        self.humanizer.move_mouse_to(page, point["x"], point["y"])
                        self.humanizer.micro_pause()
                        page.mouse.click(point["x"], point["y"])
                    else:
                        self.debug_shot(page, f"no_comment_submit_{index}")
                        return {"success": False, "error": "未找到评论提交按钮"}

            # 校验：轮询最多 ~10 秒（固定 2.8 秒在网络慢/懒加载时会误判失败，
            # 误判失败 → 配额漏记 + 下次对同一帖子重复评论，是最伤账号的行为）。
            # 先确认编辑框已清空（未清空 = 根本没提交）；
            # 编辑器随提交消失（抛异常）也算清空。
            head = text.strip()[:12]
            cleared = False
            shown = False
            deadline = time.monotonic() + 10.0
            # 提交后编辑器清空是异步的：网络慢时不会瞬间清空。
            # 给清空一个短宽限再判失败，避免误判失败 → 配额漏记 → 二次重复评论。
            clear_deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not cleared:
                    try:
                        cleared = (editor.inner_text() or "").strip() == ""
                    except Exception:
                        cleared = True  # 编辑器已随提交从 DOM 消失
                    if not cleared:
                        if time.monotonic() >= clear_deadline:
                            break  # 宽限到期仍未清空，判定未提交
                        page_wait(page, 600, jitter=0.25)
                        continue
                if cleared:
                    try:
                        if head and head in card.inner_text():
                            shown = True
                            break
                    except Exception:
                        pass
                page_wait(page, 900, jitter=0.25)

            if not cleared:
                self.debug_shot(page, f"comment_not_submitted_{index}")
                return {"success": False, "error": "点击提交后评论框未清空，评论未发出"}

            preview = ""
            try:
                preview = card.inner_text()[:60]
            except Exception:
                pass

            if not shown:
                # 编辑框已清空但没在页面上看到评论：大概率已发出、只是渲染慢。
                # 记为 uncertain——计配额、写入去重日志，宁可少发不可重发。
                self.db.increment_quota(self.name, "comment")
                self.db.log(
                    self.name, "comment", "uncertain",
                    {
                        "text": text,
                        "base_text": base_text or text,
                        "preview": preview,
                        "note": "编辑框已清空但未确认渲染，按已发出处理",
                    },
                )
                return {"success": True, "uncertain": True, "text": text}

            self.db.increment_quota(self.name, "comment")
            # base_text 存原句，去重时比对原句而不是加了变体的最终文本
            self.db.log(
                self.name, "comment", "success",
                {
                    "text": text,
                    "base_text": base_text or text,
                    "preview": preview,
                },
            )
            return {"success": True, "text": text}

        except CircuitBreakerTripped as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"评论异常: {type(exc).__name__}: {exc}"}

    _POST_TEXT_JS = """
    (el) => {
      const sels = ["[data-testid='expandable-text-box']", ".feed-shared-text",
                    ".feed-shared-update-v2__description", "[dir='ltr']"];
      for (const sel of sels) {
        for (const node of el.querySelectorAll(sel)) {
          // [dir='ltr'] 太泛：卡片里别人的评论也带这个属性，
          // 会抓到评论内容当正文。排除位于评论区里的候选节点。
          if (sel === "[dir='ltr']" &&
              node.closest("[class*='comment'], [data-testid*='comment']")) {
            continue;
          }
          const text = (node.innerText || '').trim();
          if (text.length > 20) return text.slice(0, 600);
        }
      }
      const body = (el.innerText || '').trim();
      return body.slice(0, 600);
    }
    """

    _PERMALINK_JS = """
    (el) => {
      const a = el.querySelector("a[href*='/feed/update/'], a[href*='/posts/']");
      return a ? (a.href || "") : "";
    }
    """

    def card_permalink(self, page, index: int) -> str:
        """取帖子详情页链接（转人工待办时用，人工可直接去那条帖子操作）。"""
        try:
            card = page.locator("div[role='listitem']").nth(index)
            return (card.evaluate(self._PERMALINK_JS) or "").strip()
        except Exception:
            return ""

    def post_text(self, page, index: int) -> str:
        """取帖子正文，供评论话题匹配使用。"""
        try:
            card = page.locator("div[role='listitem']").nth(index)
            return (card.evaluate(self._POST_TEXT_JS) or "").strip()
        except Exception:
            return ""

    def count_cards(self, page) -> int:
        try:
            return page.locator("div[role='listitem']").count()
        except Exception:
            return 0

    def find_commentable_index(
        self, page, start: int = 0, max_scan: int = 12
    ) -> int:
        """从 start 开始找到第一张"可评论"的卡片。

        信息流里会混入广告、推荐、系统提示卡，它们没有评论按钮，
        必须跳过，否则会误判为"未找到评论按钮"。
        """
        try:
            cards = page.locator("div[role='listitem']")
            total = cards.count()
        except Exception:
            return -1
        for i in range(start, min(total, start + max_scan)):
            card = cards.nth(i)
            try:
                # 广告 / 推广帖直接跳过
                if self._is_promoted(card):
                    continue
                if (
                    self._first_in(card, _COMMENT_BTN_SELECTORS) is not None
                    or self._first_in(card, _COMMENT_EDITOR_SELECTORS) is not None
                ):
                    return i
            except Exception:
                continue
        return -1
