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
    ├── vision_utils.py     # 图片识别 / 文生图
    ├── skills/             # Skill（Markdown 定义）
    └── tools/              # Tool Calling 工具
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
- **思考有总时限**：一轮最多想 `DEEPSEEK_ROUND_TIMEOUT_SEC`（默认 60 秒）。到点就**放弃这一轮**：一条消息都不发、不写对话历史，并按「AI 主动结束互动」处理（退出互动窗口，要再开口得等下一次唤醒）。就算模型在第 61 秒答完了，那句话也不会再进群；攒下的片段仍然留着。
- **@ 立即回应**：@ 立刻唤醒并开局，不存在等待窗口。
- **跑团模式**：进入 DM 模式后，未被 @ 的消息**直接丢弃**，群内闲聊不会污染跑团剧情；此模式不注入 `reply`/`skip`，@ 即回，行为与从前完全一致。
- **成本提示**：互动窗口内每条消息都会烧一次调用；把 `TRIGGER_ACTIVE_TIMEOUT_SEC` 调小、或者降低 `TRIGGER_PROBABILITY`，都能明显压成本。
- **图片可见性**：未 @ 的群消息里的图片也会被识别（`TRIGGER_RECOGNIZE_IMAGES`，默认开），描述以 `[图片1：…]` 形式写进上下文，所以机器人能对「群友发的图」有反应，而不是只看得见 @ 它的那些图。

### 关于图片识别的成本

开启 `TRIGGER_RECOGNIZE_IMAGES` 后，**群里每张图都会产生一次 VLM 调用**（按张计费，与「接话时只带 8 条上下文」无关——那限制的是 LLM 提示词，不是识别次数）。为此内置两层护栏：

| 护栏 | 说明 |
|---|---|
| `TRIGGER_IMAGE_RATE_PER_MIN` | 每个群每分钟最多识别 N 张图（默认 10），超出则跳过；**不会**退化成回复时再识别 |
| `TRIGGER_IMAGE_CACHE_SIZE` | 按图片 URL 缓存描述（默认 200 条），同一条消息重放或同一张图被反复引用时不重复计费 |

若发现费用或延迟偏高，可先把 `TRIGGER_RECOGNIZE_IMAGES=false` 关掉（回到「只在被 @ 时识别」），或调低 `VISION_HIGH_RES` / 关闭 `VISION_ENABLE_THINKING`。

> 识别在触发判定**之前**完成，且失败不会污染上下文：任一张识别失败就整体退回「未识别」，等真要回复时再走一次带提问的完整识别。

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

## 使用方法

| 操作 | 方式 |
|------|------|
| 和机器人聊天 | 群聊 `@机器人 你的问题`；私聊直接发消息 |
| 查看全部指令 | `@机器人 /help` |
| 清除记忆 | `@机器人 /reset` |
| 图片识别 | 发送图片并 `@机器人`，可附带问题（群里未 @ 的图片机器人也看得见） |
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
| `BOT_SYSTEM_PROMPT` | 系统提示词 | 友好群聊助手 |
| `MAX_HISTORY_LENGTH` | 每群记住几轮对话 | `10` |

### 视觉与文生图

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DASHSCOPE_API_KEY` | 阿里云百炼 API Key（图片识别/文生图） | 空（未配置则跳过该功能） |
| `VISION_BASE_URL` | 视觉模型地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `VISION_MODEL` | 视觉模型 | `qwen3-vl-flash` |
| `VISION_ENABLE_THINKING` | 视觉模型思考模式 | `false` |
| `VISION_HIGH_RES` | 高分辨率识别 | `false` |
| `IMAGE_GEN_MODEL` | 文生图模型 | `qwen-image-3.0-pro` |
| `IMAGE_GEN_ENDPOINT` | 文生图端点（DashScope 原生接口） | 官方端点 |

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
