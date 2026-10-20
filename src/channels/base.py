"""
通道无关的消息契约。

各接入通道（官方 botpy / OneBot）只负责把平台事件翻译成 IncomingMessage，
业务核心 channels/core.py 只依赖该契约，因此对通道完全无感知。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time
import uuid
from typing import Callable

from ..vision_utils import IMAGE_UNAVAILABLE, strip_tag_line

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
    received_at: float = field(default_factory=time.monotonic)
    context_entries: tuple = ()
    has_newer_input: Callable[[], bool] | None = None
    wait_recheck: bool = False
    waiting_token: object = None

    is_group: bool = True          # 群聊 / 私聊
    is_at_bot: bool = False        # 是否 @ 了机器人
    mention_user_ids: tuple[str, ...] = ()  # 结构化 @ 对象，使用通道内规范化用户 id
    is_interjection: bool = False  # 非直呼的群聊唤醒（AI 自行决定开不开口）
    # 普通群聊唤醒：把 reply / skip 工具交给 AI，由它决定这一轮是否开口
    # （私聊、跑团模式不注入，保持原有行为）
    ai_decides_reply: bool = False

    # 平台原始事件，供通道自身调试；群聊消息须在其中放入 "group_id"（平台原生群号），
    # 供触发策略做群黑白名单匹配
    raw: dict = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def is_direct_address(self) -> bool:
        """直呼机器人（@ 或私聊）：指令处理按这条判断，与唤醒方式无关"""
        return (not self.is_group) or self.is_at_bot

    @property
    def addressed_to_others(self) -> bool:
        if not self.is_group or self.is_at_bot:
            return False
        return bool(self.mention_user_ids or (
            self.quote and self.quote.author_id and not self.quote.is_bot
            and self.quote.author_id != self.user_id))

    def buffer_text(self) -> str:
        """
        写进上下文缓冲的文本：纯文本 + 图片描述摘要。

        未 @ 的群消息会先预识别图片，这里把描述拼进来，图片内容才能随缓冲
        进入后续「主动接话」的上下文。纯图片消息也因此有了长度、可以参与接话判定。
        """
        parts = [(self.text or "").strip()]
        for i, desc in enumerate(self.image_descs or []):
            # 去掉「标签：…」行：那是表情包库的归档信息，不是一个词条里该背的负担
            brief = strip_tag_line(desc).replace("\n", " ")
            if len(brief) > IMAGE_NOTE_MAX:
                brief = brief[:IMAGE_NOTE_MAX] + "…"
            # 识别不出来的图留中性占位（别写「识别失败」：主模型会拿它去琢磨，
            # 白烧思考预算，会无谓拖延本轮回复）
            parts.append(f"[图片{i + 1}：{brief or IMAGE_UNAVAILABLE}]")
        if self.image_urls and self.image_descs is None:
            parts.append(f"[{len(self.image_urls)}张图片：{IMAGE_UNAVAILABLE}]")
        return " ".join(p for p in parts if p)


#: 截断提示语。长度由它自己算，不靠调用方记住"30 够不够"
TRUNCATE_SUFFIX = "\n\n……(内容过长，已截断)"


def truncate_text(text: str, max_len: int = 2000) -> str:
    """
    截断过长的回复文本，保证结果**不超过** max_len。

    正文长度按 max_len 减去提示语算出来 —— 过去的写法是 text[: max_len - 30]，
    只要 max_len 小于提示语长度（比如某个通道把上限设得很小），切片下标就成了负数，
    于是变成"从尾巴上砍掉几个字"，返回的内容比原文还长、远超上限。
    """
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= len(TRUNCATE_SUFFIX):
        return text[:max_len]  # 上限小到放不下提示语：先保证不超长
    return text[: max_len - len(TRUNCATE_SUFFIX)] + TRUNCATE_SUFFIX


@dataclass(frozen=True)
class SendResult:
    status: str  # ok / unknown / rejected / offline / dropped / partial
    text: str = ""
    images: tuple[str, ...] = ()
    stickers: tuple[str, ...] = ()
    detail: str = ""

    @property
    def delivered(self):
        return self.status == "ok"

    def history_text(self):
        content = self.text + "".join(f"[img]{x}[/img]" for x in (*self.images, *self.stickers))
        return (f"【投递{self.status}：{self.detail}】" if self.status != "ok" else "") + content


class Sender(ABC):
    """
    绑定到某条入站消息的发送器。

    每个通道为每条消息构造一个 Sender 实例；core 只调用 send()，
    由通道负责拆分成具体的平台消息；OneBot 的表情包始终单独成条。
    """

    max_text_len: int = 2000

    @abstractmethod
    async def send(
        self,
        text: str,
        image_urls: list[str] | None = None,
        reply_to: str | None = None,
        sticker_paths: list[str] | None = None,
    ) -> SendResult:
        """发送一条逻辑回复，返回各条平台消息的汇总回执。

        OneBot 先发送文字/网络图片，再把每张本地表情包单独发送。
        纯表情包明确失败时只尝试一次情绪文字兜底；文字失败后结束，不重复发送。

        ``reply_to`` 是要引用的消息 id（AI 用 reply 工具的 reply_to 指定）。
        通道不支持引用时忽略即可，不影响正文发送。

        ``sticker_paths`` 是本地表情包文件路径（AI 用 ``send_sticker`` 工具挑中，
        工具把它包进回复里的 ``[img]``，core 再用 ``stickers_utils.resolve`` 认出来 ——
        见 src/stickers_utils.py）。只有 OneBot 通道发得出去 —— 官方通道的富媒体必须先把
        本地文件上传换 file_id，那里就降级成纯文字（见 official.py）。``text`` 可以为空，
        就是「一个表情胜过千言万语」的纯表情包回复。
        """
        raise NotImplementedError
