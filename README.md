# 社媒自动化运营系统

LinkedIn + Facebook 自动化运营工具：**登录态持久化 + 行为拟真 + 随机作息 + 自动评论/发帖 + 风控 + 人工介入**。

> ⚠️ **免责声明**：自动操作社交媒体账号违反 LinkedIn / Meta 的用户协议，存在限流、验证码、封号风险。
> 本项目仅用于技术研究与**自有账号**的运营实验，风险自负；请先用小号验证 1–2 周。

- ✅ 已实现：登录态复用、随机日程、发帖、**评论（按话题选话术）**、**关注（只 follow，不发 Connect 邀请）**、配额、时间窗、熔断、人工待办
- 🚧 未实现：Connect / 发邀请（与关注是两套限额，需独立实现）；Facebook 的实际自动化逻辑（仅有配置与页面骨架）

## 随机日程（不做固定时间）

固定时间本身就是机器特征。这套系统每天生成一份**不规律的作息表**：

```bash
python main.py plan          # 看今天的作息表
python main.py daemon        # 常驻，到点自动执行（推荐）
```

生成出来大概长这样（每次都不一样）：

```
2026-11-23 今天：3 个活跃时段
  14:15  浏览 5.3 分钟，评论 2 条
  15:46  浏览 2.1 分钟
  17:20  浏览 2.5 分钟
```

规则：

- 每天 2-4 个时段，**起始时间在窗口内随机**，相邻时段间隔用**重尾分布**
  （多数偏短、偶尔拉得很开，比均匀随机更像人）
- 每个时段做的事随机：刷一刷、顺手评论 1-2 条、队列里有稿子时可能顺手发
- 50% 概率顺带做点"不产生内容"的事：展开长文读完、去通知页扫一眼、随便滚
- **10% 概率整天空窗**（真人也有今天不上的时候）
- 错过超过 45 分钟就作废，**不补做**——补做反而暴露
- 一天结束后自动规划明天
- **前 14 天是冷启动期**：时段减到 1-3 个、评论概率降到 30%、每时段最多 1 条
  （实测 0.5 条/天 vs 稳定期 2.1 条/天）

如果想用系统计划任务而不是 `daemon`，让 Windows 每 15 分钟跑一次
`python main.py tick` 即可，判断是否到点由程序自己负责。

相关参数都在 `config.yaml` 的 `schedule` 段。

## 浏览器常驻模式（任务结束不关闭 Chrome）

默认使用 **CDP 附加模式**：程序不"拥有"浏览器，只连上去操作。
所以你启动的那个 Chrome 窗口在任务结束后会一直开着，你自己也能继续用。

### 推荐：设成开机自启（只需做一次）

**双击 `install_autostart.bat`**，它会在 Windows 的「启动」文件夹里写一个
`LinkedIn-Auto.bat`，下次登录后自动：

1. 打开专用 Chrome（端口 9222）
2. 最小化运行日程守护（日志 `data\logs\daemon.log`）

不需要管理员权限。想取消就双击 `uninstall_autostart.bat`。

> 自启后你不用管任何事。如果中途手动关掉了 Chrome，守护进程下一次取页面时会
> 检测到连接已失效、自动重连并把 Chrome 拉起来；守护进程本身崩溃时，
> `daemon_bg.bat` 的监督循环会在约 30 秒后自动重启它。
> 调试端口（9222）只绑定本机回环（127.0.0.1）——它能完全控制这个已登录的
> 浏览器，请勿改成对外暴露。

### 手动方式

- `start_all.bat`：一键启动（Chrome + 守护，能看到日志窗口）
- `start_chrome.bat`：只启动 Chrome
- `python main.py chrome`：同上，命令行方式

之后所有 `check / run / comment / browse` 命令都会**复用同一个窗口**，
不会新开、也不会关掉它。窗口里登录态一直有效。

> ⚠️ 所有 `.bat` 文件必须保持**纯 ASCII**（不要往里写中文）。cmd 按 GBK 解析
> UTF-8 文件会把命令读错，出现 `'xxx' 不是内部命令` 之类的报错。

> 如果你的 Chrome 本来就在运行（日常用的那个），不冲突——自动化用的是
> 独立配置目录 `profiles/linkedin`，两边互不影响，可以同时开着。

## 快速开始

### 1. 安装依赖

```bash
# 建议使用虚拟环境，避免污染系统 Python
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

本项目默认使用**系统已装的 Chrome**（`browser.channel: chrome`），
因此**不需要** `playwright install chromium`；
只有把 `channel` 留空、改用 Playwright 自带内核时才需要下载。

### 2. 首次登录（必须人工操作一次）

```bash
python main.py login --platform linkedin
python main.py login --platform facebook
```

命令会打开真实浏览器窗口。手动完成登录（含验证码、二次验证），
登录成功后按 Enter，会话会保存在 `profiles/` 目录，后续无需重复登录。

检查登录态：

```bash
python main.py check --platform linkedin
```

### 3. 发帖

```bash
# 直接传内容
python main.py post --platform linkedin --content "今天分享一个小技巧……"

# 两个平台一起发
python main.py post --platform all --content "本周小结……"

# 从文件读取（多篇用单独一行 --- 分隔）
python main.py post --platform facebook --file content/posts.md

# 定时执行
python main.py post --platform linkedin --content "……" --at "2026-11-23T10:30:00"
```

加入队列后，执行：

```bash
python main.py run
```

### 4. 评论

```bash
# 从话术库自动挑 3 条，评论信息流里的帖子
python main.py comment --count 3

# 按关键词搜索帖子后评论
python main.py comment --keyword "卫浴出口" --count 2

# 指定某条帖子/某个列表页，用自己写的内容
python main.py comment --url "https://www.linkedin.com/feed/update/urn:li:activity:xxx" --text "写得清楚，想请教下交期怎么控？"

# 绕过人工介入分流（明确要一次跑完时用）
python main.py comment --count 1 --no-human
```

**会跳过推广/广告帖**，不会在广告下面评论。

### 双语话术（英文为主）

话术分两个文件，**语言跟随帖子**——英文帖用英文评论，中文帖用中文评论：

| 文件 | 语言 | 条数 |
|---|---|---|
| `content/comments_en.txt` | 英文（主） | 150 |
| `content/comments.txt` | 中文（辅） | 92 |

读不到正文时默认用英文（`comment.primary_language: en`），因为客户主要是外国人。

### 只评论相关话题

评论前先判断这条帖子**值不值得评论**，三道筛：

1. 正文太短（<15 字）→ 跳过
2. 招聘 / 求职 / 简历类 → 跳过（在招聘帖下谈供应链很怪）
3. 跟自己行业话题完全无关 → 跳过（`comment.require_topic_match`，默认开）

配合 `schedule.comment_keywords`，自动运营时会**按关键词主动搜帖子**，
而不是等信息流推送——信息流里大多是不相关内容，会被全部筛掉：

```yaml
schedule:
  comment_keywords:
    - "sanitary ware"
    - "bathroom distributor"
    - "hotel procurement"
    ...
```

关键词**必须实测**，因为领英内容搜索里很多词根本搜不到帖子。用这个命令检查：

```bash
python main.py keywords          # 全部测一遍，每个约 10 秒
python main.py keywords --limit 5
```

输出会告诉你每个词搜到几张卡片、其中几张值得评论：

```
  bathroom fittings          卡片  3  可评论 3/3  可用
  bathroom renovation        卡片  6  可评论 0/3  无相关帖
```

`无相关帖` 的词会白占一次搜索机会，建议换掉。换产品线或新市场时重跑一次即可。

评论话术在 `content/comments.txt`，格式：

```
[supply] 物流这块现在最大的变量还是海运价格吧？
[quality] 品控最难的是标准统一，尤其是多工厂代工的时候。
想请教下，这套做法在团队规模不大的时候也适用吗？      ← 不带标签=通用
```

支持 7 个话题标签：`supply` 物流供应链、`quality` 质量品控、`cost` 成本价格、
`delivery` 交期交付、`market` 市场客户、`production` 生产制造、`digital` 数字化。

话题判定用**中英双语关键词加权**：短语（`supply chain`）和长中文词（`供应链`）
权重 2，单词（`logistics`）和短词（`物流`）权重 1，再乘以命中次数（截断到 3 次）。
英文按整词匹配，所以 `AI` 不会误命中 `available`。

**评论前会先读帖子正文**，按关键词命中数判断话题，优先选对应标签的话术——
避免"帖子讲海运、评论问成本"这种文不对题。话术库共 242 条
（英文 150：7 个话题组各 20 条 + 10 条通用；中文 92），7 天内不重复；
实在用完了才复用，并自动加**同语言**的随机前后缀（英文话术不会混入中文）。

每个英文话题组里有 4 条标了 `[标签,hook]`（中文组 3 条），是**轻钩子**版本——
结尾会自然地带一句"需要的话我把清单/模板发你"之类，引导对方看主页或私信；
其余保持纯讨论，避免每条评论都像在做推销。

> 话术库越大越安全。建议保持 50 条以上，并且定期补充——一直用同样的
> 几十句话，时间长了仍会被判定为重复内容。

### 人为小瑕疵

默认 8% 的评论会带**一处**打字疏漏，让它看起来不像模板：

按文本语言**自动切换规则**：

| 类型 | 中文 | 英文 |
|---|---|---|
| 手误 | 你们→你门、问题→问提、时候→时侯、订单→定单、现在→现再 | because→becuase、problem→probelm、quality→qualtiy、their→thier、standard→standrad |
| 标点疏忽 | 句末漏标点，或句号打成逗号 | 同左（用英文逗号） |
| 口语填充词 | 「嗯，」「对了，」「话说，」开头 | 「Well, 」「Honestly, 」「BTW, 」开头 |

英文还有两个额外处理：

- **保留大小写**：句首 `Because` → `Becuase`，不会变成 `becuase` 开头
- **加填充词时改小写**：`Because…` → `Well, because…`；
  如果句首疑似专有名词（LinkedIn / Amazon），直接放弃这次填充，避免写成 `linkedIn`

原则是**只加不改变意思的瑕疵**。商务场合一句话打错成别的意思，
比机器味更致命。一条评论最多一处，绝不叠加。

概率在 `config.yaml`：

```yaml
comment:
  imperfection_probability: 0.08   # 领英是职业场景，建议不超过 0.1
```

> 映射表在 `src/human_text.py` 的 `_ZH_MISTAKES` / `_EN_MISTAKES`，
> 想加词直接往里补即可——但务必只加"错了也读得懂"的。
>
> 注意：英文规则只在评论文本是英文时触发。目前话术库是中文的，
> 如果要做英文评论，还需要一套英文话术 + 英文话题关键词。

### 5. 查看状态与人工待办

```bash
python main.py status        # 配额、账号状态、日志
python main.py todos         # 需要你处理的任务
python main.py done --id 1   # 你已手动发布 → 任务结单，不占机器配额
python main.py approve --id 1 # 你批准了 → 交回机器发布，不会再让你确认
python main.py browse --platform linkedin  # 5 分钟纯浏览（无互动）
```

> `done` 和 `approve` 的区别很重要：
> `done` 表示这条内容你已经亲自发了，系统不再处理；
> `approve` 表示你只是点了确认，让机器去发（任务带 `human_approved` 标记，
> 下次 `run` 不会再被转人工，避免来回打转）。

## 内容文件格式

`--file` 支持一个文件放多篇帖子，用单独一行 `---` 分隔：

```markdown
第一篇帖子的内容……

---

第二篇帖子的内容……

---

第三篇……
```

程序会把每段分别入队，两条之间按时间窗口和随机延迟依次发布。

## 本版行为说明

- **有头模式**：浏览器窗口可见，无头模式被风控识别概率极高，不建议改。
- **行为拟真**：每次发帖前会热身（滚动 2-4 屏、停留 15-60 秒）、
  鼠标走贝塞尔曲线、点击带偏移、逐字符输入且偶尔打错退格、发布前停顿。
- **时间窗口**：默认只在工作日 9:00-11:30 和 14:00-17:30 发帖，
  开始时间随机浮动 ±40 分钟，任务间随机发呆 1-5 分钟。
- **配额**：每平台每天最多 2 帖，周末减半，用完即停。
- **发帖节流**：一次 `run` 每个平台最多发 1 条，剩下的留到下次。
  多条连着发是最容易被判定为垃圾信息的模式，所以默认不做满队列。
- **任务推迟**：不在活跃时段或配额用完时，任务会自动推迟到下一个可用时间点，
  而不是被反复捡起重试。
- **熔断**：页面的系统提示区 / 标题 / URL 一旦出现验证码、账号受限等信号，
  **立即**熔断并把账号冷却 24 小时（`circuit_breaker.cooldown_hours` 可配）——
  没有"连续失败几次才熔断"的缓冲，命中即停，宁可误停不可硬闯。
- **人工介入**：默认发帖前生成人工待办（可确认后由你手动发），
  这是降低封号风险的关键机制。

## 配置

所有参数在 `config.yaml`，改完无需改代码：

- `quota` — 每日各类操作上限
- `time_window` — 活跃时段
- `humanize` — 拟真行为各项参数
- `human_intervention` — 人工占比与确认开关
- `circuit_breaker` — 熔断关键词与冷却时长

如果想让人工介入少一点，把 `human_ratio` 调低、
`confirm_before_post` 改成 `false`。**但不建议完全关掉**——
纯机器行为曲线太干净，反而是封号主因。

## 页面改版怎么办

领英和 FB 的 DOM 会改版。每个平台的关键元素在
`src/platforms/linkedin.py` 和 `facebook.py` 顶部都有多个候选选择器，
报错"未找到发帖入口"时，用浏览器开发者工具查看当前页面元素，
把新的选择器加进列表即可，不用改逻辑。

### 已实测的领英页面特征（2026-11）

| 元素 | 实际情况 |
|------|----------|
| 发帖入口 | 中文界面文案是「发动态」（不是英文的 Start a post） |
| 编辑框 | TipTap 编辑器 `div.tiptap.ProseMirror[contenteditable='true']`，placeholder「分享您的看法...」 |
| 发布按钮 | 文案是「动态」（不是「发布」），且**空内容时 disabled**，必须输入后才可点 |
| class 名 | 全部是混淆的 `cfsXXX`，**不能靠 class 定位**，只能靠结构 / 文案 |
| 帖子卡片 | `div[role='listitem']`，注意信息流里混有广告/推荐卡，需跳过 |
| 评论按钮 | `button[aria-label='评论']`（图标按钮，无文字） |
| 评论输入框 | `div[aria-label='创建评论的文本编辑器'][contenteditable='true']` |
| 评论提交按钮 | 文字是「评论」，**且渲染在卡片容器之外**，需要页面级兜底定位 |
| 广告识别 | 卡片内前 160 字含「推广 / 赞助 / Promoted」即跳过 |

因此发布按钮的定位没有用固定选择器，而是用 JS 找"可见且已启用"且文案匹配
「动态 / 发布 / Post」的按钮，再取离编辑框最近的一个。发布后还会校验
编辑框是否消失，避免误报成功。

## 复用已有浏览器登录态

自动化默认使用独立 profile（`profiles/<platform>`），与你日常浏览器的登录态隔离。
如果希望复用 Chrome 里已登录的账号，可以把 Chrome 用户目录**复制**一份过来
（不要直接用原目录，Chrome 会拒绝自动化启动默认配置）：

```bash
# 先完全关闭 Chrome，然后复制（示例）
# 只复制 Local State 和 Default（排除缓存）即可保留 cookie
```

复制完成后把 `config.yaml` 的 `browser.profiles.linkedin` 指向复制目录即可。
注意：复制后是**独立的会话**，你在日常 Chrome 里退出登录不会影响它，反之亦然。

## 风险提示

此类自动化违反 LinkedIn 和 Meta 用户协议，存在限流或封号风险。
请先用小号跑通 1-2 周，确认账号无异常后再考虑主号。
本工具已内置多重保护，但不能保证账号绝对安全。

## 目录结构

| 路径 | 作用 |
|---|---|
| `main.py` | CLI 入口：login / check / post / run / comment / plan / tick / daemon / status / todos / keywords |
| `src/browser.py` | CDP 常驻 Chrome（复用同一窗口）+ persistent 兜底 |
| `src/config.py` | `config.yaml` 加载，点号访问 + 默认值兜底 |
| `src/database.py` | SQLite：任务、配额、日志、人工待办、作息表 |
| `src/humanize.py` | 拟真层：贝塞尔鼠标、逐字输入、阅读时长、会话热身 |
| `src/risk_control.py` | 配额 / 时间窗 / 熔断 / 人工介入 |
| `src/scheduler.py` | 随机作息表生成 + 到点执行 |
| `src/tasks/` | 评论、发帖的编排逻辑 |
| `src/platforms/` | LinkedIn / Facebook 页面适配（**选择器集中在这里**，改版只改这） |
| `content/` | 中英评论话术库、待发帖稿件 |
| `config.yaml` | 全部可调参数（配额、时段、拟真、熔断、关键词） |
| `scripts/` | 一次性小工具（删评论 / 删帖子） |

## 许可

MIT License，详见 [LICENSE](LICENSE)。

如果这个项目对你有帮助，欢迎提 Issue 交流；也请在合规范围内使用。
