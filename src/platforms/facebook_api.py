"""Facebook 公共主页 Graph API 客户端（不发个人时间线，不依赖浏览器）。

为什么单独一个模块：
- 个人号自动化是封号高危区；公共主页有官方 API，走这里是合规且稳定的。
- 用 BM 系统用户 token 发帖，不依赖个人号每天登录；个人号只做管理员与人工互动。

配置（项目根目录 `.env` 或系统环境变量）：
    FB_PAGE_ID=1234567890
    FB_PAGE_TOKEN=EAAB...          # BM 系统用户长期 token，不要提交到仓库
    FB_GRAPH_VERSION=v27.0         # 可选，默认 v27.0

命令行：
    python -m src.platforms.facebook_api --check
    python -m src.platforms.facebook_api --post "你的第一条测试帖"
    python -m src.platforms.facebook_api --post "带链接的帖子" --link "https://example.com"
    python -m src.platforms.facebook_api --list-posts
    python -m src.platforms.facebook_api --comments POST_ID
    python -m src.platforms.facebook_api --reply COMMENT_ID --message "谢谢关注"

注意：
- 这是“主页”接口，不是个人主页；不要把个人号的 token 拿来发这里。
- 恢复期（7–14 天）不要跑；账号稳定、主页至少 2 个管理员后再用。
- token 只在内存里用，不写日志；错误信息会脱敏。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRAPH_VERSION = "v27.0"
DEFAULT_TIMEOUT_S = 30


class FacebookAPIError(RuntimeError):
    """Graph API 返回的 error 对象，保留 code/subcode 方便排查权限问题。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        error_type: str | None = None,
        fbtrace_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        return " | ".join(parts)


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取：只支持 KEY=VALUE，# 注释；已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class FacebookPageAPI:
    """最小可用的公共主页 API 客户端。"""

    def __init__(
        self,
        page_id: str,
        token: str,
        *,
        graph_version: str = DEFAULT_GRAPH_VERSION,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.page_id = str(page_id).strip()
        self.token = token.strip()
        self.graph_version = graph_version.strip() or DEFAULT_GRAPH_VERSION
        self.timeout_s = timeout_s
        self.base_url = f"https://graph.facebook.com/{self.graph_version}"
        if not self.page_id:
            raise ValueError("page_id 不能为空")
        if not self.token:
            raise ValueError("token 不能为空")

    # ---------- 构造 ----------
    @classmethod
    def from_env(cls, *, dotenv_path: Path | None = None) -> "FacebookPageAPI":
        _load_dotenv(dotenv_path or (ROOT / ".env"))
        page_id = os.environ.get("FB_PAGE_ID", "").strip()
        token = os.environ.get("FB_PAGE_TOKEN", "").strip()
        version = os.environ.get("FB_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip()
        missing = [
            name
            for name, value in (("FB_PAGE_ID", page_id), ("FB_PAGE_TOKEN", token))
            if not value
        ]
        if missing:
            raise FacebookAPIError(
                "缺少环境变量：" + "、".join(missing)
                + "。请在项目根目录 .env 或系统环境变量里配置（参考 .env.example）。"
            )
        return cls(page_id, token, graph_version=version)

    # ---------- 底层请求 ----------
    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        payload = dict(params or {})
        payload["access_token"] = self.token
        url = f"{self.base_url}/{path.lstrip('/')}"

        data: bytes | None = None
        if method == "GET":
            url = f"{url}?{urllib.parse.urlencode(payload)}"
        else:
            data = urllib.parse.urlencode(payload).encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            self._raise_from_body(raw, status=exc.code)
        except urllib.error.URLError as exc:
            raise FacebookAPIError(
                f"网络请求失败：{self._redact(str(exc.reason))}"
            ) from exc

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FacebookAPIError(
                f"Graph API 返回的不是 JSON：{self._redact(raw[:300])}"
            ) from exc

        if isinstance(body, dict) and body.get("error"):
            self._raise_from_body(raw, status=None)
        return body

    def _raise_from_body(self, raw: str, *, status: int | None) -> None:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise FacebookAPIError(
                f"Graph API 请求失败（HTTP {status}）：{self._redact(raw[:300])}"
            )
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            raise FacebookAPIError(
                f"Graph API 请求失败（HTTP {status}）：{self._redact(raw[:300])}"
            )
        message = str(error.get("message") or "未知错误")
        raise FacebookAPIError(
            self._redact(message),
            code=error.get("code"),
            subcode=error.get("error_subcode"),
            error_type=error.get("type"),
            fbtrace_id=error.get("fbtrace_id"),
        )

    # ---------- 业务方法 ----------
    def get_page(self) -> dict[str, Any]:
        """返回主页基础信息；也是 token/权限是否可用的最小验证。"""
        return self._request(
            "GET",
            self.page_id,
            {
                # 只取基础字段：避免因为没开 pages_read_engagement 而连 token 都验不了
                "fields": "id,name,username,link",
            },
        )

    def check(self) -> dict[str, Any]:
        page = self.get_page()
        return {
            "ok": True,
            "page_id": page.get("id"),
            "name": page.get("name"),
            "username": page.get("username"),
            "link": page.get("link"),
            "followers": page.get("followers_count"),
        }

    def create_post(
        self,
        message: str,
        *,
        link: str | None = None,
        published: bool = True,
        scheduled_publish_time: int | str | None = None,
    ) -> dict[str, Any]:
        """发一条主页帖子；scheduled_publish_time 给了就按定时发（published=false）。"""
        text = (message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        params: dict[str, Any] = {"message": text}
        if link:
            params["link"] = link.strip()

        if scheduled_publish_time is not None:
            params["published"] = "false"
            params["scheduled_publish_time"] = scheduled_publish_time
        else:
            params["published"] = "true" if published else "false"

        return self._request("POST", f"{self.page_id}/feed", params)

    def list_posts(self, *, limit: int = 10) -> list[dict[str, Any]]:
        body = self._request(
            "GET",
            f"{self.page_id}/posts",
            {
                "fields": "id,message,created_time,permalink_url",
                "limit": max(1, min(int(limit), 100)),
            },
        )
        return body.get("data", []) if isinstance(body, dict) else []

    def list_comments(self, post_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        body = self._request(
            "GET",
            f"{post_id}/comments",
            {
                "fields": "id,message,from,created_time,permalink_url",
                "limit": max(1, min(int(limit), 100)),
            },
        )
        return body.get("data", []) if isinstance(body, dict) else []

    def reply_to_comment(self, comment_id: str, message: str) -> dict[str, Any]:
        text = (message or "").strip()
        if not text:
            raise ValueError("message 不能为空")
        return self._request("POST", f"{comment_id}/comments", {"message": text})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Facebook 公共主页 Graph API 客户端（不操作个人号）"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="验证 page_id/token 是否能读主页")
    group.add_argument("--post", metavar="TEXT", help="发一条主页帖子")
    group.add_argument("--list-posts", action="store_true", help="列出最近的主页帖子")
    group.add_argument("--comments", metavar="POST_ID", help="列出一条帖子的评论")
    group.add_argument("--reply", metavar="COMMENT_ID", help="回复一条评论")
    parser.add_argument("--link", default="", help="--post 时可附带的链接")
    parser.add_argument("--message", default="", help="--reply 时的回复正文")
    parser.add_argument("--limit", type=int, default=10, help="列表条数，默认 10")
    args = parser.parse_args(argv)

    try:
        api = FacebookPageAPI.from_env()
        if args.check:
            result = api.check()
        elif args.post:
            result = api.create_post(args.post, link=args.link or None)
        elif args.list_posts:
            result = api.list_posts(limit=args.limit)
        elif args.comments:
            result = api.list_comments(args.comments, limit=args.limit)
        elif args.reply:
            if not args.message.strip():
                parser.error("--reply 需要同时给 --message")
            result = api.reply_to_comment(args.reply, args.message)
        else:
            parser.error("没有可执行的操作")
            return 2
    except (FacebookAPIError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
