# 配置参考

## 管理员

`ADMIN_QQ` 为允许管理的 OneBot 私聊发送者 QQ，留空禁用。本项目部署设置为 `523155725`；只能直接编辑部署配置，禁止通过私聊修改。命令、自然语言和操作范围见 [管理控制台](admin-console.md)。

## 普通聊天

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `BOT_CHAT_PERSONA` | 在基础人设后追加兴趣与相处分寸，已有 `BOT_SYSTEM_PROMPT` 仍保留；不覆盖跑团角色 | 游戏、音乐、电影、动漫、漫画、ACG、电脑技术，以及宗教与哲学兴趣；政治审慎，冲突时适度卖萌化解 |
| `CHAT_MAX_SEGMENTS` | 按空行扩展的总条数预算；1 关闭拆分，范围 1..3；普通接话再限制为最多两段，详细任务按配置值 | `3` |
| `CHAT_SEGMENT_INTERVAL_SEC` | 普通聊天相邻消息的等待秒数，范围 0..3 | `0.6` |

分段由模型选择自然语义或语气停顿，以空行表示；没有空行就保持一条，不按字数强切。超过分段预算时合并剩余段落，保留完整内容。是否拆分按每条最终回复的内容判断，调用搜索、导航等工具后的普通文字解释也可分条；跑团、带图片标记的回复、链接、代码、列表和表格保持完整。同一回应只在第一段引用原消息，后续段落接着说；发送失败或回执未知时停止后续发送，历史只记录实际投递内容。

普通接话默认一句、一条，通常80字以内；分条不增加总内容量。普通群聊 `reply` 工具对整轮文字合计校验160字、最多两次回复，超限的调用不入队并要求模型压缩，不直接截断。当前消息含详细说明、代码、步骤、翻译等明确任务信号时放宽；旧背景不会自动触发放宽。普通私聊同样提示简短并限制自动分段为两段，160字工具校验仅适用于群聊 reply。技术任务和明确要求展开的内容按需要完整表达。

群聊必须由 reply/skip 明确决定参与，模型正文不会自动转成发言；未作决定时提醒一次，仍无决定则保持安静且不写入对话历史。私聊和跑团保留原回复方式。适合用表情包时，聊天规则提示按需加载「图片理解」，工具与完整技能正文仍渐进加载，不把全文塞入提示。

未提交的正文草稿会丢弃，不作为已发送的助手消息放回上下文。纠正决定的那次请求关闭思考并使用 `tool_choice=required`，兼容当前接口不支持思考模式强制调用工具的限制；正常请求仍使用配置的思考模式。提交 reply/skip 后结束本轮，不再追加模型请求。群聊明确要求网页搜索时，reply 前校验是否实际调用过 web_search；搜索失败可如实说明，关闭搜索时可直接说明不可用。“查一下本地配置”等本地操作不因此强制联网。

OneBot普通群聊先收集连续消息：安静 `CHAT_COLLECT_SEC` 秒（默认1.2秒）后合并评估，持续输入最多收集 `CHAT_MAX_COLLECT_SEC` 秒（默认4秒）。保留各条作者、@与引用关系，选最后一个符合唤醒条件的消息作为焦点；私聊、命令和跑团不参与合并，命令构成批次边界。批次最多32条，上下文仍受 `TRIGGER_CONTEXT_MESSAGES` 限制。

`skip(reason=wait)` 会保留上下文并进入按群、用户区分的等待状态。同一用户的新补充免概率唤醒；仍未说完可继续等待。安静 `CHAT_WAIT_TIMEOUT_SEC` 秒（默认12秒）后只复核一次，没有参与必要就保持安静，将旧片段标为背景，不循环定时调用；不保证超时发言。`/reset` 会清除等待。`skip(reason=irrelevant)` 表示本轮不参与，旧片段仅作背景。skip 不撤销已排好的回复。明确提问但指代不清时结合最近图文理解，必要时澄清。

普通聊天在模型开始前、状态提交前及各段发送前检查同一用户是否又有相关消息入队：有补充时不发送旧草稿，保留原上下文供下一轮合并。明确发给别人的消息不会抑制当前回复。已经调用业务工具的轮次继续完成，防止重做有副作用的工具；补充随后处理。已经送入平台的消息无法撤回。reply 必须提交 `recipient`（bot/group/others/unclear）和 `expression`（complete/incomplete），非参与对象或未完表达不能入发送队列，判断会写入 `chat.assessment` 日志。

兴趣影响参与倾向，不增加关键词强制唤醒。遇到辱骂或激烈分歧时优先轻松卖萌、适度自嘲或不接话，不对骂、不阴阳怪气，也不以玩笑敷衍真实求助。

活跃期不代表全群在和机器人对话。OneBot 保留结构化 @ 对象和引用作者，明确 @ 其他人或引用其他成员时（未同时 @ 机器人），本条只留作背景、不唤醒；引用自己不算发给别人。原参与者及引用机器人的消息在有效窗口内可免掷骰，其他成员仍按概率判断。入站消息和 wait 不续期，irrelevant 退出窗口；实际回复成功后才重置窗口并记录参与者。指代仍由模型结合这些关系和话题判断，谈论机器人不等于邀请它接话。

配置写入项目根目录 `.env`，不要提交凭据。完整模板见 [`.env.example`](../.env.example)。


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
| `TRIGGER_ACTIVE_TIMEOUT_SEC` | 互动窗口持续秒数；实际回复后重置，入站消息和 wait 不续期 | `60` |
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
| `TOOL_CALL_NOTICE_SEC` | 每次工具执行的提示阈值（秒），提示对应中文技能名并继续等待 | `30` |
| `DEEPSEEK_THINKING_NOTICE_SEC` | 长考提示阈值（秒），提示一次后继续等待；兼容旧 `DEEPSEEK_ROUND_TIMEOUT_SEC` | `60` |
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

只有识图首行明确判定为“表情包/梗图”，且有有效标签行时才采集。内容图片、非表情包、不确定或缺失类型都不入队、不为采集再次下载，即使附带标签也不收。识图本身仍需读取图片字节，这不代表已保存到表情包库。

OneBot 发送时会校验库内文件并读取为 `base64://` 图片，文字与每张表情包分别发送；NapCat 使用 Docker 时无需挂载表情包目录。仅发表情包遇到文件不可读、超限或明确的文件预处理错误时，尝试一次文字兜底，仍失败则结束；真正的发送超时不会重试或补发文字，以免重复。无需额外配置。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `STICKER_ENABLE` | 总开关：关掉后既不采集也不发（两个工具都会说自己关着，回复里的库内路径也不再认） | `true` |
| `STICKER_DIR` | 库目录，留空 = `<项目根>/data/stickers`（相对路径按项目根解析） | 空 |
| `STICKER_MAX_FILES` | 库容量上限，超出淘汰最久未使用的那张 | `500` |
| `STICKER_MAX_BYTES` | 单张采集及发送大小上限（字节），动图尤其容易超标 | `5242880`（5MB） |
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
不会被误判成 Key 报废。`AMAP_TIMEOUT_SEC`（默认 8 秒）限制建立连接的等待；连接成功后继续等待响应，工具提示阈值由 `TOOL_CALL_NOTICE_SEC` 控制。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `AMAP_KEY` | 高德 Web 服务 Key（需开通「搜索POI 2.0」「路径规划 2.0」） | 空（未配置则两个工具都回报没配置） |
| `AMAP_TIMEOUT_SEC` | 单次请求连接超时（秒） | `8` |
| `AMAP_POI_LIMIT` | 每次搜索默认返回几条（1~25） | `5` |

`tests/navi_check.py` 把这条链路钉住（全离线，只打桩高德的 HTTP 请求，走真 registry）：
工具注册与 schema、搜不到/关键词超长/near 传地名等参数校验、`city_limit` 与 `show_fields=cost` 真的发出去了、
空数组地址不显示成 `[]`、类型末级重复会合并、响应里没有 `cost` 时不编耗时、不限行时不乱下结论、
地名换坐标、公交的中文城市名换 citycode（同城只解析一次）、公交缺 `city` 的劝停、各类错误码的降级措辞。

`tests/navi_live.py` 打真接口（要 `AMAP_KEY`，约 10 次配额），核对**高德返回的字段形状**跟我们解析的假设一致：
`business` 与 `cost` 的字段名、`paths[0].cost.duration` 是否真存在、`departure_stop` 是对象还是字符串、
公交 `transits[0]` 有没有 `duration`、中文城市名是不是真被拒 —— 并把工具实际输出打印出来。没配 Key 时以退出码 2 报错，
不会假装通过。**改了字段解析就两个都跑**：离线版只证明我们发得对，证明不了返回长这样。

