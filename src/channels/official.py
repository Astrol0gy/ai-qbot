"""
官方通道适配器（QQ 开放平台 / botpy）。

只负责三件事：
1. 把 botpy 的 GroupMessage 事件翻译成通道无关的 IncomingMessage
2. 用 botpy 的被动回复 API 把 core 的回复发出去
3. 官方专属机制：原始事件 monkeypatch + 引用消息 REFIDX 索引缓存

注意：本模块带有 import 期 monkeypatch，必须由 main.py 在启用官方通道时
延迟导入，不要在 channels/__init__.py 里导入。
"""
import asyncio
import json
import os
import re
from pathlib import Path

import botpy
import aiohttp
from botpy import connection as botpy_connection
from botpy.message import GroupMessage

from ..config import (
    DEEPSEEK_ENABLE_SEARCH,
    DEEPSEEK_ENABLE_THINKING,
    OFFICIAL_DEBUG_RAW_EVENTS,
    QQ_BOT_APP_ID,
    QQ_BOT_APP_SECRET,
)
from .base import IncomingMessage, Sender, SendResult, truncate_text
from .scheduler import ConversationQueue
from .core import handle_message

# ============================================
# botpy 原始 payload 透传（monkeypatch）
# botpy 的 parse_group_at_message_create 构造 GroupMessage 后丢弃原始事件 d，
# 官方事件实际携带 msg_elements（含被引用消息原文），这里缓存原始数据供上层使用。
# ============================================
_RAW_MSG_CACHE: dict[str, dict] = {}
#: 原始事件缓存上限。它只是「事件先到、业务后到」之间的中转：正常情况下每条消息
#: 进 on_group_at_message_create 时就被取走。但事件解析完不等于业务一定走到取走那一步
#: （非 @ 的群消息、异常、botpy 改字段），留下没人取的条目就会一直攒着 —— 原始事件里
#: 有发送者 openid 和消息原文，一天下来等于把整天群聊缓存在内存里。满了丢最旧的。
_RAW_MSG_CACHE_MAX = 100
_orig_parse_group = botpy_connection.ConnectionState.parse_group_at_message_create


def _parse_group_with_raw(self, payload):
    """保留群消息原始事件数据（d 对象），按消息 id 缓存；调试开关打开时打印原始结构"""
    d = payload.get("d", {}) or {}
    if d.get("id"):
        _RAW_MSG_CACHE[d["id"]] = d
        while len(_RAW_MSG_CACHE) > _RAW_MSG_CACHE_MAX:
            _RAW_MSG_CACHE.pop(next(iter(_RAW_MSG_CACHE)))
        if OFFICIAL_DEBUG_RAW_EVENTS:
            # 排查字段结构时才开（OFFICIAL_DEBUG_RAW_EVENTS=true）：这段包含发送者 openid
            # 与消息原文，默认打在线上日志里既是刷屏也是隐私泄露
            print(f"[📦 原始事件] 字段={list(d.keys())}")
            try:
                print(f"[📦 原始事件] 内容={json.dumps(d, ensure_ascii=False, default=str)[:1500]}")
            except Exception:
                pass
    return _orig_parse_group(self, payload)


botpy_connection.ConnectionState.parse_group_at_message_create = _parse_group_with_raw

# ============================================
# 消息清理
# ============================================
_AT_PATTERN = re.compile(r"<@!\d+>|@\S+")


def clean_message(raw: str) -> str:
    """去掉 @机器人 部分，返回纯文本消息"""
    return _AT_PATTERN.sub("", raw).strip()


# ============================================
# 引用消息（官方 REFIDX 索引机制）
# msg_idx -> 消息内容 缓存；官方事件本身不携带引用原文，
# 需自行缓存并用 ref_msg_idx 回查。持久化到 data/ref_index.jsonl 抗重启。
# ============================================
_MSG_IDX_CACHE_MAX_PER_USER = 20
#: 全体用户合计条数上限。「每人 20 条」只管住了单个用户，群一多、人一多，总量照样
#: 无上限地长（每个条目还带着消息原文），进程跑几天就是一份越来越大的内存副本。
_MSG_IDX_CACHE_MAX_TOTAL = 2000
#: 攒够多少次淘汰才整体重写索引文件。索引是 JSONL 追加写，淘汰只是让旧行变成垃圾；
#: 每淘汰一条就重写一次，等于「每来一条新消息都把整个文件重写一遍」。
_COMPACT_EVERY_EVICTIONS = 20
_pending_evictions = 0
_REF_INDEX_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "ref_index.jsonl",
)


def _load_ref_index() -> dict[str, dict]:
    """启动时从磁盘加载引用索引缓存（按用户裁剪到上限后重写磁盘）"""
    cache: dict[str, dict] = {}
    try:
        if os.path.exists(_REF_INDEX_FILE):
            with open(_REF_INDEX_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        key, value = item["k"], item["v"]
                        if not isinstance(key, str) or not isinstance(value, dict):
                            continue
                        owner = str(value.get("owner") or "")
                        value["owner"] = owner
                        value["timestamp"] = str(value.get("timestamp") or "")
                        group = value.get("group_id") or (owner.rsplit(":", 1)[0] if ":" in owner else "")
                        if group and not key.startswith(str(group) + ":"):
                            key = str(group) + ":" + key
                        cache[key] = value
                        if len(cache) > max(1, _MSG_IDX_CACHE_MAX_TOTAL) * 2:
                            _prune_cache(cache)
                    except Exception:
                        continue
    except Exception as e:
        print(f"[⚠️ 引用索引加载失败] {e}")
    _prune_cache(cache)
    if cache:
        _compact_ref_index(cache)
    return cache


def _prune_cache(cache: dict[str, dict]):
    """每个用户最多保留最新 _MSG_IDX_CACHE_MAX_PER_USER 条，淘汰该用户时间戳最旧的"""
    per_user: dict[str, list] = {}
    for k, v in cache.items():
        per_user.setdefault(v.get("owner") or "", []).append((k, v))
    to_remove: list[str] = []
    for items in per_user.values():
        items.sort(key=lambda kv: kv[1].get("timestamp") or "")
        for k, _ in items[: max(0, len(items) - _MSG_IDX_CACHE_MAX_PER_USER)]:
            to_remove.append(k)
    for k in to_remove:
        cache.pop(k, None)
    while len(cache) > _MSG_IDX_CACHE_MAX_TOTAL:
        oldest = min(cache, key=lambda k: cache[k].get("timestamp") or "")
        cache.pop(oldest)


def _cache_put(msg_idx: str, info: dict):
    """写入引用索引缓存：新索引才写；该用户超上限先淘汰最旧一条再写入（内存+磁盘）"""
    global _pending_evictions
    if msg_idx in _MSG_IDX_CACHE:
        return
    owner = info.get("owner") or ""
    evicted = False
    if owner:
        same_owner = [k for k, v in _MSG_IDX_CACHE.items() if v.get("owner") == owner]
        while len(same_owner) >= _MSG_IDX_CACHE_MAX_PER_USER:
            oldest = min(same_owner, key=lambda k: _MSG_IDX_CACHE[k].get("timestamp") or "")
            _MSG_IDX_CACHE.pop(oldest, None)
            same_owner.remove(oldest)
            evicted = True
    _MSG_IDX_CACHE[msg_idx] = info
    # 全体上限：只按「每人 N 条」管不住的场景（群多、人多），总量超了继续按时间淘汰
    while len(_MSG_IDX_CACHE) > _MSG_IDX_CACHE_MAX_TOTAL:
        oldest = min(_MSG_IDX_CACHE, key=lambda k: _MSG_IDX_CACHE[k].get("timestamp") or "")
        _MSG_IDX_CACHE.pop(oldest, None)
        evicted = True
    _append_ref_index(msg_idx, info)
    if evicted:
        # 淘汰只让磁盘上多出一批过期的行，攒够一批再重写；期间引用回查走内存缓存，不受影响
        _pending_evictions += 1
        if _pending_evictions >= _COMPACT_EVERY_EVICTIONS:
            _pending_evictions = 0
            _compact_ref_index(_MSG_IDX_CACHE)


def _append_ref_index(msg_idx: str, info: dict):
    """追加写入一条引用索引记录（JSONL）"""
    try:
        os.makedirs(os.path.dirname(_REF_INDEX_FILE), exist_ok=True)
        with open(_REF_INDEX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"k": msg_idx, "v": info}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[⚠️ 引用索引写入失败] {e}")


def _compact_ref_index(cache: dict[str, dict]):
    """缓存超限时压缩重写磁盘文件"""
    try:
        os.makedirs(os.path.dirname(_REF_INDEX_FILE), exist_ok=True)
        tmp = _REF_INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for k, v in cache.items():
                f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")
        os.replace(tmp, _REF_INDEX_FILE)
    except Exception as e:
        print(f"[⚠️ 引用索引压缩失败] {e}")


_MSG_IDX_CACHE: dict[str, dict] = {}


def parse_msg_indexes(raw: dict) -> tuple[str, str]:
    """
    从群消息原始事件中解析消息索引。

    官方机制：每条消息带 msg_idx=<REFIDX...>，引用消息额外带 ref_msg_idx=<REFIDX...>
    （被引用消息的索引）。事件本身不携带引用原文，需用 ref_msg_idx 在本地缓存回查。
    """
    msg_idx, ref_idx = "", ""
    ext = (raw.get("message_scene") or {}).get("ext") or []
    for item in ext:
        if item.startswith("msg_idx="):
            msg_idx = item.split("=", 1)[1]
        elif item.startswith("ref_msg_idx="):
            ref_idx = item.split("=", 1)[1]
    return msg_idx, ref_idx


# ============================================
# 发送器
# ============================================
_MAX_IMAGES = 3

#: 重试也救不回来的错误特征：被动回复的 msg_id 已被用过 / 过期 / 消息被撤回，
#: 或者请求本身就不合法（权限、参数、鉴权失败）。
_PERMANENT_ERROR_HINTS = (
    "msg_id", "已回复", "重复", "过期", "expired", "invalid", "非法", "参数",
    "forbidden", "权限", "鉴权", "authentication", "unauthorized", "not found",
)
_PERMANENT_ERROR_TYPES = frozenset({
    "ForbiddenError", "AuthenticationError", "InvalidRequestError", "NotFoundError",
})


def _is_permanent_error(e: Exception) -> bool:
    """
    这个异常还值不值得退避重试？

    过去一律重试 5 次（2+4+6+8=20 秒，整轮对话卡在这里等），而「msg_id 已回复/已过期」
    这类错误重试 100 次也是同样的结果 —— 只是把真实报错推迟 20 秒才打出来。
    """
    for attr in ("code", "err_code", "status", "status_code"):
        raw = getattr(e, attr, None)
        try:
            code = int(raw)
        except (TypeError, ValueError):
            continue
        if 400 <= code < 500 and code != 429:
            return True  # 4xx 是「你请求得不对」；429 是限流，等一会儿还有机会
    if type(e).__name__ in _PERMANENT_ERROR_TYPES:
        return True
    text = str(e).lower()
    return any(hint in text for hint in _PERMANENT_ERROR_HINTS)


class OfficialHTTPError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


async def _official_request(api, path, group_id, payload):
    """Reuse SDK authentication, with a verified, single-attempt write transport.

botpy 1.x request() swallows timeouts and retries connection resets, so its
write method cannot provide reliable delivery receipts.
"""
    if not hasattr(api, "_http"):
        # Trusted in-process API doubles / alternate adapters.
        return await api.post_group_message(group_openid=group_id, **payload)
    from botpy.http import Route
    http = api._http
    await http._token.check_token()
    session = getattr(api, "_qbot_write_session", None)
    if session is None or session.closed:
        session = api._qbot_write_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), connector=aiohttp.TCPConnector(limit=8))
    route = Route("POST", path, group_openid=group_id)
    route.is_sandbox = http.is_sandbox
    headers = {"Authorization": http._token.get_string(), "X-Union-Appid": http._token.app_id}
    # No retries, no redirects. A timeout after POST is delivery-unknown.
    async with session.post(route.url, headers=headers, json=payload, allow_redirects=False) as response:
        body = await response.content.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise RuntimeError("官方回执超出大小上限")
        if not 200 <= response.status < 300:
            raise OfficialHTTPError(response.status, f"官方接口返回 HTTP {response.status}")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise RuntimeError("官方接口未返回合法 JSON 回执") from exc
        if not isinstance(data, dict):
            raise RuntimeError("官方接口没有返回消息回执")
        if data.get("code") not in (None, 0):
            raise OfficialHTTPError(400, f"官方接口拒绝，code={data['code']}")
        return data


async def _send_once(operation, text="", images=()):
    try:
        result = await asyncio.wait_for(operation(), 20)
        if result is None:
            raise RuntimeError("发送接口未返回回执，不能认定成功")
        return SendResult("ok", text, tuple(images))
    except Exception as exc:
        status = "rejected" if _is_permanent_error(exc) else "unknown"
        print(f"[官方发送 {status}] {type(exc).__name__}: {exc}")
        return SendResult(status, text if status == "unknown" else "", tuple(images), detail=str(exc)[:120])


async def _safe_reply(message, content):
    content = truncate_text(content)
    if hasattr(message, "_api"):
        return await _safe_send_group(message._api, message.group_openid, message.id, content)
    return await _send_once(lambda: message.reply(content=content), content)


async def _safe_send_group(api, group_openid, msg_id, content, msg_seq=1):
    content = truncate_text(content)
    return await _send_once(lambda: _official_request(api, "/v2/groups/{group_openid}/messages",
        group_openid, dict(msg_type=0, content=content, msg_id=msg_id, msg_seq=msg_seq)), content)


class OfficialSender(Sender):
    max_text_len = 2000

    def __init__(self, message, api):
        self._message, self._api = message, api
        self._seq = 0
        self._receipts = []
        self._inflight = None
        self._dropped_stickers = False

    def interrupted_result(self):
        receipts = self._receipts + ([self._inflight] if self._inflight else [])
        visible = [r for r in receipts if r.status in ("ok", "unknown")]
        status = "unknown" if any(r.status == "unknown" for r in visible) else "partial" if visible else "rejected"
        return SendResult(status, "\n".join(r.text for r in visible if r.text),
            tuple(image for r in visible for image in r.images), detail="发送中断，仅记录已发送或回执未知的内容")

    async def send(self, text, image_urls=None, reply_to=None, sticker_paths=None):
        from .. import runtime
        from ..vision_utils import _download_image
        import base64
        urls = [url for url in image_urls or [] if url][:_MAX_IMAGES]
        text = truncate_text(text or "")
        self._receipts = receipts = []
        self._inflight = None
        self._dropped_stickers = bool(sticker_paths)
        for url in urls:
            downloaded = await runtime.run_blocking(_download_image, url, 8 * 1024 * 1024)
            if downloaded is None:
                receipts.append(SendResult("rejected", detail="图片下载失败"))
                continue
            data, _ = downloaded
            try:
                media = await asyncio.wait_for(_official_request(self._api, "/v2/groups/{group_openid}/files", self._message.group_openid, {
                    "file_type": 1, "file_data": base64.b64encode(data).decode("ascii"),
                    "srv_send_msg": False}), 20)
            except Exception as exc:
                receipts.append(SendResult("rejected", detail=f"图片上传失败：{exc}"))
                continue
            self._seq += 1
            self._inflight = SendResult("unknown", images=(url,))
            receipt = await _send_once(lambda: _official_request(self._api, "/v2/groups/{group_openid}/messages", self._message.group_openid,
                dict(msg_type=7, media=media, msg_id=self._message.id, msg_seq=self._seq)), images=(url,))
            receipts.append(receipt)
            self._inflight = None
            if receipt.status == "unknown":
                break
        if text and not any(r.status == "unknown" for r in receipts):
            self._seq += 1
            self._inflight = SendResult("unknown", text)
            receipts.append(await _safe_send_group(self._api, self._message.group_openid,
                self._message.id, text, self._seq))
            self._inflight = None
        if not receipts:
            return SendResult("dropped", detail="此通道不支持本地表情包" if sticker_paths else "无内容")
        visible = [r for r in receipts if r.status in ("ok", "unknown")]
        if any(r.status == "unknown" for r in receipts):
            status = "unknown"
        elif len(visible) == len(receipts) and not sticker_paths:
            status = "ok"
        else:
            status = "partial" if visible else "rejected"
        return SendResult(status, "\n".join(r.text for r in visible if r.text),
            tuple(image for r in visible for image in r.images), detail="；".join(r.detail for r in receipts if r.detail))


# ============================================
# 机器人 Client
# ============================================
class QBot(botpy.Client):
    """QQ 群聊机器人，接入 DeepSeek 大模型"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global _MSG_IDX_CACHE
        _MSG_IDX_CACHE = _load_ref_index()
        self._queue = ConversationQueue()

    async def on_ready(self):
        flags = []
        if DEEPSEEK_ENABLE_THINKING:
            flags.append("🧠 V4 思考")
        if DEEPSEEK_ENABLE_SEARCH:
            flags.append("🌐 联网搜索")
        status = " | ".join(flags) if flags else "标准模式"
        print(f"✅ 机器人「{self.robot.name}」已上线！[DeepSeek V4 Pro | {status}] 等待群聊 @ 消息...")

    async def on_group_at_message_create(self, message: GroupMessage):
        """
        收到群聊 @ 消息时触发，归一化为 IncomingMessage 后交给共享核心。
        """
        user_msg = clean_message(message.content)
        group_id = getattr(message, "group_openid", "")
        user_id = getattr(message.author, "member_openid", "")

        # 取原始事件数据（botpy 丢弃了它，引用索引 REFIDX 就在 message_scene.ext 里）
        raw = _RAW_MSG_CACHE.pop(message.id, {}) or {}
        user_name = ((raw.get("author") or {}).get("username")) or ""

        # 引用消息机制：缓存当前消息 msg_idx，引用时用 ref_msg_idx 回查
        reply_ctx = ""
        msg_idx, ref_idx = parse_msg_indexes(raw)
        if msg_idx:
            _cache_put(f"{group_id}:{msg_idx}", {
                "content": clean_message(message.content or ""),
                "username": user_name,
                "timestamp": getattr(message, "timestamp", ""),
                "owner": f"{group_id}:{user_id}",
                "group_id": group_id,
            })

        if ref_idx:
            quoted = _MSG_IDX_CACHE.get(f"{group_id}:{ref_idx}")
            if quoted:
                quoted_text = quoted.get("content") or ""
                quoted_by = quoted.get("username") or ""
                print(f"[📎 引用原文] {quoted_by}: {quoted_text}")
                if not user_msg:
                    reply_ctx = f"用户引用了「{quoted_by}」的消息：{quoted_text}，请结合上下文回答"
            else:
                print(f"[📎 引用未命中缓存] ref_idx={ref_idx}")
                if not user_msg:
                    reply_ctx = "用户引用了一条消息，但机器人无法获取引用原文，请根据上下文尽力理解"

        # 图片附件 -> 交由核心做 VLM 预处理
        attachments = getattr(message, "attachments", []) or []
        image_urls = [
            getattr(a, "url", "")
            for a in attachments
            if getattr(a, "content_type", "").startswith("image/")
        ]
        image_urls = [u for u in image_urls if u][:_MAX_IMAGES]

        incoming = IncomingMessage(
            channel="official",
            conv_id=group_id,
            user_id=user_id,
            user_name=user_name,
            text=user_msg,
            message_id=str(message.id),
            image_urls=image_urls,
            reply_ctx=reply_ctx,
            is_group=True,
            is_at_bot=True,
            raw=raw,
        )
        sender = OfficialSender(message, self.api)
        if not hasattr(self, "_queue"):
            self._queue = ConversationQueue()
        async def dispatch(item):
            await handle_message(*item)
        try:
            await self._queue.submit(group_id, (incoming, sender), dispatch)
        except asyncio.QueueFull as exc:
            print(f"[入站过载] {exc}")
            await sender.send("当前排队消息较多，请稍后再试。")


# ============================================
# 启动
# ============================================
async def run_official() -> None:
    """启动官方通道；start() 内部的连接循环会长期占用该协程"""
    intents = botpy.Intents(public_messages=True)
    client = QBot(intents=intents)
    async with client:
        try:
            await client.start(appid=QQ_BOT_APP_ID, secret=QQ_BOT_APP_SECRET)
        finally:
            await client._queue.close()
            session = getattr(client.api, "_qbot_write_session", None)
            if session is not None:
                await session.close()
