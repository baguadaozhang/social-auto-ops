# 目录迁移记录（2026-11-26）

## 目标

把混在根目录的 Facebook / LinkedIn 专属内容分到 `fb/` 与 `LinkedIn/` 两个平台目录；
共享入口、运行目录、启动脚本保持在根目录，保证 `main.py`、`config.yaml`、`profiles/`、`data/` 的路径不变。

## 分类原则

1. **平台专属的文档、内容、手工脚本、可独立移动的启动脚本** → 对应平台目录。
2. **被 `main.py` / `src/` 导入的代码** → 留在 `src/`（改路径会破坏导入）。
3. **`config.yaml` 引用的运行路径**（`profiles/`、`data/`、`content/`）→ 留在根目录。
4. **启动/自启动入口**（`start_all.bat`、`start_chrome.bat`、`daemon_bg.bat`、`install_autostart.bat`）→ 留在根目录，避免开机自启失效。
5. **两平台共用**（`posts.md`、`media/`、数据库、浏览器 profile 根目录）→ 留在根目录。

## 分类清单

| 项目 | 归属 | 实际位置 | 说明 |
|---|---|---|---|
| `main.py` | Common | 根 | CLI 入口，所有平台共用 |
| `config.yaml` | Common | 根 | 两平台配置与路径 |
| `requirements.txt` | Common | 根 | 依赖 |
| `README.md` | Common | 根 | 项目总览，已加“目录分工” |
| `LICENSE` | Common | 根 | 许可 |
| `.gitignore` | Common | 根 | 忽略规则 |
| `.env.example` | Common | 根 | `facebook_api.py` 从根 `.env` 读配置 |
| `src/` | Common | 根 | 核心包；平台适配器/API 客户端被导入，不能移 |
| `src/platforms/linkedin.py` | LinkedIn（代码） | `src/platforms/` | 被 `src/tasks/*` 导入 |
| `src/platforms/facebook.py` | Facebook（代码） | `src/platforms/` | 被 `src/tasks/poster.py` 导入 |
| `src/platforms/facebook_api.py` | Facebook（代码） | `src/platforms/` | 导入路径 `src.platforms.facebook_api` |
| `content/posts.md` | Common | `content/` | 两平台共用稿件队列 |
| `content/comments.txt` / `comments_en.txt` | LinkedIn（内容） | `content/` | 被 `commenter.py` 通过 `storage.content_dir` 读取；保持共享路径 |
| `content/media/` | Common | `content/media/` | 两平台发帖图片/视频 |
| `scripts/README.md` | Common | `scripts/` | 改为平台脚本索引 |
| `scripts/delete_post.py` | LinkedIn | `LinkedIn/scripts/` | 已移动；`sys.path` 改为 `parents[2]` |
| `scripts/delete_comment.py` | LinkedIn | `LinkedIn/scripts/` | 已移动；`sys.path` 改为 `parents[2]` |
| `scripts/fb_cdp_check.py` | Facebook | `fb/scripts/` | 已移动；无项目内导入 |
| `scripts/test_facebook_api_offline.py` | Facebook | `fb/scripts/` | 已移动；`ROOT` 改为 `parents[2]` |
| `start_fb.bat` | Facebook | `fb/start_fb.bat` | 已移动；profile 改为 `%~dp0..\profiles\facebook` |
| `check_fb.bat` | Facebook | `fb/check_fb.bat` | 已移动；先 `cd /d "%~dp0.."` |
| `FACEBOOK-OPERATIONS-PLAN.md` | Facebook | `fb/docs/` | 已移动；内部链接已改相对路径 |
| `FB-ACCOUNT-CHECKLIST.md` | Facebook | `fb/docs/` | 已移动；命令改为 `fb\start_fb.bat` / `fb\check_fb.bat` |
| `content/facebook_content_calendar.md` | Facebook | `fb/content/` | 已移动 |
| `content/facebook_page_copy.md` | Facebook | `fb/content/` | 已移动 |
| `OPERATIONS-SOP.md` | LinkedIn | `LinkedIn/docs/` | 已移动；仍被 `.gitignore` 忽略（本地手册） |
| `start_chrome.bat` | LinkedIn（启动） | 根 | 被 `start_all.bat` / 自启动引用，**不移动** |
| `start_all.bat` | Common（启动） | 根 | 一键启动入口，**不移动** |
| `daemon_bg.bat` | Common（启动） | 根 | daemon 监督循环，**不移动** |
| `install_autostart.bat` / `uninstall_autostart.bat` | Common（启动） | 根 | 开机自启写入 `profiles\linkedin`，**不移动** |
| `profiles/` | Common（运行） | 根 | `browser.profiles.*` 指向这里；LinkedIn profile 正在使用 |
| `data/` | Common（运行） | 根 | `storage.database` / 日志 / 截图 |
| `.venv/`、`__pycache__/` | Common（运行） | 根 | 运行环境 |
| `_t.db`（测试遗留） | — | `data/_t.db` | 从根移走，避免污染根目录 |

## 移动清单

已移动（`ops_move`，源文件已删除、目标 revision 保留）：

1. `FACEBOOK-OPERATIONS-PLAN.md` → `fb/docs/FACEBOOK-OPERATIONS-PLAN.md`
2. `FB-ACCOUNT-CHECKLIST.md` → `fb/docs/FB-ACCOUNT-CHECKLIST.md`
3. `content/facebook_content_calendar.md` → `fb/content/facebook_content_calendar.md`
4. `content/facebook_page_copy.md` → `fb/content/facebook_page_copy.md`
5. `scripts/fb_cdp_check.py` → `fb/scripts/fb_cdp_check.py`
6. `scripts/test_facebook_api_offline.py` → `fb/scripts/test_facebook_api_offline.py`
7. `start_fb.bat` → `fb/start_fb.bat`
8. `check_fb.bat` → `fb/check_fb.bat`
9. `scripts/delete_comment.py` → `LinkedIn/scripts/delete_comment.py`
10. `scripts/delete_post.py` → `LinkedIn/scripts/delete_post.py`
11. `OPERATIONS-SOP.md` → `LinkedIn/docs/OPERATIONS-SOP.md`
12. `_t.db` → `data/_t.db`（清理测试遗留）

## 路径更新

| 文件 | 更新 |
|---|---|
| `fb/start_fb.bat` | `PROFILE=%~dp0..\profiles\facebook`（原 `%~dp0profiles\facebook`） |
| `fb/check_fb.bat` | `cd /d "%~dp0.."`；venv 路径 `%~dp0..\.venv\Scripts\python.exe` |
| `fb/scripts/test_facebook_api_offline.py` | `ROOT = parents[2]`（原 `parents[1]`） |
| `LinkedIn/scripts/delete_post.py` | `sys.path` 用 `parents[2]` |
| `LinkedIn/scripts/delete_comment.py` | `sys.path` 用 `parents[2]` |
| `fb/docs/FACEBOOK-OPERATIONS-PLAN.md` | 链接改为 `../start_fb.bat`、`../content/...`、`../../src/...`、`../../.env.example` |
| `fb/docs/FB-ACCOUNT-CHECKLIST.md` | 命令改为 `fb\start_fb.bat` / `fb\check_fb.bat` |
| `README.md` | 增加“目录分工”；目录表更新 |
| `config.yaml` | 顶部加目录分工注释（路径值未改） |
| `scripts/README.md` | 改为平台脚本索引 |

## 迁移后系统怎么跑

- `python main.py ...` 不变：入口在根，`src/` 仍在根，`config.yaml` 的 `profiles/`、`data/`、`content/` 路径未改。
- 领英启动不变：`start_all.bat` → `start_chrome.bat`（`profiles\linkedin`，9222）→ `main.py daemon`。
- 领英评论话术不变：`commenter.py` 仍从 `content/comments*.txt` 读取。
- Facebook 登录：双击 `fb\start_fb.bat`（profile 指回根 `profiles\facebook`，9223）；验证双击 `fb\check_fb.bat`（先切回根目录）。
- Facebook API：`src/platforms/facebook_api.py` 位置未变，仍可用 `python -m src.platforms.facebook_api --check`。
- 领英删帖/删评论：改为 `LinkedIn\scripts\delete_post.py` / `delete_comment.py`，脚本内部已能定位根目录的 `src/`。

## 验证

- `ops_move` 输出：每个文件 `revision` 保留、源路径 `removed=true`。
- `ops_glob` 确认：`fb/` 12 项、`LinkedIn/` 7 项、根目录不再有平台专属文档/脚本。
- `.bat` 文件均为纯 ASCII；`check_fb.bat` 保留 CRLF，`start_fb.bat` 重写后为 LF（已去掉 `^` 续行，cmd 可解析；如遇兼容问题用 `unix2dos` 转 CRLF）。
- Python 路径常量已核对；**实际运行需在 DSH 外**（当前会话对 `data/`、`profiles/`、`.venv` 无写/执行权限，且本次 `ops_bash` 运行时不可用，无法在这里跑 `main.py` 或 Playwright）。
- 迁移后建议运行：`.venv\Scripts\python.exe -B fb\scripts\test_facebook_api_offline.py`（应在 DSH 外普通终端执行）。

## 最终目录树

```text
D:\自动运营领英和fb\
├─ main.py
├─ config.yaml
├─ requirements.txt
├─ README.md
├─ MIGRATION-PLAN.md
├─ LICENSE
├─ .gitignore
├─ .env.example
├─ daemon_bg.bat
├─ start_all.bat
├─ start_chrome.bat
├─ install_autostart.bat
├─ uninstall_autostart.bat
├─ fb\
│  ├─ README.md
│  ├─ start_fb.bat
│  ├─ check_fb.bat
│  ├─ docs\
│  │  ├─ FACEBOOK-OPERATIONS-PLAN.md
│  │  └─ FB-ACCOUNT-CHECKLIST.md
│  ├─ content\
│  │  ├─ facebook_content_calendar.md
│  │  └─ facebook_page_copy.md
│  └─ scripts\
│     ├─ fb_cdp_check.py
│     └─ test_facebook_api_offline.py
├─ LinkedIn\
│  ├─ README.md
│  ├─ docs\
│  │  └─ OPERATIONS-SOP.md
│  ├─ content\        （占位，comments*.txt 仍在根 content/）
│  └─ scripts\
│     ├─ delete_post.py
│     └─ delete_comment.py
├─ content\
│  ├─ posts.md
│  ├─ comments.txt
│  ├─ comments_en.txt
│  └─ media\
├─ scripts\
│  └─ README.md
├─ src\
│  ├─ browser.py
│  ├─ config.py
│  ├─ database.py
│  ├─ humanize.py
│  ├─ risk_control.py
│  ├─ scheduler.py
│  ├─ comment_ramp.py
│  ├─ human_text.py
│  ├─ tasks\
│  │  ├─ poster.py
│  │  ├─ commenter.py
│  │  └─ follower.py
│  └─ platforms\
│     ├─ base.py
│     ├─ linkedin.py
│     ├─ facebook.py
│     └─ facebook_api.py
├─ profiles\          （运行目录；linkedin 已登录，facebook 待登录）
├─ data\              （运行目录；social.db、logs、debug）
└─ .venv\             （运行环境）
```

## 回滚

如需还原，按“移动清单”反向移动即可；路径更新按上表反向改回。
注意 `OPERATIONS-SOP.md` 仍被 `.gitignore` 忽略，回滚到根目录后仍不会进版本库。
