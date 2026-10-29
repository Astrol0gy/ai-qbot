# 配置参考

## 管理员

`ADMIN_QQ` 为允许管理的 OneBot 私聊发送者 QQ，留空禁用。本项目部署设置为 `523155725`；只能直接编辑部署配置，禁止通过私聊修改。命令、自然语言和操作范围见 [管理控制台](admin-console.md)。

## 普通聊天

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `BOT_CHAT_PERSONA` | 在基础人设后追加兴趣与相处分寸，已有 `BOT_SYSTEM_PROMPT` 仍保留；不覆盖跑团角色 | 游戏、音乐、电影、动漫、漫画、ACG、电脑技术，以及宗教与哲学兴趣；政治审慎，冲突时适度卖萌化解 |
| `CHAT_MAX_SEGMENTS` | 按空行扩展的总条数预算；1 关闭拆分，范围 1..3；普通接话再限制为最多两段，详细任务按配置值 | `3` |
| `CHAT_BRIEF_MAX_CHARS` | 群聊和私聊普通回复的整轮文字上限，范围20..2000；[img]标记不计入 | `80` |
| `CHAT_STYLE_SKILL_ENABLE` | 文字回答前按需加载去AI味聊天适配，成稿改写再读完整上游规则 | `true` |
| `CHAT_INTENT_ENABLE` | 独立意图判断；关闭恢复原概率及reply/skip路径 | `true` |
| `CHAT_INTENT_MODEL` | 意图模型，留空继承主模型；共用主接口及Key | 继承`DEEPSEEK_MODEL` |
| `CHAT_INTENT_MAX_TOKENS` | 意图单次输出预算，含思考，范围256..16384 | `4096` |
| `CHAT_COLLECT_SEC` | OneBot连续输入安静窗口（秒） | `1.2` |
| `CHAT_MAX_COLLECT_SEC` | 连续输入最多收集秒数 | `4` |
| `CHAT_WAIT_TIMEOUT_SEC` | 等待补充的单次复核延迟（秒） | `12` |
| `CHAT_SEGMENT_INTERVAL_SEC` | 普通聊天相邻消息的等待秒数，范围 0..3 | `0.6` |

分段由模型选择自然语义或语气停顿，以空行表示；没有空行就保持一条，不按字数强切。超过分段预算时合并剩余段落，保留完整内容。是否拆分按每条最终回复的内容判断，调用搜索、导航等工具后的普通文字解释也可分条；跑团、带图片标记的回复、链接、代码、列表和表格保持完整。同一回应只在第一段引用原消息，后续段落接着说；发送失败或回执未知时停止后续发送，历史只记录实际投递内容。

普通回复默认一两句，通常20到40字，群聊和私聊整轮最多`CHAT_BRIEF_MAX_CHARS`字符（默认80，中英文、数字、标点均计入），自然分段最多两段。超长草稿不发送、不写成已回答历史，程序反馈实际字符数并要求重写；仍不合规则结束本轮。不会直接截断代码、链接或句子。仅出现“代码、配置、报错”等技术词不再自动放宽；明确要求详细解释、完整代码/文章、翻译或成稿改写时保留完整内容。旧背景和图片描述不能触发放宽。

默认启用`lieflat-less-ai-tone`表达技能。首轮只提供技能名称和简介，文字回答前经`load_skill`读取本地聊天适配，用户要求去AI味或改写成稿时再由`read_skill_resource`读取完整上游规则；不会把完整技能预拼进系统提示，也不增加独立的改写agent。纯表情包回复不必加载表达技能，跑团不强制加载。

聊天适配控制空起手式、套话、同义复述和无请求的建议/追问；成稿清理遵守上游白名单，不删改事实、限定词、代码、引用或原文结构。上游固定提交`27d29232f10124db904ca9c0536d0b67cb3b2833`，MIT许可和未修改的原文保存在`src/skills/lieflat-less-ai-tone/`；来源见[上游项目](https://github.com/larashero3-dotcom/lieflat-less-ai-tone)。关闭`CHAT_STYLE_SKILL_ENABLE`可跳过表达技能，但字数预算仍生效。每次需要文字回答时通常增加一次技能读取往返，静默判断不加载技能。

默认开启独立意图agent（`CHAT_INTENT_ENABLE=true`）：符合群名单等过滤条件的普通群消息，按同一作者连续消息合并后判断。不同作者分别判断，彼此的原话仍作为关系背景，旁人插话不覆盖前面的提问。agent共用主接口、Key和思考配置，只有JSON输出，没有技能、文件写入或平台发送权限。

输出包含参与动作、对象、表达完整性、目标消息ID、相关背景ID和简短依据，程序校验枚举、ID与一致性后执行：

- `REPLY`：明确需要参与且表达完整，主模型只回答选定目标，不再调用reply/skip决定参与。正文由程序做长度、搜索和引用校验后投递。
- `WAIT`：保留片段，按群、用户等待补充；安静`CHAT_WAIT_TIMEOUT_SEC`秒（默认12秒）后复核一次，仍未完整就保持安静，不循环计时。
- `IGNORE`：只跳过当前内容并标为背景，不关闭或延长互动窗口。
- `END`：仅用于明确结束与机器人的整个互动，关闭窗口；“没什么”或取消单个任务不自动结束聊天。

agent引用未知ID或输出协议不合法时纠正一次，仍失败则结束本轮，不绕过判断调用主模型。`chat.intent.decision`记录决定和简短依据，`intent.request`记录请求耗时，便于检查对象与完整性判断。不会输出隐藏思考过程。

OneBot先等安静`CHAT_COLLECT_SEC`秒（默认1.2秒），持续输入最多收集`CHAT_MAX_COLLECT_SEC`秒（默认4秒），批次最多32条。私聊、命令和跑团不参与合并；命令构成批次边界。未回答片段与已消费的近期原话分别有界保留，各最多`TRIGGER_CONTEXT_MESSAGES`条，后者用于理解群友对象及指代；`/reset`同时清除。图片另保留每条最多4000字的识别事实，回答阶段对选中的图片使用事实，不只靠路由短摘要。判断历史只读最近三轮已投递对话，每条最多2400字。

判断前、判断完成后、状态提交及各段发送前检查原用户是否有相关补充入队；有补充时丢弃旧草稿，留给下一轮合并，包括已搜索或选过表情的轮次。已发起的付费生图不能靠撤销本地轮次回滚，这类轮次保留结果，补充随后处理，避免重复计费。明确给其他人的新消息不会抑制当前回复。同一批按作者保留完整片段，等待复核只消费该作者的消息；兼容模式仍向模型提供其他作者的只读背景。技能仍按目录→正文→资源渐进加载；用户明确要求网页搜索时，必须实际尝试web_search后才允许提交回答。未提交草稿不会写作已回答历史，发送历史以回执为准。

默认直接发送，`reply`控制工具仅提交回答，不等于QQ引用排版。仅用户要求引用或跨话题指代需要时，主模型调用`quote_message(message_id)`选择本轮具体消息；兼容模式按需填`reply_to`。表情始终独立发送，不继承文字的引用。

新模式不再用`TRIGGER_PROBABILITY`或`TRIGGER_MIN_LEN`提前丢弃候选，每个作者批次至少一次意图请求，REPLY再增加回答请求，应按实际群流量观察费用与延迟。设`CHAT_INTENT_ENABLE=false`并reload可恢复概率触发和主模型reply/skip的兼容路径；其格式纠正仍使用一次关闭思考的`tool_choice=required`请求。

兴趣影响参与倾向，不增加关键词强制唤醒。遇到辱骂或激烈分歧时优先轻松卖萌、适度自嘲或不接话，不对骂、不阴阳怪气，也不以玩笑敷衍真实求助。

### 后台群聊记忆

| 配置 | 作用 | 默认 |
| --- | --- | --- |
| `GROUP_MEMORY_ENABLE` | 群聊观察、后台整理与相关记忆读取；关闭不删除现有文件 | `true` |
| `GROUP_MEMORY_MODEL` | 整理模型，留空继承主模型，使用同一接口及Key | 继承主模型 |
| `GROUP_MEMORY_BATCH_SIZE` | 新增消息达到多少条后整理，范围3–96 | `20` |
| `GROUP_MEMORY_INTERVAL_SEC` | 最早待处理消息超过此时长后也整理，单条尾消息也处理；范围10–86400秒 | `180` |
| `GROUP_MEMORY_MAX_TOKENS` | 单次结构化输出预算，范围512–16384 | `4096` |

整理固定关闭思考、温度0.2，后台全局单并发；不是每条消息一次模型调用，但会增加独立的模型用量。观察入口不等整理模型，失败也不会让机器人发送错误提示。原消息和整理结论位于`data/group_memory/`，按群隔离并带证据。关闭并reload可停止观察、整理和读取；原有独立意图判断及即时偏好工具继续工作。

说话习惯至少3条本人消息支持，词典至少2条；只有置信度不低于0.8且无歧义、未过期的相关结论参与理解。全部候选被拒时纠正一次，再次失败保留待处理消息；合法空结果正常完成。`memory.validation.rejected`只记录拒绝类别和数量。私聊、命令、自身消息、跑团和名单排除群不采集。官方通道只能整理实际收到的@消息，OneBot可整理无需回复的普通群聊。`/reset`清理本轮、内存及落盘对话，不删除已保存的画像或群聊记忆；具体存储上限、保留期与恢复行为见[架构说明](architecture.md)。

活跃期不代表全群在和机器人对话。结构化@与引用作者是优先对象依据，明确给其他人的消息（未同时@机器人）只留作背景；引用自己不算发给别人。仅@保留作者、消息ID与完整呼唤语义。入站消息、WAIT和IGNORE都不续期，实际回复成功后才重置窗口。自然语言中的“你”由agent结合近期原话和话题判断；图片可见、谈论机器人或兴趣相关都不代表请求机器人回答。

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
| `TRIGGER_PROBABILITY` | 仅关闭独立意图agent时：空闲未 @ 群消息唤醒概率 | `0.1` |
| `TRIGGER_ACTIVE_TIMEOUT_SEC` | 互动窗口持续秒数；实际回复后重置，入站消息和 wait 不续期 | `60` |
| `TRIGGER_MIN_LEN` | 仅关闭独立意图agent时：未 @ 消息最小唤醒长度 | `2` |
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

只有识图明确判定为“表情包/梗图”，结构化采集判定确认可复用反应、无需依赖特定内容并有可见依据，且有唯一有效标签行时才采集。低清、动漫、截图和配字本身不是表情包依据；矛盾、不确定、旧格式或附加问答结果不授权入库。识图本身仍需读取图片字节，这不代表已保存到表情包库。已有图片不自动删除，发送前由主模型读取候选真实画面再次核对。

主模型看候选时每张最大512px，动图提供首/中/末帧；无法预览的本地候选不能发送。OneBot以`sub_type=1`发表情，发送副本最大边320px、最大2MiB，保留动画与透明；原图、标签与RAG向量不变。派生缓存自动限制为128文件/64MiB。默认普通回答不引用；表情不继承文字的引用。

OneBot 发送时会校验库内文件并读取为 `base64://` 图片，文字与每张表情包分别发送；NapCat 使用 Docker 时无需挂载表情包目录。仅发表情包遇到文件不可读、超限或明确的文件预处理错误时，尝试一次文字兜底，仍失败则结束；真正的发送超时不会重试或补发文字，以免重复。无需额外配置。

**语义检索**：`send_sticker` 默认用 Chinese-CLIP 量化 ONNX 对图片和 `index.json` 里的名称/标签做 embedding，按余弦相似度返回 top3，由模型挑最贴合的一条。每个表情包同时保留图片向量和文字向量，分数为 `0.7 * 图片相似度 + 0.3 * 标签相似度`；query 命中标签时再给一个小加分（完全相等 +0.12，两个字以上互相包含 +0.06），避免「卖萌」「无语」这类明确标签被图片向量挤掉。向量缓存在 `data/stickers/embeddings.json`；新表情包入库时只补这一张，不重建全库（图片 hash 或标签变化时也只重算对应部分）。模型未加载、索引未就绪、推理失败或没有过阈值的结果时自动回退到原有标签匹配，不影响聊天。

模型文件不提交到 git，部署时运行 `bash scripts/download_sticker_model.sh` 下载（约 190MB），并在项目根执行 `python -m src.sticker_rag --build` 预建索引。运行时依赖 `onnxruntime/numpy/Pillow/tokenizers`，不需要 torch。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `STICKER_ENABLE` | 总开关：关掉后既不采集也不发（两个工具都会说自己关着，回复里的库内路径也不再认） | `true` |
| `STICKER_DIR` | 库目录，留空 = `<项目根>/data/stickers`（相对路径按项目根解析） | 空 |
| `STICKER_MAX_FILES` | 库容量上限，超出淘汰最久未使用的那张 | `500` |
| `STICKER_MAX_BYTES` | 单张采集及发送大小上限（字节），动图尤其容易超标 | `5242880`（5MB） |
| `STICKER_MAX_PER_REPLY` | 一轮回复最多发几张 | `1` |
| `STICKER_RAG_ENABLE` | 是否启用 CLIP 语义检索；关闭后只走标签匹配 | `true` |
| `STICKER_RAG_MODEL_DIR` | ONNX 模型目录，留空 = `<项目根>/data/models/chinese-clip-vit-base-patch16` | 空 |
| `STICKER_RAG_MIN_SCORE` | 返回候选的最低余弦相似度 | `0.15` |
| `STICKER_RAG_TOP_K` | 返回给模型的候选数 | `3` |

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

