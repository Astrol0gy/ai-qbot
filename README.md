# ai-qbot

基于 **qq-botpy / OneBot + DeepSeek V4 Pro** 的 QQ 群聊 AI 机器人。

> 支持 **双通道并存**：官方 QQ 机器人通道（只能收到 @消息）与 OneBot 协议端通道（可收到群内全部消息）。
> 两条通道共用同一套能力：多群独立上下文记忆、V4 深度思考、联网搜索、图片识别、文生图、跑团（DM）。

---

## 项目结构

```
ai-qbot/
├── .env                # 配置文件（需自己填写，不提交 git）
├── .env.example        # 配置模板（可提交 git）
├── .gitignore
├── requirements.txt    # Python 依赖
├── README.md
├── data/               # 运行时数据（不提交 git）
│   ├── user_profiles/      # 用户画像
│   ├── campaigns/          # 跑团存档
│   ├── stickers/           # 表情包库（文件名 = 五个标签_序号，附 index.json 索引）
│   └── ref_index.jsonl     # 官方通道引用消息索引（官方专属）
└── src/                # 源码
    ├── __init__.py
    ├── config.py           # 配置加载
    ├── main.py             # 入口：按开关并发启动通道
    ├── runtime.py          # 单轮有效期、暂存写入、提交与有界 I/O
    ├── net_safety.py       # DNS 固定地址、TLS 校验、限量下载
    ├── runner.py           # 控制台与轮换日志入口
    ├── migrate_data.py     # 旧数据的显式身份迁移
    ├── trigger.py          # 触发策略（概率接话 / 冷却 / DM 模式过滤）
    ├── channels/           # 通道适配层
    │   ├── base.py             # IncomingMessage / Sender 契约
    │   ├── scheduler.py        # 两通道共用的完整事件 FIFO
    │   ├── commands.py         # 指令处理器（HELP_TEXT 等）
    │   ├── core.py             # 共享业务核心 handle_message()
    │   ├── official.py         # 官方 botpy 适配器
    │   └── onebot.py           # OneBot v11 适配器
    ├── deepseek_client.py  # DeepSeek V4 API 封装 + 对话记忆
    ├── user_profile.py     # 用户画像持久化
    ├── campaign_state.py   # 跑团状态持久化
    ├── dice.py             # 骰子
    ├── vision_utils.py     # 图片理解（DeepSeek 视觉）/ 文生图
    ├── stickers_utils.py   # 表情包库（采集入库 / 按情绪检索）
    ├── skills/             # Skill 元数据目录 / 按需正文 / 附加文本
    └── tools/              # Tool Calling 工具（Provider：search/image/video/profile/campaign/navi/chat/skill）
```

## 技能的渐进式加载与工具权限

技能分三层读取，**不会把全部正文预先拼进 system prompt**：

1. **目录**：启动仅索引 Markdown 的 `name`、`description`、`tools`。每轮首次请求只提供名称与简介，以及 `load_skill` / `read_skill_resource` 两个加载工具；普通群聊另有 `reply` / `skip` 控制工具。
2. **正文**：模型根据任务调用 `load_skill(name)`，按需读取该技能正文，并从**下一次模型请求**起开放它声明的业务工具。例如查询天气先加载「天气查询」，需要路线则另加载「地图导航」。多技能工具取并集，不会互相排斥。用户偏好和文生图分别由「用户偏好」「图片创作」技能声明。
3. **附加文本**：正文引用资料时，调用 `read_skill_resource(name, path)` 读取已加载技能内 `references/` 或 `scripts/` 的文件。不会自动读取全部附件，脚本只作为文本读取，不执行。

`tools:` 是实际的工具授权范围，省略或留空不授予业务工具。每次模型请求的可用列表也是执行权限快照：未加载技能、配置关闭的工具，以及同一批响应中刚加载、尚未出现在请求列表里的新工具，均会在执行前被拦截。注册表本身也校验该快照，不只依赖模型遵守提示词。加载状态只属于当前会话的当前轮次，不跨群、跨轮沿用；DM 模式也先按需加载「跑团主持」正文。

能力开关优先于技能授权：`DEEPSEEK_ENABLE_SEARCH=false` 关闭网页搜索 `web_search`、网页抓取 `fetch_url` 和依赖搜索的 `search_images`；不会误关画像、跑团、导航、视频搜索或文生图。加载需要搜索的技能时，会明确告知哪些工具没有开放。

现有平铺技能 `src/skills/foo.md` 的附件放在 `src/skills/foo/references/` 或 `foo/scripts/`；也支持 `src/skills/foo/SKILL.md` 包格式，其附件与 `SKILL.md` 同目录。仅允许 UTF-8 文本，正文读取上限 128 KiB，单个附件 64 KiB；越界路径、超限、缺失或损坏文件不会开放新工具。

离线验证：`python tests/skill_loading_check.py`。覆盖首次不含正文、按需工具开放、多技能组合、附加文件路径限制、同批越权拦截、搜索关闭后的权限以及跨群/跨轮隔离，不调用外部模型。

## 运行状态、数据与网络边界

`/reset` 清除对话历史、未回答片段与互动窗口，并使在途旧轮次失效；不删除用户画像或跑团存档。进入/退出 DM、加载/新建存档、重开与成功撤回也会清掉旧对话上下文。正常玩家行动自动登记玩家；同轮同一玩家的 log/update 合为一个可撤回决定。撤回只删除对应事实、恢复对应字段；字段已有后续修改时拒绝撤回且不扣次数，旧版全局快照只允许撤回全局最后一步。

`manage_campaign(action=view, section=state, page=1)` 按分区分页读取，每页最多 4000 字，可选 `all/state/facts/lore/players/roll_log/rules/dm_style`。提示摘要优先当前状态、玩家、真实骰点与近期事实，背景设定最后放置。多人片段保存可信的消息 ID→用户 ID 映射；画像与跑团写入在多人场景必须提供 `source_message_id`，服务端校验作者，同名用户也不会混为一人。

新画像路径为 `data/user_profiles/id-<群ID的UTF8十六进制>/id-<用户ID的UTF8十六进制>.md`，头部保存原始双方 ID；跑团使用同样的编码文件名。无歧义的旧版跑团文件可按可信会话 ID 复制迁移，原文件保留；Windows 的自动路径仅接受数字群号和固定 OneBot 前缀 ID，其他情况显式映射，避免旧大小写路径冲突。旧画像的下划线拼接无法可靠反推群与用户，旧版含转义/特殊字符的存档也不猜归属，使用显式映射迁移：

```bash
python -m src.migrate_data data/user_profiles/旧文件.md --group-id 完整会话ID --user-id 完整用户ID --user-name 昵称
python -m src.migrate_data data/campaigns/旧文件.json --group-id 完整会话ID
```

迁移保留原文件，目标存在时拒绝覆盖；应在停止机器人后执行。此次代码检查没有迁移真实运行数据。损坏存档会先备份；备份失败则拒绝覆盖，保存失败会返回失败结果。

网页和图片下载连接到本轮 DNS 校验通过的具体公网 IP，保留原始 Host、TLS SNI 和证书主机名校验，逐跳重定向重新校验。网页最多 4 MiB，视觉图片最多 64 MiB，出站图片最多 8 MiB，采集图片使用 `STICKER_MAX_BYTES`，下载含大小/耗时限制并关闭响应。两条通道先下载网络图片再上传字节，不让平台代抓任意网址。

默认不继承 `HTTP_PROXY`/`HTTPS_PROXY`，未配置 `TRUST_PROXY_URL` 时直接下载公网资源。配置后优先连接该 HTTP/Mixed 代理，代理地址本身允许为本机或内网地址；代理未启动或无法建立连接时，本次下载重新解析目标并回退直连，下次下载仍会尝试代理，开关 Clash 无需删改配置。代理路径允许 fake-IP，且本地 DNS 失败时由代理解析域名；受信代理须负责目标 DNS 和路由限制。读取超时、证书错误与代理返回的拒绝不会触发直连回退，两条路径共用单次下载总时限。直连仍拒绝 fake-IP 和非公网目标，代理路径也不允许把显式本机/内网目标作为抓取地址。配置启动时检查数字范围、有限值、布尔及枚举；主模型和视觉关闭思考均显式发送 disabled，参见 [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。代码默认 reasoning effort 为 high，现有 `.env.example` 模板设为 max，以实际配置为准。

若下载日志提示 QQ 图片域名指向 `198.18.x.x`，可能是代理 DNS 返回了 fake-IP。可将 `.env` 的 `TRUST_PROXY_URL` 设为机器人运行设备实际可达的 HTTP/Mixed 代理地址（如 `http://127.0.0.1:7890`，端口以代理配置为准，不使用 SOCKS 端口），或调整代理 DNS，让图片域名返回真实公网 IP；Mihomo 的配置参见 [fake-ip-filter](https://wiki.metacubex.one/config/dns/#fake-ip-filter)。修改后重启机器人，重新发图验证，也会清除进程中的图片失败退避缓存。不会自动放行 fake-IP 或其他内网目标。

退出时取消通道拥有的任务、撤销轮次、有限等待后台 I/O 与采集，再关闭模型客户端；不可强杀的底层阻塞 I/O 可能使进程等待到其网络超时。`start.sh` 使用项目目录区分 screen 会话和进程，按通道开关启动 NapCat，正确解析 IPv6 WS 地址；`src.runner` 将日志按 10 MiB 轮换、最多保留三个备份。当前已做 Bash 语法与离线行为检查，真实 QQ、付费模型/高德和 Termux 设备仍需部署环境联调。

## 快速开始

### 1. 选择接入通道

| 通道 | 能否看到未 @ 的群消息 | 说明 |
|------|:---:|------|
| **官方通道**（`OFFICIAL_ENABLE`） | 否 | QQ 开放平台官方机器人，平台只推送 @消息，稳定合规 |
| **OneBot 通道**（`ONEBOT_ENABLE`） | **是** | 需自建协议端（NapCatQQ），能收到群内全部消息，可概率接话 |

至少启用一个，也可以两个都开（同一进程内并发，业务能力一致）。

### 2. 安装

```bash
pip install -r requirements.txt
```

### 3. 配置

复制 `.env.example` 为 `.env`，按下方「配置说明」填写。

### 4. 启动（在项目根目录执行）

```bash
python -m src.main
```

启动后会看到：

```
🚀 启动通道：官方 botpy + OneBot
✅ 机器人「你的机器人名称」已上线！[DeepSeek V4 Pro | 🧠 V4 思考] 等待群聊 @ 消息...
[🔌 OneBot] 已连接 ws://127.0.0.1:3001
```

## 通道一：官方 QQ 机器人

1. 打开 [QQ 开放平台](https://q.qq.com) 并登录
2. 创建机器人应用，获取 **AppID** 和 **AppSecret**
3. 在应用设置中开通 **群聊消息** 权限
4. 将机器人拉入你的 QQ 群，在群里 `@机器人 你的问题`

> ⚠️ 官方平台**只推送被 @ 的消息**，这是平台侧限制，SDK 层面无法绕过。
> 想让机器人看到群里全部对话，只能使用下面的 OneBot 通道。

同一会话的消息按接收顺序进入完整事件队列，保留各自作者、图片、消息 ID 和发送器；引用回查和视觉识别在入队之后进行。两条通道共用调度组件，同会话串行、不同会话可并行，不再用单个 pending 槽覆盖中间消息。

## 通道二：OneBot 协议端（可接收全部群消息）

### 原理

OneBot 是一套接口标准（类比「OpenAI 兼容接口」），真正登录 QQ 账号、收发消息的是**协议端**：

```
真实 QQ 账号 → 协议端(NapCatQQ) → OneBot 接口(正向 WebSocket) → ai-qbot
```

### NapCatQQ 部署

1. 下载并安装 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)（基于官方 NTQQ 无头运行）
2. 启动 NapCat，用**一个 QQ 小号**扫码登录
3. 在 NapCat 的 WebUI 中开启 **网络配置 → 正向 WebSocket 服务**，记下端口（默认 `3001`）
4. 若设置了 `access_token`，把同样的值填进 `.env` 的 `ONEBOT_ACCESS_TOKEN`
5. 在 `.env` 中设置 `ONEBOT_ENABLE=true` 与 `ONEBOT_WS_URL=ws://127.0.0.1:3001`
6. 把该 QQ 号拉进目标群，启动机器人

> ⚠️ **账号风险提示**：OneBot 属于非官方协议，违反 QQ 用户协议，存在风控/封号风险，
> **强烈建议使用小号**，不要用主号。
> ⚠️ NapCat 登录的 QQ 号**不能同时登录原版 QQ 客户端**（同一账号无法双端同时在线）。

### 唤醒策略

分工：系统只负责「**什么时候把 AI 叫起来**」，AI 负责「**开不开口、跟谁说话、说什么**」。
普通群聊有两个状态，靠一个互动窗口切换：

```
空闲 ──概率命中 / @机器人──▶ 互动中 ──每条消息都唤醒──▶ …（AI 每轮处理完重新计时）
  ▲                                                              │
  └──────────────── 窗口超时（TRIGGER_ACTIVE_TIMEOUT_SEC 秒无人说话）─┘

收到消息
 ├─ 私聊 ──────────────────────────────── 进入处理队列（不注入 skip）
 └─ 群聊
     ├─ @机器人？ ─────────────────────── 唤醒 + 开启互动窗口（/ 指令走本地分支）
     ├─ 未@ 且以 / 开头 ───────────────── 直接忽略
     ├─ 未@ 且处于跑团(DM)模式 ────────── 直接丢弃（不送 AI、不写上下文）
     └─ 未@ 的其他消息
         ├─ 带图片？ → 先识别（可关） → 描述写进上下文
         ├─ 黑白名单 → 长度
         ├─ 互动中？ ──────────────────── 唤醒
         └─ 空闲中 → 按 TRIGGER_PROBABILITY 掷骰，命中才唤醒
```

- **空闲（默认状态）**：偶尔才被叫醒，由 `TRIGGER_PROBABILITY`（默认 10%）决定——这就是"主动搭话"。
- **互动中**：一旦开了局（概率命中或被人 @），**符合群名单、非指令及最小长度要求的消息都会叫醒 AI**，不再掷骰子。所以它能看到"同一个人连发三条"，也能判断"有人在接我的话"。
- **计时从回复完成开始**：AI 每处理完一轮（开口说话、或选择 `skip`）都重置窗口；`TRIGGER_ACTIVE_TIMEOUT_SEC` 秒内没人说话就退出互动，回到空闲的概率唤醒。想一件事想得久不会白白吃掉窗口。
- **AI 自己决定**：普通群聊唤醒时给模型注入两个控制类工具——`reply(text, reply_to?)` 回话、`skip()` 这轮不开口。上下文片段里每条都带说话者、消息 id、是否 @ 了机器人、引用了谁的话。
- **多人同时接话**：`reply` 可以多次调用（单轮上限 6 条），几个人分别跟它说话就各回一条，各引用各自的最后一句话。
- **完整事件排队**：忙时保留全部已接收事件，随后逐条处理；私聊、DM 和官方通道也遵守这一规则。每个会话最多等待 128 条，每个适配器处理与等待合计最多 2048 条；OneBot 最多拥有 256 个入站处理任务。超额会明确拒收并记录消息 ID/会话，直呼路径在可发送时提示繁忙，不覆盖旧事件。普通群聊每次唤醒读取当时的未回答片段快照，片段另受 `TRIGGER_CONTEXT_MESSAGES` 限制。

- **skip 不消费上下文**：它选择不开口时（群里互相聊天、或觉得对方还没说完），攒下的片段会留着，下一条消息来时连同新消息一起重新交给它——「等人说完再回」是它自己判断的结果。
- **skip 不撤销已排队的回复**：本轮已经用 `reply` 排好的回复照常发出，`skip` 只表示"不再补充"；只有一条都没排时，它才表示"本轮不开口"。（模型常把 `skip` 当"我说完了"用，旧实现会顺手撤掉已攒回复，于是它撤了又重发——群里看不出异常，但白烧一次调用、多拖十几秒。）
- **收尾不用调工具**：回复排好后直接停止调用工具，本轮即结束；`reply` 的返回文案里也会这么提示，免得它再拿 `skip` 去"收尾"。
- **思考有总时限**：一轮最多想 `DEEPSEEK_ROUND_TIMEOUT_SEC`（默认 60 秒）。到点就**放弃这一轮**：一条消息都不发、不写对话历史，并按「AI 主动结束互动」处理（退出互动窗口，要再开口得等下一次唤醒）。就算模型在第 61 秒答完了，那句话也不会再进群；攒下的片段仍然留着。
- **@ 触发调度**：@ 满足群名单规则后进入处理；仍可能等待同会话队列、视觉预处理和全局模型额度。本地命令先解析，不占模型额度。
- **跑团模式**：进入 DM 模式后，未被 @ 的消息**直接丢弃**，群内闲聊不会污染跑团剧情；此模式不注入 `reply`/`skip`，@ 即回，行为与从前完全一致。
- **成本提示**：互动窗口内每条消息都会烧一次调用；把 `TRIGGER_ACTIVE_TIMEOUT_SEC` 调小、或者降低 `TRIGGER_PROBABILITY`，都能明显压成本。
- **图片可见性**：未 @ 的群消息里的图片也会被识别（`TRIGGER_RECOGNIZE_IMAGES`，默认开），描述以 `[图片1：…]` 形式写进上下文，所以机器人能对「群友发的图」有反应，而不是只看得见 @ 它的那些图。

### 关于图片识别的成本

开启 `TRIGGER_RECOGNIZE_IMAGES` 后，允许进入普通群聊上下文的未 @ 图片会尝试预识别；群名单、DM 非 @ 消息和指令均先过滤。缓存命中、频控和失败退避会跳过调用。视觉请求按服务商规则计费，开启思考时还可能产生推理输出，不能把服务端的图片 token 规则当成整轮费用上限。

| 护栏 | 说明 |
|---|---|
| `TRIGGER_IMAGE_RATE_PER_MIN` | 每个群每分钟最多识别 N 张图（默认 10），超出则跳过；**不会**退化成回复时再识别 |
| `TRIGGER_IMAGE_CACHE_SIZE` | 共享描述缓存（默认 200 个条目），键包含图片引用、问题和细节档位；同键并发请求合并，命中时不再调用视觉模型 |
| `VISION_FAIL_BACKOFF_SEC` | 一张图识别失败后的退避窗口（默认 600 秒），窗口内不再下载、不再调用 |
| `ONEBOT_IMAGE_CONCURRENCY` | 图片预识别的并发额度（默认 2），**独立于**对话闸门，识别慢也不挡别的会话 |

若发现费用或延迟偏高，可先把 `TRIGGER_RECOGNIZE_IMAGES=false` 关掉（回到「只在被 @ 时识别」），或把 `VISION_DETAIL` 设为 `low`（图片缩到 512×512，最快最省，代价是小字会糊）。

> 识别在触发判定**之前**完成，且失败不会拖住对话：**逐张尽力**——认出几张算几张，认不出的那张在上下文里留中性占位 `[图片，未能读取]`（主模型据此知道"用户确实发过图"，但不会拿到"识别失败"去反复琢磨）。同一张图失败后 `VISION_FAIL_BACKOFF_SEC`（默认 600 秒）内不再下载、不再调用；识别请求本身读超时只有 `VISION_TIMEOUT_SEC`（默认 25 秒）且不重试。

### 关于发送可靠性（为什么超时不重发）

协议端「调用超时」不等于「消息没送达」。NapCat 在内部发送超时时会回 `retcode=1200`（`NTEvent .../sendMsg Timeout`），**而消息这时通常已经进群了**——把它当失败去重试，群里就会出现同一句话发两三遍。两条通道都通过 `SendResult` 返回投递结果：

| 结局 | 何时出现 | 处理 |
|---|---|---|
| `ok` | `retcode=0/1` | 完成 |
| `offline` | 连接不在、请求没发出去 | **确定没送达 → 重试（最多 3 次，间隔 2s/4s）** |
| `unknown` | 客户端超时、`retcode=1200/1201`、回执里带 `Timeout` | **可能已送达 → 立刻停止，日志提示「发送结果未知」** |
| `rejected` | 明确被拒（参数错、风控） | 不重试，保留未回答消息，不记成功历史 |
| `partial` | 部分媒体/文字成功 | 记录实际内容与状态，只消费已覆盖的消息 |
| `dropped` | 没有可发送内容或通道不支持纯本地表情 | 不消费输入，不写成功历史 |

官方发送绕过 botpy 内部会吞超时/重试连接重置的写请求，使用验证证书的单次 HTTP 传输；超时或网络异常返回 `unknown`，不盲目重发；同一个被动消息内的 `msg_seq` 持续递增。历史以回执中的实际文字、截断结果和媒体为准。`unknown` 会明确记入历史并避免自动重放；分条回复部分失败时，只消费有投递结果的引用来源。

读类接口（`get_msg` / `get_status` / `get_login_info`）失败返回 None；`get_status` 一次失败不会永久关闭在线探测。

### 关于思考超时（到点就放弃这一轮）

模型调用**如果不设上限，一轮能拖几十分钟**：SDK 默认 `read=600s`，而且失败还会重试（`max_retries`）。这期间这条会话的处理链一直堵着，`ONEBOT_MAX_CONCURRENCY` 个会话同时卡住，别的群也要排队。所以一轮思考有总时限 `DEEPSEEK_ROUND_TIMEOUT_SEC`（默认 60 秒），到点后：

| 项目 | 行为 |
|---|---|
| 发消息 | **一条都不发**——即便模型在第 61 秒答完了也丢弃（超时才到的回答，在群里已经过时了） |
| 对话历史 | 不写（轮次被取消，历史里不会留下半截） |
| 互动状态 | 按「AI 主动结束互动」处理：退出互动窗口，要再开口得等下一次唤醒 |
| 上下文片段 | **留着**（和 `skip` 一致），下一条消息来时连同新消息一起交给 AI |

模型阶段使用 `asyncio.wait` + 取消，并撤销该轮的有效期令牌。线程工具继承令牌，画像、存档与压缩历史先暂存；超时、重置、模式切换后的旧轮次不能晚写状态或历史。写入提交还会校验读取版本，冲突不会覆盖新数据。

阶段边界：同会话等待受队列容量约束；引用回查受 OneBot API 超时约束；视觉预处理等待最多 30 秒；共享模型额度等待最多 10 秒；模型/工具阶段默认 60 秒；单次出站发送等待最多 45 秒。多个 reply 的发送累计耗时另计，60 秒不是从接收至送达的全链路上限。工具、视觉与出站下载共用最多 8 个工作线程，不能终止的旧线程仍占额度；表情采集另有一个后台线程，不无限积压。取消本地等待不能撤销已经到达远端的请求或费用。

本地状态在发送前提交，避免保存失败却宣告成功；平台投递与本地文件没有分布式事务，因此状态提交后若发送失败，已提交的状态仍保留，投递记录不会伪装成功。多文件替换失败会尝试恢复先前内容；进程突然断电只保证单文件原子替换，跨文件不承诺崩溃原子性。

### 关于服务端不可用（熔断，快速失败）

上面那条 60 秒总时限在**服务端整体挂掉**时反而成了负担：DeepSeek 返回 503 / 连不上时，每一轮都会规规矩矩地硬等满 60 秒才放弃，群里连着几轮等不到回复，看着就像"bot 哑了"（2026-10-15 的真实日志）。

所以连续失败会熔断：超时、5xx、连接失败累计到 `DEEPSEEK_API_FAIL_LIMIT`（默认 3）次后，接下来 `DEEPSEEK_API_COOLDOWN_SEC`（默认 30 秒）内不再发起请求——**不回话也比两分钟后才回话强**：

| 场景 | 行为 |
|---|---|
| 熔断期内收到群唤醒 | 立刻结束本轮（不发请求、不发消息、退出互动），片段留着等下次唤醒 |
| 熔断期内被 @ / 私聊 | 同样不发请求，只回一句「我这会儿连不上模型服务，过一会儿再叫我试试～」 |
| 冷却结束 | 只允许一轮半开探测；探测失败重新冷却 |
| 模型阶段成功 | 失败计数清零、熔断解除；参数/程序/保存错误不计为模型服务故障，兜底请求失败不会当成功 |

> 视觉侧是同一套思路但更保守：读超时 25 秒、不重试、失败退避，认不出就留占位继续答——看图只是配菜，不该拖住对话。

`tests/_degrade_check.py` 把这一整条降级链钉住（全离线，不打桩模型就是假客户端）：本机取图后单次识别、失败退避不重复下载、一张好一张坏时逐张尽力、`choices` 为 None 时不再抛裸错、连续失败熔断后唤醒静默且不发请求、恢复后正常回话。

### 关于对话记忆的压缩

每群的历史存在内存里（`_conversations`，按群共享，不按用户隔离）。超过 `MAX_HISTORY_LENGTH × 2` 条时，
把最旧的若干轮用一次额外调用压成摘要，保留最近最多 4 轮原文（小容量时自动缩小），摘要以 `【更早的对话摘要】` 插在历史开头 —— 比硬截断丢信息强。

**压缩结果会写回历史本体**（`history[:] = 摘要 + 最近若干轮`），所以历史是真的变短，下一轮不会重复压同一段。
这点不是优化而是正确性问题：老实现只改本次请求的 `messages`、`history` 原地不动，于是从超阈值那轮起
**每一轮**都要把不断变长的全部旧历史重新摘要一遍（不是增量），白烧一次调用加延迟，而且越滚越贵
（`MAX_HISTORY_LENGTH=20` 时，从第 22 轮起每轮都把 34 条以上、还在变长的旧消息重新压一遍）。下次触发压缩时，待压部分已经以
`【更早的对话摘要】` 开头，模型把旧摘要和新对话合并即可——同样是增量。

两个配套细节：DeepSeek 处于熔断冷却期时**跳过压缩**（否则每轮都要先白等一次超时，比不压还慢），退回硬截断；
压缩失败时不写入摘要，提交时仍按硬上限裁剪，下一轮还能重试。最多保留 500 个会话，每会话最多 `2 × MAX_HISTORY_LENGTH + 2` 条；过期轮次的压缩结果不提交。

`tests/history_compress_check.py` 用假客户端把这套语义钉住（不联网）：压缩确实触发并写回、下一轮不再重压、
第二次压缩是合并而非全量重压、历史长度有界（14 轮只压 4 次而不是 7 次）、冷却期跳过压缩且照常回答。

`tests/history_live.py` 打真模型评估**效果**（要 `DEEPSEEK_API_KEY`，约 5 次调用）：灌 21 轮群聊 →
触发压缩 → 打印摘要原文并逐条核对关键事实（日期/地点/人数/预算/忌口/过敏/谁订座/电话），
再用「摘要 + 最近 4 轮」问回忆题，看模型答不答得对；最后再压一次，验证新事实进摘要、老事实不丢。
实测：旧历史 586 字 → 摘要 237~302 字（三次采样事实 11/11 全保留），摘要长度基本恒定（245~426 字），
而**摘要调用本身要 13.8 秒**（同一轮对话调用 1.9 秒）—— 这就是"不写回"的代价：从超阈值那轮起每轮多付一次。

## 表情包

机器人会**自己攒表情包**，也会**主动发**：群里或私聊中出现的图被识别为「表情包／梗图」时，原图会下载进本地库；
此后它想表达情绪，就从库里挑一张贴切的，先发文字，再单独发送表情包。

**采集**：看图模型判定为表情包时会额外输出一行五个标签（`情绪｜动作｜主体｜风格｜文字`），
文件名就是这五个标签加一个序号，用下划线连接：

```
无语_捂脸_熊猫_拼贴配字_我裂开了_1.gif
└────────── 五个标签 ──────────┘ └ 序号
```

- 序号是「同一组五个标签里的第几张」：撞名就往后排（`_1`、`_2`…），所以文件名永远是 6 段，靠标签区分内容。
- **按内容去重**：同一张图（sha1 相同）再出现只算一张，换个标签存也还是原来那张。
- 下载走后台线程，**不占对话那一轮的等待时间**；库里只收 PNG/JPEG/GIF/WebP，超过 `STICKER_MAX_BYTES` 的动图不收。
- 库容量满了淘汰最久没用过的那张。
- 采集范围是**群里 + 私聊**（同一个库）。也就是说**私聊里收到的图有可能会被发到群里** ——
  在意这一点就把 `STICKER_ENABLE` 关掉（关掉后既不采集也不发）。
- 标签行是给库归档用的，**不会进主模型的上下文**（`strip_tag_line` 会剥掉）。

**发送**：机器人想表达情绪时调 `send_sticker` 工具（工具壳 `list_stickers` / `send_sticker` 注册在
`src/tools/image_provider.py`，库本身在 `src/stickers_utils.py` —— 只管管库与挑图，不碰通道与工具注册）；
拿不准库里有什么，就先调 `list_stickers` 看现成的张数与常用标签。

工具按情绪词从库里检索（标签完全相等优先，两个字的互相包含次之，命中多个标签加权），挑中后返回一行
`[img]<库里的文件路径>[/img]`，模型把这一行放进回复，通道会先发文字，再把每张表情包单独发成一条消息（这一行本身不会显示给用户）；
**挑不到就不返回路径**（不会硬塞一张不相关的图），只发图不配字也允许。回复里的 `[img]` 是网址就当网络图发、
是库内已索引的图片路径就当表情包发，库外路径、索引 JSON、非图片与越界符号链接一律丢掉（模型抄错或编出来的路径不该进发送环节）。文字和表情包分别记录发送回执；文字成功而表情包失败时保留已送达文字，表情包回执未知时不重发，也不继续发送后续消息。多次 `reply` 仍共用整轮表情包数量上限。用法规则见 `src/skills/image.md`。

**纯表情包回复**：模型可只输出 `[img]…[/img]`，成功时不附加任何文字。图片明确被拒、未连接或文件不可用时，按该图情绪标签改发一次简短文字（如 `（无语）`，无标签时使用 `（表情）`）；文字也失败即结束本轮发送，不重试图片、不重试兜底文字，也不继续发送本轮后续回复。回执未知时图片可能已经送达，直接停止，不额外发送兜底文字。历史仅记录成功或回执未知的实际内容；文字兜底成功不把失败的表情包写成已送达。已有文字成功的图文回复不重复发送文字。

| 通道 | 能否发本地表情包 |
|------|:---:|
| OneBot（NapCatQQ） | 是，直接把本地绝对路径填进 `image` 段（协议端与机器人在同一台机器，Termux 单机部署就是这么跑的） |
| 官方通道 | **否**，官方富媒体必须先把文件上传换 `file_id`。这里降级为纯文字：有文字就发文字，纯表情包请求则尝试一次情绪文字兜底 |

跑团（DM）模式下按需加载跑团正文，**不会默认开放表情包工具**；只有额外加载「图片理解」后，这些工具才进入本轮可用范围，使用分寸仍按指引把关。
已识别的表情包照常入库；DM 中未 @ 的群消息会直接丢弃、不识图，也不会采集。

`tests/sticker_check.py` 把这条链路钉住（全离线，只打桩「看图的 HTTP 请求」和「发到 QQ」两步，其余走真代码）：
标签解析 → 命名与序号 → 后台真下载真落盘 → 按情绪检索 → `list_stickers`/`send_sticker` 两个工具 →
回复里的库内路径被认成表情包（库外路径丢掉）→ OneBot 段数组 → 官方通道降级 → 上下文剥标签行，
用临时目录当库，不碰真实 `data/stickers`。加 `--live` 会先用文生图造一张表情包再真调一次视觉模型，验证模型确实按格式吐出五个标签（会计费）。

## 使用方法

| 操作 | 方式 |
|------|------|
| 和机器人聊天 | 群聊 `@机器人 你的问题`；私聊直接发消息 |
| 查看全部指令 | `@机器人 /help` |
| 清除记忆 | `@机器人 /reset` |
| 图片识别 | 发送图片并 `@机器人`，可附带问题（群里未 @ 的图片机器人也看得见） |
| 表情包 | 发一张梗图就算投喂，机器人自己会收；它想发时会自己挑一张发出来 |
| 地图导航 | 直接问「春熙路在哪」「XX 附近有什么吃的」「从 A 到 B 怎么走」，需要 `AMAP_KEY` |
| 跑团（DM） | `@机器人 我们来玩跑团` |

> 群聊中所有指令都需要 @机器人 才会生效。

## 配置说明（.env）

### 通道开关

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `OFFICIAL_ENABLE` | 启用官方 botpy 通道 | `true` |
| `OFFICIAL_DEBUG_RAW_EVENTS` | 打印官方通道推送的原始事件（排查字段结构用；含发送者 openid 与消息原文，别在线上开） | `false` |
| `ONEBOT_ENABLE` | 启用 OneBot 通道 | `false` |
| `QQ_BOT_APP_ID` | QQ 机器人 AppID（官方通道必填） | 必填 |
| `QQ_BOT_APP_SECRET` | QQ 机器人 AppSecret（官方通道必填） | 必填 |
| `ONEBOT_WS_URL` | NapCat 正向 WebSocket 地址 | `ws://127.0.0.1:3001` |
| `ONEBOT_ACCESS_TOKEN` | 协议端 access_token | 空 |
| `ONEBOT_SELF_ID` | 机器人 QQ 号（留空自动获取） | 空 |
| `ONEBOT_API_TIMEOUT` | 单次 API 调用超时（秒） | `15` |
| `ONEBOT_MAX_CONCURRENCY` | 并发 LLM 调用上限 | `3` |
| `ONEBOT_IMAGE_CONCURRENCY` | 并发图片预识别上限（独立额度，不占上面的对话闸门） | `2` |
| `ONEBOT_HEARTBEAT` | WS 心跳间隔（秒） | `30` |
| `ONEBOT_DEDUP_SIZE` | message_id 去重窗口 | `500` |
| `ONEBOT_MAX_IMAGES` | 单条消息最多识别图片数 | `3` |
| `ONEBOT_PROBE_SEC` | 健康自检间隔（秒），`0` = 关闭 | `60` |
| `ONEBOT_PROBE_FAILS` | 连续几次自检失败后判定连接失效并重连 | `2` |
| `ONEBOT_OFFLINE_ALERT_SEC` | QQ 掉线日志的重复间隔（秒） | `600` |
| `ONEBOT_DOWN_ALERT_SEC` | 连不上协议端累计多久后告警（秒），`0` = 关闭 | `300` |

### 唤醒策略

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `TRIGGER_PROBABILITY` | 空闲状态下，未 @ 群消息触发一次唤醒的概率 | `0.1` |
| `TRIGGER_ACTIVE_TIMEOUT_SEC` | 互动窗口：AI 处理完一轮后多久没人说话就退出互动（秒） | `60` |
| `TRIGGER_MIN_LEN` | 未 @ 消息短于该值不单独唤醒（仍进缓冲，随后面带出） | `2` |
| `TRIGGER_CONTEXT_MESSAGES` | 唤醒时携带的上下文条数 | `8` |
| `TRIGGER_RECOGNIZE_IMAGES` | 未 @ 群消息里的图片是否也识别（按张计费） | `true` |
| `TRIGGER_IMAGE_RATE_PER_MIN` | 每群每分钟最多识别图片数（0=不限制） | `10` |
| `TRIGGER_IMAGE_CACHE_SIZE` | 图片描述缓存条数（按 URL 去重） | `200` |
| `ONEBOT_GROUP_WHITELIST` | 群白名单（留空=不限制） | 空 |
| `ONEBOT_GROUP_BLACKLIST` | 群黑名单（优先于白名单） | 空 |

### 模型与行为

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 必填 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_ANTHROPIC_BASE_URL` | 搜索专用兼容端点，独立设置，不随主接口自动改写 | `https://api.deepseek.com/anthropic` |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `DEEPSEEK_TEMPERATURE` | 创造性（思考模式下忽略） | `1.0` |
| `DEEPSEEK_MAX_TOKENS` | 最大回复长度 | `2048` |
| `DEEPSEEK_TOP_P` | 核采样（思考模式下忽略） | `1.0` |
| `DEEPSEEK_ENABLE_THINKING` | V4 深度思考模式 | `false` |
| `DEEPSEEK_REASONING_EFFORT` | 思考强度 (`high`/`max`) | `high` |
| `DEEPSEEK_ENABLE_SEARCH` | 网页搜索/抓取与搜图；不控制画像、跑团、导航、生图等其他能力 | `false` |
| `DEEPSEEK_ROUND_TIMEOUT_SEC` | 单轮思考总时限（秒），到点放弃本轮、不发消息 | `60` |
| `DEEPSEEK_API_FAIL_LIMIT` | 连续失败几次后熔断（不再发起请求，快速放弃本轮） | `3` |
| `DEEPSEEK_API_COOLDOWN_SEC` | 熔断后冷却多少秒 | `30` |
| `BOT_SYSTEM_PROMPT` | 系统提示词 | 友好群聊助手 |
| `MAX_HISTORY_LENGTH` | 每群记住几轮对话 | `10` |

### 图片理解与文生图

**看图走 DeepSeek 官方视觉接口**（`deepseek-flash`），与文字对话共用 `DEEPSEEK_API_KEY`：支持 JPEG/PNG/GIF/WebP（GIF 动图会看到画面里的动作变化）；先在本机校验并下载，再用 base64 或 Files API 送进去；每个缓存未命中的识别最多调用一次视觉模型，不再直传失败后追加第二次请求（同一张图复用 `file_id`，不重复上传）。文档：[图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[Files API](https://api-docs.deepseek.com/zh-cn/guides/files_api/)。

**描述的第一行固定是类型判读**：`类型：表情包/内容图片 ｜ 意图：<发送者想表达什么>`。表情包／梗图只写情绪与一句话画面，内容图片才做详细描述——主模型据此决定是**顺着情绪接话**还是**按内容回答**（规则见 `src/skills/image.md`）。这一行必须排在最前：描述进接话上下文时按 `IMAGE_NOTE_MAX` 截断，写在后面会被截掉。

**判定为表情包时，最后还会多一行五个标签**：`标签：情绪｜动作｜主体｜风格｜文字`。这一行不占额外调用（同一次看图请求里要出来的），
是给人看表情包库归档用的，进上下文前会被剥掉（见上面「表情包」一节）。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `VISION_BASE_URL` | 视觉接口地址；未设置时继承 `DEEPSEEK_BASE_URL` | 继承主接口 |
| `VISION_MODEL` | 视觉模型 | `deepseek-flash` |
| `VISION_DETAIL` | 细节档位：`low`（缩到 512×512，最快最省）/ `high` / `original` / `auto` | `auto` |
| `VISION_ENABLE_THINKING` | 看图是否走思考模式（推理与可见回答共用 `max_tokens`，开了会变慢且可能截断） | `false` |
| `VISION_FILES_API` | 自己下载的图怎么送：`auto`（超过 32MiB 才上传 Files API）/ `always` / `off` | `auto` |
| `VISION_TIMEOUT_SEC` | 单次看图请求的读超时（秒），失败不重试，认不出就留占位继续答 | `25` |
| `VISION_FAIL_BACKOFF_SEC` | 一张图识别失败后的退避窗口（秒），窗口内不再下载、不再调用 | `600` |
| `DASHSCOPE_API_KEY` | 阿里云百炼 API Key（**仅文生图**使用） | 空（未配置则跳过文生图） |
| `IMAGE_GEN_MODEL` | 文生图模型 | `qwen-image-3.0-pro` |
| `IMAGE_GEN_ENDPOINT` | 文生图端点（DashScope 原生接口） | 官方端点 |

### 表情包库

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `STICKER_ENABLE` | 总开关：关掉后既不采集也不发（两个工具都会说自己关着，回复里的库内路径也不再认） | `true` |
| `STICKER_DIR` | 库目录，留空 = `<项目根>/data/stickers`（相对路径按项目根解析） | 空 |
| `STICKER_MAX_FILES` | 库容量上限，超出淘汰最久未使用的那张 | `500` |
| `STICKER_MAX_BYTES` | 单张大小上限（字节），超了不收（动图尤其容易超标） | `5242880`（5MB） |
| `STICKER_MAX_PER_REPLY` | 一轮回复最多发几张 | `1` |

### 地图导航（navi）

**查地点与算路线走高德地图 Web 服务 2.0**（[搜索 POI 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) / [路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute)），
工具实现在 `src/tools/navi_provider.py`，用法规则见 `src/skills/navi.md`。

| 工具 | 干什么 | 调用上限 |
|------|--------|:---:|
| `search_place` | 搜地点/商家：坐标、地址、评分、人均、营业时间、电话。`near` 给了坐标就是「找这附近」 | 4 |
| `plan_route` | 算路线：距离、耗时、过路费、红绿灯、换乘方案（驾车/步行/骑行/电动车/公交） | 3 |

**Key 要自己去申请**：去 [高德开放平台](https://console.amap.com) 建应用 → 添加 Key（类型选 **Web 服务**）→
在 Key 上开通「搜索POI 2.0」和「路径规划 2.0」，填进 `.env` 的 `AMAP_KEY`。绑了 IP 白名单的话记得加服务器出口 IP。
**没配 Key 时工具不会静默失败**：它会明确回报「缺少 AMAP_KEY」，让模型直接告诉用户查不了，而不是硬试。

几个改工具时容易踩的点，都写在 `src/tools/navi_provider.py` 的模块说明里：

- 坐标一律**「经度,纬度」**（先经后纬，跟 GPS 常见的反着来），坐标系是 GCJ-02。
- **算耗时/过路费必须 `show_fields=cost`**：高德基础返回里只有距离，没有耗时 —— 代码里已强制带上，改参数时别丢掉。
- `keywords` 只支持**一个**关键词、≤80 字符；要「XX 附近」得先搜到 XX 的坐标再走周边搜索。
- **公交的城市只认 `citycode`/`adcode`，中文城市名一律被拒**：实测（2026-10-15）「成都」「成都市」都返回
  `INVALID_PARAMS`，换 `028`/`510100` 才通。所以 `city`/`city2` 对外仍收中文名，内部 `_city_code` 换一次码
  （纯数字原样用；中文名走一次 POI 搜索取返回里的 `citycode`，不用额外开通「行政区划查询」）。
- **公交返回的字段形状跟驾车不一样**（同样真跑核对过）：耗时只在 `cost.duration`（`transits[0]` 上**没有** `duration`）、
  票价是 `cost.transit_fee`（形如 `'3.0'`）、总步行在 `walking_distance`、步行段里**没有** `destination_stop`。
- 地名当起终点时内部会先搜一次 POI 换坐标，搜不到就明说没找到，不猜坐标。
- 驾车结果里的「打车约 X 元」来自 `route.taxi_cost`（高德出租车估价）；限行只在真有限行时才提示，
  **不替高德说「已规避」**（`restriction="0"` 就是没有限行信息）。

**失败怎么降级**：Key 无效 / 无权限 / 配额用尽这类重试没意义，工具会直接说明原因并让模型别再试；限流（QPS 超限）单独说「稍等一下再试」，
不会被误判成 Key 报废。单次请求读超时 `AMAP_TIMEOUT_SEC`（默认 8 秒），超了就这一轮说查不到，不拖住对话。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `AMAP_KEY` | 高德 Web 服务 Key（需开通「搜索POI 2.0」「路径规划 2.0」） | 空（未配置则两个工具都回报没配置） |
| `AMAP_TIMEOUT_SEC` | 单次请求读超时（秒） | `8` |
| `AMAP_POI_LIMIT` | 每次搜索默认返回几条（1~25） | `5` |

`tests/navi_check.py` 把这条链路钉住（全离线，只打桩高德的 HTTP 请求，走真 registry）：
工具注册与 schema、搜不到/关键词超长/near 传地名等参数校验、`city_limit` 与 `show_fields=cost` 真的发出去了、
空数组地址不显示成 `[]`、类型末级重复会合并、响应里没有 `cost` 时不编耗时、不限行时不乱下结论、
地名换坐标、公交的中文城市名换 citycode（同城只解析一次）、公交缺 `city` 的劝停、各类错误码的降级措辞。

`tests/navi_live.py` 打真接口（要 `AMAP_KEY`，约 10 次配额），核对**高德返回的字段形状**跟我们解析的假设一致：
`business` 与 `cost` 的字段名、`paths[0].cost.duration` 是否真存在、`departure_stop` 是对象还是字符串、
公交 `transits[0]` 有没有 `duration`、中文城市名是不是真被拒 —— 并把工具实际输出打印出来。没配 Key 时以退出码 2 报错，
不会假装通过。**改了字段解析就两个都跑**：离线版只证明我们发得对，证明不了返回长这样。

## 在小米平板上运行（Termux 单机部署）

平板可以**同时跑机器人和 NapCat 协议端**（NapCat 官方支持 Android Termux），
两者在同一网络命名空间，`ONEBOT_WS_URL` 直接填 `ws://127.0.0.1:3001` 即可，不需要局域网互访。

### 1. 安装 Termux 与依赖

从 [F-Droid](https://f-droid.org/packages/com.termux/) 安装 Termux（**不要用 Google Play 版**，已过时）。

```bash
pkg update && pkg upgrade
pkg install python git rust clang make openssl libffi binutils
```

> Python 依赖里 `aiohttp`、`pydantic-core` 在 Termux（bionic libc）下没有预编译 wheel，
> 需要本地编译，所以上面必须装 `rust` / `clang` / `openssl`。首次安装会比较慢。

### 2. 安装机器人

```bash
git clone <你的仓库地址> ai-qbot && cd ai-qbot
pip install aiohttp openai python-dotenv requests beautifulsoup4 anthropic
```

如果只走 OneBot 通道（`OFFICIAL_ENABLE=false`），**可以不装 `qq-botpy`**——
官方通道的 import 是延迟加载的，纯 OneBot 部署用不到它，能省掉一批依赖。

配好 `.env` 后启动：

```bash
python -m src.main
```

> 在平板上日常跑，别直接敲这条——用第 4 节的 `bash start.sh`（含唤醒锁、NapCat 与状态自检）。

### 3. 安装 NapCat 协议端

用官方脚本（三个地址依次回退，避开被墙的 GitHub 直连）：

```bash
curl -fsSL -o napcat.termux.sh https://ghfast.top/https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.termux.sh \
 || curl -fsSL -o napcat.termux.sh https://ghproxy.net/https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.termux.sh \
 || curl -fsSL -o napcat.termux.sh https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.termux.sh
bash napcat.termux.sh
```

> ⚠️ 网上很多教程用的 `nclatest.znin.net` 是**第三方加速域名**，已经不稳定，
> 常见报错 `curl: (35) TLS connect error: ... unexpected eof while reading` 就是它挂掉导致的。
> 上面三个地址都是官方仓库（镜像 + 直连），任一可用即可。
>
> ⚠️ 脚本会在 Termux 里装一个 Debian 容器（proot-distro）再装 QQ 与 NapCat，
> 需要约 **2GB** 可用存储空间，安装过程较慢属正常。

安装脚本最后会打印关键信息：

| 提示项 | 说明 |
|---|---|
| 启动命令 | `proot-distro sh napcat -- bash -c "xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox"` |
| 数据位置 | 容器外真实路径形如 `/data/data/com.termux/files/usr/var/lib/proot-distro/installed-rootfs/napcat/...` |
| WebUI 密钥 | `<NapCat安装位置>/config/webui.json` |
| TUI-CLI | 安装时选 `--cli y` 才会有 `napcat` 便捷命令 |

> **Termux 里没有 `napcat` 这个命令——这是最容易踩的坑。** NapCat 装在 proot 容器内部，
> 必须在 Termux 里用上面那条 `proot-distro ...` 命令钻进去启动（装的时候没选 TUI-CLI 的话
> 就是这样，敲 `napcat` 只会得到 `No command napcat found`）。
> `bash start.sh` 已经把这条命令内置为默认值，不用自己敲。

- **首次启动要登录 QQ**：
  1. `bash start.sh` —— 先把 NapCat 拉起来（它会把 WebUI 服务也开好）
  2. `bash start.sh webui` —— 直接弹出浏览器，**二维码就画在这个页面上**，扫它登录
     （**建议用不常用的老号小号**，新号风控较严）
  3. 登录成功后 NapCat 才会监听 `3001`，用 `bash start.sh status` 确认
- 进 `screen -r napcat` 也能看到同样的那行网址（`Ctrl+A` 再按 `D` 退出，不影响后台），
  但每次手抄 token 太费劲——`bash start.sh webui` 会自己从 `webui.json` 里读
- **`3001` 是登录成功之后才开的**——所以扫码之前，`start.sh` 会提示"等不到 WS 端口"，属正常
- 登录态缓存在容器里，一般重启不需要重新扫；掉登录了才要再扫一次
- 地址里的 `127.0.0.1` 指的是**平板自己**（proot 容器与 Termux 共用网络命名空间），
  所以要用**平板上的浏览器**打开；NapCat 装在别处时用 `WEBUI_JSON=/你的路径/webui.json bash start.sh webui`
- WebUI → **网络配置 → 新建 → WebSocket 服务器**（即「正向 WS」），端口填 `3001`
  - ⚠️ 不要选「WebSocket 客户端」（那是反向 WS，本项目不用）
  - 本项目是**主动连上去**的一方，所以 NapCat 侧必须建**服务端**
- proot 容器与 Termux 共用网络命名空间，所以机器人侧填 `ws://127.0.0.1:3001` 就能连上，不需要局域网互访

### 4. 后台保活

用自带的 `start.sh` 一条命令搞定（获取唤醒锁 → 启动 NapCat → 启动机器人）：

```bash
pkg install screen          # 首次需要
bash start.sh               # 一键启动
```

| 命令 | 作用 |
|---|---|
| `bash start.sh` | 启动全部（重复执行不会重复拉起；会先清掉残留的空壳会话） |
| `bash start.sh reload` | 仅重载机器人 Python 进程，保留 NapCat、QQ 登录和终端 |
| `bash start.sh status` | 查看**进程是否真活着** / 端口 / **QQ 是否在线** / 日志尾部 |
| `bash start.sh webui` | 打开 NapCat 的 WebUI（**扫码登录 QQ 用**，自动读 token 并弹出浏览器） |
| `bash start.sh log` | 实时跟随机器人日志 |
| `bash start.sh stop` | 停止机器人 + NapCat 并释放唤醒锁 |

更新代码后不必停止整个部署，使用：

```bash
git pull && bash start.sh reload
```

这是仅重启机器人的轻量重载：先检查新源码语法和配置，再通知当前 Python 进程退出，随后启动新代码；不会停止 NapCat、释放唤醒锁或关闭终端，QQ 登录由 NapCat 保持。Python 重启会清空内存中的对话历史、互动窗口和等待队列，正在处理的请求可能中断；画像、跑团存档及表情包库保留。新版本新增依赖时，先安装 `requirements.txt`。如果原进程仍在退出，命令会报错并拒绝重复启动，稍后重试即可。仅执行 `git pull` 不会自动重载；首次使用本功能也可直接运行该命令，无需先停止 NapCat。真实 Termux/screen 环境仍需设备验证。

> **NapCat 的启动命令写在脚本开头**（`NAPCAT_CMD`），默认就是第 3 节那条 `proot-distro ...`。
> 启动命令不一样（装了 TUI-CLI、或 NapCat 直接跑在 Termux 里）也不用改脚本：
>
> ```bash
> NAPCAT_CMD='你的启动命令' bash start.sh
> ```
>
> 旁边的 `NAPCAT_PROC` 是"判断 NapCat 在不在跑"时匹配的进程关键词（默认 `napcat`，
> 容器方式下进程命令行里就带这个词）。脚本检查命令是否存在，看的是 `NAPCAT_CMD` 的第一个词。

> **为什么写 `bash start.sh` 而不是 `./start.sh`**：项目若放在共享存储（`/sdcard`、`~/storage/...`）上，
> 那个文件系统是 `noexec`，`./start.sh` 会 `Permission denied`，而且 `chmod +x` 在那儿是假的
> （命令成功返回、权限位根本没变）。`bash start.sh` 只需要读权限，绕过它。
> 想在任何目录直接敲 `qbot`，得先把项目搬到 Termux 私有目录（`~/` 下）再软链：

```bash
ln -sf "$PWD/start.sh" "$PREFIX/bin/qbot"
```

`start.sh` 判断"在不在跑"看的是**进程和端口**，不是 screen 会话是否存在——这两件事很容易搞混：

> `screen -S qbot` 里再敲 `python -m src.main`，程序退出后会掉回 shell 提示符，
> **会话依然活着**（`screen -ls` 显示 `(Detached)`，看不出任何异常）。于是出现
> "NapCat 明明挂了、Termux 却一切正常"的假象。脚本会识别这种空壳会话、清掉后重新拉起。

不用脚本的话，手动方式等价于：

```bash
screen -S napcat && proot-distro sh napcat -- bash -c "xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox"
screen -S qbot   && python -m src.main
screen -r qbot                      # 下次回来查看
```

> **`termux-wake-lock` 每次 Termux 重启后都要重新获取**——唤醒锁挂在 Termux 应用进程上，
> App 被杀或设备重启就释放了。`start.sh` 已经把它包含在启动流程里，不用再单独记。

> **机器人"静默失联"时先跑 `bash start.sh status`**。最常见的不是机器人挂了，而是
> **NapCat 活着、QQ 掉线**：WS 连接正常（心跳照应答），但 QQ 那条长连接断了，
> 于是收不到任何消息、控制台也不报错。是这种的话重新登录 QQ 即可（`bash start.sh webui`）。
>
> 不过它现在会自己说了：进程内置**健康自检**，每 60 秒主动查一次 QQ 在线状态。
>
> - **QQ 掉线** → 日志出现 `[🩺 OneBot] ⚠️ QQ 掉线`，并附上重新登录的指引
>   （这种情况下重连机器人没有用，必须人工重新登录 QQ）；恢复后再打一条。
> - **连接假死**（socket 还在但不再应答）→ 连续 2 次自检失败后自动断开重连。
> - **压根连不上**（NapCat 没跑、或 QQ 掉登录导致 `3001` 没开）→ 累计 5 分钟后告警
>   `[🩺 OneBot] ⚠️ 连不上协议端`，附上该跑哪条命令；此后每 10 分钟重复一次，
>   连上时打一条 `✓ 已恢复连接（本次断了 X 分钟）`。短暂闪断不吭声。
>   这段看门狗管不到——它是连接级的，只在连上之后才工作，所以是单独做的。
>
> 参数都在 `.env` 里（见上面的 OneBot 配置表）：`ONEBOT_PROBE_SEC=0` 可关掉自检，
> `ONEBOT_DOWN_ALERT_SEC=0` 可关掉"连不上"的告警。

HyperOS 的省电策略很激进，必须手动放行，否则会被杀后台：

- 设置 → 应用设置 → 应用管理 → Termux → 省电策略 → **无限制**
- 设置 → 应用设置 → 应用管理 → Termux → 权限 → **允许后台活动**
- 在多任务界面把 Termux **加锁**（下拉卡片点锁图标），防止一键清理时被干掉

### 5. 已知限制

- 平板本身只做转发与调度，**模型推理全在云端 API**，所以性能不是瓶颈；
  真正吃资源的是同时挂着 NapCat + Python，建议 8GB 内存以上的机型。
- Android 系统仍可能在极端情况下杀进程。若要求 7×24 稳定，
  更稳妥的做法是：NapCat 放在常开的 PC/云服务器上，平板只跑机器人并连过去
  （`.env` 里 `ONEBOT_WS_URL` 改成 `ws://<PC的局域网IP>:3001`，
  同时 NapCat 的 WS 服务端要监听 `0.0.0.0` 并**设置 access_token**）。


## 离线回归验收

以下检查使用假模型、假平台 API 和临时数据；表情包和代理检查只启动本机测试服务，不调用付费服务。

```bash
python tests/review_regressions.py
python tests/architecture_check.py
python tests/net_proxy_check.py
python tests/message_split_check.py
python tests/skill_loading_check.py
python tests/_degrade_check.py
python tests/state_isolation_check.py
python tests/sticker_check.py
python tests/history_compress_check.py
python tests/navi_check.py
python tests/sim_chat.py 4 5
bash -n start.sh
bash tests/reload_check.sh
```

历史审查与修复对照见 `docs/implementation-audit-2026-10-15.md` 和 `docs/implementation-fixes-2026-10-16.md`。
