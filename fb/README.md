# fb/ — Facebook 专属资料与脚本

这个目录只放 **Facebook 平台专属** 的文档、内容与手工脚本。
共享入口（`main.py`、`config.yaml`、`src/`、`profiles/`、`data/`）仍在项目根目录，不要移动。

> **每天怎么操作 → 看根目录 [`OPERATIONS-MANUAL.md`](../OPERATIONS-MANUAL.md) 第 3 章「每日流程」。**
> 本文件只说明目录里有什么，不重复日常步骤。

## 目录

| 路径 | 内容 |
|---|---|
| `docs/FACEBOOK-OPERATIONS-PLAN.md` | Facebook 运营启动计划（v0.4，恢复/观察优先） |
| `docs/FB-ACCOUNT-CHECKLIST.md` | 账号体检表（限制史、主页、管理员、观察期记录） |
| `docs/FB-ACCOUNT-FACTS.md` | 已核实的账号/主页事实（个人号 ID、Page ID、限制历史） |
| `content/facebook_content_calendar.md` | 4 周 12 条主帖 + 8 条备用 |
| `content/facebook_page_copy.md` | 主页名称/简介/About/CTA/Messenger 话术 |
| `scripts/fb_cdp_check.py` | 只读检查 9223 登录态（不写 DB/profile） |
| `scripts/fb_list_pages.py` | 只读列出账号下的主页及 Page ID |
| `scripts/test_facebook_api_offline.py` | `facebook_api.py` 离线测试 |
| `start_fb.bat` | 启动 9223 专用 Chrome（DSH 外双击） |
| `check_fb.bat` | 检查 Facebook 登录态（DSH 外双击） |

## 仍然留在根目录的 Facebook 相关文件

| 路径 | 原因 |
|---|---|
| `src/platforms/facebook.py` | 平台适配器，被 `src/tasks/poster.py` 导入 |
| `src/platforms/facebook_api.py` | 公共主页 Graph API 客户端，导入路径是 `src.platforms.facebook_api` |
| `.env.example` | `facebook_api.py` 从项目根目录 `.env` 读取配置 |
| `profiles/facebook` | `config.yaml` 的 `browser.profiles.facebook` 指向这里 |
| `data/social.db` | 两个平台共用同一个数据库 |

## 使用

```bat
rem 1) 启动专用 Chrome（DSH 外双击）
fb\start_fb.bat

rem 2) 登录后检查
fb\check_fb.bat

rem 3) 主页 API（先复制根目录 .env.example 为 .env 并填 token）
.venv\Scripts\python.exe -m src.platforms.facebook_api --check
```

## 网络（重要）

Facebook 在国内无法直连。`start_fb.bat` 已默认显式走 Clash Verge 混合端口：

```bat
set "PROXY=http://127.0.0.1:7897"
```

- 如果你的 Clash 端口不是 7897，改 `fb/start_fb.bat` 里这一行；
- 出现 `ERR_CONNECTION_TIMED_OUT`：确认 Clash 在运行、节点可用；必要时先把 Clash 切到全局模式测试；
- 改完代理参数后必须**完全关闭旧的 FB Chrome**再双击，否则新参数不会生效（Chrome 复用了旧进程）；
- 顶部的 `--disable-blink-features=AutomationControlled` 警告是 Chrome 对启动参数的提示，**不是**打不开 Facebook 的原因。

在普通 cmd 里验证代理是否通：

```bat
curl -x http://127.0.0.1:7897 -I https://www.facebook.com
```

返回 `HTTP/2 200` 或 `HTTP/1.1 200` 就说明代理通；如果 `curl` 报连接失败，先修 Clash/端口，再谈浏览器。

## 红线

- 个人号不做自动化；只走公共主页 Graph API。
- 主页至少 2 个管理员；BM 认领；系统用户 token 放 `.env`，不进仓库。
- 观察期（3–7 天）不跑脚本：不加人、不进组、不群发、不发外链。
