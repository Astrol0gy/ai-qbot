"""
通道无关的业务核心。

无论消息来自官方 botpy 通道还是 OneBot 通道，最终都汇聚到 handle_message()：
指令分派 → 图片 VLM 预处理 → 调用 LLM → 解析并发送回复。
"""
import asyncio
import re

from ..config import DASHSCOPE_API_KEY, DEEPSEEK_ROUND_TIMEOUT_SEC
from ..deepseek_client import chat, clear_history
from ..vision_utils import describe_image
from .base import IncomingMessage, Sender
from .commands import HELP_TEXT, handle_dm_cmd, handle_profile_cmd

IMG_MARKER = re.compile(r"\[img\](.*?)\[/img\]")

# 主动接话时，模型可用该标记表示「这次不插嘴」
SKIP_TOKENS = {"[skip]", "__skip__", "[跳过]"}

WAKE_PROMPT = (
    "以下是群里自你上次开口以来发生的事（可能有好几个人同时在说，也可能同一个人连发了几条）。\n"
    "这一轮开不开口、跟谁说话、说什么，都由你自己决定：\n"
    "· 有人在跟你说话、问你、或者接着你的话往下说 → 用 reply 工具回话；\n"
    "· 群里只是互相聊天、跟你无关，或者你觉得对方还没说完（还会接着补）→ 用 skip 工具，\n"
    "  这条先不开口。\n"
    "不要只回最早的那一条：这一段里凡是跟你说话的都要照顾到——不同的人各调一次 reply，\n"
    "并把要引用的那条消息 id 填进 reply_to；如果几句话同属一件事，也可以合在一条里回应。\n"
    "不开口就调 skip 工具，不要用 [skip] 这类文字标记。\n\n"
    "{context}"
)


async def _send_reply(sender: Sender, text: str, reply_to: str | None = None) -> None:
    """发出一条回复：先摘出 [img] 标记的图片，再按「先图后文」发出（可带引用）"""
    image_urls = IMG_MARKER.findall(text)
    clean = IMG_MARKER.sub("", text).strip()
    if image_urls:
        await sender.send(clean or "为你找到以下图片：", image_urls, reply_to=reply_to)
    elif clean:
        await sender.send(clean, reply_to=reply_to)


def _strip_speaker_tag(text: str, speaker: str) -> str:
    """
    去掉模型照抄上下文格式、在回复开头补的「【说话者】」标签。

    群聊上下文里每条用户消息都以【说话者】开头，模型偶尔把这个格式也抄进自己的回复，
    群里就会看到「【张三】我在呢」这种顶着别人名字的机器人消息（实测出现过）。
    只剥**当前说话者**的标签，正文里的【标题】不动。
    """
    name = (speaker or "").strip()
    if not name:
        return text
    prefix = f"【{name}】"
    while text.startswith(prefix):
        text = text[len(prefix):].lstrip()
    return text


async def _image_text(msg: IncomingMessage, question: str) -> str | None:
    """
    生成拼进 user_msg 的图片描述文本；返回 None 表示 VLM 未配置。

    未 @ 的群消息可能已在适配器层预识别（image_descs 非 None），此时直接复用，
    不重复调用 VLM。describe_image 是同步阻塞调用，逐张放线程池执行 ——
    识别群图片的频率远高于 @ 消息，直接调用会阻塞事件循环、拖垮 WS 心跳。
    """
    if msg.image_descs is not None:
        descs: list[str | None] = list(msg.image_descs)
    else:
        if not DASHSCOPE_API_KEY:
            return None
        total = len(msg.image_urls)
        print(f"[图片消息] 检测到 {total} 张图片")
        descs = []
        for i, url in enumerate(msg.image_urls):
            print(f"[VLM] 图片 {i + 1}/{total}: {url[:80]}...")
            desc = await asyncio.to_thread(describe_image, url, question if i == 0 else "")
            descs.append(desc)

    return "\n\n".join(f"图片{i + 1}：{d or '识别失败'}" for i, d in enumerate(descs))


async def handle_message(msg: IncomingMessage, sender: Sender) -> None:
    """处理一条已归一化的入站消息（所有通道共用）"""
    user_msg = (msg.text or "").strip()

    # ---------- 引用消息：正文为空时，用引用内容作为提问 ----------
    if msg.reply_ctx and not user_msg:
        user_msg = msg.reply_ctx

    # ---------- 图片识别（VLM 预处理）----------
    media_text = await _image_text(msg, user_msg) if msg.image_urls else ""
    if media_text is None:
        print("[图片消息] VLM 未配置，跳过")
        if not msg.is_interjection:  # 接话是自发的，不该为此在群里报错
            await sender.send(
                "抱歉，视觉识别功能未配置，无法识别图片内容。\n"
                "请在 .env 中填写 DASHSCOPE_API_KEY（百炼 API Key）后重试。"
            )
        return

    # ---------- 群聊唤醒：把片段包成一次调用，开不开口交给 AI 自己决定 ----------
    wake_context = ""
    if msg.ai_decides_reply:
        # 预识别过的描述已随消息进入 extra_ctx，不重复拼接；
        # 其余情况（如引用的图片）补进上下文 —— 不能覆盖唤醒指令本身
        extra = "" if msg.image_descs is not None else media_text
        wake_context = "\n\n".join(p for p in (msg.extra_ctx, extra) if p) or user_msg
        user_msg = WAKE_PROMPT.format(context=wake_context)
    elif media_text:
        if user_msg:
            user_msg = (
                f"用户发送了{len(msg.image_urls)}张图片：\n{media_text}\n\n"
                f"用户的问题：{user_msg}"
            )
        else:
            user_msg = (
                f"用户发送了{len(msg.image_urls)}张图片，请根据以下描述回答：\n{media_text}"
            )

    tag = "OneBot" if msg.channel == "onebot" else "官方"
    kind = "群唤醒" if msg.ai_decides_reply else "消息"
    brief = user_msg[:120] + ("..." if len(user_msg) > 120 else "")
    print(f"[📩 {tag} {kind}] 会话={msg.conv_id} 用户={msg.user_id}({msg.user_name})：{brief}")

    # ---------- 空消息 ----------
    if not user_msg:
        if msg.is_interjection:
            return
        await sender.send("你好呀～请问有什么可以帮你的？\n发送 /help 查看我的全部功能～")
        return

    # ---------- 指令分支（只有直呼机器人才处理指令）----------
    if msg.is_direct_address:
        cmd = user_msg.strip()

        if cmd == "/help":
            await sender.send(HELP_TEXT)
            return

        if cmd == "/reset":
            clear_history(msg.conv_id)
            await sender.send("记忆已清除，让我们重新开始吧！")
            return

        # --- 跑团（DM）指令 ---
        dm_result = handle_dm_cmd(cmd, msg.conv_id, msg.user_id, msg.user_name)
        if dm_result is not None:
            await sender.send(dm_result)
            return

        # --- 用户画像指令 ---
        profile_result = handle_profile_cmd(cmd, msg.conv_id, msg.user_id)
        if profile_result is not None:
            await sender.send(profile_result)
            return

        # --- /look：交给 LLM 总结提炼后自行调用工具存储 ---
        if cmd.startswith("/look "):
            raw_info = cmd[len("/look"):].strip()
            if not raw_info:
                await sender.send("请提供要记住的信息，例如：/look 我喜欢简短的回答")
                return
            user_msg = (
                f"用户要求记住关于ta的使用偏好或设定：「{raw_info}」\n\n"
                "请按以下步骤处理：\n"
                "1. 理解用户意图：是简单的风格偏好，还是引用了某个角色/人名/风格？\n"
                "2. 如果是角色引用（如'像哆啦A梦一样'、'用鲁迅的风格'），且你对具体特点不够了解，"
                "请先用 web_search 搜索相关信息，总结关键特征\n"
                "3. 将信息提炼成简洁的要点后，使用 manage_user_profile 工具以 action=remember "
                "记录下来（记录的是当前说话者自己的偏好，与其他用户独立）\n"
                "4. 最后简短告知用户你已记住什么"
            )

    # ---------- 交给 DeepSeek ----------
    # 普通群聊唤醒：给 AI reply / skip 两个工具，由它决定这轮开不开口、回给谁
    outbox = None
    trigger = None
    if msg.ai_decides_reply:
        from ..reply_outbox import ReplyOutbox  # 局部导入，避免通道层与触发层循环依赖
        from ..trigger import trigger as _trigger

        trigger = _trigger
        outbox = ReplyOutbox(known_ids=trigger.buffer.ids(msg.conv_id))

    # 单轮思考的总时限：不设上限的话，SDK 默认 read=600s 还带重试，一轮能拖上几十分钟，
    # 这期间整条群消息处理链（以及这条会话的锁）都堵着
    thinking = asyncio.create_task(
        chat(
            msg.conv_id,
            msg.user_id,
            user_msg,
            msg.user_name,
            outbox=outbox,
            history_text=wake_context or None,
        )
    )
    done, _ = await asyncio.wait({thinking}, timeout=DEEPSEEK_ROUND_TIMEOUT_SEC)
    if not done:
        # 到点就彻底放弃这一轮：不等它、也不看它后来返回了什么 ——
        # 就算它在第 61 秒答完了，这句话也不该再冒进群里（已经过时了）。
        # 不用 asyncio.wait_for：它取消之后会等对方真正结束，一个"吞掉取消"的请求照样
        # 能把我们拖住；这里 cancel 完直接走人。
        # 收尾按「AI 主动结束这轮互动」办：退出互动窗口、回到空闲，要再开口得等下一次唤醒。
        # 片段留在缓冲里（和 skip 一致），下一条消息被叫醒时能看到完整的一段。
        thinking.cancel()
        print(f"[⏱️ 思考超时 {DEEPSEEK_ROUND_TIMEOUT_SEC:.0f}s] 放弃本轮：不发消息、退出互动")
        if trigger is not None:
            trigger.leave_session(msg.conv_id)
        return

    try:
        reply = thinking.result()
    except Exception as e:
        print(f"[❌ DeepSeek 错误] {e}")
        reply = "抱歉，我暂时无法回复，请稍后再试～"

    # ---------- AI 用它自己调的工具表了态 ----------
    if outbox is not None:
        if outbox.has_items:
            for item in outbox.items:
                await _send_reply(sender, _strip_speaker_tag(item.text, msg.user_name), item.reply_to)
            # 已经开口了：这一段到此为止。只清掉本轮开始时就已经看到的消息，
            # 思考期间新到的留给紧接着的下一轮一起处理
            trigger.buffer.clear(msg.conv_id, before=msg.wake_started_at or None)
            # 回复完成，从现在起重新计时（超时没人说话就退出互动）
            trigger.mark_answered(msg.conv_id)
            return
        if outbox.skipped:
            print("[🤐 本轮不开口] AI 选择 skip（片段留在缓冲里，等下条消息继续）")
            trigger.mark_answered(msg.conv_id)
            return
        # 没调工具：退回成一条普通文本（兼容模型直接说话的情况）

    # 解析 [img]URL[/img] 标记，提取图片链接
    image_urls = IMG_MARKER.findall(reply)
    clean_reply = IMG_MARKER.sub("", reply).strip()
    # 别让它顶着别人的名字开口（模型会照抄上下文的【说话者】格式）
    clean_reply = _strip_speaker_tag(clean_reply, msg.user_name)

    # 模型用文字标记表示放弃（旧路径兼容：@ / 私聊 / 跑团行为不变）
    if (
        (msg.ai_decides_reply or msg.is_interjection)
        and clean_reply.lower() in SKIP_TOKENS
        and not image_urls
    ):
        print("[🤐 本轮不开口] 模型回复了 skip 标记")
        if trigger is not None:
            trigger.mark_answered(msg.conv_id)
        return

    # 先发图片，再发文字
    spoke = False
    if image_urls:
        await sender.send(clean_reply or "为你找到以下图片：", image_urls)
        spoke = True
    elif clean_reply:
        await sender.send(clean_reply)
        spoke = True

    # 本轮结束，重新计时
    if trigger is not None:
        if outbox is not None and spoke:
            # 模型没用工具、直接说了话：这同样是一次回答，收尾必须和上面的工具分支一致。
            # 少了这一句，刚回过的消息还留在缓冲里，下一轮会被当成「还没回过」再答一遍
            # （实测：张三的 @ 被回了两遍，第二遍还引用了同一条消息）
            trigger.buffer.clear(msg.conv_id, before=msg.wake_started_at or None)
        trigger.mark_answered(msg.conv_id)
