"""LinkedIn 平台适配器 - 第一版仅实现发帖。"""
from __future__ import annotations

import time
from pathlib import Path

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
    supports_media = True  # 支持发帖附带图片/视频
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

    # ---------- 发帖媒体（图片 / 视频）----------
    # 真实结构（2026-11-25 在活账号上探明）：
    #   入口是 button[aria-label='媒体文件']，点下去**直接**弹系统文件选择器，没有二级菜单；
    #   隐藏 input 的 accept 覆盖 jpg/png/gif/webp/avif/bmp + video/*，可多选。
    #   上传完成后编辑器里出现 src 为 blob: 且 naturalWidth>0 的预览图、进度条消失；
    #   此时「发布」按钮仍可能是 disabled，因为正文还没填——所以顺序必须是
    #   先传媒体、再填文字、最后发布。
    _MEDIA_BTN_SELECTORS = [
        "button[aria-label='媒体文件']",
        "button[aria-label='Media']",
        "button[aria-label='Add a photo or video']",
        "button[aria-label*='照片或视频']",
        "button[aria-label*='photo or video']",
    ]
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
    _VIDEO_EXTS = {".mp4", ".mov", ".m4v"}

    _MEDIA_STATE_JS = r"""
    () => {
      const imgs = [...document.querySelectorAll('img')].filter(i => (i.src||'').startsWith('blob:'));
      const vids = [...document.querySelectorAll('video')].filter(v => (v.src||'').startsWith('blob:'));
      const progress = document.querySelectorAll('[role="progressbar"]').length;
      const txt = document.body.innerText || '';
      const processing = /处理中|正在处理|上传中|Processing|Uploading/.test(txt);
      return {
        blob_imgs: imgs.length,
        loaded_imgs: imgs.filter(i => i.naturalWidth > 0 && i.naturalHeight > 0).length,
        blob_videos: vids.length,
        videos_ready: vids.filter(v => (v.readyState || 0) >= 1).length,
        progress: progress,
        processing: processing
      };
    }
    """

    def _classify_media(self, paths: list[str]) -> tuple[list[str], list[str], list[str]]:
        """把待发文件分成图片/视频，并做本地校验。返回 (images, videos, errors)。

        先在本地拦掉明显不合法的输入，别让浏览器白传一遍才发现不行。
        """
        images: list[str] = []
        videos: list[str] = []
        errors: list[str] = []
        allow_image = bool(self.config.get("post.media.allow_image", True))
        allow_video = bool(self.config.get("post.media.allow_video", True))
        max_image_mb = float(self.config.get("post.media.max_image_mb", 20))
        max_video_mb = float(self.config.get("post.media.max_video_mb", 200))
        max_images = int(self.config.get("post.media.max_images", 9))

        for raw in paths:
            if not raw:
                continue
            fp = Path(raw).expanduser()
            if not fp.is_absolute():
                fp = Path(self.config.root) / fp
            if not fp.is_file():
                errors.append(f"文件不存在: {raw}")
                continue
            ext = fp.suffix.lower()
            size_mb = fp.stat().st_size / (1024 * 1024)
            if ext in self._IMAGE_EXTS:
                if not allow_image:
                    errors.append(f"已关闭图片发布: {fp.name}")
                    continue
                if size_mb > max_image_mb:
                    errors.append(f"图片超过 {max_image_mb:g}MB: {fp.name}")
                    continue
                images.append(str(fp))
            elif ext in self._VIDEO_EXTS:
                if not allow_video:
                    errors.append(f"已关闭视频发布: {fp.name}")
                    continue
                if size_mb > max_video_mb:
                    errors.append(f"视频超过 {max_video_mb:g}MB: {fp.name}")
                    continue
                videos.append(str(fp))
            else:
                errors.append(f"不支持的文件类型: {fp.name}")

        if len(images) > max_images:
            errors.append(f"图片最多 {max_images} 张，收到 {len(images)} 张")
        if videos and images:
            errors.append("一次只能发图片或视频，不能混发")
        if len(videos) > 1:
            errors.append(f"视频一次只能 1 个，收到 {len(videos)} 个")
        return images, videos, errors

    def _media_state(self, page) -> dict:
        try:
            st = page.evaluate(self._MEDIA_STATE_JS) or {}
        except Exception:
            return {"ready": False}
        ready = (
            st.get("progress", 0) == 0
            and not st.get("processing", False)
            and (st.get("loaded_imgs", 0) >= 1 or st.get("videos_ready", 0) >= 1)
        )
        return {**st, "ready": ready}

    def _attach_media(self, page, files: list[str], timeout_s: float = 180.0) -> dict:
        """点「媒体文件」把文件塞进编辑器，并等上传/处理完成。

        只负责"文件挂上去且处理完"，正文仍由调用方输入（顺序不能反）。
        视频处理比图片慢得多，所以给足超时，并要求"连续两次检测都就绪"才算完成，
        避免正好在进度条消失的那一瞬误判。
        """
        button = self._first_in(page, self._MEDIA_BTN_SELECTORS)
        if button is None:
            return {"ok": False, "error": "未找到媒体按钮（界面可能已改版）"}
        try:
            with page.expect_file_chooser(timeout=8000) as chooser_info:
                self.humanizer.human_click(button)
            chooser = chooser_info.value
            chooser.set_files(files)
        except Exception as exc:
            return {"ok": False, "error": f"打开文件选择器失败: {type(exc).__name__}: {exc}"}

        deadline = time.monotonic() + timeout_s
        settled = 0
        last: dict = {}
        while time.monotonic() < deadline:
            last = self._media_state(page)
            if last.get("ready"):
                settled += 1
                if settled >= 2:
                    return {"ok": True, "state": last}
            else:
                settled = 0
            page_wait(page, 1200, jitter=0.3)
        return {
            "ok": False,
            "error": f"媒体处理超时（{int(timeout_s)}s 内未就绪）",
            "state": last,
        }

    _PURGE_JS = r"""
    () => {
      const ds = [...document.querySelectorAll('dialog[open]')];
      if (!ds.length) return {open: 0};
      // 注意：不能拿整个弹窗的 innerText 判空——它必然含有「媒体文件」「动态」
      // 等界面文字。只看正文框里有没有人写的东西。
      const has_media = document.querySelectorAll(
        'dialog[open] img[src^="blob:"], dialog[open] video[src^="blob:"]'
      ).length > 0;
      const ed = document.querySelector('dialog[open] div[contenteditable="true"]');
      const text = ed ? (ed.innerText || "").trim() : "";
      return {open: ds.length, text_len: text.length, has_media: has_media};
    }
    """
    _DISCARD_JS = r"""
    () => {
      const ds = [...document.querySelectorAll('dialog[open]')];
      for (let i = ds.length - 1; i >= 0; i--) {
        const d = ds[i];
        // 有内容时会弹「保存为草稿 / 放弃」，先点放弃
        for (const b of d.querySelectorAll('button')) {
          const t = (b.innerText || "").trim();
          if (t === '放弃' || /Discard/.test(b.getAttribute('aria-label') || '')) {
            b.click(); return 'discard';
          }
        }
        // 空窗口点「关闭」会直接关掉，不需要确认
        for (const b of d.querySelectorAll('button')) {
          const s = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim();
          if (/关闭|Dismiss/.test(s)) { b.click(); return 'close'; }
        }
      }
      return '';
    }
    """

    def _purge_leftover_composer(self, page) -> dict:
        """处理上一次失败留下的发布窗口。

        2026-11-25 踩过：一次演练中途抛异常，发帖弹窗（原生 <dialog>）没被关掉，
        它会把后面每次点击都拦掉，导致连续多次"发帖失败"，而真正原因只是残留窗口。
        这里做两件事：
          - 空窗口（没有文字也没有媒体）直接丢弃，没有任何东西会丢；
          - 有内容的窗口不碰——可能是人正在写的草稿，宁可这次不发也不覆盖人的内容。
        """
        try:
            state = page.evaluate(self._PURGE_JS) or {}
        except Exception:
            return {"ok": True}
        if not state.get("open"):
            return {"ok": True}
        if state.get("text_len", 0) > 0 or state.get("has_media"):
            return {
                "ok": False,
                "error": "检测到未关闭的发布窗口且里面有内容，为避免覆盖人工草稿，本次不发布",
            }
        # 可能有两层（发布框 + 「保存为草稿?」确认框），要循环关
        for _ in range(6):
            try:
                if not (page.evaluate(self._PURGE_JS) or {}).get("open"):
                    break
                action = page.evaluate(self._DISCARD_JS)
            except Exception:
                action = ""
            page_wait(page, 1000)
            if not action:
                # 没有可点的关闭按钮，退到 Esc
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                page_wait(page, 800)
        still = False
        try:
            still = bool((page.evaluate(self._PURGE_JS) or {}).get("open"))
        except Exception:
            pass
        if still:
            return {"ok": False, "error": "残留的发布窗口无法关闭，本次不发布"}
        self.db.log(self.name, "post", "purged_leftover", {"note": "清理了上次遗留的空发布窗口"})
        return {"ok": True, "purged": True}

    # 账号级拦截页的 URL 特征（2026-11-27 实测：登录态还在，但所有页面都被
    # 重定向到 /check/china/add-phone，页面不渲染内容，脚本会误判成 DOM 改版）
    _GATE_URL_PATTERNS = ("/check/", "add-phone", "checkpoint", "authwall")

    def detect_blocker(self, page) -> str | None:
        """识别账号级拦截页，返回给人类看的原因；没有拦截返回 None。

        只读 URL 和页面标题里极少量的文案，不做任何绕行尝试——这类拦截
        只能由人工在真实浏览器里完成验证。
        """
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        low = url.lower()
        for pattern in self._GATE_URL_PATTERNS:
            if pattern in low:
                short = url.split("?")[0]
                return (
                    f"账号被拦在验证页（{short}）：需要人工在浏览器里完成验证，"
                    "脚本不会绕过"
                )
        return None

    def post(self, content: str, media: list[str] | None = None) -> dict:
        """发帖流程：安全检查 → 热身 → 打开编辑器 → [上传媒体] → 拟真输入 → 发布。"""
        try:
            self._precheck()
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            return self._on_failure(content, str(exc))

        try:
            # 浏览器启动 / 页面加载失败也要作为任务失败处理，不能中断整个队列
            page = self.page()
            # 页面级风控扫描
            self.risk.check_page(page, self.name)

            # 账号级拦截页（补手机号 / checkpoint / 登录墙）先判掉。
            # 2026-11-27 那次就是栽在这：所有页面被重定向到 /check/china/add-phone，
            # 页面一片空白，脚本却报「未找到发帖入口，页面结构可能已改版」，
            # 看上去像 DOM 改版，实际是账号被卡在验证流程里，白白浪费排查时间。
            blocker = self.detect_blocker(page)
            if blocker:
                self.debug_shot(page, "blocked_gate")
                return self._on_failure(content, blocker)

            # 会话热身：先像人一样逛一会儿
            self.humanizer.warmup(page)

            # 先处理上次失败留下的发布窗口，否则它会把后续所有点击拦掉
            purged = self._purge_leftover_composer(page)
            if not purged.get("ok"):
                return self._on_failure(content, str(purged.get("error")))

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
            # 顺序很关键：必须「先写正文、再挂媒体」。
            # 2026-11-25 在活账号上实测：图片上传后预览图会盖住正文框（正文框只有 32px 高），
            # 此时再点正文框会被 <img alt="图片预览"> 拦截，文字根本输不进去，
            # 于是「动态」按钮一直 disabled、发帖必然失败。反过来先写正文再挂媒体，
            # 实测正文保留、按钮仍可用，是可行顺序。
            self.humanizer.human_type(editor, content)

            media_note: dict = {}
            if media:
                images, videos, errors = self._classify_media(media)
                if errors:
                    return self._on_failure(content, "媒体校验未通过: " + "；".join(errors))
                files = images or videos
                attached = self._attach_media(page, files)
                if not attached.get("ok"):
                    self.debug_shot(page, "media_upload_failed")
                    return self._on_failure(
                        content, f"媒体上传失败: {attached.get('error')}"
                    )
                media_note = {
                    "media_kind": "image" if images else "video",
                    "media_count": len(files),
                    "media_files": [Path(f).name for f in files],
                }

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
            return self._on_success(content, url, extra=media_note or None)

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

    def wait_cards(self, page, timeout_s: float = 15.0) -> int:
        """等信息流/搜索结果真正把卡片渲染出来，返回卡片数。

        2026-11-26 10:11 和 11-27 10:43 两个上午时段都是 cards_found=0：
        页面 readyState 已经 complete，但结果卡还没插进 DOM，数一次就是 0，
        关注步骤整段空转。所以不能只数一次，要给它渲染时间；超时再刷新一次。
        """
        deadline = time.time() + timeout_s
        count = 0
        while time.time() < deadline:
            count = self.count_cards(page)
            if count > 0:
                return count
            page.wait_for_timeout(500)
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return count
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            count = self.count_cards(page)
            if count > 0:
                return count
            page.wait_for_timeout(500)
        return count

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
