"""主页发帖链路 dry-run：只验证「编辑器能打开、文字能输入、发布按钮会变可用」，
最后点「取消」丢弃草稿，**绝不点发布**。

用法（先 fb\\start_fb.bat 打开 9223 并保持登录）：
    .venv\\Scripts\\python.exe -B fb\\scripts\\test_page_post_dryrun.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.browser import BrowserManager
from src.config import Config
from src.database import Database
from src.humanize import Humanizer
from src.platforms.facebook import FacebookPlatform
from src.risk_control import RiskController

TEXT = "【自动化联通性测试】这条不会发布，稍后丢弃。"


def main() -> int:
    config = Config.load()
    db = Database(config.path("storage.database", "./data/social.db"))
    humanizer = Humanizer(config)
    risk = RiskController(config, db)
    page = None
    out: dict = {"ok": False}

    with BrowserManager(config) as browser:
        fb = FacebookPlatform(browser, humanizer, risk, db, config)
        try:
            page = fb.page(fb.composer_url())
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(1500)
            out["url"] = page.url

            editor = fb._bs_editor(page)
            out["editor_found"] = editor is not None
            if editor is None:
                fb.debug_shot(page, "dryrun_no_editor")
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 1

            editor.click(timeout=4000)
            page.wait_for_timeout(600)
            humanizer.human_type(editor, TEXT)
            page.wait_for_timeout(1200)

            publish = fb._bs_publish_button(page)
            out["publish_found"] = publish is not None
            if publish is not None:
                out["publish_aria_disabled"] = publish.get_attribute("aria-disabled")
            out["typed_ok"] = TEXT[:8] in page.inner_text("body")
            out["ok"] = bool(out["publish_found"]) and (
                (out.get("publish_aria_disabled") or "").lower() != "true"
            )

            # 丢弃草稿：点「取消」，若弹二次确认再点一次「取消/丢弃」
            cancel = FacebookPlatform.mark_button(page, ["取消", "Cancel"], "data-dsh-cancel")
            if cancel is not None:
                fb._bs_click(page, cancel)
                page.wait_for_timeout(1200)
                discard = FacebookPlatform.mark_button(
                    page, ["取消", "丢弃", "Discard"], "data-dsh-discard"
                )
                if discard is not None:
                    fb._bs_click(page, discard)
                    page.wait_for_timeout(800)
            out["cancelled"] = True
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
