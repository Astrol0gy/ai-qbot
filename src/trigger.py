"""
触发策略控制器（通道无关）。

系统只回答一个问题：**收到这条消息，要不要把 AI 叫起来**；「开不开口、跟谁说话、
说什么」一律交给 AI 自己用 reply / skip 工具决定。普通群聊有两个状态：

- **空闲**：按概率决定要不要开一局（`TRIGGER_PROBABILITY`），@ 必定开局
- **互动中**：原参与者或引用机器人的消息无需掷骰；其他成员仍按概率评估

实际回复后重置 `TRIGGER_ACTIVE_TIMEOUT_SEC` 窗口；入站消息和 wait 不续期，
irrelevant 结束窗口。截止时间到期后回到概率唤醒，不需要定时器任务。

其余规则：
- 私聊                          → 叫（不看概率，也不进互动窗口）
- 群聊 @机器人                   → 叫，并开启/延续互动窗口
- 群聊未被 @ 且以 / 开头          → 不叫（发送者显然想调指令，当闲聊交给 AI 只会得到无意义回复）
- 群聊未被 @ 且处于 DM（跑团）模式 → 不叫（不送 AI、不入缓冲，等价于官方框架 @ 语义）
- 群聊明确说给其他人             → 留作背景，不唤醒
- 群聊未被 @ 的其他消息           → 群黑白名单 → 最小长度 → 按参与者及窗口判断 / 掷骰

叫醒时会把「自上次开口以来攒下的群聊片段」一并交给 AI（每条带说话者、消息 id、
是否 @ 了机器人、引用了谁的话）。
"""
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

from .config import (
    ONEBOT_GROUP_BLACKLIST,
    ONEBOT_GROUP_WHITELIST,
    TRIGGER_ACTIVE_TIMEOUT_SEC,
    TRIGGER_CONTEXT_MESSAGES,
    TRIGGER_MIN_LEN,
    TRIGGER_PROBABILITY,
    TRIGGER_RECOGNIZE_IMAGES,
    CHAT_WAIT_TIMEOUT_SEC,
)
from .channels.base import IncomingMessage


class Decision(str, Enum):
    """触发结果"""

    WAKE = "wake"      # 把 AI 叫起来；是否开口、怎么开口由 AI 用 reply/skip 决定
    IGNORE = "ignore"  # 不进 AI，静默丢弃


@dataclass
class TriggerResult:
    decision: Decision
    context: str = ""   # 仅 WAKE 时填充：拼好的群聊片段（含本条）
    reason: str = ""    # 仅用于日志


@dataclass
class BufferEntry:
    """缓冲里的一条群消息：交给 AI 的关系信号全在这里"""

    speaker: str = ""
    user_id: str = ""
    text: str = ""
    message_id: str = ""
    at_bot: bool = False
    quote: str = ""     # 已拼好的人类可读描述，如「引用了你的消息：「…」」
    ts: float = 0.0     # 入缓冲时刻（monotonic），用于「只清掉这一轮之前」的判断
    reviewed: bool = False  # 已考虑但选择略过，只作后续理解话题的背景
    mention_user_ids: tuple[str, ...] = ()
    quote_author_id: str = ""
    quote_is_bot: bool = False

    def line(self) -> str:
        marks = []
        if self.reviewed:
            marks.append("已看过并略过，仅作背景，无需单独补答；仍可用于理解后续补充")
        if self.at_bot:
            marks.append("@了你")
        if self.mention_user_ids:
            marks.append("明确@对象=" + ",".join(self.mention_user_ids))
        if self.quote_author_id:
            marks.append("引用作者id=" + self.quote_author_id + ("（机器人）" if self.quote_is_bot else "（非机器人）"))
        if self.quote:
            marks.append(self.quote)
        if self.message_id:
            marks.append(f"消息id={self.message_id}")
        if self.user_id:
            marks.append(f"用户id={self.user_id}")
        suffix = f"  [{'，'.join(marks)}]" if marks else ""
        return f"【{self.speaker or '某人'}】{self.text}{suffix}"


class MessageBuffer:
    """
    维护每个会话最近若干条「机器人还没开口回应」的群消息。

    - 每个会话最多保留 TRIGGER_CONTEXT_MESSAGES 条
    - 会话数量设 LRU 上限，避免长期运行内存无界增长
    - 只有 AI 真的开口了才消费（core 调 clear）；AI 选择 skip 就留着，
      这样它下一条消息被叫醒时看到的是完整的一段，而不是被截断的半句
    """

    def __init__(self, max_per_conv: int | None = None, max_convs: int = 500):
        self._max_per_conv = max_per_conv or TRIGGER_CONTEXT_MESSAGES
        self._max_convs = max_convs
        self._buf: OrderedDict[str, list[BufferEntry]] = OrderedDict()

    def add(self, conv_id: str, entry: BufferEntry) -> None:
        if not conv_id or not (entry.text or "").strip():
            return

        entries = self._buf.get(conv_id)
        if entries is None:
            entries = []
            self._buf[conv_id] = entries
        else:
            self._buf.move_to_end(conv_id)

        entry.ts = entry.ts or time.monotonic()
        entries.append(entry)
        if len(entries) > self._max_per_conv:
            del entries[: len(entries) - self._max_per_conv]

        while len(self._buf) > self._max_convs:
            self._buf.popitem(last=False)

    def peek(self, conv_id: str) -> str:
        """看一眼缓冲（不消费），拼成带关系信号的文本"""
        entries = self._buf.get(conv_id) or []
        return "\n".join(e.line() for e in entries)

    def ids(self, conv_id: str) -> set[str]:
        """缓冲里出现过的消息 id：AI 只允许引用这些，防止它自己编一个 id"""
        return {e.message_id for e in (self._buf.get(conv_id) or []) if e.message_id}

    def snapshot(self, conv_id):
        return tuple(self._buf.get(conv_id, ()))

    def consume(self, conv_id, entries):
        identities = {id(entry) for entry in entries}
        kept = [entry for entry in self._buf.get(conv_id, []) if id(entry) not in identities]
        if kept:
            self._buf[conv_id] = kept
        else:
            self._buf.pop(conv_id, None)

    def mark_reviewed(self, conv_id, entries):
        identities = {id(entry) for entry in entries}
        for entry in self._buf.get(conv_id, []):
            if id(entry) in identities:
                entry.reviewed = True

    def clear(self, conv_id: str, before: float | None = None) -> None:
        """
        丢弃该会话的缓冲（机器人已经开口应答，上下文重新开始）。

        ``before`` 限定只清「这一轮开始时就已经看到」的消息：AI 思考期间新到的
        消息要留给紧接着的下一轮（那一轮会把它们合成一次交给 AI），不能一起抹掉。
        """
        if before is None:
            self._buf.pop(conv_id, None)
            return

        entries = self._buf.get(conv_id)
        if not entries:
            return
        kept = [e for e in entries if e.ts > before]
        if kept:
            self._buf[conv_id] = kept
        else:
            self._buf.pop(conv_id, None)


class TriggerController:
    """按群维度判定是否响应，并维护接话用的上下文缓冲。"""

    def __init__(self):
        self.buffer = MessageBuffer()
        # 互动窗口：conv_id → 截止时间（monotonic）。过期即视为「空闲」，
        # 所以状态不需要额外的定时器任务去清理。
        self._active_until: dict[str, float] = {}
        self._session_users: dict[str, set[str]] = {}
        self._waiting = {}

    def mark_waiting(self, conv_id, user_id, focus_id):
        now = time.monotonic()
        self._waiting = {key: value for key, value in self._waiting.items() if value[0] > now}
        if len(self._waiting) >= 1000:
            self._waiting.pop(next(iter(self._waiting)))
        state = (now + CHAT_WAIT_TIMEOUT_SEC, focus_id, object())
        self._waiting[conv_id, user_id] = state
        return state

    def waiting_for(self, conv_id, user_id):
        return self._waiting.get((conv_id, user_id))

    def clear_waiting(self, conv_id, user_id=None):
        if user_id is None:
            self._waiting = {key:value for key,value in self._waiting.items() if key[0] != conv_id}
        else:
            self._waiting.pop((conv_id, user_id), None)

    # ------------------------------------------------------------------
    # 主判定入口
    # ------------------------------------------------------------------
    def decide(self, msg: IncomingMessage) -> TriggerResult:
        # 私聊：必回
        if not msg.is_group:
            return TriggerResult(Decision.WAKE)

        text = (msg.text or "").strip()
        # 上下文文本 = 纯文本 + 图片描述（未 @ 消息可能已预识别）；
        # 「只引用不说话」的消息正文是空的，用引用原文兜底，否则这一条会从上下文里消失
        ctx_text = msg.buffer_text() or msg.reply_ctx
        entry = BufferEntry(
            speaker=msg.user_name,
            user_id=msg.user_id,
            text=ctx_text,
            message_id=msg.message_id,
            at_bot=msg.is_at_bot,
            quote=msg.quote.describe() if msg.quote else "",
            ts=msg.received_at,
            mention_user_ids=msg.mention_user_ids,
            quote_author_id=msg.quote.author_id if msg.quote else '',
            quote_is_bot=bool(msg.quote and msg.quote.is_bot),
        )

        # 未被 @ 的指令：直接忽略，不执行、不唤醒、不入缓冲
        if not msg.is_at_bot and text.startswith("/"):
            return TriggerResult(Decision.IGNORE, reason="非@指令")

        # DM（跑团）模式：非 @ 消息一律丢弃，不送 AI、不入缓冲
        if not msg.is_at_bot and self._is_dm_mode(msg.conv_id):
            return TriggerResult(Decision.IGNORE, reason="DM模式非@")

        # 群黑白名单
        if not self._group_allowed(msg.raw.get("group_id")):
            return TriggerResult(Decision.IGNORE, reason="群名单过滤")

        if msg.is_at_bot and text.startswith("/"):
            return TriggerResult(Decision.WAKE, reason="本地命令")

        # 入缓冲：叫醒时它就是要交给 AI 的上下文
        self.buffer.add(msg.conv_id, entry)

        # Explicit recipients take precedence over the group-wide activity state.
        if msg.addressed_to_others:
            entry.reviewed = True
            return TriggerResult(Decision.IGNORE, reason="对其他人说话")

        waiting = self.waiting_for(msg.conv_id, msg.user_id)
        if waiting:
            return TriggerResult(Decision.WAKE, context=self.buffer.peek(msg.conv_id), reason="等待补充中")

        # 未被 @ 且过短：不值得单独把 AI 叫起来（仍留在缓冲里，等后面某条一起带出去）
        if not msg.is_at_bot and len(ctx_text) < TRIGGER_MIN_LEN:
            return TriggerResult(Decision.IGNORE, reason="过短")

        now = time.monotonic()

        # @机器人：立即唤醒，并开启/延续互动窗口
        if msg.is_at_bot:
            self._enter_session(msg.conv_id, now, msg.user_id)
            return TriggerResult(
                Decision.WAKE,
                context=self.buffer.peek(msg.conv_id),
                reason="@",
            )

        # Only the ongoing participant (or a reply to the bot) bypasses sampling.
        # Incoming messages never slide the deadline forward.
        if self.is_active(msg.conv_id, now) and (
                msg.user_id in self._session_users.get(msg.conv_id, set())
                or msg.quote and msg.quote.is_bot):
            return TriggerResult(
                Decision.WAKE,
                context=self.buffer.peek(msg.conv_id),
                reason="互动中",
            )

        # 空闲：按概率决定要不要开一局
        if random.random() >= TRIGGER_PROBABILITY:
            return TriggerResult(Decision.IGNORE, reason="概率未命中")

        if not self.is_active(msg.conv_id, now):
            self._enter_session(msg.conv_id, now, msg.user_id)
        return TriggerResult(
            Decision.WAKE,
            context=self.buffer.peek(msg.conv_id),
            reason=f"概率命中({TRIGGER_PROBABILITY:.0%})",
        )

    # ------------------------------------------------------------------
    # 互动窗口（空闲 ↔ 互动中）
    # ------------------------------------------------------------------
    def is_active(self, conv_id: str, now: float | None = None) -> bool:
        """该会话当前是否处于互动中（窗口未过期）"""
        deadline = self._active_until.get(conv_id)
        return deadline is not None and deadline > (now or time.monotonic())

    def leave_session(self, conv_id: str) -> None:
        """
        立刻退出互动窗口（回到空闲的概率唤醒）。

        模式切换（进出跑团）时调用：两种模式互斥，普通 chat 的互动状态不该带过去。
        """
        self._active_until.pop(conv_id, None)
        self._session_users.pop(conv_id, None)

    def mark_answered(self, conv_id: str, user_id: str = '') -> None:
        """
        实际回复成功后重新计时，并记录本轮对话参与者；skip 不调用。

        计时从「回复完成」开始算，所以 AI 慢慢想的这段时间不会白白吃掉窗口。
        """
        self.clear_waiting(conv_id, user_id)
        self._enter_session(conv_id, user_id=user_id)

    def _enter_session(self, conv_id: str, now: float | None = None, user_id: str = '') -> None:
        self._active_until[conv_id] = (now or time.monotonic()) + TRIGGER_ACTIVE_TIMEOUT_SEC
        self._session_users[conv_id] = {user_id} if user_id else set()
        if len(self._active_until) > 1000:
            # 顺手清掉已过期的会话，避免长期运行下无界增长
            current = time.monotonic()
            self._active_until = {
                k: v for k, v in self._active_until.items() if v > current
            }
            while len(self._active_until) > 1000:
                self._active_until.pop(next(iter(self._active_until)))
            self._session_users = {key: users for key, users in self._session_users.items() if key in self._active_until}

    def is_dm_mode(self, conv_id: str) -> bool:
        """该会话当前是不是跑团（DM）模式：跑团不注入 reply/skip，行为与从前完全一致"""
        return self._is_dm_mode(conv_id)

    # ------------------------------------------------------------------
    # 图片预识别：由通道在 decide() 之前调用
    # ------------------------------------------------------------------
    def needs_image_recognition(self, msg: IncomingMessage) -> bool:
        """
        该消息是否需要在触发判定前预识别图片。

        只对「会被纳入上下文的消息」识别：@ 消息走 core 里的正式识别（会带上
        用户提问，质量更好），黑名单群 / DM 模式 / 指令消息一律跳过，省 VLM 费用。
        """
        if not TRIGGER_RECOGNIZE_IMAGES or not msg.image_urls:
            return False
        if not msg.is_group or msg.is_at_bot:
            return False
        if (msg.text or "").strip().startswith("/"):
            return False
        if self._is_dm_mode(msg.conv_id):
            return False
        return self._group_allowed(msg.raw.get("group_id"))

    # ------------------------------------------------------------------
    @staticmethod
    def _is_dm_mode(conv_id: str) -> bool:
        from . import campaign_state  # 延迟导入，避免与业务层循环依赖

        return campaign_state.is_dm_mode(conv_id)

    def _group_allowed(self, raw_group_id) -> bool:
        if raw_group_id is None:
            return True
        gid = str(raw_group_id)
        if ONEBOT_GROUP_BLACKLIST and gid in ONEBOT_GROUP_BLACKLIST:
            return False
        if ONEBOT_GROUP_WHITELIST and gid not in ONEBOT_GROUP_WHITELIST:
            return False
        return True


# 全局单例：所有通道共用（当前仅 OneBot 通道使用）
trigger = TriggerController()
