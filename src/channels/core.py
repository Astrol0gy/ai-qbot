"""
通道无关的业务核心。

无论消息来自官方 botpy 通道还是 OneBot 通道，最终都汇聚到 handle_message()：
指令分派 → 图片 VLM 预处理 → 调用 LLM → 解析并发送回复。
"""
import asyncio
import re
import time

from ..config import (DEEPSEEK_THINKING_NOTICE_SEC, TOOL_CALL_NOTICE_SEC, STICKER_MAX_PER_REPLY,
                      CHAT_MAX_SEGMENTS, CHAT_SEGMENT_INTERVAL_SEC)
from ..deepseek_client import (
    api_cooldown_left,
    chat,
    clear_history,
    note_api_failure,
    note_api_success,
)
from .. import stickers_utils
from ..vision_utils import (
    IMAGE_UNAVAILABLE,
    describe_image,
    strip_tag_line,
    vision_available,
)
from .base import IncomingMessage, Sender
from .commands import HELP_TEXT, handle_dm_cmd, handle_profile_cmd
from ..observability import active_stage, log_event, outcome, set_round, stage, traced_message

IMG_MARKER = re.compile(r"\[img\](.*?)\[/img\]")


def _drain_abandoned_task(task: asyncio.Task) -> None:
    """
    取走「超时被放弃的那一轮」的最终结果。

    cancel() 只是**请求**它停下：它真正结束前，局部变量已经随函数返回消失，也没人读过它的
    结果 —— 于是任务被 GC 掉是 "Task was destroyed but it is pending"，抛了异常则是
    "exception was never retrieved"，两种都只在日志里留一句看不出前因后果的警告。
    这个回调挂在任务自己身上，结束时读一次，把话说完。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log_event('model.late_result', status='discarded', error_type=type(exc).__name__, detail=str(exc)[:300])

# 主动接话时，模型可用该标记表示「这次不插嘴」
SKIP_TOKENS = {"[skip]", "__skip__", "[跳过]"}

WAKE_PROMPT = (
    "以下是群里自你上次开口以来发生的事（可能有好几个人同时在说，也可能同一个人连发了几条）。\n"
    "这一轮开不开口、跟谁说话、说什么，都由你自己决定：\n"
    "按系统的聊天参与规则判断表达是否完整，再选择 reply 或 skip。被 @ 或处于互动中也不必抢答未完的表达。\n"
    "当前触发消息id：{focus}。优先理解这条新消息及当前话题，再选择值得参与的内容。\n"
    "上下文不是待办清单，不要求逐条回复或照顾每个人；旧内容可用于理解新补充，不自动逐条补答。\n"
    "尚在铺垫时 skip(reason=wait)，无需参与时 skip(reason=irrelevant)；有清晰问题则结合近期图文回答或澄清。\n"
    "确实需要回应多个不同话题时才分别调用 reply。用 reply_to 引用实际选择回应的消息，避免连续重复引用。\n"
    "不开口就调 skip 工具，不要用 [skip] 这类文字标记。\n\n"
    "{context}"
)


def _chat_parts(text, limit):
    """Use model-chosen paragraph boundaries; keep structured content intact."""
    if (limit <= 1 or IMG_MARKER.search(text) or re.search(r'https?://|```', text)
            or re.search(r'^\s*(?:\d+[.、)]|[-*·]|\|)', text, re.M)):
        return [text]
    parts = [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    if len(parts) <= 1:
        return [text]
    if len(parts) <= limit:
        return parts
    return parts[:limit-1] + ['\n\n'.join(parts[limit-1:])]


def _split_image_payloads(payloads: list[str]) -> tuple[list[str], list[str]]:
    """
    把回复里所有 ``[img]`` 的内容分成两类 -> (网络图片地址, 本地表情包路径)。

    网址交给通道去拉；不是 http(s) 的交给表情包库解析 —— AI 用 ``send_sticker`` 工具
    拿到的就是库里的本地路径（见 src/stickers_utils.py），它把那一行原样放进回复，
    这里负责认出来。库外的路径一律丢掉：模型抄错、编出来的路径不该进发送环节。
    """
    urls: list[str] = []
    local: list[str] = []
    for raw in payloads:
        item = (raw or "").strip()
        if not item:
            continue
        if item.startswith(("http://", "https://")):
            urls.append(item)
            continue
        path = stickers_utils.resolve(item)
        if path is None:
            print(f"[表情包] 这段 [img] 既不是网址、也不在库里，忽略：{item[:80]}")
            continue
        if len(local) >= STICKER_MAX_PER_REPLY:
            print(f"[表情包] 一轮最多发 {STICKER_MAX_PER_REPLY} 张，多余的丢掉")
            continue
        local.append(path)
    return urls, local


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
        if not vision_available():
            return None
        total = len(msg.image_urls)
        print(f"[图片消息] 检测到 {total} 张图片")
        descs = []
        for i, url in enumerate(msg.image_urls):
            print(f"[VLM] 图片 {i + 1}/{total}: {url[:80]}...")
            desc = await runtime.run_blocking(describe_image, url, question if i == 0 else "")
            descs.append(desc)

    # 识别不出来的图留中性占位（见 IMAGE_UNAVAILABLE）：主模型要知道「用户确实发过图」，
    # 但不能拿到「识别失败」四个字去琢磨 —— 那会白烧思考预算，拖延本轮回复
    # （线上日志实测：思考第一段就在纠结「图没识别出来，我该不该提一句」）。
    # 描述里的「标签：…」行是给表情包库归档用的，不进上下文（见 stickers_utils.py）。
    return "\n\n".join(
        f"图片{i + 1}：{strip_tag_line(d) or IMAGE_UNAVAILABLE}" for i, d in enumerate(descs)
    )


# Each event loop has one shared model gate for both channels.
import weakref
from .. import runtime
from .. import deepseek_client as dc
from ..config import ONEBOT_MAX_CONCURRENCY
from .base import SendResult, truncate_text

_MODEL_GATES = weakref.WeakKeyDictionary()
_ABANDONED = set()


def _abandon(task):
    _ABANDONED.add(task)
    def done(finished):
        _ABANDONED.discard(finished)
        _drain_abandoned_task(finished)
    task.add_done_callback(done)
    task.cancel()


async def _deliver(sender, text, reply_to, budget):
    urls, stickers = _split_image_payloads(IMG_MARKER.findall(text))
    stickers = stickers[:budget[0]]
    budget[0] -= len(stickers)
    clean = _strip_speaker_tag(IMG_MARKER.sub("", text).strip(), budget[1])
    if not clean and urls:
        clean = "为你找到以下图片："
    if not clean and not urls and not stickers:
        return SendResult("dropped", detail="没有可发送内容")
    try:
        kwargs = {}
        if urls:
            kwargs["image_urls"] = urls
        if stickers:
            kwargs["sticker_paths"] = stickers
        if reply_to:
            kwargs["reply_to"] = reply_to
        receipt = await asyncio.wait_for(sender.send(clean, **kwargs), timeout=45)
    except asyncio.TimeoutError:
        if hasattr(sender, "interrupted_result"):
            return sender.interrupted_result()
        return SendResult("unknown", clean, tuple(urls), tuple(stickers), "发送回执超时，不自动重试")
    except Exception as exc:
        if hasattr(sender, "interrupted_result"):
            return sender.interrupted_result()
        return SendResult("unknown", clean, tuple(urls), tuple(stickers), str(exc)[:120])
    # Compatibility for trusted in-process senders. Production adapters always
    # return SendResult; an adapter returning None is not an implicit success.
    if receipt is None and not isinstance(sender, Sender):
        receipt = SendResult("ok", truncate_text(clean, getattr(sender, "max_text_len", 4000)), tuple(urls), tuple(stickers))
    return receipt if isinstance(receipt, SendResult) else SendResult("unknown", detail="发送器没有返回回执")


async def _send_notice(sender, text):
    with stage('delivery', kind='notice'):
        receipt = await sender.send(text)
    if receipt is None and not isinstance(sender, Sender):
        receipt = SendResult('ok', text)
    status = receipt.status if isinstance(receipt, SendResult) else 'unknown'
    log_event('delivery.receipt', status=status, kind='notice',
        text=receipt.text if isinstance(receipt, SendResult) else text,
        detail=receipt.detail if isinstance(receipt, SendResult) else 'missing_receipt')
    if status != 'ok':
        outcome(status)


@traced_message
async def handle_message(msg: IncomingMessage, sender: Sender) -> None:
    """Local commands precede enrichment; only model work occupies the model gate."""
    user_msg = (msg.text or "").strip()
    outcome('local_command' if user_msg.startswith('/') and msg.is_direct_address else 'completed')
    if msg.is_direct_address:
        if user_msg == "/help":
            await _send_notice(sender, HELP_TEXT)
            return
        if user_msg == "/reset":
            clear_history(msg.conv_id)
            await _send_notice(sender, "记忆已清除，让我们重新开始吧！")
            return
        result = handle_dm_cmd(user_msg, msg.conv_id, msg.user_id, msg.user_name)
        if result is None:
            result = handle_profile_cmd(user_msg, msg.conv_id, msg.user_id)
        if result is not None:
            await _send_notice(sender, result)
            return
        if user_msg == "/look" or user_msg.startswith("/look "):
            raw_info = user_msg[5:].strip()
            if not raw_info:
                await _send_notice(sender, "请提供要记住的信息，例如：/look 我喜欢简短的回答")
                return
            user_msg = f"用户要求记住偏好：「{raw_info}」。加载用户偏好技能，提炼后记录当前用户的偏好并简短确认。"
            msg.ai_decides_reply = False
    user_msg = user_msg or msg.reply_ctx
    try:
        with stage('media', image_count=len(msg.image_urls)):
            media = await asyncio.wait_for(_image_text(msg, user_msg), timeout=30) if msg.image_urls else ""
    except asyncio.TimeoutError:
        media = IMAGE_UNAVAILABLE
        log_event('media.timeout', timeout_sec=30)
    if media is None:
        media = IMAGE_UNAVAILABLE
    wake_context = ""
    entries = ()
    trigger = None
    outbox = None
    if msg.ai_decides_reply:
        from ..trigger import trigger
        from ..tools.chat_provider import ReplyOutbox
        entries = msg.context_entries or trigger.buffer.snapshot(msg.conv_id)
        wake_context = "\n".join(entry.line() for entry in entries) or msg.extra_ctx or user_msg
        if media and msg.image_descs is None:
            wake_context += "\n" + media
        user_msg = WAKE_PROMPT.format(context=wake_context, focus=msg.message_id or "最新一条消息")
        outbox = ReplyOutbox(known_ids={entry.message_id for entry in entries if entry.message_id})
    elif media:
        user_msg = f"{user_msg}\n图片描述：{media}"
    log_event('prompt.context', text=wake_context or user_msg, ai_decides_reply=msg.ai_decides_reply,
        quote_id=msg.quote.message_id if msg.quote else None,
        quote_chars=len(msg.quote.text) if msg.quote else len(msg.reply_ctx),
        quote_forwarded=bool(msg.quote and msg.quote.text and msg.quote.describe() in user_msg
            or msg.reply_ctx and msg.reply_ctx in user_msg),
        context_message_ids=[entry.message_id for entry in entries])
    if not user_msg:
        if not msg.is_interjection:
            await _send_notice(sender, "你好呀～发送 /help 查看功能。")
        return
    if api_cooldown_left() > 0 or len(_ABANDONED) >= 16:
        outcome('model_unavailable')
        log_event('model.unavailable', cooldown_sec=round(api_cooldown_left(), 1), abandoned_tasks=len(_ABANDONED))
        if trigger:
            trigger.leave_session(msg.conv_id)
        if msg.is_direct_address:
            await _send_notice(sender, "我暂时连不上模型服务，过一会儿再试～")
        return
    loop = asyncio.get_running_loop()
    gate = _MODEL_GATES.setdefault(loop, asyncio.Semaphore(ONEBOT_MAX_CONCURRENCY))
    try:
        with stage('model.gate'):
            await asyncio.wait_for(gate.acquire(), timeout=10)
    except asyncio.TimeoutError:
        outcome('busy')
        if msg.is_direct_address:
            await _send_notice(sender, "当前请求较多，请稍后再试。")
        return
    if not dc.acquire_service_probe():
        outcome('probe_busy')
        gate.release()
        return
    lease = runtime.RoundLease(msg.conv_id, None)
    set_round(lease.id)
    log_event('model.round.start', round_id=lease.id, notice_after_sec=DEEPSEEK_THINKING_NOTICE_SEC)
    lease.actors = {entry.message_id: (entry.user_id, entry.speaker) for entry in entries if entry.message_id and entry.user_id}
    if msg.message_id:
        lease.actors.setdefault(msg.message_id, (msg.user_id, msg.user_name))
    try:
        with runtime.use(lease):
            thinking = asyncio.create_task(chat(msg.conv_id, msg.user_id, user_msg,
                msg.user_name, outbox=outbox, history_text=wake_context or None))
        with stage('model.wait'):
            notified = set()
            model_notified = False
            while not thinking.done():
                kind, tag, started, phase_id = lease.progress
                threshold = TOOL_CALL_NOTICE_SEC if kind == 'tool' else DEEPSEEK_THINKING_NOTICE_SEC
                remaining = threshold - (time.monotonic() - started)
                already_notified = phase_id in notified or kind == "model" and model_notified
                interval = .05 if already_notified else min(.05, max(.001, remaining))
                done, _ = await asyncio.wait({thinking}, timeout=interval)
                if done:
                    break
                # Recheck after yielding: the active phase may have changed.
                if lease.progress[3] is not phase_id or time.monotonic() - started < threshold:
                    continue
                if phase_id in notified or kind == 'model' and model_notified:
                    continue
                notified.add(phase_id)
                if kind == 'model':
                    model_notified = True
                log_event('tool.long_running' if kind == 'tool' else 'model.round.long_running',
                    round_id=lease.id, tag=tag, notice_after_sec=threshold, stage=active_stage())
                if msg.is_direct_address:
                    text = f"正在调用{tag}技能" if kind == 'tool' else "正在思考中..."
                    try:
                        await asyncio.wait_for(_send_notice(sender, text), timeout=10)
                    except Exception as notice_error:
                        log_event('model.notice.failed', error_type=type(notice_error).__name__)
            reply = await asyncio.shield(thinking)
        lease.check()
        # Durable state is committed before announcing success to the user.
        with runtime.use(lease):
            lease.commit()
        note_api_success()
        log_event('model.round.finish', round_id=lease.id, status='ok')
    except asyncio.CancelledError:
        lease.revoke()
        if "thinking" in locals() and not thinking.done():
            _abandon(thinking)
        raise
    except Exception as exc:
        lease.revoke()
        outcome('error')
        log_event('model.round.finish', round_id=lease.id, status='error', error_type=type(exc).__name__, detail=str(exc)[:500])
        # Parameter/programming/persistence errors do not indicate model outage.
        if dc.is_service_failure(exc):
            note_api_failure(str(exc)[:120])
        if trigger:
            trigger.leave_session(msg.conv_id)
        if msg.is_direct_address:
            await _send_notice(sender, "我暂时连不上模型服务，请稍后重试。" if dc.is_service_failure(exc) else "本次处理未完成，请稍后重试。")
        return
    finally:
        gate.release()
        dc.release_service_probe()
    try:
        if outbox is not None and outbox.skipped and not outbox.has_items:
            waiting = outbox.skip_reason == 'wait'
            outcome('waiting' if waiting else 'skipped')
            log_event('chat.skipped', reason=outbox.skip_reason)
            if trigger:
                if not waiting:
                    trigger.buffer.mark_reviewed(msg.conv_id, entries)
                trigger.mark_answered(msg.conv_id)
            return
        items = [(item.text, item.reply_to) for item in outbox.items] if outbox is not None and outbox.has_items else [(reply, None)]
        if len(items) == 1 and items[0][0].strip().lower() in SKIP_TOKENS and msg.ai_decides_reply:
            outcome('skipped')
            if trigger:
                trigger.buffer.mark_reviewed(msg.conv_id, entries)
                trigger.mark_answered(msg.conv_id)
            return
        selected = {target for _, target in items if target}
        if trigger and selected:
            trigger.buffer.mark_reviewed(msg.conv_id,
                [entry for entry in entries if entry.message_id not in selected])
        # Content protection lives in _chat_parts, independently for each reply.
        natural_chat = lease.chat_segments_allowed
        planned = []
        extra = max(0, CHAT_MAX_SEGMENTS - len(items))
        for text, target in items:
            parts = _chat_parts(text, extra + 1) if natural_chat else [text]
            extra -= len(parts) - 1
            planned.extend((part, target if index == 0 else None, target) for index, part in enumerate(parts))
        items = planned
        receipts = []
        budget = [STICKER_MAX_PER_REPLY, msg.user_name]
        try:
            for index, (text, reply_to, source_target) in enumerate(items):
                if natural_chat and index and CHAT_SEGMENT_INTERVAL_SEC:
                    await asyncio.sleep(CHAT_SEGMENT_INTERVAL_SEC)
                budget[1] = lease.actors.get(source_target, (msg.user_id, msg.user_name))[1]
                with stage('delivery', reply_to=reply_to):
                    receipt = await _deliver(sender, text, reply_to, budget)
                log_event('delivery.receipt', status=receipt.status, text=receipt.text,
                    image_count=len(receipt.images), sticker_count=len(receipt.stickers), detail=receipt.detail,
                    reply_to=reply_to)
                outcome(receipt.status)
                receipts.append(receipt)
                if receipt.status not in ("ok", "dropped"):
                    break  # No duplicate retries after ambiguous or partial delivery.
        finally:
            recorded = [r.history_text() for r in receipts if r.status in ("ok", "partial", "unknown")]
            if recorded:
                dc.commit_delivery(lease, "\n".join(recorded))
            if trigger:
                complete = len(receipts) == len(items) and all(r.delivered for r in receipts)
                if complete:
                    trigger.buffer.consume(msg.conv_id, entries)
                else:
                    covered = set()
                    for (_, _, target), receipt in zip(items, receipts):
                        if receipt.status in ("ok", "partial", "unknown"):
                            if target:
                                covered.add(target)
                            elif receipt.status in ("partial", "unknown"):
                                covered.update(entry.message_id for entry in entries)
                    trigger.buffer.consume(msg.conv_id, [entry for entry in entries if entry.message_id in covered])
                if complete:
                    trigger.mark_answered(msg.conv_id)
                else:
                    trigger.leave_session(msg.conv_id)
    finally:
        lease.revoke()
