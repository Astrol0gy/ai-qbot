"""
回复决策工具（控制类）：让 AI 自己决定「要不要开口、怎么开口、跟谁说话」。

与其他工具不同，这两个不是"干活"的，而是"表态"：

- ``reply(text, reply_to?)`` → 记一条要发出去的回复。可以调用多次：几个人分别
  接你的话，就为每个人各调一次，并用 ``reply_to`` 引用他最后那句话。
- ``skip()`` → 本轮不开口（跟你无关的群聊、或你觉得对方还没说完）。

它们**不发消息，也不进 ToolRegistry**：注册表里的 handler 是无状态同步函数，
而这里每一条记录都必须绑定"这一轮该发到哪个会话、引用哪条消息"。因此
``deepseek_client.chat()`` 在工具循环里直接拦截这两个名字，把记录攒进
``ReplyOutbox``；``chat()`` 返回后由 ``channels/core.py`` 用 sender 逐条发出。

唤醒由系统负责（收到消息就交给 AI），开口与否由这里的工具决定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

REPLY_TOOL = "reply"
SKIP_TOOL = "skip"

_OWNED = {REPLY_TOOL, SKIP_TOOL}


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

    @staticmethod
    def owns(tool_name: str) -> bool:
        """是否是本模块接管的名字（chat() 用它决定拦截还是走注册表）"""
        return tool_name in _OWNED

    @property
    def has_items(self) -> bool:
        return bool(self.items)

    def as_history_text(self) -> str:
        """写进对话历史的内容：必须是**真正发出去的字**，否则下次醒来它会忘了自己说过什么"""
        return "\n".join(item.text for item in self.items)

    def tool_defs(self) -> list[dict]:
        """OpenAI Tool Calling 格式定义（只在普通群聊唤醒路径注入）"""
        return [
            {
                "type": "function",
                "function": {
                    "name": REPLY_TOOL,
                    "description": (
                        "回一条话。可以多次调用：不同的人在分别接你的话时，"
                        "为每个人各调一次，并用 reply_to 引用他最后那句话；"
                        "一次想说几件事也可以分几条发。不调用它、改用 skip 表示这轮不开口。"
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
                },
            },
            {
                "type": "function",
                "function": {
                    "name": SKIP_TOOL,
                    "description": (
                        "这轮不开口。用在：群里互相聊天、跟你无关；或者你觉得对方还没说完"
                        "（还会接着补充），想等他继续。不回复没有副作用，"
                        "群里有新消息时会再叫你。"
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    # ------------------------------------------------------------------
    def handle(self, tool_name: str, arguments: str) -> str:
        """执行一次控制类工具调用，返回给模型的 tool 结果"""
        if tool_name == SKIP_TOOL:
            return self._skip()
        if tool_name == REPLY_TOOL:
            return self._reply(arguments)
        return f"未知工具：{tool_name}"

    def _reply(self, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "reply 的参数不是合法 JSON，没有记录任何内容。"
        text = str(args.get("text") or "").strip()
        if not text:
            return "reply 缺少 text，没有记录任何内容。"

        raw_id = str(args.get("reply_to") or "").strip()
        note = ""
        if raw_id and raw_id not in self.known_ids:
            # 模型可能自己编一个 id：宁可少引用，也不能去引用一条不存在（或别处的）消息
            note = f"（reply_to={raw_id} 不在本轮消息里，已改为不引用发送）"
            raw_id = ""

        self.items.append(ReplyItem(text=text, reply_to=raw_id or None))
        self.skipped = False
        return f"已记录第 {len(self.items)} 条回复，本轮结束时按顺序发出。{note}"

    def _skip(self) -> str:
        # skip = 不开口，所以要撤掉本轮已经攒下的回复，避免"既回又不回"
        dropped = len(self.items)
        self.items.clear()
        self.skipped = True
        tail = f"（已撤掉本轮先前的 {dropped} 条）" if dropped else ""
        return f"已记录：本轮不开口。群里有新消息时会再叫你。{tail}"
