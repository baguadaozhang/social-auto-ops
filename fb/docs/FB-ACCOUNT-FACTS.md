# Facebook 账号与主页事实（已核实）

> 只记录可公开/低敏的账号标识，**不记录密码、2FA、token**。
> 核实时间：2026-11-26 13:55（Asia/Shanghai）

## 个人账号

| 项 | 值 | 来源 |
|---|---|---|
| 显示名 | Wu Wenhao | 页面左上角 |
| 用户 ID | `61591562485588` | CDP 读取 `c_user` cookie |
| 登录态 | 正常，未跳登录/验证页 | `fb/scripts/fb_cdp_check.py` 返回 0 |
| 限制历史 | 无 | 用户确认（仅长期未使用） |

## 公司主页（Page）

| 项 | 值 | 来源 |
|---|---|---|
| 名称 | Junzhou Sanitary Ware | 首页左侧「快速访问列表」 |
| Page ID | `61591663326933` | 主页入口链接 `facebook.com/61591663326933/` |
| 已知内容 | 页面已有历史内容（用户确认已建好、有内容） | 用户描述 |
| 当前角色 | **待确认**（需看到「以主页身份」编辑入口或 Graph API 返回） | — |

## 待补齐

1. 主页当前是否为你本人管理员、是否有其他管理员 → 主页设置 → 主页访问权限。
2. 是否已创建 Business Manager（商务管理平台）及商务账户。
3. Graph API 的 Page access token（放 `.env`，不进仓库）。

## 复现命令

```bat
fb\start_fb.bat                 :: 启动 9223 Chrome（带代理）
.venv\Scripts\python.exe -B fb\scripts\fb_cdp_check.py    :: 登录态
.venv\Scripts\python.exe -B fb\scripts\fb_list_pages.py   :: 列出主页及 ID
.venv\Scripts\python.exe -B fb\scripts\test_facebook_api_offline.py  :: 离线用例
```
