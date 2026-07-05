"""
DeepSeek V4 API 封装
使用 OpenAI 兼容接口调用 DeepSeek V4 Pro，支持：
- 多用户对话上下文记忆
- 深度思考模式（thinking + reasoning_effort）
- Tool Calling 联网搜索（模型自主决定调用搜索工具）
"""
from collections import defaultdict
from datetime import datetime

from openai import AsyncOpenAI

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
    BOT_SYSTEM_PROMPT,
    MAX_HISTORY_LENGTH,
)
from .tools import execute as execute_tool, get_definitions

# 异步客户端
client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# 按群+用户维度存储对话历史：{ "group_id:user_id": [{role, content}, ...] }
_conversations: dict[str, list[dict]] = defaultdict(list)


def _make_key(group_id: str, user_id: str) -> str:
    return f"{group_id}:{user_id}"


async def chat(group_id: str, user_id: str, user_message: str) -> str:
    """
    发送消息给 DeepSeek V4 Pro 并获取回复。
    以 group_id + user_id 区分不同用户的对话上下文。
    联网搜索通过 Tool Calling 实现：模型自主决定是否需要搜索。
    """
    key = _make_key(group_id, user_id)
    history = _conversations[key]

    # 构建请求消息（注入当前日期，避免模型不知道"今天"是哪天）
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    system_prompt = f"{BOT_SYSTEM_PROMPT}\n\n当前日期：{today}。涉及时间、天气、新闻等需要实时信息的问题，请使用 web_search 工具搜索，不要凭记忆编造。"
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-MAX_HISTORY_LENGTH * 2:])
    messages.append({"role": "user", "content": user_message})

    # 工具列表
    tools = get_definitions() if DEEPSEEK_ENABLE_SEARCH else None

    # 工具调用循环（最多 8 轮。思考模式下每轮 15-30s，复杂搜索需要更多轮次）
    for round_num in range(1, 9):
        kwargs = _build_request_kwargs(messages, tools)
        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # 调试：打印思考过程
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            print(f"[🧠 思考 R{round_num}]\n{reasoning}")

        # 有工具调用 → 执行并继续
        if msg.tool_calls:
            print(f"[🔧 工具调用 R{round_num}] {[tc.function.name for tc in msg.tool_calls]}")
            messages.append(msg)

            for tc in msg.tool_calls:
                result = execute_tool(tc.function.name, tc.function.arguments)
                print(f"[📡 搜索结果 R{round_num}]\n{result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            continue

        # 最终回答
        reply = msg.content or ""
        print(f"[✅ 完成] 共 {round_num} 轮")
        break
    else:
        # fallback：轮数用完，用已获取的信息做最终总结
        tool_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        print(f"[⚠️ 超时] 达到 8 轮上限，已获取 {tool_count} 条搜索结果，执行 fallback 总结...")
        reply = await _fallback_summarize(messages)

    # 保存对话历史（只存 user/assistant，不存中间 tool 消息）
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})

    return reply


async def _fallback_summarize(messages: list[dict]) -> str:
    """用已有的搜索结果做最终总结，告知用户信息不完整"""
    try:
        messages.append({
            "role": "user",
            "content": (
                "以上是你多次搜索和抓取网页获取到的所有信息。"
                "请根据这些已有信息做一个总结回答用户的原始问题。"
                "如果信息不足以完整回答，请在总结后诚实地告知用户：由于搜索次数超限，可能未能获取到全部所需信息。"
            ),
        })
        kwargs = _build_request_kwargs(messages, tools=None)
        response = await client.chat.completions.create(**kwargs)
        reply = response.choices[0].message.content or ""
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
    """清除对话记忆。不传 user_id 则清除整个群的所有用户记忆。"""
    if user_id:
        _conversations.pop(_make_key(group_id, user_id), None)
    else:
        prefix = f"{group_id}:"
        keys = [k for k in _conversations if k.startswith(prefix)]
        for k in keys:
            del _conversations[k]


def get_history_length(group_id: str, user_id: str) -> int:
    """获取指定用户在当前群的对话轮数"""
    return len(_conversations[_make_key(group_id, user_id)]) // 2
