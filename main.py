#!/usr/bin/env python3
"""社媒自动化运营系统 - 命令行入口。

用法：
  python main.py login --platform linkedin      # 首次人工登录，持久化 profile
  python main.py post --platform linkedin --content "内容"
  python main.py post --platform all --file content/posts.md
  python main.py run                             # 消费任务队列
  python main.py comment --count 3               # 自动挑话术评论 3 条
  python main.py comment --keyword "卫浴" --count 2  # 搜关键词后评论
  python main.py comment --url "<帖子地址>" --text "……"  # 指定帖子评论
  python main.py status                          # 查看配额与账号状态
  python main.py todos                           # 查看人工待办
  python main.py done --id 1                     # 我已手动发布，任务结单
  python main.py approve --id 1                  # 批准后交回机器发布
  python main.py browse --platform linkedin      # 纯浏览（无互动）
  python main.py chrome                         # 启动常驻 Chrome（任务结束不关）
  python main.py plan                           # 生成/查看今天的随机作息表
  python main.py tick                           # 执行已到点的时段
  python main.py daemon                         # 常驻：到点自动执行（推荐）
  python main.py keywords                       # 验证评论关键词能不能搜到相关帖
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.browser import BrowserManager
from src.config import Config, ROOT
from src.database import Database
from src.humanize import Humanizer, page_wait
from src.risk_control import RiskController
from src.scheduler import DayPlanner, Scheduler
from src.tasks.commenter import Commenter
from src.tasks.poster import Poster

PLATFORMS = ("linkedin", "facebook")


def build(argv: list[str] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="社媒自动化运营系统 v1.0（LinkedIn + Facebook）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="首次人工登录并保存会话")
    p_login.add_argument("--platform", choices=(*PLATFORMS, "all"), required=True)
    p_login.add_argument(
        "--wait", type=int, default=0,
        help="自动检测登录态的最长等待秒数；0 表示手动按 Enter 确认",
    )

    p_check = sub.add_parser("check", help="检查登录态是否有效")
    p_check.add_argument("--platform", choices=(*PLATFORMS, "all"), required=True)

    p_post = sub.add_parser("post", help="把帖子加入发送队列")
    p_post.add_argument("--platform", choices=(*PLATFORMS, "all"), required=True)
    p_post.add_argument("--content", help="帖子文本内容")
    p_post.add_argument("--file", help="从文件读取帖子文本（优先级低于 --content）")
    p_post.add_argument("--at", help="定时执行时间，ISO 格式如 2026-11-23T10:30:00")

    p_run = sub.add_parser("run", help="执行队列中的待发帖任务")
    p_run.add_argument("--max", type=int, default=20, help="本次最多处理任务数")

    p_comment = sub.add_parser("comment", help="给领英帖子发评论")
    p_comment.add_argument(
        "--platform", default="linkedin", choices=("linkedin",),
        help="评论模块暂只支持 linkedin",
    )
    p_comment.add_argument("--count", type=int, default=3, help="本次评论条数")
    p_comment.add_argument("--keyword", help="按关键词搜索帖子后评论")
    p_comment.add_argument("--url", help="指定帖子或列表页地址")
    p_comment.add_argument(
        "--text", help="指定评论内容；多条用 || 分隔，不填则从话术库自动挑",
    )
    p_comment.add_argument(
        "--no-human", action="store_true",
        help="本次跳过人工介入分流（仅用于你明确要一次跑完的场合）",
    )

    sub.add_parser("status", help="查看账号状态、当日配额与待办数量")

    sub.add_parser("todos", help="查看人工待办列表")

    p_done = sub.add_parser("done", help="待办已由你手动发布，直接结单")
    p_done.add_argument("--id", type=int, required=True, help="待办 ID")

    p_approve = sub.add_parser("approve", help="批准待办，交回机器发布")
    p_approve.add_argument("--id", type=int, required=True, help="待办 ID")

    p_browse = sub.add_parser("browse", help="纯浏览时段，不产生互动")
    p_browse.add_argument("--platform", choices=(*PLATFORMS, "all"), required=True)
    p_browse.add_argument("--minutes", type=float, help="浏览时长（分钟）")

    p_reset = sub.add_parser("reset", help="解除熔断冷却，账号状态恢复 healthy")
    p_reset.add_argument("--platform", choices=(*PLATFORMS, "all"), required=True)

    p_chrome = sub.add_parser(
        "chrome", help="启动常驻 Chrome（任务结束后不关闭，供你继续使用）"
    )
    p_chrome.add_argument("--platform", default="linkedin", choices=PLATFORMS)

    p_plan = sub.add_parser("plan", help="生成/查看随机作息表")
    p_plan.add_argument("--platform", default="linkedin", choices=PLATFORMS)
    p_plan.add_argument("--days", type=int, default=1, help="规划未来几天（默认今天）")

    p_tick = sub.add_parser("tick", help="执行已到点的时段（可配合系统计划任务）")
    p_tick.add_argument("--platform", default="linkedin", choices=PLATFORMS)
    p_tick.add_argument(
        "--force", action="store_true", help="跳过时间判断，立即执行下一个时段"
    )

    p_kw = sub.add_parser("keywords", help="验证评论关键词能搜到多少可评论的帖子")
    p_kw.add_argument("--platform", default="linkedin", choices=("linkedin",))
    p_kw.add_argument("--limit", type=int, default=0, help="最多测几个（0=全部）")

    p_daemon = sub.add_parser("daemon", help="常驻运行随机日程（推荐）")
    p_daemon.add_argument("--platform", default="linkedin", choices=PLATFORMS)
    p_daemon.add_argument("--poll", type=int, default=60, help="轮询间隔秒数")

    return parser


def resolve_platforms(value: str) -> list[str]:
    return list(PLATFORMS) if value == "all" else [value]


def read_contents(args: argparse.Namespace) -> list[str]:
    """读取待发内容。--file 支持用单独的 --- 行分隔多条帖子。"""
    if args.content:
        return [args.content.strip()]
    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"内容文件不存在: {path}")
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"\n\s*-{3,}\s*\n", text)
        items = [p.strip() for p in parts if p.strip()]
        if not items:
            raise ValueError(f"文件内容为空: {path}")
        return items
    raise ValueError("请提供 --content 或 --file")


def prune_debug_shots(config: Config, days: int = 30) -> None:
    """清理 N 天前的诊断截图。截图里是信息流内容，属于隐私敏感物，不该无限堆积。"""
    folder = config.path("storage.debug_dir", "./data/debug")
    if not folder.exists():
        return
    cutoff = time.time() - days * 86400
    for png in folder.glob("*.png"):
        try:
            if png.stat().st_mtime < cutoff:
                png.unlink()
        except OSError:
            continue


def main(argv: list[str] | None = None) -> int:
    args = build().parse_args(argv)
    config = Config.load()
    db = Database(config.path("storage.database", "./data/social.db"))
    humanizer = Humanizer(config)
    risk = RiskController(config, db)
    prune_debug_shots(config)

    if args.command == "status":
        pending_by_platform: dict[str, int] = {}
        for task in db.pending_tasks(limit=1000):
            pending_by_platform[task["platform"]] = (
                pending_by_platform.get(task["platform"], 0) + 1
            )
        for platform in PLATFORMS:
            state = db.account_status(platform)
            quota = db.get_quota(platform)
            print(f"\n[{platform}]")
            print(f"  账号状态  : {state['status']}")
            print(f"  今日发帖  : {quota['posts']}")
            print(f"  今日评论  : {quota['comments']}")
            print(f"  今日关注  : {quota['follows']}")
            print(f"  熔断至    : {state.get('circuit_break_until') or '无'}")
            print(f"  待执行任务: {pending_by_platform.get(platform, 0)}")
            print(f"  人工待办  : {db.pending_todo_count(platform, 'post')}")
        print("\n最近操作日志：")
        for log in db.recent_logs(10):
            print(f"  #{log['id']} {log['created_at']} {log['platform']} "
                  f"{log['action']} {log['status']}")
        return 0

    if args.command == "todos":
        todos = db.pending_todos()
        if not todos:
            print("当前没有人工待办。")
            return 0
        for todo in todos:
            detail = json.loads(todo["detail"] or "{}")
            body = detail.get("content") or detail.get("suggested_text") or ""
            print(f"\n[待办 #{todo['id']}] {todo['platform']} / {todo['action']}")
            print(f"  原因: {detail.get('reason', '-')}")
            if body:
                print(f"  内容: {body[:120]}")
            if detail.get("url"):
                print(f"  目标: {detail['url']}")
            if todo["action"] == "post":
                print(f"  我已手动发布 → python main.py done --id {todo['id']}")
                print(f"  批准由机器发 → python main.py approve --id {todo['id']}")
            else:
                print(f"  我已手动评论 → python main.py done --id {todo['id']}")
                print(f"  批准由机器发 → python main.py approve --id {todo['id']}")
        return 0

    if args.command in ("done", "approve"):
        # 先查出待办类型：评论和发帖的后续动作与提示文案不同
        todo_action = ""
        for todo in db.pending_todos():
            if todo["id"] == args.id:
                todo_action = todo["action"] or ""
                break
        ok = Poster(
            BrowserManager(config), humanizer, risk, db, config
        ).complete_human_todo(args.id, dispatch=(args.command == "approve"))
        if not ok:
            print("待办不存在或已完成。")
            return 1
        if args.command == "approve":
            if todo_action == "comment":
                print(f"待办 #{args.id} 已批准，评论任务重新入队，"
                      f"下次 `python main.py run` 由机器发布。")
            else:
                print(f"待办 #{args.id} 已批准，任务重新入队，下次 run 由机器发布。")
        else:
            print(f"待办 #{args.id} 已记为手动完成，任务结单。")
        return 0

    if args.command == "post":
        contents = read_contents(args)
        poster = Poster(BrowserManager(config), humanizer, risk, db, config)
        platforms = resolve_platforms(args.platform)
        ids = []
        for platform in platforms:
            for content in contents:
                ids.append(poster.enqueue(platform, content, args.at))
        print(
            f"已加入队列：{len(contents)} 条内容 × {len(platforms)} 个平台，"
            f"任务 ID: {ids}"
        )
        print("提示：运行 `python main.py run` 执行；当前配置可能要求人工确认。")
        return 0

    if args.command == "run":
        with BrowserManager(config) as browser:
            poster = Poster(browser, humanizer, risk, db, config)
            result = poster.run_pending(max_tasks=args.max)
            # 已批准的评论待办（approve 回队的）也在这里一起消费
            comment_result = Commenter(
                browser, humanizer, risk, db, config
            ).run_pending_tasks()
        if comment_result["processed"]:
            result["approved_comments"] = comment_result
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "login":
        with BrowserManager(config) as browser:
            poster = Poster(browser, humanizer, risk, db, config)
            for platform in resolve_platforms(args.platform):
                url = config.get(f"platforms.{platform}.base_url", "")
                page = browser.page(platform, url)
                print(f"\n=== {platform} ===", flush=True)
                print(f"已打开 {url}", flush=True)
                print("请在浏览器窗口中完成登录（含验证码 / 二次验证）。", flush=True)

                if args.wait > 0:
                    ok = wait_for_login(poster, platform, args.wait, page=page)
                else:
                    input("登录完成后按 Enter 保存会话并继续...")
                    ok = poster_is_logged_in(browser, humanizer, risk, db, config, platform)

                if ok:
                    risk.mark_healthy(platform)
                    print(f"{platform} 登录态已保存。", flush=True)
                else:
                    print(f"警告：{platform} 未检测到有效登录态，请重试。", flush=True)
                browser.close_platform(platform)
        return 0

    if args.command == "check":
        with BrowserManager(config) as browser:
            poster = Poster(browser, humanizer, risk, db, config)
            for platform in resolve_platforms(args.platform):
                ok = poster.platform(platform).is_logged_in()
                print(f"{platform}: {'登录态有效' if ok else '未登录或登录态失效'}")
        return 0

    if args.command == "comment":
        texts = (
            [t.strip() for t in args.text.split("||") if t.strip()]
            if args.text
            else None
        )
        if texts:
            args.count = max(args.count, len(texts))
        with BrowserManager(config) as browser:
            commenter = Commenter(browser, humanizer, risk, db, config)
            result = commenter.run(
                count=args.count,
                platform=args.platform,
                keyword=args.keyword,
                url=args.url,
                texts=texts,
                skip_human=args.no_human,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "keywords":
        keywords = list(config.get("schedule.comment_keywords", []) or [])
        if not keywords:
            print("config.yaml 的 schedule.comment_keywords 是空的")
            return 0
        if args.limit:
            keywords = keywords[: args.limit]
        print(f"逐个验证 {len(keywords)} 个关键词（每个约 10 秒）…\n")
        with BrowserManager(config) as browser:
            commenter = Commenter(browser, humanizer, risk, db, config)
            impl = commenter.platform(args.platform)
            for keyword in keywords:
                url = commenter.target_url(args.platform, keyword, None)
                page = browser.page(args.platform, url)
                page_wait(page, 4000)
                cards = impl.count_cards(page)  # type: ignore[attr-defined]
                good = checked = 0
                index = impl.find_commentable_index(page, 0)  # type: ignore[attr-defined]
                while index >= 0 and checked < 3:
                    text = impl.post_text(page, index)  # type: ignore[attr-defined]
                    if commenter.is_discussable(text):
                        good += 1
                    checked += 1
                    index = impl.find_commentable_index(page, index + 1)  # type: ignore[attr-defined]
                flag = "可用" if good else ("无相关帖" if cards else "搜不到内容")
                print(f"  {keyword:26} 卡片 {cards:2}  可评论 {good}/{checked}  {flag}")
        return 0

    if args.command == "plan":
        planner = DayPlanner(config, db, args.platform)
        for offset in range(max(1, args.days)):
            day = date.today() + timedelta(days=offset)
            sessions = planner.plan_day(day)
            label = "今天" if offset == 0 else ("明天" if offset == 1 else day.isoformat())
            if not sessions:
                print(f"\n{day} {label}：整天空窗（模拟真人今天不上）")
                continue
            print(f"\n{day} {label}：{len(sessions)} 个活跃时段")
            for session in sessions:
                actions = session.get("actions") or {}
                pretty = []
                if actions.get("browse_minutes"):
                    pretty.append(f"浏览 {actions['browse_minutes']} 分钟")
                if actions.get("comments"):
                    pretty.append(f"评论 {actions['comments']} 条")
                if actions.get("post"):
                    pretty.append("发帖 1 条")
                print(f"  {session['planned_at'][11:16]}  " + "，".join(pretty))
        return 0

    if args.command in ("tick", "daemon"):
        with BrowserManager(config) as browser:
            scheduler = Scheduler(config, db, browser, humanizer, risk)
            if args.command == "daemon":
                scheduler.daemon(args.platform, poll_seconds=args.poll)
                return 0
            scheduler.ensure_plan(args.platform)
            result = scheduler.tick(args.platform, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "chrome":
        browser = BrowserManager(config)
        try:
            port = browser.ensure_chrome(args.platform)
            print(f"Chrome 已就绪（{args.platform}，调试端口 {port}）")
            print("之后的自动化任务都会复用这个窗口，任务结束也不会关闭它。")
            print("如果这个命令执行完窗口就没了，请改用双击 start_chrome.bat。")
        finally:
            browser.close()
        return 0

    if args.command == "reset":
        for platform in resolve_platforms(args.platform):
            risk.mark_healthy(platform)
            print(f"{platform}: 已恢复 healthy，熔断冷却已解除。")
        return 0

    if args.command == "browse":
        with BrowserManager(config) as browser:
            for platform in resolve_platforms(args.platform):
                page = browser.page(
                    platform,
                    config.get(f"platforms.{platform}.feed_url", ""),
                )
                minutes = args.minutes or config.get("humanize.idle_browse_minutes", 5)
                print(f"{platform}: 开始 {minutes} 分钟纯浏览...")
                humanizer.idle_browse(page, minutes=minutes)
                print(f"{platform}: 浏览结束。")
        return 0

    return 1


def wait_for_login(poster: Poster, platform: str, timeout_s: int, page=None) -> bool:
    """轮询登录态，进入登录状态立即返回，不需要用户按 Enter。

    会识别"设备验证"页面并给出明确提示，避免用户以为程序卡死。
    """
    started = time.time()
    deadline = started + timeout_s
    last_report = -30
    device_check_hinted = False
    while time.time() < deadline:
        try:
            if poster.platform(platform).is_logged_in(fast=True):
                return True
        except Exception:
            pass

        # 识别领英设备验证页，提示用户去手机确认
        current_url = ""
        try:
            if page is not None and not page.is_closed():
                current_url = page.url
        except Exception:
            pass
        if "checkpoint" in current_url and not device_check_hinted:
            print(
                "  检测到设备验证：请打开手机上的领英 APP，点击「是」确认登录。",
                flush=True,
            )
            print(
                "  （页面上的「记住这台设备」保持勾选，确认一次以后就不用再验）",
                flush=True,
            )
            device_check_hinted = True

        elapsed = int(time.time() - started)
        if elapsed - last_report >= 30:
            remaining = max(0, int(deadline - time.time()))
            print(f"  等待登录中… 已等待 {elapsed}s，最长再等 {remaining}s", flush=True)
            last_report = elapsed
        time.sleep(3)
    return False


def poster_is_logged_in(browser, humanizer, risk, db, config, platform: str) -> bool:
    """登录命令内的轻量登录态检查。"""
    try:
        return Poster(browser, humanizer, risk, db, config).platform(platform).is_logged_in()
    except Exception:
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        # 浏览器内核缺失等环境问题，给出干净提示而不是堆栈
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
