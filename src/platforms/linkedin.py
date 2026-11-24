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

    def _open_comment_editor(self, page, card, index: int):
        """展开并返回评论编辑器；已展开则直接复用（再点一次会把它收起）。"""
        editor = self._first_in(card, _COMMENT_EDITOR_SELECTORS)
        if editor is not None:
            return editor
        btn = self._first_in(card, _COMMENT_BTN_SELECTORS)
        if btn is None:
            return None
        self.humanizer.human_click(btn)
        page_wait(page, 2200)
        editor = self._first_in(card, _COMMENT_EDITOR_SELECTORS)
        if editor is None:
            # 有些版式下评论框渲染在卡片容器之外，兜底时找"离当前卡片最近"的编辑器，
            # 避免选到上一条帖子的残留评论框
            editor = self._editor_near_card(page, card)
        return editor

    def _click_comment_submit(self, page, card, editor) -> bool:
        """点提交：卡片内按钮 → 从当前编辑器向上找就近按钮 → 就近编辑器兜底。"""
        submit = self._first_in(card, _COMMENT_SUBMIT_SELECTORS)
        if submit is not None:
            self.humanizer.human_click(submit)
            return True
        point = None
        try:
            point = editor.evaluate(_COMMENT_SUBMIT_POINT_JS)
        except Exception:
            point = None
        if not point:
            near = self._editor_near_card(page, card) or editor
            try:
                point = near.evaluate(_COMMENT_SUBMIT_POINT_JS)
            except Exception:
                point = None
        if not point:
            return False
        self.humanizer.move_mouse_to(page, point["x"], point["y"])
        self.humanizer.micro_pause()
        page.mouse.click(point["x"], point["y"])
        return True

    @staticmethod
    def _editor_cleared(editor) -> bool:
        """编辑框是否已清空（提交成功后 LinkedIn 会清空它或把它从 DOM 移走）。"""
        try:
            if editor is None or editor.count() == 0:
                return True
            return (editor.inner_text() or "").strip() == ""
        except Exception:
            return True

    def _wait_comment_visible(
        self,
        page,
        card,
        head: str,
        timeout_s: float = 8.0,
        index: int | None = None,
        editor=None,
    ) -> bool:
        """提交后等 2-4 秒，回读评论区确认自己的评论已经出现。

        踩过的坑（2026-11-24）：那次评论其实发布成功了，却判成「没发出去」，
        进而误触发 24 小时熔断。原因是旧实现只认 ``card.inner_text()``，而且把
        异常整个吞掉——卡片一旦因为列表重排而指向别的帖子，就永远匹配不上。
        另外注意：LinkedIn 现在的 class 名是哈希化的，``[class*="comment"]``
        一个都命中不了，所以不能靠 class 选择器定位评论。

        现在是两级判定：
          1) 卡片文本里出现 head —— 精确，优先；
          2) 卡片里没有，但**编辑框确实已清空**、且整页文本里出现 head —— 兜底。
             这一条必须卡住「编辑框已清空」，否则命中的是还留在编辑框里的草稿，
             会变成「没发出去却算成功」的假阳性。
        """
        page_wait(page, 3000, jitter=0.34)  # 2000~4000ms
        deadline = time.monotonic() + timeout_s
        while True:
            if head:
                try:
                    if head in card.inner_text():
                        return True
                except Exception:
                    pass
                if self._editor_cleared(editor):
                    try:
                        if head in page.inner_text("body"):
                            return True
                    except Exception:
                        pass
            if time.monotonic() >= deadline:
                return False
            page_wait(page, 900, jitter=0.25)

    _AUTHOR_JS = """
    (el) => {
      const sels = [
        ".update-components-actor a[href*='/in/']",
        "a[data-test-id*='actor']",
        "a[href*='/in/']",
      ];
      for (const sel of sels) {
        for (const a of el.querySelectorAll(sel)) {
          const href = a.href || "";
          if (!href.includes("/in/")) continue;
          const name = (a.innerText || "").trim().split("\\n")[0];
          if (name) return {name: name, url: href.split("?")[0]};
        }
      }
      return null;
    }
    """

    _ACTOR_JS = """
    (el) => {
      // 关注的到底是"人"还是"公司"，必须记下来。
      // 2026-11-24 14:32 关注了一条 "Pure Charming"，URL 和度数都是空的——
      // 因为公司页的链接是 /company/，而 _AUTHOR_JS 只认 /in/。
      // 结果就是"关注了谁"无法追溯，等于审计断链。
      const pick = (sel, kind) => {
        for (const a of el.querySelectorAll(sel)) {
          const href = a.href || "";
          if (!href) continue;
          const text = (a.innerText || "").trim();
          if (text) return { name: text, url: href.split("?")[0], kind: kind };
        }
        return null;
      };
      return pick("a[href*='/in/']", "person")
          || pick("a[href*='/company/']", "company")
          || pick("a[href*='/school/']", "school");
    }
    """

    def _record_lead(self, page, card, index: int, text: str) -> None:
        """评论成功后记一条线索：评论了谁、在哪条帖子下、说了什么。

        取不到昵称或主页也照记（字段留空），至少有帖子链接可以回看；
        记录失败不能影响评论结果，所以整体包在 try 里。
        """
        try:
            author = card.evaluate(self._AUTHOR_JS) or {}
        except Exception:
            author = {}
        try:
            self.db.add_lead(
                self.name,
                str(author.get("name") or "").strip(),
                str(author.get("url") or "").strip(),
                self.card_permalink(page, index),
                text,
            )
        except Exception:
            pass

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

            editor = self._open_comment_editor(page, card, index)
            if editor is None:
                # 区分"没有评论按钮"和"有按钮但没弹出编辑器"，方便排查页面改版
                if self._first_in(card, _COMMENT_BTN_SELECTORS) is None:
                    self.debug_shot(page, f"no_comment_btn_{index}")
                    return {"success": False, "error": "未找到评论按钮"}
                self.debug_shot(page, f"no_comment_editor_{index}")
                return {"success": False, "error": "未找到评论输入框"}

            head = text.strip()[:12]
            visible = False
            reason = ""

            # P0：提交后必须回读评论区确认评论真的出现。
            # 只信"编辑框清空"不够——11-23 出现过 3 次提交后静默失败
            # （截图 linkedin_no_comment_*）。那种失败一旦计了配额、进了去重表，
            # 下次对同一帖子重发才是真正伤账号的行为。
            for attempt in (1, 2):
                if attempt == 2:
                    # 重发前再给一次更长的观察窗口。宁可多等，也不能盲目重发——
                    # 同一帖子下留两条一模一样的评论是很强的 spam 信号
                    # （11-24 那次是碰巧被 LinkedIn 自己的重复评论拦截兜住了，
                    #   不能把账号安全寄托在平台兜底上）。
                    if self._wait_comment_visible(
                        page, card, head, timeout_s=6.0, index=index, editor=editor
                    ):
                        visible = True
                        break
                    # 确认真的没发出去，才重新打开编辑器从头来一次
                    editor = self._open_comment_editor(page, card, index)
                    if editor is None:
                        reason = "重试时未找到评论输入框"
                        break
                    try:
                        editor.fill("")  # 清掉可能残留的草稿，避免叠加输入
                    except Exception:
                        pass

                self.humanizer.human_type(editor, text)
                self.humanizer.pause_before_submit()
                if not self._click_comment_submit(page, card, editor):
                    reason = "未找到评论提交按钮"
                    continue

                # 提交后等 2-4 秒回读评论区；没出现先继续轮询，仍没有才判失败
                if self._wait_comment_visible(page, card, head, index=index, editor=editor):
                    visible = True
                    break

                cleared = True
                try:
                    cleared = (editor.inner_text() or "").strip() == ""
                except Exception:
                    cleared = True  # 编辑器已随提交从 DOM 消失
                reason = (
                    "提交后评论区未出现该评论"
                    if cleared
                    else "点击提交后评论框未清空"
                )

            if not visible:
                shot = self.debug_shot(page, f"comment_not_visible_{index}")
                # 失败时把帖子链接一起记下来：11-24 那次失败没有 post_url，
                # 只能事后翻「我的动态-评论」才确认评论其实发出去了。
                try:
                    post_url = self.card_permalink(page, index)
                except Exception:
                    post_url = ""
                self.db.log(
                    self.name, "comment", "verify_failed",
                    {
                        "text": text,
                        "base_text": base_text or text,
                        "reason": reason,
                        "attempts": 2,
                        "post_url": post_url,
                        "screenshot": shot,
                        "note": "提交后 2-4 秒回读评论区未见到该评论，重试 1 次仍失败；不计配额",
                    },
                )
                # 软限制：两次都没在评论区看到自己的评论，按"疑似限流"处理。
                # 但只给一段短冷却：这是单次、证据偏弱的信号（11-24 那次评论其实
                # 发布成功了，是我们的回读判定看漏了），用 24 小时冷却等于白白停一天。
                # 真正明确的限流提示会走 check_soft_limit，那条仍按 24 小时走。
                try:
                    self.risk.trip(
                        self.name,
                        "评论提交后不可见（疑似限流）",
                        hours=int(self.config.get("circuit_breaker.verify_failed_hours", 3)),
                    )
                except CircuitBreakerTripped:
                    pass
                return {
                    "success": False,
                    "error": f"评论未确认发出：{reason}",
                    "screenshot": shot,
                }

            # 只有回读确认过，才计配额、写成功日志、记一条线索
            self.db.increment_quota(self.name, "comment")
            # base_text 存原句，去重时比对原句而不是加了变体的最终文本
            self.db.log(
                self.name, "comment", "success",
                {
                    "text": text,
                    "base_text": base_text or text,
                    "verified": True,
                },
            )
            self._record_lead(page, card, index, text)
            return {"success": True, "text": text, "verified": True}

        except CircuitBreakerTripped as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"评论异常: {type(exc).__name__}: {exc}"}

    # ---------- 关注（Follow）----------
    # 注意：这是「关注」按钮，不是「Connect / 发邀请」。两者是两套限额：
    # 关注不需要对方接受、不占邀请配额；邀请才是硬限制（成熟号约 100/周）。
    _FOLLOW_BTN_SELECTORS = [
        "button[aria-label^='关注']",
        "button[aria-label^='Follow']",
    ]
    # 点完之后按钮/文案会变成这些（中英双语）
    _FOLLOWED_MARKERS = (
        "取消关注", "Unfollow", "正在关注", "Following", "已关注",
    )
    _DEGREE_JS = """
    (el) => {
      const t = (el.innerText || '').slice(0, 320);
      let m = t.match(/(\\d)\\s*度/);
      if (m) return parseInt(m[1], 10);
      m = t.match(/(\\d)(st|nd|rd|th)/i);
      if (m) return parseInt(m[1], 10);
      return 0;
    }
    """

    def follow_target(self, page, index: int) -> dict:
        """读第 index 张卡片的关注信息：有没有关注按钮、作者是谁、是人还是公司。

        预检就用 _ACTOR_JS（而不是只认 /in/ 的 _AUTHOR_JS），这样关注门槛
        在点下去之前就知道对方是公司页，才能按公司策略单独判定。
        """
        try:
            card = self._card_locator(page, index)
        except Exception:
            return {"ok": False, "reason": "card_error"}
        button = self._first_in(card, self._FOLLOW_BTN_SELECTORS)
        actor: dict = {}
        try:
            actor = card.evaluate(self._ACTOR_JS) or {}
        except Exception:
            pass
        try:
            degree = int(card.evaluate(self._DEGREE_JS) or 0)
        except Exception:
            degree = 0
        kind = str(actor.get("kind") or "")
        raw_name = str(actor.get("name") or "").strip()
        name = raw_name.splitlines()[0].strip() if raw_name else ""
        return {
            "ok": button is not None,
            "has_button": button is not None,
            "target": name,
            "url": str(actor.get("url") or "").strip(),
            # 公司页没有「几度人脉」，取 0 是正常值而非异常
            "degree": degree if kind == "person" else 0,
            "kind": kind,
        }

    def _wait_followed(self, page, card, timeout_s: float = 6.0) -> bool:
        """点完关注后回读按钮状态，确认真的生效（而不是只点了一下）。"""
        page_wait(page, 1500, jitter=0.3)
        deadline = time.monotonic() + timeout_s
        while True:
            blob = ""
            try:
                labels = card.evaluate(
                    "(el) => [...el.querySelectorAll('button')].map("
                    "b => (b.getAttribute('aria-label') || '') + '|' + (b.innerText || ''))"
                    ".join('§')"
                )
                blob = (card.inner_text()[:600] or "") + "§" + (labels or "")
            except Exception:
                blob = ""
            if any(mark in blob for mark in self._FOLLOWED_MARKERS):
                return True
            if time.monotonic() >= deadline:
                return False
            page_wait(page, 700, jitter=0.3)

    def follow_on_card(self, page, index: int) -> dict:
        """关注这张帖子的作者；不做 Connect / 不发邀请。"""
        try:
            cards = page.locator("div[role='listitem']")
            if index >= cards.count():
                return {"success": False, "error": f"卡片索引 {index} 超出范围"}
            card = self._card_locator(page, index)
            try:
                card.scroll_into_view_if_needed(timeout=5000)
                page_wait(page, 700, jitter=0.4)
            except Exception:
                pass

            button = self._first_in(card, self._FOLLOW_BTN_SELECTORS)
            if button is None:
                return {"success": False, "error": "没有关注按钮（可能已关注）"}

            label = ""
            try:
                label = (button.get_attribute("aria-label") or "").strip()
            except Exception:
                pass

            self.humanizer.human_click(button)
            if not self._wait_followed(page, card):
                shot = self.debug_shot(page, f"follow_not_confirmed_{index}")
                self.db.log(
                    self.name, "follow", "verify_failed",
                    {"target": label, "screenshot": shot,
                     "note": "点击关注后按钮状态未变，不计配额"},
                )
                return {
                    "success": False,
                    "error": "点击关注后状态未变，关注未生效",
                    "screenshot": shot,
                }

            url = ""
            degree = 0
            kind = ""
            name = ""
            try:
                actor = card.evaluate(self._ACTOR_JS) or {}
                url = str(actor.get("url") or "")
                kind = str(actor.get("kind") or "")
                name = str(actor.get("name") or "").splitlines()[0].strip() if actor.get("name") else ""
            except Exception:
                pass
            if kind == "person":
                try:
                    degree = int(card.evaluate(self._DEGREE_JS) or 0)
                except Exception:
                    degree = 0

            self.db.increment_quota(self.name, "follow")
            self.db.log(
                self.name, "follow", "success",
                {"target": label, "url": url, "degree": degree, "kind": kind, "author": name},
            )
            return {
                "success": True,
                "target": label,
                "url": url,
                "degree": degree,
                "kind": kind,
                "author": name,
            }

        except CircuitBreakerTripped as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"关注异常: {type(exc).__name__}: {exc}"}

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
