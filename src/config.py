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

# ============================================
# 机器人行为
# ============================================
BOT_SYSTEM_PROMPT = os.getenv(
    "BOT_SYSTEM_PROMPT",
    "你是一个友好的QQ群聊助手，请用简洁有趣的方式回答问题。"
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


def validate():
    """检查必要配置是否已填写"""
    missing = []
    if not QQ_BOT_APP_ID or QQ_BOT_APP_ID == "your_app_id_here":
        missing.append("QQ_BOT_APP_ID")
    if not QQ_BOT_APP_SECRET or QQ_BOT_APP_SECRET == "your_app_secret_here":
        missing.append("QQ_BOT_APP_SECRET")
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-your-deepseek-api-key":
        missing.append("DEEPSEEK_API_KEY")

    if missing:
        raise ValueError(
            f"请在 .env 文件中填写以下配置项后重试：\n  " + "\n  ".join(missing)
        )
