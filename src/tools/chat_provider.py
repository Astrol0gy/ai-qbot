"""
回答与表态工具（Provider「chat」）—— 让 AI 自己决定「要不要开口、怎么开口、跟谁说话」

与其他工具不同，这两个不是"干活"的，而是"表态"：

- ``reply(text, reply_to?)`` → 记一条要发出去的回复。可以调用多次：几个人分别
  接你的话，就为每个人各调一次，并用 ``reply_to`` 引用他最后那句话。
- ``skip()`` → 本轮不开口（跟你无关的群聊、或你觉得对方还没说完）。它**不撤销**
  已经 reply 过的回复：队列里有回复时，skip 只表示"不再补充"，那些回复照常发出。

它们**不发消息**，只是攒下这一轮"要说什么、引用谁"，等模型思考结束由
``channels/core.py`` 用 sender 逐条发出。因此 handler 需要一个**每轮新建的会话上下文**
（:class:`ChatContext`，待发消息箱在它的 ``outbox`` 字段里），在工具元信息里声明
``needs_context=True``，由 ``tools.execute(name, args, context=ctx)`` 显式传入。

上下文**绝不存进 Provider 或注册表**：注册表是全局单例，把"待发消息"存进去，两个群
同时说话就会串台（A 群的话发到 B 群）。所以状态跟着调用走，不跟着对象走。

唤醒由系统负责（收到消息就交给 AI），开口与否由这里的工具决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ChatContext, ToolProvider

REPLY_TOOL = "reply"
SKIP_TOOL = "skip"

#: 本 Provider 的工具名（普通群聊唤醒路径按它显式注入；私聊、跑团不要注入）
CHAT_TOOL_NAMES = frozenset({REPLY_TOOL, SKIP_TOOL})

#: 单轮最多 reply 几次：一批里可能好几个人同时说话，要给"各回一条"留足额度，
#: 同时防止模型没完没了地"补一句"烧钱
MAX_CHAT_CALLS = 6

#: skip 单独给一个小额度。它只是"本轮不开口"的一个标记，调一次就够了；
#: 过去跟 reply 一样给 6 次，于是一轮理论上能花 12 次工具调用（reply 6 + skip 6），
#: 全是白烧的往返。给 2 是留一点余量（同一批回复里重复表态不至于报错），不再给足额。
MAX_SKIP_CALLS = 2


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
                    "旧消息只作背景，已回应或已略过的内容不补答。同一话题可以合并回应。"
                    "确实需要分别回应不同话题时才多次调用。普通聊天想自然分条时，用一次 text 内的空行表示语义停顿。"
                    "reply_to 可引用你实际选择回应的消息；不想参与时用 skip。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要发送的内容，纯文本，不要 Markdown",
                        },
                        "reply_to": {
                            "type": "string",
                            "description": (
                                "可选：要引用的那条消息的 id（上下文里每条消息末尾给出）。"
                                "留空就是普通发言"
                            ),
                        },
                    },
                    "required": ["text"],
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
                    "这轮不开口，一条都不回。用在：群里互相聊天、跟你无关；或者你觉得对方"
                    "还没说完（还会接着补充），想等他继续。不回复没有副作用，"
                    "群里有新消息时会再叫你。已经用 reply 排好回复后就不要再用它："
                    "回复已经进了发送队列，skip 不会撤销，想结束本轮停止调用工具即可。"
                ),
                "parameters": {"type": "object", "properties": {}},
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
    def _reply(cls, context: ChatContext, text: str = "", reply_to: str = "") -> str:
        outbox = cls._outbox_of(context)
        if outbox is None:
            return "本轮没有可用的表态通道（reply 只在群聊唤醒路径注入），本次调用没有生效。"
        if not str(text).strip():
            return "reply 缺少 text，没有记录任何内容。"
        content = str(text).strip()

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
    def _skip(cls, context: ChatContext) -> str:
        outbox = cls._outbox_of(context)
        if outbox is None:
            return "本轮没有可用的表态通道（skip 只在群聊唤醒路径注入），本次调用没有生效。"
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
        return "已记录：本轮不开口。群里有新消息时会再叫你。"


__all__ = [
    "CHAT_TOOL_NAMES",
    "MAX_CHAT_CALLS",
    "MAX_SKIP_CALLS",
    "REPLY_TOOL",
    "SKIP_TOOL",
    "ChatProvider",
    "ReplyItem",
    "ReplyOutbox",
]
