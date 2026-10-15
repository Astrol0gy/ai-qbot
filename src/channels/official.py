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
from botpy import connection as botpy_connection
from botpy.message import GroupMessage

from ..config import (
    DEEPSEEK_ENABLE_SEARCH,
    DEEPSEEK_ENABLE_THINKING,
    QQ_BOT_APP_ID,
    QQ_BOT_APP_SECRET,
)
from .base import IncomingMessage, Sender, truncate_text
from .core import handle_message

# ============================================
# botpy 原始 payload 透传（monkeypatch）
# botpy 的 parse_group_at_message_create 构造 GroupMessage 后丢弃原始事件 d，
# 官方事件实际携带 msg_elements（含被引用消息原文），这里缓存原始数据供上层使用。
# ============================================
_RAW_MSG_CACHE: dict[str, dict] = {}
_orig_parse_group = botpy_connection.ConnectionState.parse_group_at_message_create


def _parse_group_with_raw(self, payload):
    """保留群消息原始事件数据（d 对象），按消息 id 缓存，并打印原始结构供调试"""
    d = payload.get("d", {}) or {}
    if d.get("id"):
        _RAW_MSG_CACHE[d["id"]] = d
        # 调试：打印官方实际推送的原始事件结构
        print(f"[📦 原始事件] 字段={list(d.keys())}")
        try:
            import json as _json
            print(f"[📦 原始事件] 内容={_json.dumps(d, ensure_ascii=False, default=str)[:1500]}")
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
                        cache[item["k"]] = item["v"]
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


def _cache_put(msg_idx: str, info: dict):
    """写入引用索引缓存：新索引才写；该用户超上限先淘汰最旧一条再写入（内存+磁盘）"""
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
    _append_ref_index(msg_idx, info)
    if evicted:
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


_MSG_IDX_CACHE: dict[str, dict] = _load_ref_index()


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


async def _safe_reply(message: GroupMessage, content: str):
    """安全回复，截断过长的消息，断连时自动等待 SDK 重连"""
    content = truncate_text(content)
    for attempt in range(1, 6):
        try:
            await message.reply(content=content)
            if attempt > 1:
                print(f"[✅ 回复成功] 第 {attempt} 次重试")
            return
        except Exception as e:
            wait = attempt * 2  # 2, 4, 6, 8, 10 秒
            if attempt < 5:
                print(f"[⚠️ 回复等待重连 {attempt}/5] 等 {wait}s... {e}")
                await asyncio.sleep(wait)
            else:
                print(f"[❌ 回复最终失败] {e}")


async def _send_group_images(
    api, group_openid: str, msg_id: str, urls: list[str],
    max_images: int = _MAX_IMAGES, start_seq: int = 1,
):
    """上传图片到 QQ 服务器并以被动回复方式发送到群聊"""
    sent = 0
    for i, url in enumerate(urls[:max_images]):
        try:
            # 步骤 1: 上传图片（不发送），获取 file_info
            media_info = await api.post_group_file(
                group_openid=group_openid,
                file_type=1,          # 1 = 图片
                url=url,
                srv_send_msg=False,   # 不主动发送，只获取 file_info
            )
            # 步骤 2: 以被动回复方式发送（msg_type=7 富媒体，递增 msg_seq 避免去重）
            await api.post_group_message(
                group_openid=group_openid,
                msg_type=7,           # 富媒体消息
                media=media_info,
                msg_id=msg_id,        # 被动回复
                msg_seq=start_seq + i,
            )
            sent += 1
            print(f"[🖼️ 图片] 发送成功 ({sent}/{min(len(urls), max_images)}): {url[:80]}...")
        except Exception as e:
            print(f"[🖼️ 图片失败] {url[:80]}... -> {e}")
    if sent > 0:
        print(f"[🖼️ 图片] 共发送 {sent}/{min(len(urls), max_images)} 张")


async def _safe_send_group(api, group_openid: str, msg_id: str, content: str, msg_seq: int = 1):
    """以被动回复方式发送文字消息并指定 msg_seq（避免与图片 seq 冲突）"""
    content = truncate_text(content)
    for attempt in range(1, 6):
        try:
            await api.post_group_message(
                group_openid=group_openid,
                msg_type=0,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
            if attempt > 1:
                print(f"[✅ 文字发送成功] 第 {attempt} 次重试")
            return
        except Exception as e:
            wait = attempt * 2
            if attempt < 5:
                print(f"[⚠️ 文字发送等待重连 {attempt}/5] 等 {wait}s... {e}")
                await asyncio.sleep(wait)
            else:
                print(f"[❌ 文字发送最终失败] {e}")


class OfficialSender(Sender):
    """把回复以被动回复的形式发给官方通道的群聊"""

    max_text_len = 2000

    def __init__(self, message: GroupMessage, api):
        self._message = message
        self._api = api

    async def send(
        self,
        text: str,
        image_urls: list[str] | None = None,
        reply_to: str | None = None,
        sticker_paths: list[str] | None = None,
    ) -> None:
        # 官方通道的回复都是"被动回复当前这条"，没法指定引用哪一条；
        # 这里接口对齐后忽略 reply_to（OneBot 通道会真的发引用段）
        urls = [u for u in (image_urls or []) if u][:_MAX_IMAGES]
        text = truncate_text(text or "")

        # 本地表情包发不了：官方富媒体必须先把文件上传换 file_id（见 README）。
        # 与其为一条情绪丢一轮好话，不如降级 —— 有文字就发文字，没文字就什么都不发。
        stickers = [p for p in (sticker_paths or []) if p]
        if stickers:
            print(
                f"[😀 表情包] 官方通道不支持本地表情包（需先上传换 file_id），"
                f"本轮降级为纯文字，丢弃 {len(stickers)} 张："
                f"{[Path(p).name for p in stickers]}"
            )

        if urls:
            await _send_group_images(
                self._api, self._message.group_openid, self._message.id, urls, start_seq=1
            )
            await _safe_send_group(
                self._api,
                self._message.group_openid,
                self._message.id,
                text or "为你找到以下图片：",
                msg_seq=len(urls) + 1,
            )
        elif text:
            await _safe_reply(self._message, text)


# ============================================
# 机器人 Client
# ============================================
class QBot(botpy.Client):
    """QQ 群聊机器人，接入 DeepSeek 大模型"""

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
            _cache_put(msg_idx, {
                "content": clean_message(message.content or ""),
                "username": user_name,
                "timestamp": getattr(message, "timestamp", ""),
                "owner": f"{group_id}:{user_id}",
            })

        if ref_idx:
            quoted = _MSG_IDX_CACHE.get(ref_idx)
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
        image_urls = [u for u in image_urls if u]

        incoming = IncomingMessage(
            channel="official",
            conv_id=group_id,
            user_id=user_id,
            user_name=user_name,
            text=user_msg,
            image_urls=image_urls,
            reply_ctx=reply_ctx,
            is_group=True,
            is_at_bot=True,
            raw=raw,
        )
        await handle_message(incoming, OfficialSender(message, self.api))


# ============================================
# 启动
# ============================================
async def run_official() -> None:
    """启动官方通道；start() 内部的连接循环会长期占用该协程"""
    intents = botpy.Intents(public_messages=True)
    client = QBot(intents=intents)
    async with client:
        await client.start(appid=QQ_BOT_APP_ID, secret=QQ_BOT_APP_SECRET)
