"""
配置加载模块
所有配置项从项目根目录的 .env 文件读取，填写 .env 即可，无需改代码。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 无论从哪里启动，都能找到项目根目录的 .env
_env_file = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_file)

# ============================================
# QQ Bot
# ============================================
QQ_BOT_APP_ID = os.getenv("QQ_BOT_APP_ID", "")
QQ_BOT_APP_SECRET = os.getenv("QQ_BOT_APP_SECRET", "")

# ============================================
# DeepSeek V4 API
# ============================================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_TEMPERATURE = float(os.getenv("DEEPSEEK_TEMPERATURE", "1.0"))
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "2048"))
DEEPSEEK_TOP_P = float(os.getenv("DEEPSEEK_TOP_P", "1.0"))
DEEPSEEK_ENABLE_THINKING = os.getenv("DEEPSEEK_ENABLE_THINKING", "false").lower() == "true"
DEEPSEEK_REASONING_EFFORT = os.getenv("DEEPSEEK_REASONING_EFFORT", "high")
DEEPSEEK_ENABLE_SEARCH = os.getenv("DEEPSEEK_ENABLE_SEARCH", "false").lower() == "true"
# 单轮思考总时限（秒）：到点就放弃这一轮 —— 一条消息都不发、不写历史、退出互动窗口，
# 之后即便 API 才返回也一律丢弃（超时才到的回答，在群里已经过时了）
DEEPSEEK_ROUND_TIMEOUT_SEC = float(os.getenv("DEEPSEEK_ROUND_TIMEOUT_SEC", "60"))
# DeepSeek 官方内置 web search（Anthropic 兼容端点）
DEEPSEEK_ANTHROPIC_BASE_URL = os.getenv(
    "DEEPSEEK_ANTHROPIC_BASE_URL",
    "https://api.deepseek.com/anthropic",
)

# ============================================
# 机器人行为
# ============================================
BOT_SYSTEM_PROMPT = os.getenv(
    "BOT_SYSTEM_PROMPT",
    "你是一个友好的QQ群聊助手，请用简洁有趣的方式回答问题。"
    "核心特质：1.热情亲切，像朋友一样真诚交流，自然不生硬，适度使用语气词拉近距离；"
    "2.简洁高效，回答直奔重点、条理清晰，不啰嗦不废话；"
    "3.诚实可靠，不知道的事如实说明、绝不编造，涉及实时信息（天气/新闻/时间等）主动使用搜索工具核实；"
    "4.乐于助人，善于使用搜索、查天气、找图、找视频、记偏好等工具解决用户需求；"
    "5.有分寸感，群聊中注意不刷屏、不打乱话题节奏，语气随和但不油腻。"
)
MAX_HISTORY_LENGTH = int(os.getenv("MAX_HISTORY_LENGTH", "10"))

# ============================================
# Qwen 视觉识别（阿里云百炼 Model Studio）
# ============================================
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
VISION_BASE_URL = os.getenv(
    "VISION_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl-flash")
VISION_ENABLE_THINKING = os.getenv("VISION_ENABLE_THINKING", "false").lower() == "true"
VISION_HIGH_RES = os.getenv("VISION_HIGH_RES", "false").lower() == "true"

# ============================================
# 文生图（Qwen-Image，DashScope 原生接口）
# ============================================
# 注意：文生图模型不支持 OpenAI 兼容接口，需走 DashScope 原生 HTTP 端点
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "qwen-image-3.0-pro")
IMAGE_GEN_ENDPOINT = os.getenv(
    "IMAGE_GEN_ENDPOINT",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
)

# ============================================
# 接入通道开关（至少启用一个）
# ============================================
# 官方通道：QQ 开放平台机器人，只能收到 @ 消息
OFFICIAL_ENABLE = os.getenv("OFFICIAL_ENABLE", "true").lower() == "true"
# OneBot 通道：对接 NapCatQQ 等协议端，可收到群内全部消息
ONEBOT_ENABLE = os.getenv("ONEBOT_ENABLE", "false").lower() == "true"

# ============================================
# OneBot v11 通道（NapCatQQ 协议端）
# ============================================
ONEBOT_WS_URL = os.getenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")
# 机器人自身 QQ 号；留空则从事件 self_id 自动获取（用于判断是否被 @）
ONEBOT_SELF_ID = os.getenv("ONEBOT_SELF_ID", "")
ONEBOT_API_TIMEOUT = float(os.getenv("ONEBOT_API_TIMEOUT", "15"))
ONEBOT_MAX_CONCURRENCY = int(os.getenv("ONEBOT_MAX_CONCURRENCY", "3"))
ONEBOT_HEARTBEAT = float(os.getenv("ONEBOT_HEARTBEAT", "30"))
ONEBOT_DEDUP_SIZE = int(os.getenv("ONEBOT_DEDUP_SIZE", "500"))
ONEBOT_MAX_IMAGES = int(os.getenv("ONEBOT_MAX_IMAGES", "3"))
# 健康自检间隔（秒）：定期主动查一次 get_status，用来发现"QQ 掉线"和"连接假死"；0 = 关闭
ONEBOT_PROBE_SEC = float(os.getenv("ONEBOT_PROBE_SEC", "60"))
# 连续几次自检失败后判定连接已失效，强制断开重连
ONEBOT_PROBE_FAILS = int(os.getenv("ONEBOT_PROBE_FAILS", "2"))
# QQ 掉线日志的重复间隔（秒），避免刷屏
ONEBOT_OFFLINE_ALERT_SEC = float(os.getenv("ONEBOT_OFFLINE_ALERT_SEC", "600"))
# 连不上协议端（NapCat 没跑 / QQ 掉登录导致端口没开）累计多久后开始告警（秒）；0 = 关闭
ONEBOT_DOWN_ALERT_SEC = float(os.getenv("ONEBOT_DOWN_ALERT_SEC", "300"))


def _parse_id_set(var_name: str) -> set[str]:
    """解析逗号分隔的 ID 集合（支持中英文逗号）"""
    raw = os.getenv(var_name, "")
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


# 群黑白名单（仅影响概率接话；黑名单优先于白名单，留空表示不限制）
ONEBOT_GROUP_WHITELIST = _parse_id_set("ONEBOT_GROUP_WHITELIST")
ONEBOT_GROUP_BLACKLIST = _parse_id_set("ONEBOT_GROUP_BLACKLIST")

# ============================================
# 唤醒策略（OneBot 通道的普通群聊）
# ============================================
# 两个状态：
#   空闲   → 系统按概率决定要不要开一局（@ 必定开局）
#   互动中 → 每条消息都叫醒 AI（不再掷骰子），由它用 reply / skip 决定这轮开不开口
# 每轮处理完都重新计时：TRIGGER_ACTIVE_TIMEOUT_SEC 秒内没人说话就退出互动，
# 回到空闲的概率唤醒。（原 TRIGGER_COOLDOWN_SEC 已废弃：互动窗口替代了它。）
# ============================================
# 空闲状态下，未被 @ 的群消息触发一次唤醒的概率（0.1 = 10%）
TRIGGER_PROBABILITY = float(os.getenv("TRIGGER_PROBABILITY", "0.1"))
# 互动窗口：AI 处理完一轮后，多久没人说话就算聊完、退出互动回到概率唤醒（秒）
TRIGGER_ACTIVE_TIMEOUT_SEC = int(os.getenv("TRIGGER_ACTIVE_TIMEOUT_SEC", "60"))
# 消息长度低于该值不单独唤醒（仍会进入上下文缓冲，随后面某条一起带出去）
TRIGGER_MIN_LEN = int(os.getenv("TRIGGER_MIN_LEN", "2"))
# 叫醒时最多携带多少条群聊上下文
TRIGGER_CONTEXT_MESSAGES = int(os.getenv("TRIGGER_CONTEXT_MESSAGES", "8"))
# 未 @ 群消息里的图片是否也在触发判定前送 VLM 识别
# （true=机器人「看得见」群里所有人发的图，不只是 @ 它的那些；按张计费）
TRIGGER_RECOGNIZE_IMAGES = os.getenv("TRIGGER_RECOGNIZE_IMAGES", "true").lower() == "true"
# 每个会话每分钟最多预识别多少张图（0 = 不限制），防止刷图把 VLM 费用与延迟拉爆
TRIGGER_IMAGE_RATE_PER_MIN = int(os.getenv("TRIGGER_IMAGE_RATE_PER_MIN", "10"))
# 图片描述缓存条数（按图片 URL 去重，同一张图不重复计费）
TRIGGER_IMAGE_CACHE_SIZE = int(os.getenv("TRIGGER_IMAGE_CACHE_SIZE", "200"))


def validate():
    """按启用的通道检查必要配置是否已填写"""
    missing = []
    enabled = []

    if OFFICIAL_ENABLE:
        enabled.append("官方通道")
        if not QQ_BOT_APP_ID or QQ_BOT_APP_ID == "your_app_id_here":
            missing.append("QQ_BOT_APP_ID")
        if not QQ_BOT_APP_SECRET or QQ_BOT_APP_SECRET == "your_app_secret_here":
            missing.append("QQ_BOT_APP_SECRET")

    if ONEBOT_ENABLE:
        enabled.append("OneBot 通道")
        if not ONEBOT_WS_URL:
            missing.append("ONEBOT_WS_URL")

    if not enabled:
        raise ValueError(
            "未启用任何接入通道：请在 .env 中将 OFFICIAL_ENABLE 或 ONEBOT_ENABLE "
            "至少一个设为 true。"
        )

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-your-deepseek-api-key":
        missing.append("DEEPSEEK_API_KEY")

    if missing:
        raise ValueError(
            f"请在 .env 文件中填写以下配置项后重试：\n  " + "\n  ".join(missing)
        )
