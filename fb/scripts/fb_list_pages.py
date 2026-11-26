"""只读列出当前登录账号身份下的 Facebook 主页（Page）及主页 ID。

用法（先双击 fb\\start_fb.bat，登录后保持 Chrome 开着）：
    .venv/Scripts/python.exe -B fb/scripts/fb_list_pages.py

做的事：
  1. 连接 9223 上已在运行的 Chrome（复用已登录的 profile）；
  2. 新开一个标签页访问 facebook.com/pages/?category=your_pages（只读页面）；
  3. 抓主页名称与链接，尽量解析出数字 Page ID；
  4. 关闭这个标签页，不动原来的窗口。

不点任何按钮、不发布内容、不写数据库。
"""
from __future__ import annotations

import json
import re
import sys

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9223"
PAGES_URL = "https://www.facebook.com/pages/?category=your_pages"

JS_COLLECT = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!/facebook\.com/.test(href)) continue;
    const text = (a.innerText || '').trim().replace(/\s+/g, ' ');
    if (!text || text.length > 80) continue;
    // 主页链接：/pages/<slug>/<id> 或 /profile.php?id=<id>
    const m1 = href.match(/\/pages\/[^/]+\/(\d+)/);
    const m2 = href.match(/[?&]id=(\d+)/);
    const id = m1 ? m1[1] : (m2 ? m2[1] : '');
    const key = text + '|' + (id || href);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name: text, id, href: href.split('?')[0] });
  }
  return out;
}
"""


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=5000)
        except Exception as exc:
            print(f"无法连接 {CDP_URL}：{type(exc).__name__}: {exc}")
            print("请先双击 fb\\start_fb.bat，确认 Chrome 窗口已打开。")
            return 1

        if not browser.contexts:
            print("已连接，但没有浏览器上下文；Chrome 可能刚启动，重试一次。")
            return 2

        context = browser.contexts[0]
        if not any(c["name"] == "c_user" for c in context.cookies()):
            print("未检测到 c_user，请先在 Chrome 里登录 Facebook。")
            return 2

        candidates: list[dict] = []
        body = ""
        page = context.new_page()
        try:
            page.goto(PAGES_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            candidates = page.evaluate(JS_COLLECT)
            body = page.inner_text("body")[:1500]
        except Exception as exc:
            body = f"(打开 your_pages 失败：{type(exc).__name__}: {exc})"
        finally:
            try:
                page.close()
            except Exception:
                pass

        # 兜底：直接扫已打开标签页里的主页入口（左侧“快速访问列表”/顶部切换器）
        if not [c for c in candidates if c.get("id")]:
            for tab in context.pages:
                if "facebook.com" not in (tab.url or ""):
                    continue
                try:
                    candidates += tab.evaluate(JS_COLLECT)
                except Exception:
                    continue

    pages = [c for c in candidates if c.get("id")]
    print(json.dumps({"count": len(pages), "pages": pages}, ensure_ascii=False, indent=2))

    if not pages:
        print("\n没抓到带数字 ID 的主页链接。页面可见文本片段如下，可据此人工确认：")
        print(body)
        return 3

    print("\n把上面的 id 作为 FB_PAGE_ID 候选；确认是“你以管理员身份管理的主页”。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
