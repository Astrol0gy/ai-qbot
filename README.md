# ai-qbot

基于 DeepSeek 的 QQ AI 机器人，支持 QQ 官方机器人与 OneBot 双通道。当前以 **Linux 服务器 + Conda + NapCat Docker** 部署。

## 功能

- 群聊与私聊，会话独立、同会话消息按顺序处理，不同会话并发。
- 普通聊天可按语义自然分条，选择性参与话题，旧消息只作背景。
- 深度思考、联网搜索、图片理解、文生图与地图导航。
- 用户偏好、跑团存档、对话记忆摘要。
- 技能按需加载，工具权限按每次模型请求校验。
- 表情包独立发送，支持纯表情与失败后的文字兜底。
- 长考提示、发送回执、结构化日志与消息链路分析。

## 接入方式

| 通道 | 能收到的消息 | 依赖 |
| --- | --- | --- |
| OneBot | 群内全部消息与私聊 | NapCat 等协议端 |
| QQ 官方机器人 | 平台推送的 @ 消息 | 官方机器人应用凭据 |

至少启用一个通道，也可以同时启用。两条通道共用模型、技能与业务逻辑。

## 快速开始

需要 Python 3.12、GNU screen；使用 OneBot 时还需要已配置的 NapCat Docker Compose 服务。

```bash
cd /opt/project_workspace/ai-qbot
conda activate qbot
pip install -r requirements.txt
```

首次部署将 `.env.example` 复制为 `.env`，填写 API Key 和通道配置；已有 `.env` 不要覆盖。仅用 OneBot 的基本配置：

```dotenv
OFFICIAL_ENABLE=false
ONEBOT_ENABLE=true
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=与NapCat中一致的token
DEEPSEEK_API_KEY=你的APIKey
DEEPSEEK_THINKING_NOTICE_SEC=60
```

在 NapCat 中启用正向 WebSocket 服务器并登录 QQ，然后启动：

```bash
bash start.sh
bash start.sh status
bash start.sh log
```

完整安装、Docker 配置、WebUI 隧道与登录步骤见 [服务器部署与运维](docs/operations.md)。

## 日常使用

群内 @ 机器人或发送私聊即可对话。`/help` 查看命令，`/reset` 清除当前会话记忆。OneBot 普通群聊还可按配置参与未 @ 的讨论。

模型处理超过默认 60 秒时，@/私聊会收到一次“正在思考中...”，工具执行超过默认 30 秒时提示“正在调用生图技能”等，按工具分别计时并继续等待；接口报错或主动取消才结束。进度提示不写入对话历史。

画像、跑团与表情包保存在 `data/`；对话历史保存在内存，重启后清空。`.env`、运行数据和日志不提交 Git。

## 更新

```bash
git pull --ff-only
pip install -r requirements.txt
bash start.sh reload
```

`reload` 仅重启 Python，不重启 NapCat。更新前先检查服务器未提交修改；正在处理的请求和内存对话会随重启清除。

## 文档

- [服务器部署与运维](docs/operations.md)
- [配置参考](docs/configuration.md)
- [命令使用](docs/usage.md)
- [私聊管理控制台](docs/admin-console.md)
- [架构、技能与运行行为](docs/architecture.md)
- [运行日志与故障定位](docs/runtime-logging.md)
- [开发与回归验证](docs/development.md)
