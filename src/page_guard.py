"""公司主页创建资格的每日检查。

背景（2026-11-27 实测）：南京恒霖的 LinkedIn 公司主页建不了，向导第一步点“公司”
就报“出现错误”，抓包是

    POST /voyager/api/voyagerOrganizationDashEntryPageCreationForm
         ?action=validatePageCreationForViewer  ->  403 {"code":"4027"}

这不是脚本问题，是 LinkedIn 的账号资格门槛：官方口径要求先完成“工作场所验证”
（绑定与官网同域的企业邮箱并设为主要邮箱），且新账号通常要养几周才放行。

资格是**窗口性**的——放行过一次没建，过阵子可能又被收回。所以这里每天查一次：
一旦返回 200 就记日志、写标记文件，等人（或调用方）立刻去建，不要再等。

只读检查：只发这一个校验请求，不点任何“创建”按钮，不会误建。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

VALIDATE_PATH = (
    "/voyager/api/voyagerOrganizationDashEntryPageCreationForm"
    "?action=validatePageCreationForViewer"
)

# 用页面自身的会话发请求：带上 csrf-token（值就是 JSESSIONID）和凭据，
# 这样查到的结果和真人点按钮时看到的完全一致。
_CHECK_JS = r"""
async (path) => {
  const m = document.cookie.match(/JSESSIONID="?([^";]+)/);
  const csrf = m ? m[1] : '';
  try {
    const r = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'csrf-token': csrf,
        'content-type': 'application/json',
        'x-restli-protocol-version': '2.0.0'
      },
      body: '{}'
    });
    const text = await r.text();
    return {status: r.status, body: text.slice(0, 300)};
  } catch (e) {
    return {status: -1, body: String(e).slice(0, 200)};
  }
}
"""


class PageGuard:
    """每天查一次“能不能建公司主页”。"""

    META_KEY = "page_guard.last_check"

    def __init__(self, config: Any, db: Any, browser: Any) -> None:
        self.config = config
        self.db = db
        self.browser = browser

    # ---------- 对外 ----------
    def due_today(self, platform: str = "linkedin") -> bool:
        """今天还没查过就返回 True（守护进程每分钟调用，靠这个去重）。"""
        return self.db.get_meta(self._key(platform)) != date.today().isoformat()

    def check(self, platform: str = "linkedin", force: bool = False) -> dict:
        """执行一次检查，返回 {status, eligible, code, checked}。"""
        if not force and not self.due_today(platform):
            return {"checked": False, "reason": "今天已经查过"}

        result = {"checked": True, "platform": platform, "eligible": False,
                  "status": None, "code": None}
        try:
            page = self.browser.page(platform, self._entry_url(platform))
            raw = page.evaluate(_CHECK_JS, VALIDATE_PATH) or {}
            status = int(raw.get("status") or 0)
            body = str(raw.get("body") or "")
            result["status"] = status
            result["code"] = self._extract_code(body)
            # 200 = 放行；403/4027 = 还没资格；其他状态码按“未知”记录，不误判为可建
            result["eligible"] = status == 200
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"

        self.db.set_meta(self._key(platform), date.today().isoformat())
        self.db.log(
            platform, "page_guard",
            "eligible" if result["eligible"] else "not_eligible",
            {k: v for k, v in result.items() if k != "checked"},
        )
        if result["eligible"]:
            self._write_flag(platform, result)
        return result

    # ---------- 内部 ----------
    @staticmethod
    def _key(platform: str) -> str:
        return f"{PageGuard.META_KEY}.{platform}"

    def _entry_url(self, platform: str) -> str:
        base = self.config.get(f"platforms.{platform}.base_url", "https://www.linkedin.com")
        return f"{base.rstrip('/')}/feed/"

    @staticmethod
    def _extract_code(body: str) -> str | None:
        try:
            data = json.loads(body)
            return str((data.get("data") or {}).get("code") or "") or None
        except Exception:
            return None

    @staticmethod
    def _write_flag(platform: str, result: dict) -> None:
        """放行时留一个标记文件，方便人和其它脚本发现。"""
        try:
            path = Path("data") / f"page_eligible.{platform}.json"
            path.write_text(
                json.dumps({"platform": platform, **result},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            pass
