# ai-qbot

基于 **qq-botpy + DeepSeek V4 Pro** 的 QQ 群聊 AI 机器人。

> 在群里 @机器人 即可对话，支持多群独立上下文记忆、V4 深度思考、联网搜索。

---

## 项目结构

```
ai-qbot/
├── .env                # 配置文件（需自己填写，不提交 git）
├── .env.example        # 配置模板（可提交 git）
├── .gitignore
├── requirements.txt    # Python 依赖
├── README.md
└── src/                # 源码
    ├── __init__.py
    ├── config.py           # 配置加载
    ├── tools.py            # Tool Calling 工具注册与执行
    ├── deepseek_client.py  # DeepSeek V4 API 封装 + 对话记忆
    └── main.py             # 机器人入口
```

## 前置准备

### 1. 注册 QQ 机器人

1. 打开 [QQ 开放平台](https://q.qq.com) 并登录
2. 创建机器人应用，获取 **AppID** 和 **AppSecret**
3. 在应用设置中开通 **群聊消息** 权限
4. 将机器人拉入你的 QQ 群

### 2. 获取 DeepSeek API Key

1. 打开 [DeepSeek 开放平台](https://platform.deepseek.com)
2. 注册并获取 **API Key**

## 安装 & 运行

```bash
# 安装依赖
pip install -r requirements.txt

# 填写配置（编辑 .env 文件）
# QQ_BOT_APP_ID=你的AppID
# QQ_BOT_APP_SECRET=你的AppSecret
# DEEPSEEK_API_KEY=你的DeepSeek API Key

# 启动机器人（在项目根目录执行）
python -m src.main
```

启动成功后会看到：
```
✅ 机器人「你的机器人名称」已上线！[DeepSeek V4 Pro | 🧠 V4 思考 | 🌐 联网搜索] 等待群聊 @ 消息...
```

## 使用方法

| 操作 | 方式 |
|------|------|
| 和机器人聊天 | 在群里 `@机器人 你的问题` |
| 清除记忆 | 在群里 `@机器人 /reset` |

## 配置说明（.env）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `QQ_BOT_APP_ID` | QQ 机器人 AppID | 必填 |
| `QQ_BOT_APP_SECRET` | QQ 机器人 AppSecret | 必填 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 必填 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `DEEPSEEK_TEMPERATURE` | 创造性（思考模式下忽略） | `1.0` |
| `DEEPSEEK_MAX_TOKENS` | 最大回复长度 | `2048` |
| `DEEPSEEK_TOP_P` | 核采样（思考模式下忽略） | `1.0` |
| `DEEPSEEK_ENABLE_THINKING` | V4 深度思考模式 | `false` |
| `DEEPSEEK_REASONING_EFFORT` | 思考强度 (`high`/`max`) | `high` |
| `DEEPSEEK_ENABLE_SEARCH` | 联网搜索 | `false` |
| `BOT_SYSTEM_PROMPT` | 系统提示词 | 友好群聊助手 |
| `MAX_HISTORY_LENGTH` | 每群记住几轮对话 | `10` |

## 在小米平板上运行（Termux）

1. 从 [F-Droid](https://f-droid.org/packages/com.termux/) 安装 Termux
2. 在 Termux 中执行：
   ```bash
   pkg update && pkg upgrade
   pkg install python git
  cd ~/storage/downloads/ai-qbot   # 或你的项目路径
  pip install -r requirements.txt
  python -m src.main
   ```

> ⚠️ Android 系统可能杀掉后台进程，长期运行建议部署到云服务器。
