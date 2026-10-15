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
    ├── trigger.py          # 触发策略（概率接话 / 冷却 / DM 模式过滤）
    ├── channels/           # 通道适配层
    │   ├── base.py             # IncomingMessage / Sender 契约
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
    ├── skills/             # Skill（Markdown 定义）
    └── tools/              # Tool Calling 工具（Provider：search/image/video/profile/campaign/navi/chat）
```

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
 ├─ 私聊 ──────────────────────────────── 唤醒（不注入 skip，必回）
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
- **互动中**：一旦开了局（概率命中或被人 @），**每条消息都会叫醒 AI**，不再掷骰子。所以它能看到"同一个人连发三条"，也能判断"有人在接我的话"。
- **计时从回复完成开始**：AI 每处理完一轮（开口说话、或选择 `skip`）都重置窗口；`TRIGGER_ACTIVE_TIMEOUT_SEC` 秒内没人说话就退出互动，回到空闲的概率唤醒。想一件事想得久不会白白吃掉窗口。
- **AI 自己决定**：普通群聊唤醒时给模型注入两个控制类工具——`reply(text, reply_to?)` 回话、`skip()` 这轮不开口。上下文片段里每条都带说话者、消息 id、是否 @ 了机器人、引用了谁的话。
- **多人同时接话**：`reply` 可以多次调用（单轮上限 6 条），几个人分别跟它说话就各回一条，各引用各自的最后一句话。
- **思考期间不排队**：一轮还在处理时新到的消息**不会各起一次调用**（旧实现是一人一句排成一队、各自成局，于是"一直不说话"然后迟到地回最早那条），而是合并进紧接着的下一轮——下一轮的上下文**重取**，所以 A、B、C 说了什么它一次就能看到。回复后只清理「本轮开始时已经看到」的消息，思考期间新到的留给下一轮。
- **skip 不消费上下文**：它选择不开口时（群里互相聊天、或觉得对方还没说完），攒下的片段会留着，下一条消息来时连同新消息一起重新交给它——「等人说完再回」是它自己判断的结果。
- **skip 不撤销已排队的回复**：本轮已经用 `reply` 排好的回复照常发出，`skip` 只表示"不再补充"；只有一条都没排时，它才表示"本轮不开口"。（模型常把 `skip` 当"我说完了"用，旧实现会顺手撤掉已攒回复，于是它撤了又重发——群里看不出异常，但白烧一次调用、多拖十几秒。）
- **收尾不用调工具**：回复排好后直接停止调用工具，本轮即结束；`reply` 的返回文案里也会这么提示，免得它再拿 `skip` 去"收尾"。
- **思考有总时限**：一轮最多想 `DEEPSEEK_ROUND_TIMEOUT_SEC`（默认 60 秒）。到点就**放弃这一轮**：一条消息都不发、不写对话历史，并按「AI 主动结束互动」处理（退出互动窗口，要再开口得等下一次唤醒）。就算模型在第 61 秒答完了，那句话也不会再进群；攒下的片段仍然留着。
- **@ 立即回应**：@ 立刻唤醒并开局，不存在等待窗口。
- **跑团模式**：进入 DM 模式后，未被 @ 的消息**直接丢弃**，群内闲聊不会污染跑团剧情；此模式不注入 `reply`/`skip`，@ 即回，行为与从前完全一致。
- **成本提示**：互动窗口内每条消息都会烧一次调用；把 `TRIGGER_ACTIVE_TIMEOUT_SEC` 调小、或者降低 `TRIGGER_PROBABILITY`，都能明显压成本。
- **图片可见性**：未 @ 的群消息里的图片也会被识别（`TRIGGER_RECOGNIZE_IMAGES`，默认开），描述以 `[图片1：…]` 形式写进上下文，所以机器人能对「群友发的图」有反应，而不是只看得见 @ 它的那些图。

### 关于图片识别的成本

开启 `TRIGGER_RECOGNIZE_IMAGES` 后，**群里每张图都会产生一次视觉调用**（按张计费、单张最多 1024 tokens，与「接话时只带 8 条上下文」无关——那限制的是 LLM 提示词，不是识别次数）。为此内置两层护栏：

| 护栏 | 说明 |
|---|---|
| `TRIGGER_IMAGE_RATE_PER_MIN` | 每个群每分钟最多识别 N 张图（默认 10），超出则跳过；**不会**退化成回复时再识别 |
| `TRIGGER_IMAGE_CACHE_SIZE` | 按图片 URL 缓存描述（默认 200 条），同一条消息重放或同一张图被反复引用时不重复计费 |
| `VISION_FAIL_BACKOFF_SEC` | 一张图识别失败后的退避窗口（默认 600 秒），窗口内不再下载、不再调用 |
| `ONEBOT_IMAGE_CONCURRENCY` | 图片预识别的并发额度（默认 2），**独立于**对话闸门，识别慢也不挡别的会话 |

若发现费用或延迟偏高，可先把 `TRIGGER_RECOGNIZE_IMAGES=false` 关掉（回到「只在被 @ 时识别」），或把 `VISION_DETAIL` 设为 `low`（图片缩到 512×512，最快最省，代价是小字会糊）。

> 识别在触发判定**之前**完成，且失败不会拖住对话：**逐张尽力**——认出几张算几张，认不出的那张在上下文里留中性占位 `[图片，未能读取]`（主模型据此知道"用户确实发过图"，但不会拿到"识别失败"去反复琢磨）。同一张图失败后 `VISION_FAIL_BACKOFF_SEC`（默认 600 秒）内不再下载、不再调用；识别请求本身读超时只有 `VISION_TIMEOUT_SEC`（默认 25 秒）且不重试。

### 关于发送可靠性（为什么超时不重发）

协议端「调用超时」不等于「消息没送达」。NapCat 在内部发送超时时会回 `retcode=1200`（`NTEvent .../sendMsg Timeout`），**而消息这时通常已经进群了**——把它当失败去重试，群里就会出现同一句话发两三遍。所以发送的每一步都区分四种结局：

| 结局 | 何时出现 | 处理 |
|---|---|---|
| `ok` | `retcode=0/1` | 完成 |
| `offline` | 连接不在、请求没发出去 | **确定没送达 → 重试（最多 3 次，间隔 2s/4s）** |
| `unknown` | 客户端超时、`retcode=1200/1201`、回执里带 `Timeout` | **可能已送达 → 立刻停止，日志提示「发送结果未知」** |
| `rejected` | 其他非 0 `retcode`（参数错、风控） | 不再重试（重发也是同样的结果） |

读类接口（`get_msg` / `get_status` / `get_login_info`）不关心这个区别，仍走 `call_api()` 的「失败返回 None」。

### 关于思考超时（到点就放弃这一轮）

模型调用**如果不设上限，一轮能拖几十分钟**：SDK 默认 `read=600s`，而且失败还会重试（`max_retries`）。这期间这条会话的处理链一直堵着，`ONEBOT_MAX_CONCURRENCY` 个会话同时卡住，别的群也要排队。所以一轮思考有总时限 `DEEPSEEK_ROUND_TIMEOUT_SEC`（默认 60 秒），到点后：

| 项目 | 行为 |
|---|---|
| 发消息 | **一条都不发**——即便模型在第 61 秒答完了也丢弃（超时才到的回答，在群里已经过时了） |
| 对话历史 | 不写（轮次被取消，历史里不会留下半截） |
| 互动状态 | 按「AI 主动结束互动」处理：退出互动窗口，要再开口得等下一次唤醒 |
| 上下文片段 | **留着**（和 `skip` 一致），下一条消息来时连同新消息一起交给 AI |

实现上不是 `asyncio.wait_for`（它取消之后会等对方真正结束，一个"吞掉取消"的请求照样能把流程拖住），而是 `asyncio.wait` + 到点直接 `cancel()` 走人——所以"晚到的回答"从机制上就没有发送的路径。`tests/sim_chat.py 4` 用打桩模型钉住了这条：模型拖到第 12 秒才返回、且取消没拦住，群里依然是 0 条。

### 关于服务端不可用（熔断，快速失败）

上面那条 60 秒总时限在**服务端整体挂掉**时反而成了负担：DeepSeek 返回 503 / 连不上时，每一轮都会规规矩矩地硬等满 60 秒才放弃，群里连着几轮等不到回复，看着就像"bot 哑了"（2026-10-15 的真实日志）。

所以连续失败会熔断：超时、5xx、连接失败累计到 `DEEPSEEK_API_FAIL_LIMIT`（默认 3）次后，接下来 `DEEPSEEK_API_COOLDOWN_SEC`（默认 30 秒）内不再发起请求——**不回话也比两分钟后才回话强**：

| 场景 | 行为 |
|---|---|
| 熔断期内收到群唤醒 | 立刻结束本轮（不发请求、不发消息、退出互动），片段留着等下次唤醒 |
| 熔断期内被 @ / 私聊 | 同样不发请求，只回一句「我这会儿连不上模型服务，过一会儿再叫我试试～」 |
| 一次成功 | 失败计数清零、熔断解除（日志打印「服务恢复」） |

> 视觉侧是同一套思路但更保守：读超时 25 秒、不重试、失败退避，认不出就留占位继续答——看图只是配菜，不该拖住对话。

`tests/_degrade_check.py` 把这一整条降级链钉住（全离线，不打桩模型就是假客户端）：QQ 域名跳过直传、失败退避不重复下载、一张好一张坏时逐张尽力、`choices` 为 None 时不再抛裸错、连续失败熔断后唤醒静默且不发请求、恢复后正常回话。

## 表情包

机器人会**自己攒表情包**，也会**主动发**：群里或私聊中出现的图被识别为「表情包／梗图」时，原图会下载进本地库；
此后它想表达情绪，就从库里挑一张贴切的，随自己的话一起发出去。

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
`[img]<库里的文件路径>[/img]`，模型把这一行放进回复，那张图就随这句话一起发出（这一行本身不会显示给用户）；
**挑不到就不返回路径**（不会硬塞一张不相关的图），只发图不配字也允许。回复里的 `[img]` 是网址就当网络图发、
是库内路径就当表情包发，库外的本地路径一律丢掉（模型抄错或编出来的路径不该进发送环节）。用法规则见 `src/skills/image.md`。

| 通道 | 能否发本地表情包 |
|------|:---:|
| OneBot（NapCatQQ） | 是，直接把本地绝对路径填进 `image` 段（协议端与机器人在同一台机器，Termux 单机部署就是这么跑的） |
| 官方通道 | **否**，官方富媒体必须先把文件上传换 `file_id`。这里降级为纯文字：有文字就发文字，没有就这一轮什么也不发（日志会写明丢了几张） |

跑团（DM）模式下**不往 system prompt 里注入技能指引**（库里有什么、怎么发都不提），免得它往剧情里甩梗图 ——
但采集照常（识别到表情包就入库）；工具本身仍在（跑团里真调了还是会发），分寸靠工具说明自己把关。

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
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `DEEPSEEK_TEMPERATURE` | 创造性（思考模式下忽略） | `1.0` |
| `DEEPSEEK_MAX_TOKENS` | 最大回复长度 | `2048` |
| `DEEPSEEK_TOP_P` | 核采样（思考模式下忽略） | `1.0` |
| `DEEPSEEK_ENABLE_THINKING` | V4 深度思考模式 | `false` |
| `DEEPSEEK_REASONING_EFFORT` | 思考强度 (`high`/`max`) | `high` |
| `DEEPSEEK_ENABLE_SEARCH` | 联网搜索 | `false` |
| `DEEPSEEK_ROUND_TIMEOUT_SEC` | 单轮思考总时限（秒），到点放弃本轮、不发消息 | `60` |
| `DEEPSEEK_API_FAIL_LIMIT` | 连续失败几次后熔断（不再发起请求，快速放弃本轮） | `3` |
| `DEEPSEEK_API_COOLDOWN_SEC` | 熔断后冷却多少秒 | `30` |
| `BOT_SYSTEM_PROMPT` | 系统提示词 | 友好群聊助手 |
| `MAX_HISTORY_LENGTH` | 每群记住几轮对话 | `10` |

### 图片理解与文生图

**看图走 DeepSeek 官方视觉接口**（`deepseek-flash`），与文字对话共用 `DEEPSEEK_API_KEY`：支持 JPEG/PNG/GIF/WebP（GIF 动图会看到画面里的动作变化）；优先直传图片 URL，服务端拉不到的图（如 QQ CDN）会自己下载后用 base64 或 Files API 送进去（同一张图复用 `file_id`，不重复上传）。文档：[图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[Files API](https://api-docs.deepseek.com/zh-cn/guides/files_api/)。

**描述的第一行固定是类型判读**：`类型：表情包/内容图片 ｜ 意图：<发送者想表达什么>`。表情包／梗图只写情绪与一句话画面，内容图片才做详细描述——主模型据此决定是**顺着情绪接话**还是**按内容回答**（规则见 `src/skills/image.md`）。这一行必须排在最前：描述进接话上下文时按 `IMAGE_NOTE_MAX` 截断，写在后面会被截掉。

**判定为表情包时，最后还会多一行五个标签**：`标签：情绪｜动作｜主体｜风格｜文字`。这一行不占额外调用（同一次看图请求里要出来的），
是给人看表情包库归档用的，进上下文前会被剥掉（见上面「表情包」一节）。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `VISION_BASE_URL` | 视觉接口地址 | `https://api.deepseek.com` |
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
| `bash start.sh status` | 查看**进程是否真活着** / 端口 / **QQ 是否在线** / 日志尾部 |
| `bash start.sh webui` | 打开 NapCat 的 WebUI（**扫码登录 QQ 用**，自动读 token 并弹出浏览器） |
| `bash start.sh log` | 实时跟随机器人日志 |
| `bash start.sh stop` | 停止机器人 + NapCat 并释放唤醒锁 |

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
