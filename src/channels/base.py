"""
通道无关的消息契约。

各接入通道（官方 botpy / OneBot）只负责把平台事件翻译成 IncomingMessage，
业务核心 channels/core.py 只依赖该契约，因此对通道完全无感知。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..vision_utils import IMAGE_UNAVAILABLE

# 单张图片的描述写入上下文时的截断长度：上下文要拼进 LLM 提示词，
# 描述太长会把提示词撑大（每群最多 TRIGGER_CONTEXT_MESSAGES 条）
IMAGE_NOTE_MAX = 100


@dataclass
class QuoteInfo:
    """被引用的那条消息。给 AI 的关系信号：谁引了谁、引的是不是机器人自己"""

    message_id: str = ""
    author_id: str = ""
    author_name: str = ""
    text: str = ""
    is_bot: bool = False           # 引用的是不是机器人自己发的

    def describe(self) -> str:
        who = "你" if self.is_bot else (self.author_name or self.author_id or "某人")
        body = (self.text or "").strip().replace("\n", " ")
        if len(body) > 60:
            body = body[:60] + "…"
        return f"引用了{who}的消息：「{body}」" if body else f"引用了{who}的消息"


@dataclass
class IncomingMessage:
    """一条已归一化的入站消息"""

    channel: str = ""              # 通道标识："official" / "onebot"
    conv_id: str = ""              # 会话 key，即业务层的 group_id（群聊=群会话，私聊=该用户独占会话）
    user_id: str = ""              # 业务层的 user_id
    user_name: str = ""            # 展示名（昵称/群名片），官方通道可能为空

    text: str = ""                 # 已清理 @ 前缀的纯文本
    image_urls: list[str] = field(default_factory=list)  # 待 VLM 识别的图片直链
    # 图片描述；None=尚未识别（由 core 识别），非 None=适配器已预识别（未 @ 消息），
    # core 直接复用以免重复调用 VLM
    image_descs: list[str] | None = None
    reply_ctx: str = ""            # 引用消息原文（已拼好，可直接作为提问）
    extra_ctx: str = ""            # 群聊唤醒时携带的上下文片段（自上次回复以来的消息）

    message_id: str = ""           # 本条消息的平台 id：AI 要引用回复时得靠它
    quote: QuoteInfo | None = None # 本条消息引用的消息；None=没有引用
    # 本轮唤醒开始时的时间戳（monotonic）：AI 回完只清理此刻之前就在上下文里的消息，
    # 它思考期间新到的消息要留给下一轮，不能一起丢掉
    wake_started_at: float = 0.0

    is_group: bool = True          # 群聊 / 私聊
    is_at_bot: bool = False        # 是否 @ 了机器人
    is_interjection: bool = False  # 非直呼的群聊唤醒（AI 自行决定开不开口）
    # 普通群聊唤醒：把 reply / skip 工具交给 AI，由它决定这一轮是否开口
    # （私聊、跑团模式不注入，保持原有行为）
    ai_decides_reply: bool = False

    # 平台原始事件，供通道自身调试；群聊消息须在其中放入 "group_id"（平台原生群号），
    # 供触发策略做群黑白名单匹配
    raw: dict = field(default_factory=dict)

    @property
    def is_direct_address(self) -> bool:
        """直呼机器人（@ 或私聊）：指令处理按这条判断，与唤醒方式无关"""
        return (not self.is_group) or self.is_at_bot

    def buffer_text(self) -> str:
        """
        写进上下文缓冲的文本：纯文本 + 图片描述摘要。

        未 @ 的群消息会先预识别图片，这里把描述拼进来，图片内容才能随缓冲
        进入后续「主动接话」的上下文。纯图片消息也因此有了长度、可以参与接话判定。
        """
        parts = [(self.text or "").strip()]
        for i, desc in enumerate(self.image_descs or []):
            brief = (desc or "").strip().replace("\n", " ")
            if len(brief) > IMAGE_NOTE_MAX:
                brief = brief[:IMAGE_NOTE_MAX] + "…"
            # 识别不出来的图留中性占位（别写「识别失败」：主模型会拿它去琢磨，
            # 白烧思考预算，日志里实测把一轮拖过 60 秒超时）
            parts.append(f"[图片{i + 1}：{brief or IMAGE_UNAVAILABLE}]")
        return " ".join(p for p in parts if p)


def truncate_text(text: str, max_len: int = 2000) -> str:
    """截断过长的回复文本"""
    if len(text) > max_len:
        return text[: max_len - 30] + "\n\n……(内容过长，已截断)"
    return text


class Sender(ABC):
    """
    绑定到某条入站消息的发送器。

    每个通道为每条消息构造一个 Sender 实例；core 只调用 send()，
    由通道自行决定「先图后文」的具体实现。
    """

    max_text_len: int = 2000

    @abstractmethod
    async def send(
        self,
        text: str,
        image_urls: list[str] | None = None,
        reply_to: str | None = None,
    ) -> None:
        """发送一条回复（先发图片，再发文字）。

        ``reply_to`` 是要引用的消息 id（AI 用 reply 工具的 reply_to 指定）。
        通道不支持引用时忽略即可，不影响正文发送。
        """
        raise NotImplementedError
