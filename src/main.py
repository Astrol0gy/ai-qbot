"""
QQ 群聊机器人 - 入口文件

启动方式（在项目根目录执行）：
    python -m src.main

前提：已正确填写项目根目录的 .env 文件中的所有配置项。
"""
import re
import botpy
from botpy.message import GroupMessage

from .config import (
    QQ_BOT_APP_ID,
    QQ_BOT_APP_SECRET,
    DEEPSEEK_ENABLE_THINKING,
    DEEPSEEK_ENABLE_SEARCH,
    validate,
)
from .deepseek_client import chat, clear_history

# ============================================
# 消息清理
# ============================================
_AT_PATTERN = re.compile(r"<@!\d+>|@\S+")


def clean_message(raw: str) -> str:
    """去掉 @机器人 部分，返回纯文本消息"""
    return _AT_PATTERN.sub("", raw).strip()


# ============================================
# 机器人 Client
# ============================================
class QBot(botpy.Client):
    """QQ 群聊机器人，接入 DeepSeek 大模型"""

    async def on_ready(self):
        flags = []
        if DEEPSEEK_ENABLE_THINKING:
            flags.append("🧠 V4 思考")
        if DEEPSEEK_ENABLE_SEARCH:
            flags.append("🌐 联网搜索")
        status = " | ".join(flags) if flags else "标准模式"
        print(f"✅ 机器人「{self.robot.name}」已上线！[DeepSeek V4 Pro | {status}] 等待群聊 @ 消息...")

    async def on_group_at_message_create(self, message: GroupMessage):
        """
        收到群聊 @ 消息时触发。
        message 对象包含 content / group_openid / author 等字段。
        """
        user_msg = clean_message(message.content)
        group_id = getattr(message, "group_openid", "")
        user_id = getattr(message.author, "member_openid", "")

        # 检测引用消息（bot 看不到引用原文）
        ref = getattr(message, "message_reference", None)
        if ref and getattr(ref, "message_id", None):
            print(f"[📎 有引用消息] msg_id={ref.message_id}，但我看不到内容")
            if not user_msg:
                user_msg = "用户引用了一条消息，请根据上下文尽力理解"

        print(f"[📩 群消息] 群={group_id} 用户={user_id}：{user_msg}")

        # --- 空消息 ---
        if not user_msg:
            await self._safe_reply(message, "你好呀～请问有什么可以帮你的？")
            return

        # --- 内置指令 ---
        if user_msg.strip() == "/reset":
            clear_history(group_id, user_id)
            await self._safe_reply(message, "记忆已清除，让我们重新开始吧！")
            return

        # --- 交给 DeepSeek ---
        try:
            reply = await chat(group_id, user_id, user_msg)
        except Exception as e:
            print(f"[❌ DeepSeek 错误] {e}")
            reply = "抱歉，我暂时无法回复，请稍后再试～"

        await self._safe_reply(message, reply)

    @staticmethod
    async def _safe_reply(message: GroupMessage, content: str):
        """安全回复，截断过长的消息"""
        max_len = 2000  # QQ 群消息大致上限
        if len(content) > max_len:
            content = content[: max_len - 30] + "\n\n……(内容过长，已截断)"
        try:
            await message.reply(content=content)
        except Exception as e:
            print(f"[❌ 回复失败] {e}")


# ============================================
# 启动
# ============================================
if __name__ == "__main__":
    validate()  # 启动前检查配置

    intents = botpy.Intents(public_messages=True)
    client = QBot(intents=intents)
    client.run(appid=QQ_BOT_APP_ID, secret=QQ_BOT_APP_SECRET)
