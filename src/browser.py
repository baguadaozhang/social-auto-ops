"""浏览器管理：有头模式 + 持久化 profile，每个平台独立会话。

支持两种模式：
- persistent：Playwright 自己启动浏览器，脚本退出时浏览器随之关闭
- cdp：程序拉起一个常驻 Chrome 并通过调试端口接入，脚本退出后浏览器继续开着
       （用户可以继续用这个窗口，下次任务直接复用）
"""
from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅在类型检查时导入，轻量命令无需安装 playwright
    from playwright.sync_api import BrowserContext, Page

from .humanize import page_wait

# 常用 User-Agent 候选，启动时随机选一个，避免所有账号 UA 一致
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


class BrowserManager:
    """管理 Playwright 持久化上下文。

    profile 目录保存 cookie / localStorage / 登录态。
    首次使用需运行 login 命令由人工登录一次，之后自动复用。
    """

    def __init__(self, config: Any):
        self.config = config
        self._pw = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}
        self._cdp_browsers: dict[str, Any] = {}
        self.headless = bool(config.get("browser.headless", False))
        self.timeout = int(config.get("browser.timeout_ms", 30000))
        self.mode = str(config.get("browser.mode", "persistent")).lower()
        self.keep_alive = self.mode == "cdp"

    # ---------- Chrome 常驻（CDP 模式） ----------
    def debug_port(self, platform: str) -> int:
        default = 9222 if platform == "linkedin" else 9223
        return int(self.config.get(f"browser.debug_ports.{platform}", default))

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.6)
                return sock.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False

    def chrome_executable(self) -> str:
        """定位 Chrome 可执行文件：配置优先，其次常见安装路径。"""
        configured = str(self.config.get("browser.executable", "") or "").strip()
        if configured and Path(configured).exists():
            return configured
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        found = shutil.which("chrome") or shutil.which("chrome.exe")
        return found or ""

    def ensure_chrome(self, platform: str) -> int:
        """确保该平台的 Chrome 在调试端口上运行，返回端口。"""
        return self._ensure_chrome(platform)

    def _ensure_chrome(self, platform: str) -> int:
        """确保目标调试端口上有 Chrome 在跑；没有就拉起来。返回端口。"""
        port = self.debug_port(platform)
        if self._port_open(port):
            return port
        executable = self.chrome_executable()
        if not executable:
            raise RuntimeError(
                "找不到 Chrome 可执行文件，请在 config.yaml 的 browser.executable 填写完整路径"
            )
        profile = self.profile_dir(platform)
        args = [
            executable,
            f"--remote-debugging-port={port}",
            # 显式只监听本机回环：调试端口一旦暴露，本机任何进程都能完全控制
            # 这个已登录的浏览器（读 cookie、以你的身份发帖）
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ]
        # 关键：必须让 Chrome 脱离脚本的进程树，否则命令行一结束浏览器就被回收。
        # Windows 下用 `cmd /c start` 让 Chrome 成为孤儿进程最可靠。
        popen_kwargs: dict[str, Any] = {
            "close_fds": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            shell_args = ["cmd", "/c", "start", "", *args]
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
            try:
                subprocess.Popen(shell_args, **popen_kwargs)
            except OSError:
                subprocess.Popen(shell_args, close_fds=True)
        else:
            popen_kwargs["start_new_session"] = True
            subprocess.Popen(args, **popen_kwargs)
        # 等调试端口就绪
        for _ in range(80):
            if self._port_open(port):
                return port
            time.sleep(0.5)
        raise RuntimeError(
            f"Chrome 启动超时（端口 {port}）。可能已有 Chrome 占用同一配置目录，"
            "请先关闭它再重试。"
        )

    @staticmethod
    def _context_alive(context: "BrowserContext") -> bool:
        """探活：Chrome 被手动关掉后，缓存的 context 访问任何属性都会抛异常。"""
        try:
            _ = context.pages
            return True
        except Exception:
            return False

    def _invalidate(self, platform: str) -> None:
        """清掉某个平台的所有缓存连接（CDP 断连后调用，下次访问时重建）。"""
        self._contexts.pop(platform, None)
        self._pages.pop(platform, None)
        browser = self._cdp_browsers.pop(platform, None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    def _cdp_context(self, platform: str) -> "BrowserContext":
        self.start()
        if platform in self._contexts:
            cached = self._contexts[platform]
            if self._context_alive(cached):
                return cached
            # Chrome 中途被关掉/重启过：旧连接已死，清缓存后重新接入
            self._invalidate(platform)
        port = self._ensure_chrome(platform)
        browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        self._cdp_browsers[platform] = browser
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.set_default_timeout(self.timeout)
        self._contexts[platform] = context
        return context

    def profile_dir(self, platform: str) -> Path:
        path = self.config.path(f"browser.profiles.{platform}", f"./profiles/{platform}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start(self) -> "BrowserManager":
        if self._pw is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "浏览器模块未安装。请运行: python -m pip install -r requirements.txt "
                    "&& python -m playwright install chromium"
                ) from exc
            self._pw = sync_playwright().start()
        return self

    def context(self, platform: str) -> "BrowserContext":
        """获取/创建平台的浏览器上下文。"""
        self.start()
        if platform in self._contexts:
            cached = self._contexts[platform]
            if self._context_alive(cached):
                return cached
            # 缓存的 context 已失效（Chrome 被关过），清掉重建
            self._invalidate(platform)
        if self.mode == "cdp":
            return self._cdp_context(platform)
        profile = self.profile_dir(platform)
        # channel 指定系统浏览器内核（msedge / chrome）；留空则用 Playwright 自带 chromium
        channel = str(self.config.get("browser.channel", "") or "").strip()
        launch_kwargs: dict[str, Any] = {}
        if channel:
            launch_kwargs["channel"] = channel
        else:
            # 用自带 chromium 时才伪造 UA；用系统内核时保持原生 UA 更自然
            launch_kwargs["user_agent"] = random.choice(_USER_AGENTS)
        try:
            context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
                **launch_kwargs,
            )
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                raise RuntimeError(
                    "未安装 Chromium 浏览器内核。请先运行：\n"
                    "  python -m playwright install chromium"
                ) from exc
            raise
        context.set_default_timeout(self.timeout)
        self._contexts[platform] = context
        return context

    def page(self, platform: str, url: str | None = None) -> "Page":
        """获取平台页面；可选导航到指定 URL。

        CDP 断连（Chrome 被手动关闭）时自动清缓存重连一次，
        保证常驻 daemon 能自愈而不是此后每个任务都失败。
        """
        try:
            return self._page(platform, url)
        except Exception:
            self._invalidate(platform)
            return self._page(platform, url)

    def _page(self, platform: str, url: str | None = None) -> "Page":
        context = self.context(platform)
        if platform not in self._pages or self._pages[platform].is_closed():
            self._pages[platform] = context.pages[0] if context.pages else context.new_page()
        page = self._pages[platform]
        if url:
            page.goto(url, wait_until="domcontentloaded")
            page_wait(page, 1500)
        return page

    def close_platform(self, platform: str) -> None:
        context = self._contexts.pop(platform, None)
        self._pages.pop(platform, None)
        self._cdp_browsers.pop(platform, None)
        # CDP 模式下浏览器是用户共用的，只断开引用，绝不关闭窗口
        if context and not self.keep_alive:
            try:
                context.close()
            except Exception:
                pass

    def close(self) -> None:
        try:
            for platform in list(self._contexts):
                try:
                    self.close_platform(platform)
                except Exception:
                    pass
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    def describe(self) -> str:
        """给用户看的模式说明。"""
        if self.keep_alive:
            return "Chrome 保持运行（任务结束后不关闭，下次直接复用）"
        return "浏览器随任务关闭"

    def __enter__(self) -> "BrowserManager":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        kept = self.keep_alive
        self.close()
        if kept:
            print(
                "\nChrome 窗口保持打开，你可以继续使用；下次任务会直接复用这个窗口。",
                flush=True,
            )
