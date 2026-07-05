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
    DASHSCOPE_API_KEY,
    DEEPSEEK_ENABLE_THINKING,
    DEEPSEEK_ENABLE_SEARCH,
    validate,
)
from .deepseek_client import chat, clear_history
from .vision_utils import describe_image
from . import user_profile

# ============================================
# 消息清理
# ============================================
_AT_PATTERN = re.compile(r"<@!\d+>|@\S+")
_IMG_MARKER = re.compile(r"\[img\](.*?)\[/img\]")

HELP_TEXT = """【小Q 功能指南】

聊天空谈、查资料、搜图片视频，@我就能帮你～

命令：
  /help  - 查看本帮助
  /reset - 清除对话记忆
  /look <信息> - 让我记住你的偏好（如 /look 我喜欢简短回答）
  /forget <关键词> - 忘记已记录的偏好（/forget all 清除全部）
  /list - 查看已记录的偏好

能力：
  · 联网搜索 - 查天气、新闻、实时信息
  · 图片识别 - 发图让我帮你分析图片内容
  · 图片搜索 - 告诉我你想看什么图
  · 视频搜索 - B站搜视频直达链接
  · 用户画像 - 记住你的偏好，越聊越懂你"""


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

        # --- 图片识别（VLM 预处理）---
        attachments = getattr(message, "attachments", []) or []
        image_attachments = [
            a for a in attachments
            if getattr(a, "content_type", "").startswith("image/")
        ]
        if image_attachments:
            print(f"[图片消息] 检测到 {len(image_attachments)} 个图片附件")

            # 检查 VLM 是否可用
            if not DASHSCOPE_API_KEY:
                print("[图片消息] VLM 未配置，跳过")
                await self._safe_reply(
                    message,
                    "抱歉，视觉识别功能未配置，无法识别图片内容。\n"
                    "请在 .env 中填写 DASHSCOPE_API_KEY（百炼 API Key）后重试。"
                )
                return

            media_descs = []
            for i, att in enumerate(image_attachments):
                media_url = getattr(att, "url", "")
                if not media_url:
                    continue
                print(f"[VLM] 图片 {i + 1}/{len(image_attachments)}: {media_url[:80]}...")

                desc = describe_image(media_url, question=user_msg if i == 0 else "")
                label = f"图片{i + 1}"

                if desc:
                    media_descs.append(f"{label}：{desc}")
                else:
                    media_descs.append(f"{label}：识别失败")

            if media_descs:
                media_text = "\n\n".join(media_descs)
                if user_msg:
                    user_msg = f"用户发送了{len(image_attachments)}张图片：\n{media_text}\n\n用户的问题：{user_msg}"
                else:
                    user_msg = f"用户发送了{len(image_attachments)}张图片，请根据以下描述回答：\n{media_text}"

        print(f"[📩 群消息] 群={group_id} 用户={user_id}：{user_msg[:100]}{'...' if len(user_msg) > 100 else ''}")

        # --- 空消息 ---
        if not user_msg:
            await self._safe_reply(message, "你好呀～请问有什么可以帮你的？\n发送 /help 查看我的全部功能～")
            return

        # --- 内置指令 ---
        if user_msg.strip() == "/help":
            await self._safe_reply(message, HELP_TEXT)
            return

        if user_msg.strip() == "/reset":
            clear_history(group_id, user_id)
            await self._safe_reply(message, "记忆已清除，让我们重新开始吧！")
            return

        # 用户画像指令
        profile_result = self._handle_profile_cmd(user_msg.strip(), group_id, user_id)
        if profile_result is not None:
            await self._safe_reply(message, profile_result)
            return

        # /look 指令：交给 LLM 总结提炼后自行调用工具存储
        if user_msg.startswith("/look "):
            raw_info = user_msg[6:].strip()
            if not raw_info:
                await self._safe_reply(message, "请提供要记住的信息，例如：/look 我喜欢简短的回答")
                return
            user_msg = (
                f"用户要求记住关于ta的使用偏好或设定：「{raw_info}」\n\n"
                "请按以下步骤处理：\n"
                "1. 理解用户意图：是简单的风格偏好，还是引用了某个角色/人名/风格？\n"
                "2. 如果是角色引用（如'像哆啦A梦一样'、'用鲁迅的风格'），且你对具体特点不够了解，请先用 web_search 搜索相关信息，总结关键特征\n"
                "3. 将信息提炼成简洁的要点后，使用 manage_user_profile 工具以 action=remember 记录下来\n"
                "4. 最后简短告知用户你已记住什么"
            )

        # --- 交给 DeepSeek ---
        try:
            reply = await chat(group_id, user_id, user_msg)
        except Exception as e:
            print(f"[❌ DeepSeek 错误] {e}")
            reply = "抱歉，我暂时无法回复，请稍后再试～"

        # 解析 [img]URL[/img] 标记，提取图片链接
        image_urls = _IMG_MARKER.findall(reply)
        clean_reply = _IMG_MARKER.sub("", reply).strip()

        # 先发图片，再发文字
        if image_urls:
            await self._send_group_images(message.group_openid, message.id, image_urls, start_seq=1)
            text_seq = len(image_urls) + 1
            text = clean_reply or "为你找到以下图片："
            await self._safe_send_group(message.group_openid, message.id, text, msg_seq=text_seq)
        elif clean_reply:
            await self._safe_reply(message, clean_reply)

    @staticmethod
    def _handle_profile_cmd(user_msg: str, group_id: str, user_id: str) -> str | None:
        """
        处理用户画像指令：/forget, /list
        /look 由 LLM 处理器负责（总结提炼后调用 manage_user_profile 工具）
        返回操作结果字符串，非指令时返回 None。
        """
        if user_msg.startswith("/forget"):
            info = user_msg[7:].strip()
            if not info:
                return "请提供要忘记的信息关键词，例如：/forget 简短\n或使用 /forget all 清除所有记录。"
            return user_profile.forget_info(info, group_id, user_id)

        elif user_msg == "/list":
            result = user_profile.list_profile(group_id, user_id)
            if result == "暂无任何记录。" or result == "暂无记录。":
                return "还没有记录任何关于你的使用偏好。\n你可以用 /look <信息> 来记录，或者我会在对话中自己记住你的偏好。"
            return f"已记录的偏好和习惯：\n{result}"

        return None

    @staticmethod
    async def _safe_reply(message: GroupMessage, content: str):
        """安全回复，截断过长的消息，断连时自动等待 SDK 重连"""
        import asyncio
        max_len = 2000
        if len(content) > max_len:
            content = content[: max_len - 30] + "\n\n……(内容过长，已截断)"
        for attempt in range(1, 6):
            try:
                await message.reply(content=content)
                if attempt > 1:
                    print(f"[✅ 回复成功] 第 {attempt} 次重试")
                return
            except Exception as e:
                wait = attempt * 2  # 2, 4, 6, 8, 10 秒
                if attempt < 5:
                    print(f"[⚠️ 回复等待重连 {attempt}/5] 等 {wait}s... {e}")
                    await asyncio.sleep(wait)
                else:
                    print(f"[❌ 回复最终失败] {e}")

    async def _send_group_images(self, group_openid: str, msg_id: str, urls: list[str], max_images: int = 3, start_seq: int = 2):
        """上传图片到 QQ 服务器并以被动回复方式发送到群聊"""
        sent = 0
        for i, url in enumerate(urls[:max_images]):
            try:
                # 步骤 1: 上传图片（不发送），获取 file_info
                media_info = await self.api.post_group_file(
                    group_openid=group_openid,
                    file_type=1,          # 1 = 图片
                    url=url,
                    srv_send_msg=False,   # 不上动发送，只获取 file_info
                )
                # 步骤 2: 以被动回复方式发送（msg_type=7 富媒体，递增 msg_seq 避免去重）
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=7,           # 富媒体消息
                    media=media_info,
                    msg_id=msg_id,        # 被动回复
                    msg_seq=start_seq + i,
                )
                sent += 1
                print(f"[🖼️ 图片] 发送成功 ({sent}/{min(len(urls), max_images)}): {url[:80]}...")
            except Exception as e:
                print(f"[🖼️ 图片失败] {url[:80]}... -> {e}")
        if sent > 0:
            print(f"[🖼️ 图片] 共发送 {sent}/{min(len(urls), max_images)} 张")

    async def _safe_send_group(self, group_openid: str, msg_id: str, content: str, msg_seq: int = 1):
        """以被动回复方式发送文字消息并指定 msg_seq（避免与图片 seq 冲突）"""
        import asyncio
        max_len = 2000
        if len(content) > max_len:
            content = content[: max_len - 30] + "\n\n……(内容过长，已截断)"
        for attempt in range(1, 6):
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    content=content,
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                )
                if attempt > 1:
                    print(f"[✅ 文字发送成功] 第 {attempt} 次重试")
                return
            except Exception as e:
                wait = attempt * 2
                if attempt < 5:
                    print(f"[⚠️ 文字发送等待重连 {attempt}/5] 等 {wait}s... {e}")
                    await asyncio.sleep(wait)
                else:
                    print(f"[❌ 文字发送最终失败] {e}")


# ============================================
# 启动
# ============================================
if __name__ == "__main__":
    validate()  # 启动前检查配置

    intents = botpy.Intents(public_messages=True)
    client = QBot(intents=intents)
    client.run(appid=QQ_BOT_APP_ID, secret=QQ_BOT_APP_SECRET)
