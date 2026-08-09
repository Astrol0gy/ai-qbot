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
from .tools import execute as execute_tool, get_definitions, get_max_calls
from . import skills
from . import user_profile

# 异步客户端
client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# 按群维度存储对话历史（群内所有用户共享同一上下文，消息带说话者标识区分是谁说的）
_conversations: dict[str, list[dict]] = defaultdict(list)


def _make_key(group_id: str) -> str:
    return group_id


async def chat(group_id: str, user_id: str, user_message: str, user_name: str = "") -> str:
    """
    发送消息给 DeepSeek V4 Pro 并获取回复。
    对话上下文按群共享（不按用户隔离），每条消息带【说话者】标识供模型区分是谁在说。
    联网搜索通过 Tool Calling 实现：模型自主决定是否需要搜索。
    """
    key = _make_key(group_id)
    history = _conversations[key]

    # 设置当前用户上下文（供 profile 工具使用）
    user_profile.set_current_context(group_id, user_id, user_name)

    # 说话者标识：优先昵称，缺失时用 member_openid 尾号
    speaker = (user_name or "").strip() or f"用户{str(user_id)[-4:]}"
    tagged_message = f"【{speaker}】{user_message}"

    # 构建请求消息（注入当前日期 + skill 指引 + 用户画像）
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    skills_meta = skills.get_metadata_section()        # Level 1: 元数据摘要
    skills_body = skills.get_all_skill_bodies()         # 完整 skill body
    profile_text = user_profile.get_profile_for_prompt()
    system_prompt = (
        f"{BOT_SYSTEM_PROMPT}\n\n"
        f"当前日期：{today}。涉及时间、天气、新闻等需要实时信息的问题，请使用 web_search 工具搜索，不要凭记忆编造。\n\n"
        "【群聊共享对话说明 — 必须遵守】\n"
        "你在 QQ 群里服务，群内所有用户共享同一段对话上下文，不会因换了说话者而遗忘前面的内容。\n"
        "每条用户消息都以【说话者】开头，请据此区分是谁在说话，并结合该用户的历史画像调整语气和内容；"
        "多人同时在场时，注意称呼和回应对应的说话者。\n\n"
        "【可用命令】\n"
        "/help — 查看全部功能  /reset — 清除对话记忆\n"
        "/look <信息> — 让我记住你的偏好  /forget <关键词> — 忘记已记录的偏好  /list — 查看已记录的偏好\n\n"
        "【回复格式要求 — 必须遵守】\n"
        "你的回复发送到 QQ 群聊，QQ 不支持 Markdown 渲染，禁止使用任何 Markdown 语法。\n"
        "请使用以下纯文本格式替代：\n"
        "- 用【标题】或「关键词」突出重点，代替 **粗体**\n"
        "- 用数字序号 1. 2. 3. 表达分点，代替 ### 标题\n"
        "- 用 · 开头表示列表项，代替 - 或 * 列表\n"
        "- 禁止使用 # * _ ` ``` ~~ 等 Markdown 标记\n"
        "- 禁止使用 [文字](链接) 格式，直接写链接 URL 即可\n"
        f"{skills_meta}"                                # skill 列表
        f"\n\n## 技能详细指引\n{skills_body}"            # 完整流程
        f"{profile_text}"                                # 用户画像（如有）
    )
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-MAX_HISTORY_LENGTH * 2:])
    messages.append({"role": "user", "content": tagged_message})

    # 工具列表
    tools = get_definitions() if DEEPSEEK_ENABLE_SEARCH else None

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

    # 保存对话历史（只存 user/assistant，不存中间 tool 消息）
    history.append({"role": "user", "content": tagged_message})
    history.append({"role": "assistant", "content": reply})

    return reply


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
