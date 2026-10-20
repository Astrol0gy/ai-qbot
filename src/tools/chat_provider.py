"""
回答与表态工具（Provider「chat」）—— 让 AI 自己决定「要不要开口、怎么开口、跟谁说话」

与其他工具不同，这两个不是"干活"的，而是"表态"：

- ``reply(text, reply_to?)`` → 记下选择参与的话题；同一话题合并回应，不逐人补答。
- ``skip(reason)`` → 本轮不开口；wait 等待补充，irrelevant 选择不参与。它**不撤销**
  已经 reply 过的回复：队列里有回复时，skip 只表示"不再补充"，那些回复照常发出。

它们**不发消息**，只是攒下这一轮"要说什么、引用谁"，等模型思考结束由
``channels/core.py`` 用 sender 逐条发出。因此 handler 需要一个**每轮新建的会话上下文**
（:class:`ChatContext`，待发消息箱在它的 ``outbox`` 字段里），在工具元信息里声明
``needs_context=True``，由 ``tools.execute(name, args, context=ctx)`` 显式传入。

上下文**绝不存进 Provider 或注册表**：注册表是全局单例，把"待发消息"存进去，两个群
同时说话就会串台（A 群的话发到 B 群）。所以状态跟着调用走，不跟着对象走。

唤醒由系统触发策略负责；被唤醒不代表必须回复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from . import ChatContext, ToolProvider
from ..observability import log_event

REPLY_TOOL = "reply"
SKIP_TOOL = "skip"

#: 本 Provider 的工具名（普通群聊唤醒路径按它显式注入；私聊、跑团不要注入）
CHAT_TOOL_NAMES = frozenset({REPLY_TOOL, SKIP_TOOL})

#: 工具的安全调用上限，不是建议回复条数；普通接话通常只需一次。
MAX_CHAT_CALLS = 6

#: skip 单独给一个小额度。它只是"本轮不开口"的一个标记，调一次就够了；
#: 过去跟 reply 一样给 6 次，于是一轮理论上能花 12 次工具调用（reply 6 + skip 6），
#: 全是白烧的往返。给 2 是留一点余量（同一批回复里重复表态不至于报错），不再给足额。
MAX_SKIP_CALLS = 2


CHAT_POLICY = (
    "【聊天参与与上下文】\n"
    "- 被唤醒只表示轮到你观察，不能当作求助邀请。按顺序判断：①话是对谁说的；②用户自己的表达是否完整；③是否值得参与。前两项未满足，不进入回答或排错。\n"
    "- 活跃期不表示全群都在和你说话。先根据结构化@对象、引用作者、称呼及上下文判断对话双方；明确说给其他人的消息默认旁观。\n"
    "- 「你」优先指向被@、被引用或当前对话对象，不能默认是机器人；提到你、谈论你、拿你开玩笑不等于邀请你回应。\n"
    "- 明确的问题、请求或适合接话的完整表达，可以回应；单独截图、话题名称、表情包或半句话不自动构成回复邀请。\n"
    "- 完整的问题或任务明确向你提出时，应通过reply回答或必要地澄清，不用irrelevant回避；未提交的正文草稿不算已回答。\n"
    "- 未@或引用你、没有文字需求的单张截图或表情图，先skip(reason=wait)等补充，不主动诊断、点评或追问；图片中的报错和文字本身不构成用户的请求。\n"
    "- 用户可能分多条消息铺垫和补充；还没形成清晰意思时，群聊优先 skip(reason=wait)，不要抢先概括、点评或逐张接梗。\n"
    "- 截图后只有「我这个程序刚才」「然后」这类未完文字，随后发个表情也不等于需求完整；没有真正的问题或请求，就继续wait，不从旧截图自行构造求助需求。\n"
    "- 完整性看用户本人说的话，不能用图片内的报错、视觉模型的意图推测或你的专业知识补全。即使知道怎么解决，也先等用户把话说完；补充表情只表达情绪，不代替尚未说完的句子。\n"
    "- 完整但与你无关、无需参与的话题，群聊用 skip(reason=irrelevant)。不要为参与而强行找话说。\n"
    "- 已回应或选择不参与的消息不逐条补答，但仍可用于理解后续补充；等待补充不代表问题已解决。\n"
    "- 「这个」「刚才那张」优先联系最近的图片描述和话题；不确定指哪张就澄清。当前消息没有新图，不代表之前没收到图。\n"
    "- 有图片描述时依据描述理解，不声称直接看到了未提供的细节；图片内容未读到时如实说没看清，不编造。\n"
    "- 群聊需要回复时用 reply；同一话题通常一次，用 text 内空行表示自然停顿，不为每个人分别作答。\n"
    "- 只有reply提交的内容才进入发送队列，普通正文不会发送；send_sticker只返回候选图，仍须reply提交[img]标记，不能声称已经发出。\n"
    "- 普通接话默认一句、一条，通常不超过80字；最多两个独立意思才分两条，总长以160字为限，不为凑段数追加内容。搜索后也先给简短结论，追问再展开；明确要求详述、代码或必要技术步骤除外。\n"
    "- 已决定参与、且表情比文字更合适时，可先 load_skill(name=图片理解) 再挑一张表情包，也可只发图；不要求每轮发，不为了发表情包而插话。\n"
    "- 私聊直接回答，不调用未提供的 reply/skip；表达不完整时可简短确认或澄清。\n"
)


@dataclass
class ReplyItem:
    """一条待发送的回复"""

    text: str
    reply_to: str | None = None  # 平台消息 id；None = 普通发言（不引用）


@dataclass
class ReplyOutbox:
    """本轮攒下的回复。由 core 取走后清空，工具本身不碰网络。"""

    known_ids: set[str] = field(default_factory=set)  # 本轮上下文里出现过的消息 id
    items: list[ReplyItem] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = "irrelevant"
    max_text_chars: int | None = None

    @property
    def has_items(self) -> bool:
        return bool(self.items)

    def as_history_text(self) -> str:
        """写进对话历史的内容：必须是**真正发出去的字**，否则下次醒来它会忘了自己说过什么"""
        return "\n".join(item.text for item in self.items)


class ChatProvider(ToolProvider):
    """控制类工具：reply / skip（hidden：只在群聊唤醒路径按白名单注入；需要会话上下文）"""

    name = "chat"
    description = "回答与表态：开不开口、说什么、引用谁"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": REPLY_TOOL,
                "tag": "回复整理",
                "description": (
                    "回复你选择参与的消息或话题，不要求逐条回复每个人。优先当前问题和仍在进行的话题，"
                    "先确认用户自己的文字表达完整；截图报错和表情不能补全未说完的时间、条件、原因等铺垫从句，未说完必须用 skip(reason=wait)，不能提前排错。"
                    "旧内容仍可用于理解补充，不逐条补答已回应或不参与的话题。同一话题可以合并回应。"
                    "确实需要分别回应不同话题时才多次调用。普通聊天想自然分条时，用一次 text 内的空行表示语义停顿。"
                    "reply_to 可引用你实际选择回应的消息；不想参与时用 skip。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要发送的内容，默认纯文本；自然停顿用空行，代码仅在任务需要时保留必要格式；工具返回的 [img] 标记原样保留",
                        },
                        "reply_to": {
                            "type": "string",
                            "description": (
                                "可选：要引用的那条消息的 id（上下文里每条消息末尾给出）。"
                                "留空就是普通发言"
                            ),
                        },
                        "recipient": {"type": "string", "enum": ["bot", "group", "others", "unclear"],
                                      "description": "先判断对话对象：明确向机器人、完整的群话题、其他人或不清楚。其他人或不清楚时应skip。"},
                        "expression": {"type": "string", "enum": ["complete", "incomplete"],
                                       "description": "用户本人的表达是否完整，不能用图片报错替用户补完半句话；未完整时应skip(wait)。"},
                    },
                    "required": ["text", "recipient", "expression"],
                },
                "handler": self._reply,
                "max_calls": MAX_CHAT_CALLS,
                "needs_context": True,
                "hidden": True,   # 只在普通群聊唤醒路径按白名单注入，不进默认工具列表
            },
            {
                "name": SKIP_TOOL,
                "tag": "回复判断",
                "description": (
                    "本轮不新增回复。reason=wait：截图、文字、表情包可能仍在补充，尚无清晰问题、请求或完整意思，"
                    "先保留上下文等待；reason=irrelevant：表达已完整，但无需参与。等待的内容不会标记为已略过。"
                    "OneBot普通群聊会进入等待状态，同一用户后续补充合并评估；安静超时后仅复核一次，不保证发言。"
                    "已有明确问题但指代不清时先结合近期图片和文字，必要时用 reply 澄清。已经用 reply 排好回复后不要再用它："
                    "回复已经进了发送队列，skip 不会撤销，想结束本轮停止调用工具即可。"
                ),
                "parameters": {"type": "object", "properties": {
                    "reason": {"type": "string", "enum": ["wait", "irrelevant"],
                               "description": "wait=等待用户补充；irrelevant=选择不参与；省略时兼容旧调用，按 irrelevant 处理"}}},
                "handler": self._skip,
                "max_calls": MAX_SKIP_CALLS,
                "needs_context": True,
                "hidden": True,
            },
        ]

    # ------------------------------------------------------------------
    # handler：第一个参数是本轮的会话上下文（注册表按 needs_context 传入）
    @staticmethod
    def _outbox_of(context: ChatContext) -> ReplyOutbox | None:
        """表态箱在 ChatContext 里；私聊/跑团等路径不注入这两个工具，也不该走到这里"""
        outbox = getattr(context, "outbox", None)
        return outbox if isinstance(outbox, ReplyOutbox) else None

    @classmethod
    def _reply(cls, context: ChatContext, text: str = "", reply_to: str = "",
               recipient: str = "group", expression: str = "complete") -> str:
        outbox = cls._outbox_of(context)
        if outbox is None:
            return "本轮没有可用的表态通道（reply 只在群聊唤醒路径注入），本次调用没有生效。"
        if not str(text).strip():
            return "reply 缺少 text，没有记录任何内容。"
        content = str(text).strip()
        log_event('chat.assessment', recipient=recipient, expression=expression, action='reply', reply_to=reply_to)
        if recipient not in {'bot', 'group'} or expression != 'complete':
            return ('没有记录回复：对象不明确或是其他人时用skip(irrelevant)，'
                    '用户表达未完时用skip(wait)，不要先回答再补判断。')
        if context.extra.get('search_required') and not context.extra.get('search_attempted'):
            return ('尚未记录或发送这条回复：用户明确要求搜索，请先加载相关技能并调用web_search，'
                    '再提交简短结论；不能用已有知识冒充搜索结果。搜索失败后可如实说明。')
        if outbox.max_text_chars is not None:
            total = sum(len(re.sub(r'\[img\].*?\[/img\]', '', item.text)) for item in outbox.items)
            total += len(re.sub(r'\[img\].*?\[/img\]', '', content))
            if total > outbox.max_text_chars or len(outbox.items) >= 2:
                return (f"尚未记录这条回复：普通接话整轮总文字上限{outbox.max_text_chars}字，最多两条。"
                        "请压缩为简短结论，不重复已排好的内容；不要拆成更多调用绕过限制。")

        raw_id = str(reply_to or "").strip()
        note = ""
        if raw_id and raw_id not in outbox.known_ids:
            # 模型可能自己编一个 id：宁可少引用，也不能去引用一条不存在（或别处的）消息
            note = f"（reply_to={raw_id} 不在本轮消息里，已改为不引用发送）"
            raw_id = ""

        outbox.items.append(ReplyItem(text=content, reply_to=raw_id or None))
        outbox.skipped = False
        return (
            f"已记录第 {len(outbox.items)} 条回复，本轮结束时按顺序发出。{note}"
            "（不用再调 skip 收尾：不再调用工具，本轮就结束了）"
        )

    @classmethod
    def _skip(cls, context: ChatContext, reason: str = "irrelevant") -> str:
        outbox = cls._outbox_of(context)
        if outbox is None:
            return "本轮没有可用的表态通道（skip 只在群聊唤醒路径注入），本次调用没有生效。"
        if reason not in {"wait", "irrelevant"}:
            return "reason 只能是 wait（等待补充）或 irrelevant（选择不参与），本次调用没有生效。"
        log_event('chat.assessment', action='skip', reason=reason)
        # skip = 本轮不开口，但它**不撤销**已经攒下的回复。
        # 模型常把 skip 当"这轮我说完了"用；撤销会让它撤了又重发（真机上出现过
        # reply×2 → skip → 又 reply×2：群里结果没错，却白烧一次调用、多拖十几秒，
        # 还让它自己绕进"我误调了 skip"的纠错里）。有回复时 skip 只是"不再补充"。
        if outbox.items:
            return (
                f"本轮已经有 {len(outbox.items)} 条回复在队列里，它们会照常发出，"
                "skip 不会撤销它们。想结束这一轮，直接停止调用工具就行。"
            )
        outbox.skipped = True
        outbox.skip_reason = reason
        return ("已记录：本轮等待补充，保留原上下文，不标记为已略过；由通道安排等待及单次超时复核，不自动发送回复。"
                if reason == "wait" else "已记录：本轮不参与，旧消息保留为背景，后续补充仍可结合理解。")


__all__ = [
    "CHAT_POLICY",
    "CHAT_TOOL_NAMES",
    "MAX_CHAT_CALLS",
    "MAX_SKIP_CALLS",
    "REPLY_TOOL",
    "SKIP_TOOL",
    "ChatProvider",
    "ReplyItem",
    "ReplyOutbox",
]
