"""
OneBot v11 通道适配器（对接 NapCatQQ 等协议端）。

与官方通道的区别：
- 能收到群内**全部**消息，无需被 @（是否响应由 trigger.py 决定）
- 消息发送是「主动发送」，不依赖被动回复的 msg_id
- 引用消息通过 message 段里的 reply.data.id + get_msg 回查，比官方的 REFIDX 简单

实现要点：
- 复用 botpy 已经依赖的 aiohttp 建立 WebSocket，不引入新依赖
- 断线指数退避重连（1→2→4…→60 秒 + 抖动），连接失败不退出进程
- 消息用段数组（segment array）发送，天然规避 CQ 码转义问题
- API 调用用 echo + Future 关联响应，带超时
- 同会话串行（asyncio.Lock），全局并发上限（Semaphore）
- message_id 做 LRU 去重，防止重连后事件重放导致重复回复
- 健康自检（看门狗）：定期调 get_status，自己发现「QQ 掉线」和「连接假死」
- 连不上协议端（NapCat 没跑 / QQ 掉登录）累计超时也告警，避免"默默重连、没人知道"
"""
import asyncio
import contextlib
import json
import random
import re
import time
import uuid
from dataclasses import replace
from collections import OrderedDict
from pathlib import Path

import aiohttp

from ..config import (
    ONEBOT_ACCESS_TOKEN,
    ONEBOT_API_TIMEOUT,
    ONEBOT_DEDUP_SIZE,
    ONEBOT_DOWN_ALERT_SEC,
    ONEBOT_HEARTBEAT,
    ONEBOT_IMAGE_CONCURRENCY,
    ONEBOT_MAX_CONCURRENCY,
    ONEBOT_MAX_IMAGES,
    ONEBOT_OFFLINE_ALERT_SEC,
    ONEBOT_PROBE_FAILS,
    ONEBOT_PROBE_SEC,
    ONEBOT_SELF_ID,
    ONEBOT_WS_URL,
    CHAT_COLLECT_SEC, CHAT_MAX_COLLECT_SEC, CHAT_INTENT_ENABLE,
)
from ..trigger import Decision, trigger
from ..vision_utils import describe_for_context
from .base import IncomingMessage, QuoteInfo, Sender, SendResult, truncate_text
from .scheduler import ConversationQueue
from .core import handle_message
from ..observability import log_event, message_fields, outcome, stage, traced_message

# ============================================
# CQ 码解析（兼容协议端以字符串形式下发消息的情况）
# ============================================
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\[\]]*)?)\]")


def _cq_unescape(text: str) -> str:
    """反转义 CQ 码中被转义的特殊字符（&amp; 必须最后处理）"""
    return (
        text.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def _parse_cq_params(param_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in param_str.strip(",").split(","):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key.strip()] = _cq_unescape(value)
    return params


def _extract_from_cq(raw: str, self_id: str) -> tuple[str, list[str], bool, str]:
    """从 CQ 码字符串中提取（文本, 图片URL列表, 是否@机器人, 引用的消息id）"""
    text_parts: list[str] = []
    images: list[str] = []
    at_bot = False
    reply_id = ""
    pos = 0

    for match in _CQ_RE.finditer(raw):
        if match.start() > pos:
            text_parts.append(_cq_unescape(raw[pos:match.start()]))
        pos = match.end()

        seg_type = match.group(1)
        data = _parse_cq_params(match.group(2))

        if seg_type == "text":
            text_parts.append(data.get("text", ""))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq and qq == self_id:
                at_bot = True
            elif qq:
                text_parts.append(f"@{qq}")
        elif seg_type == "image":
            url = data.get("url") or ""
            if url:
                images.append(url)
            elif data.get("file", "").startswith("http"):
                images.append(data["file"])
        elif seg_type == "reply":
            reply_id = str(data.get("id", ""))
        elif seg_type == "face":
            text_parts.append(f"[表情{data.get('id', '')}]")

    if pos < len(raw):
        text_parts.append(_cq_unescape(raw[pos:]))

    return "".join(text_parts), images, at_bot, reply_id


def mention_ids(message, self_id: str) -> tuple[str, ...]:
    """Keep actual @ segments separate from ordinary text containing an @ sign."""
    if isinstance(message, str):
        ids = [_parse_cq_params(m.group(2)).get('qq', '') for m in _CQ_RE.finditer(message) if m.group(1) == 'at']
    else:
        ids = [(seg.get('data') or {}).get('qq', '') for seg in (message or [])
               if isinstance(seg, dict) and seg.get('type') == 'at']
    return tuple(dict.fromkeys(f'onebot_{uid}' for value in ids
                              if (uid := str(value)) and uid != self_id))


def extract_message(message, self_id: str) -> tuple[str, list[str], bool, str]:
    """把 OneBot 的 message 字段归一化为（文本, 图片URL列表, 是否@机器人, 引用的消息id）"""
    if isinstance(message, str):
        return _extract_from_cq(message, self_id)

    text_parts: list[str] = []
    images: list[str] = []
    at_bot = False
    reply_id = ""

    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") or {}

        if seg_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq and qq == self_id:
                at_bot = True
            elif qq:
                text_parts.append(f"@{qq}")
        elif seg_type == "image":
            url = data.get("url") or ""
            if not url and str(data.get("file", "")).startswith("http"):
                url = data["file"]
            if url:
                images.append(url)
        elif seg_type == "reply":
            reply_id = str(data.get("id", ""))
        elif seg_type == "face":
            text_parts.append(f"[表情{data.get('id', '')}]")

    return "".join(text_parts), images, at_bot, reply_id


# ============================================
# API 调用结局
# ============================================
# 发送类接口必须区分「确定没送达」和「不知道有没有送达」：NapCat 在内部发送超时时
# 会回 retcode=1200（NTEvent .../sendMsg Timeout），而消息其实已经进群了。
# 把它当失败重试，群里就会出现同一句话发两三遍。
API_OK = "ok"              # 明确成功
API_OFFLINE = "offline"    # 请求根本没发出去：确定没送达，重试安全
API_REJECTED = "rejected"  # 服务端明确拒绝（参数错 / 风控）：重试也没用
API_UNKNOWN = "unknown"    # 超时或回执超时：可能已经送达，绝不自动重试
# 协议端「回执超时」用的 retcode：1200 = 发送超时，1201 = 发送失败但**可能已送达**
API_TIMEOUT_RETCODES = (1200, 1201)


def _sticker_base64(payload):
    """Read only a validated library image; NapCat may run on another filesystem."""
    import base64
    from .. import stickers_utils
    from ..config import STICKER_MAX_BYTES
    path = stickers_utils.resolve(payload)
    if path is None:
        return None
    try:
        with Path(path).open('rb') as image:
            data = image.read(STICKER_MAX_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > STICKER_MAX_BYTES:
        return None
    return path, 'base64://' + base64.b64encode(data).decode('ascii')


# ============================================
# 发送器
# ============================================
class OneBotSender(Sender):
    """把回复发给 OneBot 群聊 / 私聊（主动发送，无被动回复限制）"""

    max_text_len = 4000

    def __init__(self, client: "OneBotClient", is_group: bool, group_id, user_id):
        self._client = client
        self._is_group = is_group
        self._group_id = group_id
        self._user_id = user_id
        self._pending_result = SendResult("rejected", detail="尚未提交至平台")

    def interrupted_result(self):
        return self._pending_result

    async def send(
        self,
        text: str,
        image_urls: list[str] | None = None,
        reply_to: str | None = None,
        sticker_paths: list[str] | None = None,
    ) -> SendResult:
        self._pending_result = SendResult("rejected", detail="发送准备未完成，没有提交至平台")
        text = truncate_text(text or "", self.max_text_len)
        urls = [u for u in (image_urls or []) if u][:ONEBOT_MAX_IMAGES]
        stickers = [p for p in (sticker_paths or []) if p]
        sticker_only = bool(stickers) and not text and not urls
        requested_stickers = stickers
        from .. import runtime, stickers_utils
        from ..vision_utils import _download_image
        import base64
        safe_images, visible_urls = [], []
        media_failed = False
        for url in urls:
            downloaded = await runtime.run_blocking(_download_image, url, 8 * 1024 * 1024)
            if downloaded is None:
                media_failed = True
                continue
            safe_images.append("base64://" + base64.b64encode(downloaded[0]).decode("ascii"))
            visible_urls.append(url)
        prepared_stickers = []
        for path in stickers:
            prepared = await runtime.run_blocking(_sticker_base64, path)
            if prepared is None:
                media_failed = True
                print('[表情包] 文件不可用或超过大小限制，跳过发送')
            else:
                prepared_stickers.append(prepared)
        stickers = prepared_stickers
        urls = visible_urls
        if sticker_only and not stickers:
            return await self._sticker_fallback(requested_stickers[0], reply_to, [])
        if not text and not urls and not stickers:
            return SendResult("dropped", detail="无可发送内容")

        # Text/network images retain their layout. Each sticker is a separate
        # platform message, including when a single model reply contains both.
        parts = []
        if text or urls:
            segments = [{"type": "image", "data": {"file": encoded}} for encoded in safe_images]
            if text:
                segments.append({"type": "text", "data": {"text": text}})
            parts.append((segments, SendResult(API_OK, text, tuple(urls))))
        for path, encoded in stickers:
            parts.append(([{"type": "image", "data": {"file": encoded}}],
                          SendResult(API_OK, stickers=(path,))))
        receipts = []
        for segments, content in parts:
            if reply_to:
                segments.insert(0, {"type": "reply", "data": {"id": str(reply_to)}})
            receipt = await self._send_part(segments, content, receipts,
                                            max_attempts=1 if sticker_only else 3)
            if sticker_only and receipt.status in (API_REJECTED, API_OFFLINE, "dropped"):
                receipt = await self._sticker_fallback(content.stickers[0], reply_to, receipts)
                receipts.append(receipt)
                break
            receipts.append(receipt)
            if receipt.status != API_OK:
                break
            self._pending_result = self._combine(receipts, incomplete=True)
        result = self._combine(receipts, incomplete=media_failed)
        self._pending_result = result
        return result

    async def _sticker_fallback(self, path, reply_to, earlier):
        from .. import stickers_utils
        text = stickers_utils.fallback_text(path)
        segments = [{"type": "text", "data": {"text": text}}]
        if reply_to:
            segments.insert(0, {"type": "reply", "data": {"id": str(reply_to)}})
        print("[表情包降级] 尝试文字兜底")
        receipt = await self._send_part(segments, SendResult(API_OK, text), earlier, max_attempts=1)
        self._pending_result = self._combine([*earlier, receipt])
        return receipt

    @staticmethod
    def _combine(receipts, *, incomplete=False):
        visible = [r for r in receipts if r.status in (API_OK, API_UNKNOWN)]
        if any(r.status == API_UNKNOWN for r in receipts):
            status = API_UNKNOWN
        elif visible and (incomplete or len(visible) != len(receipts)):
            status = "partial"
        elif visible:
            status = API_OK
        else:
            status = receipts[-1].status if receipts else API_REJECTED
        detail = "；".join(r.detail for r in receipts if r.detail)
        if incomplete:
            detail = "；".join(filter(None, (detail, "部分内容尚未送达")))
        return SendResult(status, "\n".join(r.text for r in visible if r.text),
            tuple(url for r in visible for url in r.images),
            tuple(path for r in visible for path in r.stickers), detail)

    async def _send_part(self, segments, content, earlier, *, max_attempts=3):
        from .. import stickers_utils
        if self._is_group:
            action, params = "send_group_msg", {"group_id": self._group_id}
        else:
            action, params = "send_private_msg", {"user_id": self._user_id}
        params["message"] = segments

        for attempt in range(1, max_attempts + 1):
            pending = SendResult(API_UNKNOWN, content.text, content.images, content.stickers,
                                 "已尝试投递，回执未知")
            self._pending_result = self._combine([*earlier, pending])
            result, status = await self._client.call_api_ex(action, params)
            if result is not None:
                if content.images:
                    print(f"[🖼️ 图片] 已发送 {len(content.images)} 张")
                if content.stickers:
                    print(f"[😀 表情包] 已单独发送：{[Path(p).name for p in content.stickers]}")
                for path in content.stickers:
                    stickers_utils.mark_used(path)
                return content
            if status == API_UNKNOWN:
                # 超时 / 回执超时：消息很可能已经进群了，再发一次就是刷屏
                print("[⚠️ 发送结果未知] 消息可能已送达，不再重试以免群里出现重复")
                return SendResult(API_UNKNOWN, content.text, content.images, content.stickers)
            if status != API_OFFLINE:
                # 明确被拒（参数错、风控）：同一份内容再发还是被拒
                print("[❌ 发送失败] 协议端拒绝了这条消息，不再重试")
                return SendResult(API_REJECTED, detail="协议端拒绝")
            if attempt < max_attempts:
                self._pending_result = self._combine([*earlier, SendResult(API_OFFLINE)])
                wait = attempt * 2
                print(f"[⚠️ 发送等待重试 {attempt}/{max_attempts}] 等 {wait}s...")
                await asyncio.sleep(wait)
        print("[❌ 发送最终失败] OneBot 未连接，消息未送达")
        return SendResult(API_OFFLINE)


# ============================================
# 客户端
# ============================================
def _fmt_duration(sec: float) -> str:
    """人话时长：45 秒 / 12.5 分钟 / 2.3 小时。

    统一走这里，避免阈值调小时出现"已 0 分钟"这种读起来没有意义的告警。
    """
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec / 60:.1f} 分钟"
    return f"{sec / 3600:.1f} 小时"


class OneBotClient:
    """OneBot v11 正向 WebSocket 客户端"""

    def __init__(self):
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo_seq = 0
        self._self_id = str(ONEBOT_SELF_ID or "")
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._queue = ConversationQueue(collect_sec=CHAT_COLLECT_SEC, max_collect_sec=CHAT_MAX_COLLECT_SEC)
        self._tasks = set()
        self._wait_tasks = {}
        # 图片预识别用**独立**额度：一张图最坏要走「下载 + 两次请求」（直传失败再自取），
        # 跟整轮对话抢同一把闸门时，图片一慢，别的会话全在排队干等
        # （日志里实测：图片预识别密集时其他会话的回复明显延后）
        self._img_sem = asyncio.Semaphore(max(1, ONEBOT_IMAGE_CONCURRENCY))
        self._skip_sampled = 0
        # 健康自检状态（看门狗）
        self._probe_fails = 0
        self._probe_online = True  # 协议端是否支持 get_status（不支持则退化）
        self._offline_since: float | None = None  # QQ 掉线起点，用于算掉线时长
        self._last_alert = 0.0  # 上次掉线告警时刻；0 表示本次掉线还没报过
        # 连不上协议端的状态：看门狗是连接级的，「一直连不上」它管不到，得单独记
        self._down_since: float | None = None  # 本轮连不上的起点
        self._down_alerted = False  # 本轮是否已经报过警
        self._last_down_alert = 0.0  # 上次"连不上"告警时刻

    # ------------------------------------------------------------------
    # 生命周期：连接 + 重连
    # ------------------------------------------------------------------
    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                await self._connect_and_serve()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[🔌 OneBot 连接异常] {type(e).__name__}: {e}")

            self._note_down()
            wait = min(backoff, 60) + random.uniform(0, 1)
            print(f"[🔌 OneBot 重连] {wait:.1f}s 后重试")
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 60)

    async def _connect_and_serve(self) -> None:
        headers = {}
        if ONEBOT_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {ONEBOT_ACCESS_TOKEN}"

        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.ws_connect(
                    ONEBOT_WS_URL,
                    heartbeat=ONEBOT_HEARTBEAT,
                    receive_timeout=None,
                    max_msg_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._probe_online = True
                    print(f"[🔌 OneBot] 已连接 {ONEBOT_WS_URL}")
                    self._note_up()
                    # 看门狗是连接级的：断开/重连时随这条连接一起结束
                    watchdog = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            if raw.type == aiohttp.WSMsgType.TEXT:
                                self._on_text(raw.data)
                            elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        # 看门狗只是旁观者：它自己的异常不能影响连接，更不能盖掉这里的
                        # 取消（否则 CancelledError 被替换成别的异常，通道就退不出去了）
                        watchdog.cancel()
                        try:
                            with contextlib.suppress(asyncio.CancelledError):
                                await watchdog
                        except Exception as e:
                            print(f"[🩺 OneBot] 看门狗异常退出：{type(e).__name__}: {e}")
                    self._ws = None
                    print("[🔌 OneBot] 连接已断开")

        finally:
            self._ws = None
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("OneBot 连接已断开"))
            self._pending.clear()

    # ------------------------------------------------------------------
    # 健康自检（看门狗）
    #
    # 这个通道最危险的故障是「静默失联」：不抛异常、不报错、控制台一个字没有。
    #   ① QQ 掉线：NapCat 活着、WS 连接正常、心跳照应答，但 QQ 那条长连接断了，
    #      于是永远收不到消息。
    #   ② 连接假死：TCP 层看着还在，其实已经不通，发包不报错、收不到回包。
    # 两者都只能靠「主动去问」发现。之所以不会误判（比如把「群很安静」当成
    # 「掉线」），是因为自检本身就在周期性产生流量 —— 连不上就是真的连不上。
    # ------------------------------------------------------------------
    async def _watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        if ONEBOT_PROBE_SEC <= 0:
            return
        self._probe_fails = 0
        while True:
            await asyncio.sleep(max(1.0, ONEBOT_PROBE_SEC))
            if ws.closed:
                return

            try:
                online, alive = await self._probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 自检自己出错，绝不能让被监视的连接受影响，下一轮再来
                print(f"[🩺 OneBot] 自检异常：{type(e).__name__}: {e}")
                continue
            if not alive:
                self._probe_fails += 1
                print(
                    f"[🩺 OneBot] 健康自检失败 {self._probe_fails}/{max(1, ONEBOT_PROBE_FAILS)}"
                    "：NapCat 没有响应"
                )
                if self._probe_fails >= max(1, ONEBOT_PROBE_FAILS):
                    print("[🩺 OneBot] 判定连接已失效，断开重连")
                    await ws.close()
                    return
                continue

            self._probe_fails = 0
            self._check_qq_online(online)

    async def _probe(self) -> tuple[bool | None, bool]:
        """
        自检一次，返回 (QQ 是否在线, 连接是否可用)。

        QQ 在线状态靠 get_status；万一协议端没实现这个 API 就退化为只检查连接
        存活 —— 绝不能把「查不到」当成「连接坏了」，否则会陷入无意义的重连。
        """
        if self._probe_online:
            data = await self.call_api("get_status", {})
            if data is not None:
                return bool(data.get("online")), True
            # 没拿到结果：可能真断线了，也可能只是不支持这个 API。
            # get_login_info 是 OneBot v11 必备接口，用它区分这两种情况。
            if not await self._alive():
                return None, False
            print("[🩺 OneBot] 本次 get_status 无结果，下次继续探测")
            return None, True
        return None, await self._alive()

    async def _alive(self) -> bool:
        """连接存活判断：能拿到 get_login_info 的响应就算活着"""
        return await self.call_api("get_login_info", {}) is not None

    def _note_down(self) -> None:
        """
        连不上协议端时的告警（累计超过 ONEBOT_DOWN_ALERT_SEC 才出声）。

        看门狗是连接级的——只在连上之后才跑，所以「NapCat 没跑 / QQ 掉登录导致
        端口根本没开」这段它完全管不到：日志里只有默默重连，没有任何人被告知
        已经断了多久。这里把话说明白，并给出能直接照做的命令。
        """
        if ONEBOT_DOWN_ALERT_SEC <= 0:
            return
        now = time.monotonic()
        if self._down_since is None:
            self._down_since = now
            self._down_alerted = False
        down = now - self._down_since
        if down < ONEBOT_DOWN_ALERT_SEC:
            return
        # 首次超阈值就报；之后按掉线告警的间隔重复（避免刷屏）
        if self._down_alerted and now - self._last_down_alert < ONEBOT_OFFLINE_ALERT_SEC:
            return
        first = not self._down_alerted
        self._down_alerted = True
        self._last_down_alert = now
        head = (
            f"[🩺 OneBot] ⚠️ 连不上协议端（已 {_fmt_duration(down)}）"
            if first
            else f"[🩺 OneBot] ⚠️ 仍然连不上（已 {_fmt_duration(down)}）"
        )
        print(f"{head}：{ONEBOT_WS_URL}")
        if first:
            print("[🩺 OneBot]    NapCat 没跑、或 QQ 掉登录，都会让这个端口不开，先看是哪种：")
            print("[🩺 OneBot]      bash start.sh status   # 进程在不在 / 端口开没开")
            print("[🩺 OneBot]      bash start.sh          # NapCat 没跑就把它拉起来")
            print("[🩺 OneBot]      bash start.sh webui    # QQ 掉登录了就用它打开页面扫码")

    def _note_up(self) -> None:
        """连上了：断得够久（或已经报过警）就说一句恢复了，短暂闪断不吭声"""
        if self._down_since is None:
            return
        down = time.monotonic() - self._down_since
        alerted = self._down_alerted
        self._down_since = None
        self._down_alerted = False
        if alerted or down >= ONEBOT_DOWN_ALERT_SEC > 0:
            print(f"[🩺 OneBot] ✓ 已恢复连接（本次断了 {_fmt_duration(down)}）")

    def _check_qq_online(self, online: bool | None) -> None:
        """
        QQ 掉线告警：首次立即报，之后每 ONEBOT_OFFLINE_ALERT_SEC 报一次，避免刷屏。

        这种情况重连自己的 WS 没有任何意义 —— NapCat 是好的，是 QQ 自己断了，
        必须人工重新登录，所以日志要写得足够醒目、足够可操作。
        """
        if online is None:
            return
        now = time.monotonic()

        if online:
            if self._offline_since is not None:
                print(f"[🩺 OneBot] ✓ QQ 已恢复在线（本次掉线持续 {_fmt_duration(now - self._offline_since)}）")
                self._offline_since = None
                self._last_alert = 0.0
            return

        first = self._offline_since is None
        if first:
            self._offline_since = now
        elif now - self._last_alert < ONEBOT_OFFLINE_ALERT_SEC:
            return

        self._last_alert = now
        head = (
            "[🩺 OneBot] ⚠️ QQ 掉线"
            if first
            else f"[🩺 OneBot] ⚠️ QQ 仍然掉线（已 {_fmt_duration(now - self._offline_since)}）"
        )
        print(f"{head}：NapCat 正常、连接正常，但 QQ 已离线，消息收不到也发不出")
        if first:
            print("[🩺 OneBot]    重连机器人没有用，需要重新登录 QQ：")
            print("[🩺 OneBot]      screen -r napcat                     # 看 NapCat 提示/二维码（Ctrl+A D 退出）")
            print("[🩺 OneBot]      bash start.sh stop && bash start.sh # 或整体重启")

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------
    def _on_text(self, data: str) -> None:
        try:
            payload = json.loads(data)
        except Exception:
            return
        for item in payload if isinstance(payload, list) else [payload]:
            if isinstance(item, dict):
                self._handle_payload(item)

    def _handle_payload(self, payload: dict) -> None:
        # API 调用的响应（带 echo）→ 唤醒等待中的 Future
        echo = payload.get("echo")
        if echo is not None:
            key = str(echo)
            future = self._pending.pop(key, None)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        post_type = payload.get("post_type")
        if post_type == "message":
            if len(self._tasks) >= 256:
                print(f"[入站过载] 拒绝消息 id={payload.get('message_id')}，最多 256 个处理任务")
                return
            task = asyncio.create_task(self._safe_process(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif post_type == "meta_event":
            self._learn_self_id(payload.get("self_id"))

    def _learn_self_id(self, self_id) -> None:
        """从事件中学习机器人自身 QQ 号（仅变更时打印一次）"""
        sid = str(self_id or "")
        if sid and sid != self._self_id:
            self._self_id = sid
            print(f"[🔌 OneBot] 机器人 QQ 号：{sid}")

    async def _safe_process(self, event: dict) -> None:
        try:
            await self._process(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[❌ OneBot 处理异常] {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # 单条消息处理
    # ------------------------------------------------------------------
    async def _process(self, event: dict) -> None:
        if self._is_duplicate(event.get("message_id")):
            return

        self._learn_self_id(event.get("self_id"))

        is_group = event.get("message_type") == "group"
        sender_info = event.get("sender") or {}
        raw_uid = str(event.get("user_id") or "")
        user_name = str(sender_info.get("card") or "").strip() or str(
            sender_info.get("nickname") or ""
        ).strip()

        text, images, at_bot, reply_id = extract_message(event.get("message"), self._self_id)
        text = text.strip()

        raw_gid = event.get("group_id")
        if is_group:
            conv_id = f"onebot_group_{raw_gid}"
            user_id = f"onebot_{raw_uid}"
        else:
            conv_id = f"onebot_private_{raw_uid}"
            user_id = f"onebot_{raw_uid}"

        incoming = IncomingMessage(
            channel="onebot",
            conv_id=conv_id,
            user_id=user_id,
            user_name=user_name,
            text=text,
            image_urls=images[:ONEBOT_MAX_IMAGES],
            reply_ctx="",
            message_id=str(event.get("message_id") or ""),
            quote=None,
            is_group=is_group,
            is_at_bot=at_bot,
            mention_user_ids=mention_ids(event.get('message'), self._self_id),
            raw={"message_type": event.get("message_type"), "group_id": raw_gid, "user_id": raw_uid, "message_id": event.get("message_id"), "reply_id": reply_id},
        )

        sender = OneBotSender(self, is_group, raw_gid, raw_uid)
        from ..admin import handle as handle_admin
        if await handle_admin(incoming, sender):
            return
        log_event('message.received', **message_fields(incoming), text=text, at_bot=at_bot,
            image_count=len(incoming.image_urls), quote_id=reply_id, mention_user_ids=incoming.mention_user_ids)
        try:
            batch = self._prepare_batch if self._batchable(incoming) else None
            await self._queue.submit(conv_id, (incoming, sender), self._prepare_and_dispatch, batch_handler=batch)
        except asyncio.QueueFull as exc:
            print(f"[入站过载] {exc}")
            if incoming.is_direct_address:
                await sender.send("当前排队消息较多，请稍后再试。")

    @staticmethod
    def _batchable(incoming):
        return (incoming.is_group and not incoming.text.startswith('/')
                and not trigger.is_dm_mode(incoming.conv_id))

    def _has_newer_input(self, incoming):
        for later, _ in self._queue.pending(incoming.conv_id):
            if (later.user_id == incoming.user_id and self._batchable(later)
                    and not later.wait_recheck and not later.addressed_to_others
                    and (not later.raw.get('reply_id') or later.is_at_bot)):
                return True
        return False

    async def _prepare_batch(self, items):
        candidates = []
        for incoming, sender in items:
            result = await self._prepare(incoming, batching=True)
            if result.decision is Decision.WAKE:
                candidates.append((incoming, sender))
        if not candidates:
            return
        if CHAT_INTENT_ENABLE:
            # Merge each author's burst, not different people's pending requests.
            # Otherwise a later bystander's statement can erase an earlier @.
            authors = {}
            for candidate in candidates:
                user = candidate[0].user_id
                authors.pop(user, None)
                authors[user] = candidate
            for incoming, sender in authors.values():
                incoming.context_entries = tuple(e for e in trigger.buffer.snapshot(incoming.conv_id)
                                                 if e.user_id == incoming.user_id)
                incoming.extra_ctx = trigger.buffer.peek(incoming.conv_id)
                await self._dispatch(incoming, sender)
            return
        # Keep every message in the shared buffer, including other participants.
        # The last eligible wake provides focus, not authorship for the entire batch.
        incoming, sender = candidates[-1]
        incoming.context_entries = trigger.buffer.snapshot(incoming.conv_id)
        incoming.extra_ctx = trigger.buffer.peek(incoming.conv_id)
        await self._dispatch(incoming, sender)

    @traced_message
    async def _prepare_and_dispatch(self, item):
        incoming, sender = item
        if incoming.wait_recheck:
            state = trigger.waiting_for(incoming.conv_id, incoming.user_id)
            if state is not incoming.waiting_token:
                return
            incoming.context_entries = trigger.buffer.snapshot(incoming.conv_id)
            if not incoming.context_entries:
                trigger.clear_waiting(incoming.conv_id, incoming.user_id)
                return
            try:
                await self._dispatch(incoming, sender)
            finally:
                if trigger.waiting_for(incoming.conv_id, incoming.user_id) is state:
                    trigger.clear_waiting(incoming.conv_id, incoming.user_id)
            return
        result = await self._prepare(incoming)
        if result.decision is Decision.WAKE:
            await self._dispatch(incoming, sender)

    async def _prepare(self, incoming, batching=False):
        reply_id = incoming.raw.get("reply_id")
        if reply_id and not incoming.text.startswith("/"):
            quote, images = await self._quote_info(reply_id)
            incoming.quote = quote
            incoming.image_urls = (incoming.image_urls + images)[:ONEBOT_MAX_IMAGES]
            if quote and not incoming.text:
                incoming.reply_ctx = quote.text
        if incoming.image_descs is None and (trigger.needs_image_recognition(incoming) or batching and incoming.is_at_bot
                and incoming.image_urls and trigger._group_allowed(incoming.raw.get('group_id'))):
            with stage('media.preprocess', image_count=len(incoming.image_urls)):
                async with self._img_sem:
                    try:
                        incoming.image_descs = await asyncio.wait_for(describe_for_context(incoming.conv_id, incoming.image_urls), 30)
                    except asyncio.TimeoutError:
                        from ..vision_utils import IMAGE_UNAVAILABLE
                        log_event('media.timeout', timeout_sec=30)
                        incoming.image_descs = [IMAGE_UNAVAILABLE] * len(incoming.image_urls)
        elif incoming.image_descs is None and incoming.is_group and not incoming.is_at_bot and incoming.image_urls:
            from ..vision_utils import IMAGE_UNAVAILABLE
            incoming.image_descs = [IMAGE_UNAVAILABLE] * len(incoming.image_urls)
        result = trigger.decide(incoming)
        log_event('trigger.decision', **message_fields(incoming), decision=result.decision.value, reason=result.reason,
            context_messages=len(trigger.buffer.snapshot(incoming.conv_id)))
        if result.decision is Decision.IGNORE:
            outcome('ignored')
            return result
        if incoming.is_group and not trigger.is_dm_mode(incoming.conv_id) and not incoming.text.startswith("/"):
            incoming.ai_decides_reply = True
            incoming.is_interjection = not incoming.is_at_bot
            incoming.extra_ctx = result.context
            incoming.context_entries = trigger.buffer.snapshot(incoming.conv_id)
        incoming.wake_started_at = time.monotonic()
        return result

    async def _dispatch(self, incoming: IncomingMessage, sender: Sender) -> None:
        incoming.has_newer_input = lambda: self._has_newer_input(incoming)
        await handle_message(incoming, sender)
        state = trigger.waiting_for(incoming.conv_id, incoming.user_id)
        key = (incoming.conv_id, incoming.user_id)
        if state is None:
            previous = self._wait_tasks.get(key)
            if previous and previous is not asyncio.current_task() and not previous.done():
                previous.cancel()
        if state is not None and not incoming.wait_recheck:
            previous = self._wait_tasks.get(key)
            if previous and not previous.done():
                previous.cancel()
            task = asyncio.create_task(self._recheck_wait(incoming, sender, state))
            self._wait_tasks[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(lambda done: self._wait_tasks.pop(key, None)
                                   if self._wait_tasks.get(key) is done else None)

    async def _recheck_wait(self, incoming, sender, state):
        await asyncio.sleep(max(0, state[0] - time.monotonic()))
        if trigger.waiting_for(incoming.conv_id, incoming.user_id) is not state:
            return
        retry = replace(incoming, wait_recheck=True, waiting_token=state,
                        is_at_bot=False, is_interjection=True, context_entries=(),
                        trace_id=uuid.uuid4().hex)
        log_event('chat.wait.recheck', conversation_id=incoming.conv_id, user_id=incoming.user_id)
        try:
            await self._queue.submit(incoming.conv_id, (retry, sender), self._prepare_and_dispatch)
        except asyncio.QueueFull:
            trigger.clear_waiting(incoming.conv_id, incoming.user_id)

    async def close(self):
        await self._queue.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _is_duplicate(self, message_id) -> bool:
        if message_id is None:
            return False
        try:
            key = int(message_id)
        except (TypeError, ValueError):
            return False
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > max(1, ONEBOT_DEDUP_SIZE):
            self._seen.popitem(last=False)
        return False

    async def _quote_info(self, reply_id: str) -> tuple[QuoteInfo | None, list[str]]:
        """
        回查被引用的消息，返回（关系信号, 被引用消息里的图片）。

        以前只在"用户只引用、不说话"时才回查（省一次 API 调用）；现在无论如何都查：
        AI 需要知道「谁引用了谁、引的是不是机器人自己」，才能判断有人在接它的话。
        """
        try:
            message_id = int(reply_id)
        except (TypeError, ValueError):
            log_event('quote.lookup', quote_id=reply_id, status='invalid_id')
            return None, []

        with stage('quote.fetch', quote_id=reply_id):
            data = await self.call_api("get_msg", {"message_id": message_id})
        if not data:
            log_event('quote.lookup', quote_id=reply_id, status='unavailable')
            return None, []

        quoted_text, quoted_images, _, _ = extract_message(data.get("message"), self._self_id)
        sender = data.get("sender") or {}
        uid = str(sender.get("user_id") or "")
        quote = QuoteInfo(
            message_id=str(reply_id),
            author_id=f'onebot_{uid}' if uid else '',
            author_name=str(sender.get("card") or "").strip()
            or str(sender.get("nickname") or "").strip(),
            text=(quoted_text or "").strip(),
            is_bot=bool(self._self_id) and uid == str(self._self_id),
        )
        log_event('quote.lookup', quote_id=reply_id, status='ok' if quote.text or quoted_images else 'empty',
            author_id=uid, text=quote.text, text_chars=len(quote.text), image_count=len(quoted_images),
            prompt_quote_chars=min(len(quote.text), 60), prompt_truncated=len(quote.text)>60,
            segment_types=[s.get('type') for s in data.get('message', []) if isinstance(s, dict)]
                if isinstance(data.get('message'), list) else ['cq_string'])
        return quote, quoted_images

    def _log_ignore(self, conv_id: str, reason: str) -> None:
        """跳过类日志做采样，避免群消息刷屏"""
        if reason in ("概率未命中", "非@指令", "过短"):
            # 高频且不值得逐条关心的跳过，每 20 条打印一次
            self._skip_sampled += 1
            if self._skip_sampled % 20:
                return
            print(f"[⏭️ 跳过] 会话={conv_id} 累计 {self._skip_sampled} 条未唤醒（最新原因={reason}，每 20 条采样一次）")
            return
        print(f"[⏭️ 忽略] 会话={conv_id} 原因={reason}")

    async def call_api(self, action: str, params: dict, timeout: float | None = None):
        """调用 OneBot API，用 echo 关联响应；失败/超时返回 None（读类接口用这个就够）"""
        data, _ = await self.call_api_ex(action, params, timeout)
        return data

    async def call_api_ex(
        self, action: str, params: dict, timeout: float | None = None
    ) -> tuple[dict | None, str]:
        """
        调用 OneBot API，返回 (data, 结局)。

        结局必须区分「确定没送达」和「不知道有没有送达」，否则发送会重复：超时和
        `retcode=1200` 通常说明**没等到回执**，但明确的文件预处理错误说明未发送。
        读类接口（get_msg / get_status / get_login_info）不关心这个，用 call_api 即可。
        """
        ws = self._ws
        if ws is None or ws.closed:
            print(f"[⚠️ OneBot 未连接] 跳过 API 调用：{action}")
            return None, API_OFFLINE

        self._echo_seq += 1
        echo = f"ai-qbot-{self._echo_seq}"
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        try:
            await ws.send_str(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
        except asyncio.CancelledError:
            self._pending.pop(echo, None)
            future.cancel()
            raise
        except Exception as e:
            # A socket error can occur after the frame was written; do not retry.
            self._pending.pop(echo, None)
            print(f"[⚠️ OneBot API 发送失败] {action}: {type(e).__name__}: {e}")
            return None, API_UNKNOWN

        try:
            resp = await asyncio.wait_for(future, timeout=timeout or ONEBOT_API_TIMEOUT)
        except asyncio.CancelledError:
            self._pending.pop(echo, None)
            future.cancel()
            raise
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            print(f"[⚠️ OneBot API 超时] {action}（结果未知，可能已经生效）")
            return None, API_UNKNOWN
        except Exception as e:
            self._pending.pop(echo, None)
            print(f"[⚠️ OneBot API 失败] {action}: {type(e).__name__}: {e}")
            return None, API_UNKNOWN

        retcode = resp.get("retcode")
        if resp.get("status") == "failed" or (retcode is not None and retcode not in (0, 1)):
            message = resp.get("message") or ""
            log_event('onebot.api_error', action=action, echo=echo, retcode=retcode, detail=message[:500])
            # NapCat also uses 1200 for media preparation failures, before sendMsg.
            # Match the explicit stage, never infer rejection from a generic error.
            if message.startswith(('文件处理失败', '识别URL失败')):
                return None, API_REJECTED
            # 回执超时 ≠ 投递失败：协议端内部发送超时时，消息通常已经发出去了
            if retcode in API_TIMEOUT_RETCODES or "timeout" in message.lower():
                return None, API_UNKNOWN
            return None, API_REJECTED
        return resp.get("data") or {}, API_OK


async def run_onebot() -> None:
    """启动 OneBot 通道；内部是无限重连循环，不会自行返回"""
    client = OneBotClient()
    try:
        await client.run()
    finally:
        await client.close()
