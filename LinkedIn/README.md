# LinkedIn/ — 领英专属资料与手工脚本

这个目录只放 **LinkedIn 平台专属** 的文档、内容与手工脚本。
共享入口与运行环境仍在项目根目录，不要移动。

## 目录

| 路径 | 内容 |
|---|---|
| `docs/OPERATIONS-SOP.md` | 领英运营 SOP（v1 现状快照） |
| `scripts/delete_post.py` | 删除测试期间发出的帖子（手工，直连 CDP） |
| `scripts/delete_comment.py` | 删除测试期间发出的评论（手工，直连 CDP） |
| `content/` | 领英话术库（如后续迁入；当前 `comments*.txt` 仍在根 `content/`） |

## 仍然留在根目录的 LinkedIn 相关文件

| 路径 | 原因 |
|---|---|
| `src/platforms/linkedin.py` | 平台适配器，被 `src/tasks/*` 导入 |
| `src/tasks/commenter.py` / `follower.py` | 领英评论/关注任务 |
| `content/comments.txt` / `comments_en.txt` | 被 `commenter.py` 通过 `storage.content_dir` 读取；保持共享 content 目录 |
| `content/posts.md` | 两个平台共用稿件队列 |
| `start_chrome.bat` | 启动 9222 专用 Chrome，被 `start_all.bat` / 自启动引用 |
| `start_all.bat` / `daemon_bg.bat` / `install_autostart.bat` | 启动/自启动入口，位置不能动 |
| `profiles/linkedin` | `config.yaml` 的 `browser.profiles.linkedin` 指向这里 |
| `data/social.db` | 两个平台共用同一个数据库 |

## 手工脚本

```bat
rem 删除某条测试帖子 / 评论（先确认 Chrome 9222 已登录）
.venv\Scripts\python.exe LinkedIn\scripts\delete_post.py
.venv\Scripts\python.exe LinkedIn\scripts\delete_comment.py "这是一条测试评论"
```

> 这些脚本绕过风控/配额/拟真层，只用于清理测试内容，不要被 daemon 调用。
