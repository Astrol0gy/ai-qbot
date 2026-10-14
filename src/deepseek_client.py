"""
DeepSeek V4 API 封装
使用 OpenAI 兼容接口调用 DeepSeek V4 Pro，支持：
- 多用户对话上下文记忆
- 深度思考模式（thinking + reasoning_effort）
- Tool Calling 联网搜索（模型自主决定调用搜索工具）
"""
from collections import defaultdict
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
    BOT_SYSTEM_PROMPT,
    MAX_HISTORY_LENGTH,
)
from .tools import execute as execute_tool, get_definitions, get_max_calls
from .reply_outbox import ReplyOutbox
from . import skills
from . import user_profile
from . import campaign_state

# 控制类工具（reply / skip）单轮最多调用几次：一批里可能好几个人同时说话，
# 要给"各回一条"留足额度，同时防止模型没完没了地"补一句"烧钱
MAX_CONTROL_CALLS = 6

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


def _make_key(group_id: str) -> str:
    return group_id


async def chat(
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
    history = _conversations[key]

    # 设置当前用户上下文（供 profile / campaign 工具使用）
    user_profile.set_current_context(group_id, user_id, user_name)
    campaign_state.set_current_context(group_id, user_id)

    # 说话者标识：优先昵称，缺失时用 member_openid 尾号
    speaker = (user_name or "").strip() or f"用户{str(user_id)[-4:]}"
    tagged_message = f"【{speaker}】{user_message}"
    # 写进对话历史的内容：唤醒路径的 user_message 外面包了唤醒指令，把包装存进历史会
    # 污染上下文，所以用原始群聊片段代替。片段里每行都已经带【说话者】，不能再套一层——
    # 否则历史里会出现「【张三】【张三】行，那就先这样」，模型还会照这个格式在自己的回复里也加标签
    history_user = tagged_message if not history_text else history_text

    # 构建请求消息（注入当前日期 + skill 指引 + 用户画像 + 跑团状态）
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    skills_meta = skills.get_metadata_section()        # Level 1: 元数据摘要
    profile_text = user_profile.get_profile_for_prompt()
    campaign_text = campaign_state.get_state_for_prompt(group_id)
    dm_mode = campaign_state.is_dm_mode(group_id)

    # 公共系统提示（日期 + 群聊说明 + 回复格式）
    common_prompt = (
        f"当前日期：{today}。涉及时间、天气、新闻等需要实时信息的问题，请使用 web_search 工具搜索，不要凭记忆编造。\n\n"
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
        dm_body = skills.get_full_prompt_for_skill("跑团主持")
        system_prompt = (
            "你是资深跑团主持人（Dungeon Master），正在主持一场文字冒险（TRPG）游戏。\n"
            "你的职责是引导剧情、描述场景、判定行动、管理角色状态，让玩家沉浸其中。\n"
            "多人同场时，根据【说话者】区分不同玩家，并记住各自角色。\n\n"
            f"{common_prompt}"
            f"{dm_body}"
            f"{campaign_text}"
        )
    else:
        # --- 普通模式：原有群 bot 设定 ---
        skills_body = skills.get_all_skill_bodies()
        system_prompt = (
            f"{BOT_SYSTEM_PROMPT}\n\n"
            f"{common_prompt}"
            "【可用命令】\n"
            "/help — 查看全部功能  /reset — 清除对话记忆\n"
            "/look <信息> — 让我记住你的偏好  /forget <关键词> — 忘记已记录的偏好  /list — 查看已记录的偏好\n\n"
            f"{skills_meta}"
            f"\n\n## 技能详细指引\n{skills_body}"
            f"{profile_text}"
        )

    messages = [{"role": "system", "content": system_prompt}]

    # --- 滑动窗口对话压缩（全局启用）---
    # 当历史接近上限时，把最旧的若干轮压缩成摘要，保留最近 K 轮原文，避免硬截断丢信息
    _KEEP_RECENT_ROUNDS = 4  # 保留最近 4 轮原文
    _COMPRESS_THRESHOLD = MAX_HISTORY_LENGTH * 2
    if len(history) > _COMPRESS_THRESHOLD:
        messages = await _compress_history(
            history, messages, _COMPRESS_THRESHOLD, _KEEP_RECENT_ROUNDS
        )
    else:
        messages.extend(history[-MAX_HISTORY_LENGTH * 2:])

    messages.append({"role": "user", "content": tagged_message})

    # 工具列表（普通群聊唤醒时额外带上 reply / skip）
    tools = list(get_definitions()) if DEEPSEEK_ENABLE_SEARCH else []
    if outbox is not None:
        tools += outbox.tool_defs()
    tools = tools or None  # 空列表会被 API 拒绝，统一成 None

    # 工具调用循环
    # 每个工具有独立的调用上限（max_calls），全局安全上限 20 轮防止死循环
    _MAX_GLOBAL_ROUNDS = 20
    _tool_call_counts: dict[str, int] = {}  # tool_name → 已调用次数

    for round_num in range(1, _MAX_GLOBAL_ROUNDS + 1):
        kwargs = _build_request_kwargs(messages, tools)
        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # 调试：打印思考过程
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            print(f"[🧠 思考 R{round_num}]\n{reasoning}")

        # 有工具调用 → 检查各工具配额
        if msg.tool_calls:
            print(f"[🔧 工具调用 R{round_num}] {[tc.function.name for tc in msg.tool_calls]}")
            messages.append(msg)

            all_blocked = True
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                # 控制类工具（reply / skip）：不干活，只在本轮记下"要说什么、回给谁"
                is_control = outbox is not None and ReplyOutbox.owns(tool_name)
                max_calls = MAX_CONTROL_CALLS if is_control else get_max_calls(tool_name)
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
                    if is_control:
                        result = outbox.handle(tool_name, tc.function.arguments)
                        print(f"[🗣️ 表态 R{round_num}][{tool_name}] {result}")
                    else:
                        result = execute_tool(tool_name, tc.function.arguments)
                        print(f"[📡 结果 R{round_num}][{tool_name} #{_tool_call_counts[tool_name]}/{max_calls}]\n{result}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

            if not all_blocked:
                continue  # 有工具成功执行，继续下一轮

        # 最终回答（无工具调用 或 所有工具都被阻塞）
        reply = msg.content or ""
        print(f"[✅ 完成] 共 {round_num} 轮")
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

    return reply


async def _compress_history(
    history: list[dict],
    messages: list[dict],
    threshold: int,
    keep_recent_rounds: int,
) -> list[dict]:
    """
    滑动窗口对话压缩：把最旧的轮次用 LLM 压缩成摘要，保留最近 keep_recent_rounds 轮原文。

    返回新的 messages 列表（system + 摘要 + 最近若干轮）。
    压缩失败时退回硬截断（与旧行为一致）。
    """
    keep_msgs = keep_recent_rounds * 2  # 每轮 = user + assistant 两条
    old_part = history[:-keep_msgs] if keep_msgs > 0 else history
    recent_part = history[-keep_msgs:] if keep_msgs > 0 else []

    if not old_part:
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
                "用纯文本、中文分点叙述，不要使用 Markdown。只输出摘要本身。"
            ),
        })
        kwargs = _build_request_kwargs(summary_input, tools=None)
        kwargs["tool_choice"] = "none"
        response = await client.chat.completions.create(**kwargs)
        summary = response.choices[0].message.content or ""

        if not summary:
            messages.extend(history[-threshold:])
            return messages

        # 摘要作为历史开头（system 之后第一条 user 消息，标记为压缩历史）
        messages.append({"role": "user", "content": f"【更早的对话摘要】\n{summary}"})
        messages.append({"role": "assistant", "content": "（已了解以上历史，继续对话）"})
        messages.extend(recent_part)
        print(f"[🗜️ 压缩] 将 {len(old_part)} 条旧消息压缩为摘要，保留最近 {len(recent_part)} 条")
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

        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
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
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                paragraphs = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
                reply = paragraphs[-1] if paragraphs else reasoning[-500:]
            else:
                reply = "抱歉，搜索过程耗时过长，请简化问题后重试。"

        print(f"[🔄 Fallback 总结]\n{reply[:200]}...")
        return reply
    except Exception as e:
        print(f"[❌ Fallback 失败] {e}")
        return "抱歉，搜索耗时过长，请稍后重试。"


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
    _conversations.pop(group_id, None)


def get_history_length(group_id: str) -> int:
    """获取当前群的对话轮数"""
    return len(_conversations[group_id]) // 2
