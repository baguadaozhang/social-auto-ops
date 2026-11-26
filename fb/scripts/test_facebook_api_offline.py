"""离线测试 facebook_api：不发真实请求，用假的 urlopen 验证 URL/参数/错误解析。

运行（从项目根目录）：
    .venv/Scripts/python.exe -B fb/scripts/test_facebook_api_offline.py
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.platforms.facebook_api import FacebookAPIError, FacebookPageAPI  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _capture(calls: list, payload: dict):
    def fake_urlopen(request, timeout=30):
        calls.append(request)
        return FakeResponse(payload)

    return fake_urlopen


def test_create_post():
    calls: list = []
    api = FacebookPageAPI("123", "tok")
    with mock.patch("urllib.request.urlopen", _capture(calls, {"id": "123_456"})):
        result = api.create_post("hello world")
    assert result["id"] == "123_456"
    req = calls[0]
    assert req.full_url == "https://graph.facebook.com/v27.0/123/feed"
    assert req.get_method() == "POST"
    body = req.data.decode("utf-8")
    assert "message=hello+world" in body
    assert "access_token=tok" in body


def test_scheduled_post():
    calls: list = []
    api = FacebookPageAPI("123", "tok")
    with mock.patch("urllib.request.urlopen", _capture(calls, {"id": "123_789"})):
        api.create_post("later", scheduled_publish_time=1767225600)
    body = calls[0].data.decode("utf-8")
    assert "published=false" in body
    assert "scheduled_publish_time=1767225600" in body


def test_reply_comment():
    calls: list = []
    api = FacebookPageAPI("123", "tok")
    with mock.patch("urllib.request.urlopen", _capture(calls, {"id": "c2"})):
        api.reply_to_comment("c1", "thanks")
    assert calls[0].full_url == "https://graph.facebook.com/v27.0/c1/comments"
    assert "message=thanks" in calls[0].data.decode("utf-8")


def test_check():
    calls: list = []
    api = FacebookPageAPI("123", "tok")
    with mock.patch(
        "urllib.request.urlopen",
        _capture(calls, {"id": "123", "name": "Demo Page", "username": "demo"}),
    ):
        result = api.check()
    assert result["ok"] is True
    assert result["name"] == "Demo Page"
    assert "fields=id%2Cname%2Cusername%2Clink" in calls[0].full_url


def test_error_parsing():
    api = FacebookPageAPI("123", "tok")
    payload = {
        "error": {
            "message": "Invalid OAuth access token.",
            "type": "OAuthException",
            "code": 190,
            "fbtrace_id": "abc",
        }
    }
    http_error = urllib.error.HTTPError(
        "https://graph.facebook.com/v27.0/123",
        400,
        "Bad Request",
        None,
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )

    def fake_urlopen(request, timeout=30):
        raise http_error

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        try:
            api.get_page()
        except FacebookAPIError as exc:
            assert exc.code == 190
            assert "Invalid OAuth" in str(exc)
        else:
            raise AssertionError("应当抛出 FacebookAPIError")


def test_empty_message():
    api = FacebookPageAPI("123", "tok")
    try:
        api.create_post("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("空 message 应当抛 ValueError")


if __name__ == "__main__":
    for fn in (
        test_create_post,
        test_scheduled_post,
        test_reply_comment,
        test_check,
        test_error_parsing,
        test_empty_message,
    ):
        fn()
        print(f"ok  {fn.__name__}")
    print("all offline tests passed")
