# 服务器部署与运维

部署结构：Python/Conda 运行 ai-qbot，Docker Compose 运行 NapCat；两者通过 OneBot 正向 WebSocket 连接。服务器路径可调整，以下使用 `/opt/project_workspace/ai-qbot` 与 Conda 环境 `qbot`。

## Python 环境

```bash
cd /opt/project_workspace/ai-qbot
conda create -n qbot python=3.12 -y  # 首次创建时执行
conda activate qbot
pip install -r requirements.txt
```

首次部署复制 `.env.example` 为 `.env`，已有配置不要覆盖。按 [配置参考](configuration.md) 设置通道和 API 凭据。仅使用 OneBot 时设 `OFFICIAL_ENABLE=false`，不需要官方机器人凭据。

## NapCat Docker

现有部署由 `/opt/napcat` 中的 Compose 文件管理，默认服务及容器名为 `napcat`。`start.sh` 的 `NAPCAT_DIR`、`NAPCAT_SERVICE`、`NAPCAT_CONTAINER` 可按实际环境覆盖。先安装 Docker Compose、GNU screen 和 Python 依赖，再运行启动脚本；脚本使用现有 Compose 配置，不会创建或修改容器配置。

NapCat 中开启“WebSocket 服务器”（正向 WS），端口默认 `3001`；机器人设置 `ONEBOT_WS_URL=ws://127.0.0.1:3001`。两端的 access token 必须一致。`6099` 是 NapCat 管理页面端口，与 OneBot 端口不同。

建议将 WebUI 和 OneBot 端口只映射到宿主机回环地址。管理页面使用 SSH 本地转发访问：

```bash
ssh -L 6099:127.0.0.1:6099 root@服务器公网IP
```

在本机访问 `http://127.0.0.1:6099/webui`，使用 NapCat 配置中的 WebUI token 登录，然后按页面提示扫码登录 QQ。不要把 WebUI token、API Key 或 `.env` 发进群聊或提交 Git。

## 启动与管理

```bash
conda activate qbot
cd /opt/project_workspace/ai-qbot
bash start.sh
```

| 命令 | 作用 |
| --- | --- |
| `bash start.sh` | 启动 NapCat Docker 和机器人 |
| `bash start.sh status` | 检查进程、Docker、OneBot 与 QQ 登录状态 |
| `bash start.sh log` | 跟随机器人日志 |
| `bash start.sh naplog` | 跟随 NapCat 日志 |
| `bash start.sh webui` | 查看管理页面与 SSH 隧道说明 |
| `bash start.sh reload` | 只重启 Python，保留 NapCat 登录 |
| `bash start.sh stop` | 停止机器人及 NapCat |

`PYTHON_CMD` 默认使用当前环境的 `python`。从其他环境调用时可显式设置 `/opt/miniconda3/envs/qbot/bin/python`。容器名、Compose 服务名和项目位置以实际部署为准。

## 更新

```bash
git pull --ff-only
pip install -r requirements.txt
bash start.sh reload
```

更新前用 `git status` 检查服务器修改，不用 reset/clean 覆盖部署改动。`reload` 检查源码与配置后，只重启机器人；不会重启 NapCat 或要求重新扫码。它会清空内存对话历史、互动窗口和等待队列，中断尚未完成的请求；画像、跑团与表情包文件保留。仅执行 `git pull` 不会更新已加载的 Python 模块。

## 长考与排障

`DEEPSEEK_THINKING_NOTICE_SEC=60` 控制进度提示，不再取消正常长考。提示仅发送一次，不能代表已确认模型服务端内部正在推理；没有响应时可能仍在排队或网络等待。未完成请求会占用本会话及一个模型额度。

日志默认在 `logs/qbot.log`，按 10 MiB 轮转，保留三个备份。分析时应包括所有轮转文件，见 [运行日志](runtime-logging.md)。

```bash
python -m src.log_report --group 1050411559
python -m src.log_report --trace 追踪ID
```

检查消息是否入站、触发判断、排队时间、模型请求/工具阶段以及投递回执，不能只凭出现 @ 就认定模型已经开始处理。进度提示与最终答复分别记录实际回执。
