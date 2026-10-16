"""
DeepSeek V4 API 封装
使用 OpenAI 兼容接口调用 DeepSeek V4 Pro，支持：
- 多用户对话上下文记忆
- 深度思考模式（thinking + reasoning_effort）
- Tool Calling 联网搜索（模型自主决定调用搜索工具）
"""
import asyncio
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from openai import AsyncOpenAI, Timeout

from .config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TEMPERATURE,
    DEEPSEEK_MAX_TOKENS,
    DEEPSEEK_TOP_P,
    DEEPSEEK_ENABLE_THINKING,
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_ENABLE_SEARCH,
    DEEPSEEK_ROUND_TIMEOUT_SEC,
    DEEPSEEK_API_FAIL_LIMIT,
    DEEPSEEK_API_COOLDOWN_SEC,
    BOT_SYSTEM_PROMPT,
    MAX_HISTORY_LENGTH,
)
from .tools import (
    ChatContext,
    execute as execute_tool,
    get_definitions,
    get_max_calls,
    get_tool_names,
    has_tool,
    needs_context as tool_needs_context,
)
from .tools.chat_provider import CHAT_TOOL_NAMES, ReplyOutbox
from . import runtime
from . import skills
from . import user_profile
from . import campaign_state
from .observability import log_event, stage, tool_arguments

# 异步客户端。
# 这里只管**单次请求**的上限；真正的总时限在 core 里（到点就放弃这一轮，晚到的回答一律
# 作废）。所以既不能让一次请求无限等（SDK 默认 read=600s），也不能让 SDK 悄悄重试出
# 一堆"晚到的回答"—— 重试 1 次够应付抖动，剩下的交给 core 的总时限。
client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    timeout=Timeout(
        connect=5.0,
        read=DEEPSEEK_ROUND_TIMEOUT_SEC,
        write=DEEPSEEK_ROUND_TIMEOUT_SEC,
        pool=DEEPSEEK_ROUND_TIMEOUT_SEC,
    ),
    max_retries=1,
)

# 按群维度存储对话历史（群内所有用户共享同一上下文，消息带说话者标识区分是谁说的）
_conversations: dict[str, list[dict]] = defaultdict(list)


# ============================================
# 服务健康熔断
#
# 线上事故（2026-10-15 日志）：DeepSeek 服务端那段时间在挂（日志里有 503 Service is too
# busy），而这里没有任何兜底 —— 每一轮都规规矩矩地硬等满 60 秒才放弃，群里连着几轮都
# 等不到回复，用户看到的就是「bot 哑了」。连续失败到阈值就进冷却，冷却期内不再发请求、
# 直接放弃本轮：不回话也比两分钟后才回话强，而且不占着会话锁。
# ============================================
_fail_streak = 0
_cooldown_until = 0.0
_recovery_probe = False


def acquire_service_probe():
    global _recovery_probe
    if api_cooldown_left() > 0:
        return False
    if _cooldown_until:
        if _recovery_probe:
            return False
        _recovery_probe = True
    return True


def release_service_probe():
    global _recovery_probe
    _recovery_probe = False


def is_service_failure(exc):
    from openai import APIConnectionError, APITimeoutError, APIStatusError
    return (isinstance(exc, (APIConnectionError, APITimeoutError, asyncio.TimeoutError, ConnectionError))
            or isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)
            or isinstance(exc, RuntimeError) and "choices" in str(exc))


def api_cooldown_left() -> float:
    """熔断剩余秒数（0 表示服务可用）"""
    left = _cooldown_until - time.monotonic()
    return left if left > 0 else 0.0


def note_api_failure(reason: str) -> None:
    """记一次失败：连续失败够阈值就熔断一段时间"""
    global _fail_streak, _cooldown_until
    recovering = bool(_cooldown_until)
    _fail_streak = DEEPSEEK_API_FAIL_LIMIT if _cooldown_until else _fail_streak + 1
    log_event('service.failure', reason=reason, failure_count=None if recovering else _fail_streak,
        recovery_probe=recovering)
    if _fail_streak >= DEEPSEEK_API_FAIL_LIMIT:
        _cooldown_until = time.monotonic() + DEEPSEEK_API_COOLDOWN_SEC
        log_event('service.cooldown', reason=reason, cooldown_sec=DEEPSEEK_API_COOLDOWN_SEC,
            failure_threshold=DEEPSEEK_API_FAIL_LIMIT, recovery_probe=recovering)
        _fail_streak = 0  # 冷却结束后重新计数，避免刷屏


def note_api_success() -> None:
    """记一次成功：清零失败计数，并用一条日志宣告服务恢复"""
    global _fail_streak, _cooldown_until
    if _cooldown_until:
        log_event('service.recovered')
    _fail_streak = 0
    _cooldown_until = 0.0


def _make_key(group_id: str) -> str:
    return group_id


async def _chat_round(
    group_id: str,
    user_id: str,
    user_message: str,
    user_name: str = "",
    outbox: ReplyOutbox | None = None,
    history_text: str | None = None,
) -> str:
    """
    发送消息给 DeepSeek V4 Pro 并获取回复。
    对话上下文按群共享（不按用户隔离），每条消息带【说话者】标识供模型区分是谁在说。
    联网搜索通过 Tool Calling 实现：模型自主决定是否需要搜索。

    ``outbox`` 非空时（普通群聊唤醒路径）额外注入 reply / skip 两个控制类工具，
    由模型自己决定这轮开不开口、引用哪条消息。回复不在循环里发出，只记进 outbox，
    由通道层在返回后按顺序发送；返回值就是「真正发出去的内容」（skip 时为空串）。

    ``history_text`` 是写进对话历史的用户侧原文。唤醒路径的 ``user_message`` 外面
    包了一层唤醒指令，直接存进历史会污染上下文，所以用原始片段代替。
    """
    key = _make_key(group_id)
    history = list(_conversations.get(key, []))
    runtime.check()

    # 说话者标识：优先昵称，缺失时用 member_openid 尾号
    speaker = (user_name or "").strip() or f"用户{str(user_id)[-4:]}"

    # 本轮会话上下文：工具 handler 要据此知道"现在服务哪个群、谁在说话"。
    # 必须随调用链传下去 —— 过去是写进 user_profile / campaign_state 的模块级全局变量，
    # 而不同群可以并发跑同一轮 chat（通道层只保证同会话串行 + 一个全局并发上限），
    # 于是 A 群在这条 await 上让出时 B 群会把全局覆盖成 B，A 群的工具调用随后就写进了
    # B 群的存档 / B 用户的画像。改成显式传递后，工具层拿不到别人的会话。
    ctx = ChatContext(
        conv_id=group_id,
        user_id=user_id,
        user_name=speaker,
        outbox=outbox,
    )

    tagged_message = f"【{speaker}】{user_message}"
    # 写进对话历史的内容：唤醒路径的 user_message 外面包了唤醒指令，把包装存进历史会
    # 污染上下文，所以用原始群聊片段代替。片段里每行都已经带【说话者】，不能再套一层——
    # 否则历史里会出现「【张三】【张三】行，那就先这样」，模型还会照这个格式在自己的回复里也加标签
    history_user = tagged_message if not history_text else history_text

    # 构建请求消息（注入当前日期 + skill 指引 + 用户画像 + 跑团状态）
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    skills_meta = skills.get_metadata_section()  # Level 1：仅目录，不读取正文
    profile_text = user_profile.get_profile_for_prompt(group_id)
    campaign_text = campaign_state.get_state_for_prompt(group_id)
    dm_mode = campaign_state.is_dm_mode(group_id)

    # 能力开关先收紧可授予的范围；技能只能在这个范围内逐步开放业务工具。
    # 搜索开关不再误关画像、跑团、导航、生图等其他能力。
    enabled_names = {t["function"]["name"] for t in get_definitions()}
    if not DEEPSEEK_ENABLE_SEARCH:
        enabled_names -= {"web_search", "fetch_url", "search_images"}
    controls = set(skills.SKILL_TOOL_NAMES)
    if outbox is not None:
        controls.update(CHAT_TOOL_NAMES)
        enabled_names.update(CHAT_TOOL_NAMES)
    skill_session = skills.SkillSession(enabled_names, set(get_tool_names()))
    ctx.extra["skills"] = skill_session
    lease = runtime.current()
    ctx.extra["actors"] = getattr(lease, "actors", {})
    if ctx.extra["actors"]:
        system_actors = "\n".join(f"??id={mid} ??id={uid} ??={name}" for mid, (uid, name) in ctx.extra["actors"].items())
    else:
        system_actors = ""

    # 公共系统提示（日期 + 群聊说明 + 回复格式）
    common_prompt = (
        f"当前日期：{today}。涉及时间、天气、新闻等实时信息，先加载相关技能，再使用开放的工具核实，不要凭记忆编造。工具因配置不可用时直接说明。\n\n"
        "【群聊共享对话说明 — 必须遵守】\n"
        "你在 QQ 群里服务，群内所有用户共享同一段对话上下文，不会因换了说话者而遗忘前面的内容。\n"
        "每条用户消息都以【说话者】开头，请据此区分是谁在说话。多人同时在场时，注意称呼和回应对应的说话者。\n\n"
        "【回复格式要求 — 必须遵守】\n"
        "你的回复发送到 QQ 群聊，QQ 不支持 Markdown 渲染，禁止使用任何 Markdown 语法。\n"
        "需要强调或分点时，用以下纯文本写法代替：\n"
        "- 用【标题】或「关键词」突出重点，代替 **粗体**\n"
        "- 用数字序号 1. 2. 3. 表达分点，代替 ### 标题\n"
        "- 用 · 开头表示列表项，代替 - 或 * 列表\n"
        "- 禁止使用 # * _ ` ``` ~~ 等 Markdown 标记\n"
        "- 禁止使用 [文字](链接) 格式，直接写链接 URL 即可\n"
        "- 不要在回复开头加【说话者】这类人名标签（那是用户消息的标识，不是你的），直接说内容\n\n"
        "【说话风格 — 必须遵守】\n"
        "这是群聊，你是在跟朋友接话，不是在写作文、报告或说明书：\n"
        "- 默认就回一两句，像群里随口接话那么短；说完就停，别铺垫、别收尾总结\n"
        "- 分点、列表是例外：只有对方明确要你「详细说」「列一下」，或者内容本身就是几个并列选项时才用\n"
        "- 别用「总结一下」「总的来说」收尾，也别复述对方刚说过的话\n"
        "- 对方没要求展开时，长篇大论只会像个机器人在念稿\n"
    )

    if dm_mode:
        # --- DM 模式：跑团主持 ---
        system_prompt = (
            "你是资深跑团主持人（Dungeon Master），正在主持一场文字冒险（TRPG）游戏。\n"
            "你的职责是引导剧情、描述场景、判定行动、管理角色状态，让玩家沉浸其中。\n"
            "多人同场时，根据【说话者】区分不同玩家，并记住各自角色。\n\n"
            f"{common_prompt}"
            f"{skills_meta}"
            "当前是 DM 模式，本轮开始先用 load_skill 加载「跑团主持」，再按指引处理玩家行动。\n"
            f"{campaign_text}"
        )
    else:
        # --- 普通模式：原有群 bot 设定 ---
        system_prompt = (
            f"{BOT_SYSTEM_PROMPT}\n\n"
            f"{common_prompt}"
            "【可用命令】\n"
            "/help — 查看全部功能  /reset — 清除对话记忆\n"
            "/look <信息> — 让我记住你的偏好  /forget <关键词> — 忘记已记录的偏好  /list — 查看已记录的偏好\n\n"
            f"{skills_meta}"
            f"{profile_text}"
        )

    system_prompt += "\n【本轮消息的可信作者映射】\n" + system_actors
    messages = [{"role": "system", "content": system_prompt}]

    # --- 滑动窗口对话压缩（全局启用）---
    # 当历史接近上限时，把最旧的若干轮压缩成摘要，保留最近 K 轮原文，避免硬截断丢信息。
    # 压缩结果由 _compress_history **写回** history（不写回的话历史永不缩短，从超阈值那轮
    # 起每一轮都会把全部旧历史重压一遍 —— 不是增量，白烧一次调用加延迟，且越滚越贵）
    _KEEP_RECENT_ROUNDS = min(4, max(0, MAX_HISTORY_LENGTH - 1))  # 保留最近 4 轮原文
    _COMPRESS_THRESHOLD = MAX_HISTORY_LENGTH * 2
    if len(history) > _COMPRESS_THRESHOLD:
        messages = await _compress_history(
            history, messages, _COMPRESS_THRESHOLD, _KEEP_RECENT_ROUNDS
        )
    else:
        messages.extend(history[-MAX_HISTORY_LENGTH * 2:])

    messages.append({"role": "user", "content": tagged_message})

    # 工具调用循环
    # 每个工具有独立的调用上限（max_calls），全局安全上限 20 轮防止死循环
    _MAX_GLOBAL_ROUNDS = 20
    _tool_call_counts: dict[str, int] = {}  # tool_name → 已调用次数

    for round_num in range(1, _MAX_GLOBAL_ROUNDS + 1):
        definitions = get_definitions(whitelist=skill_session.allowed_tools(controls))
        # 一次响应内的所有调用共用快照：不能先 load_skill，再在同一批偷调新工具。
        allowed_for_request = frozenset(t["function"]["name"] for t in definitions)
        request_ctx = replace(ctx, allowed_tools=allowed_for_request)
        kwargs = _build_request_kwargs(messages, definitions or None)
        with stage('model.request', model=DEEPSEEK_MODEL, iteration=round_num,
                message_count=len(messages), input_chars=sum(len(str(m)) for m in messages),
                thinking=DEEPSEEK_ENABLE_THINKING, reasoning_effort=DEEPSEEK_REASONING_EFFORT,
                max_tokens=DEEPSEEK_MAX_TOKENS):
            response = await client.chat.completions.create(**kwargs)
        runtime.check()
        msg = _first_choice(response).message

        # 调试：打印思考过程
        reasoning = getattr(msg, "reasoning_content", None)
        usage = getattr(response, 'usage', None)
        log_event('model.response', iteration=round_num, request_id=getattr(response, '_request_id', None),
            finish_reason=getattr(_first_choice(response), 'finish_reason', None),
            reasoning_chars=len(reasoning or ''), output_chars=len(msg.content or ''),
            usage=usage.model_dump() if hasattr(usage, 'model_dump') else None)

        # 有工具调用 → 检查各工具配额
        if msg.tool_calls:
            log_event('model.tool_calls', iteration=round_num, tools=[tc.function.name for tc in msg.tool_calls])
            messages.append(msg)

            all_blocked = True
            available_names = ", ".join(sorted(allowed_for_request))
            for tc in msg.tool_calls:
                tool_name = tc.function.name

                # 未注册的工具要单独说清楚。过去它走的是一条假提示：get_max_calls 对未注册的
                # 工具返回 0，而判断是 count >= max_calls → 0>=0 成立，模型收到的是
                # "已达到本轮的调用上限（0次）"，完全看不出"这工具根本不存在"，排查会被带偏。
                if not has_tool(tool_name):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"没有名为「{tool_name}」的工具。本轮可用工具：{available_names}",
                    })
                    print(f"[❓ 未知工具] {tool_name}")
                    all_blocked = False  # 让模型有机会换个工具重试，别就此收尾
                    continue

                if tool_name not in allowed_for_request:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"工具「{tool_name}」未在本次请求中开放，本次没有执行。先 load_skill 加载对应技能；新工具从下一次请求起生效。当前可用：{available_names}",
                    })
                    all_blocked = False
                    continue

                # 额度写在 Provider 的元信息里（reply / skip 各 6 / 2，其余工具各有上限）
                max_calls = get_max_calls(tool_name)
                count = _tool_call_counts.get(tool_name, 0)

                if count >= max_calls:
                    # 工具已达调用上限，返回提示让模型换其他方式
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"工具「{tool_name}」已达到本轮的调用上限（{max_calls}次），请用已有信息回答，或尝试其他工具。",
                    })
                    print(f"[🔒 上限] {tool_name} 已达上限 {max_calls} 次，跳过")
                else:
                    all_blocked = False
                    _tool_call_counts[tool_name] = count + 1
                    # 工具实现全是同步阻塞的（requests，单次 8~10s）。直接在事件循环里调，
                    # 一次高德/搜索往返就把**所有群、两条通道**一起冻住 —— 连 core 那道 60 秒
                    # 总时限也一起失效（asyncio.wait 的 timeout 靠事件循环计时，循环被堵住时
                    # 定时器根本轮不到触发）。丢进工作线程后循环保持可用，闸门才算真存在。
                    with stage('tool', tool=tool_name, iteration=round_num,
                            arguments=tool_arguments(tc.function.arguments)):
                        result = await runtime.run_blocking(
                            execute_tool, tool_name, tc.function.arguments, request_ctx
                        )
                    runtime.check()
                    # Tool bodies remain in the model context, never in runtime logs.
                    log_event('tool.result', tool=tool_name, iteration=round_num,
                        result_chars=len(str(result)), call_count=_tool_call_counts[tool_name])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

            if not all_blocked:
                continue  # 有工具成功执行，继续下一轮

        # 最终回答（无工具调用 或 所有工具都被阻塞）
        reply = msg.content or ""
        log_event('model.complete', iterations=round_num)
        break
    else:
        # fallback：全局轮数用完
        tool_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        print(f"[⚠️ 超时] 达到 {_MAX_GLOBAL_ROUNDS} 轮全局上限，已获取 {tool_count} 条结果，执行 fallback 总结...")
        reply = await _fallback_summarize(messages)

    # ---------- 普通群聊唤醒：真正的"回答"是模型用工具记下的回复 ----------
    if outbox is not None:
        if outbox.has_items:
            sent = outbox.as_history_text()
            history.append({"role": "user", "content": history_user})
            history.append({"role": "assistant", "content": sent})
            _stage_history(key, history)
            return sent
        if outbox.skipped:
            # 选择不开口：什么都不写。这段上下文仍留在缓冲里，
            # 下次被叫醒时会连同新消息一起重新交给它
            print("[🤐 skip] 本轮不写入对话历史")
            return ""
        # 没调工具：当作普通回复，继续往下走

    # 保存对话历史（只存 user/assistant，不存中间 tool 消息）
    history.append({"role": "user", "content": history_user})
    history.append({"role": "assistant", "content": reply})
    _stage_history(key, history)

    return reply


# 摘要消息的标记：历史里靠它认出「这已经是压过的部分」，下次压缩时把旧摘要一起合并
_SUMMARY_MARKER = "【更早的对话摘要】"


async def _compress_history(
    history: list[dict],
    messages: list[dict],
    threshold: int,
    keep_recent_rounds: int,
) -> list[dict]:
    """
    滑动窗口对话压缩：把最旧的轮次用 LLM 压缩成摘要，保留最近 keep_recent_rounds 轮原文。

    **成功时会把 history 就地改写成「摘要 + 最近若干轮」**，这样历史真的变短了，下一轮不会
    再压一遍（老实现只改本次请求的 messages，history 原地不动 —— 于是从超阈值那轮起，每轮
    都要把不断变长的全部旧历史重新摘要一次）。改写是增量的：下次触发压缩时，待压部分已经
    以【更早的对话摘要】开头，模型把旧摘要和新对话合并即可。

    返回新的 messages 列表（system + 摘要 + 最近若干轮）。
    压缩失败（或正在熔断冷却）时退回硬截断，且不动 history（下轮还能重试）。
    """
    keep_msgs = keep_recent_rounds * 2  # 每轮 = user + assistant 两条
    old_part = history[:-keep_msgs] if keep_msgs > 0 else history
    recent_part = history[-keep_msgs:] if keep_msgs > 0 else []

    if not old_part:
        messages.extend(history[-threshold:])
        return messages

    if api_cooldown_left() > 0:
        # 熔断冷却期里连摘要都不要试：否则每轮都会先白等一次超时，比不压还慢
        print("[🗜️ 压缩跳过] DeepSeek 正在冷却期，本轮退回硬截断")
        messages.extend(history[-threshold:])
        return messages

    try:
        # 构造摘要请求（不带工具）
        summary_input = []
        for m in old_part:
            summary_input.append(m)
        summary_input.append({
            "role": "user",
            "content": (
                "请把以上对话内容压缩成一段简洁的摘要，保留关键信息、事实、决定、人物和上下文。"
                f"如果开头已有{_SUMMARY_MARKER}，把它和后面的新对话合并成这一段摘要，"
                "不要丢掉旧摘要里的关键信息。"
                "用纯文本、中文分点叙述，不要使用 Markdown。只输出摘要本身。"
            ),
        })
        kwargs = _build_request_kwargs(summary_input, tools=None)
        kwargs["tool_choice"] = "none"
        with stage('history.summary', model=DEEPSEEK_MODEL, old_messages=len(old_part)):
            response = await client.chat.completions.create(**kwargs)
        summary = _first_choice(response).message.content or ""

        if not summary:
            messages.extend(history[-threshold:])
            return messages

        # 摘要作为历史开头（system 之后第一条 user 消息，标记为压缩历史）
        summary_pair = [
            {"role": "user", "content": f"{_SUMMARY_MARKER}\n{summary}"},
            {"role": "assistant", "content": "（已了解以上历史，继续对话）"},
        ]
        before = len(history)
        # 关键一步：写回历史本体。只改 messages 的话，下一轮 len(history) 依旧超阈值，
        # 于是每轮都把这段旧历史重压一遍（且越压越长）——摘要等于白做
        history[:] = summary_pair + recent_part
        messages.extend(summary_pair)
        messages.extend(recent_part)
        log_event('history.compressed', old_messages=before, new_messages=len(history), kept_messages=len(recent_part))
        return messages
    except Exception as e:
        print(f"[⚠️ 压缩失败，退回硬截断] {e}")
        messages.extend(history[-threshold:])
        return messages


async def _fallback_summarize(messages: list[dict]) -> str:
    """用已有的搜索结果做最终总结。强制不调用工具，清理残留工具调用文本。"""
    import re
    try:
        messages.append({
            "role": "user",
            "content": (
                "以上是你多次搜索和抓取网页获取到的所有信息。"
                "请根据这些已有信息做一个最终总结回答用户的原始问题，不要再调用任何工具。"
                "如果信息不足以完整回答，请在总结后诚实地告知用户：由于搜索次数超限，可能未能获取到全部所需信息。"
            ),
        })
        kwargs = _build_request_kwargs(messages, tools=None)
        kwargs["tool_choice"] = "none"  # 强制禁止工具调用

        with stage('model.fallback', model=DEEPSEEK_MODEL):
            response = await client.chat.completions.create(**kwargs)
        msg = _first_choice(response).message
        reply = msg.content or ""

        # 清理可能残留的工具调用 XML 标签
        reply = re.sub(
            r"<[|]?\s*(invoke|tool_calls?|function_calls?).*?>.*?</[|]?\s*\1\s*>",
            "", reply, flags=re.DOTALL | re.IGNORECASE,
        )
        reply = re.sub(r"<[|]?\s*invoke\s+name=.*?/>", "", reply, flags=re.IGNORECASE)
        reply = reply.strip()

        # 模型坚持输出工具调用（content 为空或只剩工具标签）
        if not reply:
            raise RuntimeError("模型没有返回可发送的正文")

        print(f"[🔄 Fallback 总结]\n{reply[:200]}...")
        return reply
    except Exception as e:
        print(f"[❌ Fallback 失败] {e}")
        raise


def _first_choice(response):
    """
    取第一个 choice，顺手把「200 但没有 choices」这种响应变成一句查得动的报错。

    线上真出现过（视觉那边炸成 'NoneType' object is not subscriptable，什么都看不出来）。
    主链路、历史压缩、兜底总结都从这里取，免得旁路各写一份、漏一个就是又一句无头 AttributeError。
    """
    choices = getattr(response, "choices", None)
    if not choices:
        try:
            raw = str(response.model_dump())[:600]
        except Exception:
            raw = repr(response)[:600]
        raise RuntimeError(f"DeepSeek 返回了没有 choices 的响应：{raw}")
    return choices[0]


def _build_request_kwargs(messages: list[dict], tools: list[dict] | None) -> dict:
    """构建 API 请求参数"""
    kwargs: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
    }

    extra_body: dict = {}

    if DEEPSEEK_ENABLE_THINKING:
        extra_body["thinking"] = {"type": "enabled"}
        kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
    else:
        extra_body["thinking"] = {"type": "disabled"}
        kwargs["temperature"] = DEEPSEEK_TEMPERATURE
        kwargs["top_p"] = DEEPSEEK_TOP_P

    if tools:
        kwargs["tools"] = tools

    if extra_body:
        kwargs["extra_body"] = extra_body

    return kwargs


# ============================================
# 对话管理
# ============================================
def clear_history(group_id: str, user_id: str = ""):
    """清除对话记忆（按群，群内所有用户共享同一上下文）"""
    runtime.reset_context(group_id)


def get_history_length(group_id: str) -> int:
    """获取当前群的对话轮数"""
    return len(_conversations.get(group_id, [])) // 2


def _stage_history(key, history):
    runtime.check()
    lease = runtime.current()
    if lease:
        lease.history = (key, history)
    else:
        _store_history(key, history)


def _store_history(key, history):
    _conversations.pop(key, None)
    _conversations[key] = history[-max(2, MAX_HISTORY_LENGTH * 2 + 2):]
    while len(_conversations) > 500:
        _conversations.pop(next(iter(_conversations)))


def commit_delivery(lease, text):
    if lease.history and text:
        key, history = lease.history
        if lease.reset_history:
            history = history[-2:]
        history[-1] = {"role": "assistant", "content": text}
        _store_history(key, history)
    lease.history = None


async def chat(*args, **kwargs):
    if runtime.current() is not None:
        return await _chat_round(*args, **kwargs)
    lease = runtime.RoundLease(args[0] if args else kwargs["group_id"], DEEPSEEK_ROUND_TIMEOUT_SEC)
    with runtime.use(lease):
        try:
            reply = await _chat_round(*args, **kwargs)
            lease.commit()
            commit_delivery(lease, reply)
            return reply
        finally:
            lease.revoke()
