"""只读检查 Facebook 登录态：连接已在 9223 上运行的 Chrome，不写数据库、不写 profile。

用法（先双击 fb\start_fb.bat，登录后保持 Chrome 开着）：
    .venv/Scripts/python.exe -B fb/scripts/fb_cdp_check.py

返回：
    0 = 已登录（有 c_user cookie）
    2 = 未登录 / 被重定向到登录页
    1 = 连不上 9223（Chrome 没开）
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9223"


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=5000)
        except Exception as exc:
            print(f"无法连接 {CDP_URL}：{type(exc).__name__}: {exc}")
            print("请先双击 start_fb.bat，确认 Chrome 窗口已打开。")
            return 1

        contexts = browser.contexts
        if not contexts:
            print("已连接，但没有浏览器上下文；Chrome 可能刚启动，重试一次。")
            return 2

        context = contexts[0]
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        c_user = cookies.get("c_user")

        fb_pages = [
            page for page in context.pages if "facebook.com" in (page.url or "")
        ]
        url = fb_pages[0].url if fb_pages else ""
        title = ""
        if fb_pages:
            try:
                title = fb_pages[0].title()
            except Exception:
                title = ""

        result = {
            "connected": True,
            "logged_in": bool(c_user),
            "user_id": c_user or "",
            "url": url,
            "title": title,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if not c_user:
            print("未检测到 c_user：请在 Chrome 窗口里完成 Facebook 登录。")
            return 2
        if any(part in url for part in ("/login", "/checkpoint", "/recover")):
            print("有 c_user 但页面在登录/验证路径，请人工完成验证。")
            return 2
        print("已登录。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
