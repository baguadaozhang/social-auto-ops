# scripts/ — 手工脚本索引

平台专属脚本已归档到对应平台目录；这里只保留索引。

| 平台 | 路径 | 用途 |
|---|---|---|
| LinkedIn | `LinkedIn/scripts/delete_post.py` | 删除测试期间发出的帖子 |
| LinkedIn | `LinkedIn/scripts/delete_comment.py` | 删除测试期间发出的评论 |
| Facebook | `fb/scripts/fb_cdp_check.py` | 只读检查 9223 登录态 |
| Facebook | `fb/scripts/test_facebook_api_offline.py` | `facebook_api.py` 离线测试 |

**警告**：

- 删除类脚本直连 CDP 操作真实账号，**绕过全部风控、配额、拟真层**（固定 sleep、无冷却、无熔断）。
- 只在你明确知道要删哪条内容时手动运行，**不要**被 daemon、计划任务或任何自动化调用。
- 运行前确认对应 Chrome（领英 9222 / Facebook 9223）已在调试端口上运行且已登录。
