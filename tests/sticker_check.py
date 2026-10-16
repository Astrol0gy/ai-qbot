#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
表情包链路自检：除了「看图模型的一次 HTTP 请求」和「把消息发到 QQ」这两步打桩，
其余全走项目里的真实代码（真下载、真写文件、真调工具、真拼 OneBot 段数组）。

    VLM 描述（打桩）→ vision_utils.describe_image → stickers_utils.harvest（后台线程）
    → 真 HTTP 下载（本地起的小服务）→ stickers_utils.save（真写 data/stickers）
    → send_sticker 工具（tools/image_provider.py）→ 回复里的 [img]库内路径
    → core.handle_message → _split_image_payloads → OneBotSender.send

覆盖 10 项：
  1. 标签解析：五维中文标签行 → 五个 tag（缺维补「无」，<3 个不认）
  2. 入库命名：five_tag_序号.ext，同一张图去重，同 tag 不同图序号递增
  3. 采集链路：describe_image 认出表情包 → 后台线程真下载真落盘
  4. 检索：情绪词命中；不相干的词宁可不返回
  5. 工具：list_stickers 报库现状；send_sticker 给库内 [img] 路径，挑不到就不给
  6. 回复解析：[img] 里的库内路径认成表情包，库外路径一律丢掉
  7. OneBot 发送：段数组里 image.data.file 就是本地绝对路径
  8. 端到端：core.handle_message 收回来的回复里带库内路径 → 交给通道
  9. 官方通道：降级为纯文字，不崩、不发表情包
  10. 上下文净化：标签行不进主模型的上下文（buffer_text / 图片描述）

用临时目录当表情包库，不碰真实 data/stickers，不连真 QQ，不调任何外部 API。

用法：
    python tests/sticker_check.py           # 全离线，不连网、不花钱
    python tests/sticker_check.py --live    # 额外用文生图造一张表情包，真调一次视觉模型
                                           # 验证「模型确实会输出五个标签」（会计费）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，打 emoji 会抛 UnicodeEncodeError（沿用 main.py 的做法）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# 表情包库指向临时目录 —— 必须在 import 项目模块之前设置（config 在导入期读环境变量）
_TMP_LIB = Path(tempfile.mkdtemp(prefix="sticker_check_"))
os.environ["STICKER_DIR"] = str(_TMP_LIB)
os.environ["STICKER_ENABLE"] = "true"

# 1x1 的 PNG（真实字节，够 _sniff_mime 认出 image/png）
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000200000002080600000072b60d24"
    "0000001749444154789c6360f8cf801b18a9c1e40c0c000ea0010d0a3d1e1e0000"
    "000049454e44ae426082"
)
PNG_C = PNG_B[:-1] + b"\x00"  # 第三张：字节不同（避开内容去重），头还是 PNG 魔数
PNG_D = PNG_B[:-1] + b"\x01"  # 第四张：留给"采集链路真的下载了新图"用

DESC_STICKER = (
    "类型：表情包 ｜ 意图：表达无语和「你在说什么」\n"
    "一只熊猫捂着脸，图上有「我裂开了」几个字。\n"
    "标签：无语｜捂脸｜熊猫｜拼贴配字｜我裂开了"
)
DESC_PHOTO = "类型：内容图片 ｜ 意图：让我看报错信息\n截图里是 Python 的 KeyError 栈，提示字典缺 key。"

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


# ============================================
# 本地图床：给采集链路一个真实的下载源
# ============================================
class _Handler(BaseHTTPRequestHandler):
    body = PNG_A

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")  # 故意不给对：靠魔数嗅探
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # 静音
        pass


def start_image_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/a.png"


async def main() -> int:
    from src import stickers_utils
    from src import tools
    from src import vision_utils as vu
    from src.config import STICKER_MAX_PER_REPLY
    from src.channels import core
    from unittest.mock import patch
    from src.channels import official
    from src.channels.base import IncomingMessage
    from src.channels.onebot import API_OK, OneBotSender

    print(f"临时表情包库：{_TMP_LIB}\n")

    # ---------- 1. 标签解析 ----------
    print("1. 标签解析")
    tags = stickers_utils.parse_tags(DESC_STICKER)
    check("五个标签按顺序取出", tags == ["无语", "捂脸", "熊猫", "拼贴配字", "我裂开了"], str(tags))
    check("内容图片没有标签行 → 不认", stickers_utils.parse_tags(DESC_PHOTO) is None)
    check(
        "不足三维不认（模型写歪时不入库）",
        stickers_utils.parse_tags("类型：表情包 ｜ 意图：无\n标签：无语") is None,
    )
    check(
        "标签内混进下划线：按分隔符切开，仍然只留五个",
        stickers_utils._clean_tag("无_语") == "无语"
        and stickers_utils.parse_tags("类型：表情包 ｜ 意图：无语\n标签：无语_了｜捂脸｜猫｜实拍｜无文字")
        == ["无语", "了", "捂脸", "猫", "实拍"],
    )
    check("缺的维度补「无」", stickers_utils.parse_tags("类型：表情包 ｜ 意图：无语\n标签：无语｜捂脸｜猫｜实拍") == ["无语", "捂脸", "猫", "实拍", "无"])

    # ---------- 2. 入库命名与去重 ----------
    print("\n2. 入库命名与去重")
    name1 = stickers_utils.save(PNG_A, "image/png", tags)
    check("命名 = 五个标签_序号.png", name1 == "无语_捂脸_熊猫_拼贴配字_我裂开了_1.png", str(name1))
    check("文件真的落盘", (Path(stickers_utils.stats()["dir"]) / str(name1)).exists())
    check("同一张图再存一次 → 去重，不占新坑", stickers_utils.save(PNG_A, "image/png", tags) == name1)
    name2 = stickers_utils.save(PNG_B, "image/png", tags)
    check("同五个标签、不同图 → 序号递增", name2 == "无语_捂脸_熊猫_拼贴配字_我裂开了_2.png", str(name2))
    name3 = stickers_utils.save(PNG_B, "image/png", ["赞同", "点赞", "猫", "实拍", "无文字"])
    check("同一张图换个标签再存 → 还是那张（内容说了算）", name3 == name2, f"{name3} vs {name2}")
    name4 = stickers_utils.save(PNG_C, "image/png", ["赞同", "点赞", "猫", "实拍", "无文字"])
    check("换一组标签 → 从 _1 重新编号", name4 == "赞同_点赞_猫_实拍_无文字_1.png", str(name4))

    # ---------- 3. 采集链路（真下载真落盘） ----------
    print("\n3. 采集链路（真下载）")
    server, url = start_image_server()

    # 测试用的图床跑在本机（127.0.0.1），而 src/net_safety.py 的 SSRF 防护按设计拒掉内网地址。
    # 这里只为本机地址开一个口子，其余校验照旧（协议、域名解析、重定向逐跳），
    # 生产代码不做任何放宽 —— 线上那个地址来自模型，正是要被拦住的东西。
    import src.net_safety as net_safety

    _real_public = net_safety._is_public_ip
    net_safety._is_public_ip = lambda ip: ip.is_loopback or _real_public(ip)
    _Handler.body = PNG_D  # 换张没见过的图，才能证明是"新下载入库"而不是被去重挡回

    real_describe = vu._describe_media
    vu._describe_media = lambda block, prompt, max_tokens=500: DESC_STICKER  # 只打桩这一次 HTTP 调用
    try:
        desc = await asyncio.to_thread(vu.describe_image, url)
    finally:
        vu._describe_media = real_describe
    check("描述原样返回（含标签行）", bool(desc) and "标签：" in str(desc), str(desc)[:40])
    check("采集队列跑空", await asyncio.to_thread(stickers_utils.wait_idle, 15))
    harvested = sorted(p.name for p in _TMP_LIB.glob("无语_捂脸_熊猫_拼贴配字_我裂开了_*.png"))
    check("图被真下载并入库", harvested == ["无语_捂脸_熊猫_拼贴配字_我裂开了_1.png",
                                            "无语_捂脸_熊猫_拼贴配字_我裂开了_2.png",
                                            "无语_捂脸_熊猫_拼贴配字_我裂开了_3.png"], str(harvested))
    check("库统计对得上", stickers_utils.stats()["total"] == 4, str(stickers_utils.stats()))

    # 内容图片不该入库
    before_photo = stickers_utils.stats()["total"]
    _Handler.body = PNG_A
    vu._describe_media = lambda block, prompt, max_tokens=500: DESC_PHOTO
    try:
        await asyncio.to_thread(vu.describe_image, url.replace("/a.png", "/b.png"))
    finally:
        vu._describe_media = real_describe
    await asyncio.to_thread(stickers_utils.wait_idle, 15)
    check("内容图片不入库", stickers_utils.stats()["total"] == before_photo, str(before_photo))
    server.shutdown()
    net_safety._is_public_ip = _real_public

    # ---------- 4. 检索 ----------
    print("\n4. 检索")
    hit = stickers_utils.pick("无语")
    check("情绪词命中", hit is not None and hit.tags[0] == "无语", getattr(hit, "name", "None"))
    hit2 = stickers_utils.pick("我想要一张表达震惊的图")
    check("没相关的就不返回（宁可少发，也不糊一张）", hit2 is None, getattr(hit2, "name", "None"))
    check("空关键词不返回", stickers_utils.pick("") is None)
    check("库现状能报给模型", "库里现有" in stickers_utils.tag_summary(), stickers_utils.tag_summary())

    # ---------- 5. 工具 ----------
    print("\n5. 工具（list_stickers / send_sticker）")
    listed = tools.execute("list_stickers", "{}")
    check("list_stickers 报出库现状", "库里现有" in listed and "无语" in listed, listed)

    out = tools.execute("send_sticker", json.dumps({"query": "无语"}))
    found = core.IMG_MARKER.search(out)
    check("send_sticker 返回库内 [img] 路径",
          found is not None and Path(found.group(1)).is_file(),
          out.splitlines()[0] if out else "")

    miss = tools.execute("send_sticker", json.dumps({"query": "十分罕见的心情"}))
    check("挑不到就不给路径（宁可这一轮不发）", "[img]" not in miss, miss)

    # ---------- 6. 回复解析 ----------
    print("\n6. 回复解析（[img] 里的本地路径 → 表情包）")
    payload = found.group(1) if found else ""
    check("纯表情包的文字兜底取自情绪标签", stickers_utils.fallback_text(payload) == "（无语）")
    check("兜底不泄露库外文件路径", stickers_utils.fallback_text(str(ROOT / "README.md")) == "（表情）")
    urls, paths = core._split_image_payloads([payload])
    check("库内路径认成表情包", urls == [] and paths == [payload], str(paths))
    urls2, paths2 = core._split_image_payloads(["https://example.com/a.jpg", str(ROOT / "README.md")])
    check("网址走网络图、库外路径一律丢掉",
          urls2 == ["https://example.com/a.jpg"] and paths2 == [], f"{urls2} {paths2}")
    _, paths3 = core._split_image_payloads([payload] * 3)
    check(f"一轮最多发 {STICKER_MAX_PER_REPLY} 张（多余的丢掉）",
          len(paths3) == STICKER_MAX_PER_REPLY, str(paths3))
    check("纯表情包的回复：正文为空",
          core.IMG_MARKER.sub("", f"[img]{payload}[/img]").strip() == "")

    print("\n7. OneBot 发送")
    sent: list[tuple] = []

    class _FakeClient:
        async def call_api_ex(self, action, params):
            sent.append((action, params))
            return {}, API_OK

    sender = OneBotSender(_FakeClient(), True, "8888", "9999")  # type: ignore[arg-type]
    await sender.send("行吧", sticker_paths=paths)
    action, params = sent[-1]
    seg = params["message"][0]
    check("action 是 send_group_msg", action == "send_group_msg", action)
    check("段数组里有 image 段且 file 是本地绝对路径",
          len(sent) == 2 and len(params["message"]) == 1
          and seg["type"] == "image" and seg["data"]["file"] == paths[0],
          str(params["message"]))
    check("文字独立为前一条消息", sent[0][1]["message"] == [{"type": "text", "data": {"text": "行吧"}}])
    await sender.send("", sticker_paths=paths)
    check("纯表情包（text 为空）也会发出去", len(sent[-1][1]["message"]) == 1)
    await sender.send("", sticker_paths=None)
    check("空内容不产生任何请求", len(sent) == 3)

    # ---------- 端到端：core.handle_message ----------
    print("\n8. 端到端（工具给的 [img] → 通道）")
    routed: list[dict] = []

    class _Sender:
        max_text_len = 2000

        async def send(self, text, image_urls=None, reply_to=None, sticker_paths=None):
            routed.append({"text": text, "images": list(image_urls or []),
                           "stickers": [str(p) for p in (sticker_paths or [])]})

    async def _fake_chat(group_id, user_id, user_message, user_name="", outbox=None, history_text=None):
        # 模型照着 send_sticker 的指示把那一行原样放进回复
        return f"这事我真没法评价 [img]{payload}[/img]"

    real_chat = core.chat
    core.chat = _fake_chat
    try:
        msg = IncomingMessage(
            channel="onebot", conv_id="8888", user_id="9999", user_name="张三",
            text="你觉得这事怎么样", is_group=True, is_at_bot=True, message_id="1",
        )
        await core.handle_message(msg, _Sender())  # type: ignore[arg-type]
    finally:
        core.chat = real_chat
    check("发出去一条", len(routed) == 1, str(routed))
    check("文本里的 [img] 已被摘掉", routed and routed[0]["text"] == "这事我真没法评价", str(routed))
    check("表情包路径一起交给了通道", routed and len(routed[0]["stickers"]) == 1
          and Path(routed[0]["stickers"][0]).is_file(), str(routed))

    # reply 工具那条路（有 outbox 的普通群聊唤醒）也走同一个出口
    from src.tools.chat_provider import ReplyItem
    from src.trigger import BufferEntry
    async def _fake_reply_chat(*args, outbox=None, **kwargs):
        outbox.items.append(ReplyItem(f"行吧 [img]{payload}[/img]", "42"))
        return ""
    core.chat = _fake_reply_chat
    try:
        await core.handle_message(IncomingMessage(
            channel="onebot", conv_id="8888", user_id="9999", user_name="张三",
            text="再说一句", is_at_bot=True, ai_decides_reply=True,
            context_entries=(BufferEntry(text="再说一句", message_id="42", user_id="9999"),),
        ), _Sender())
    finally:
        core.chat = real_chat
    check("reply 工具路径同样能带表情包",
          routed[-1]["stickers"] and routed[-1]["text"] == "行吧", str(routed[-1]))

    # ---------- 官方通道降级 ----------
    print("\n9. 官方通道降级")
    official_sent: list[str] = []

    class _Msg:
        group_openid = "g"
        id = "m"

    async def _fake_safe_reply(message, content):
        official_sent.append(content)
        from src.channels.base import SendResult
        return SendResult("ok", content)

    async def _fake_safe_group(api, group_openid, msg_id, content, msg_seq=1):
        official_sent.append(content)
        from src.channels.base import SendResult
        return SendResult("ok", content)

    real_reply, real_group = official._safe_reply, official._safe_send_group
    official._safe_reply, official._safe_send_group = _fake_safe_reply, _fake_safe_group
    try:
        off = official.OfficialSender(_Msg(), None)  # type: ignore[arg-type]
        await off.send("行吧", sticker_paths=paths)
        await off.send("", sticker_paths=paths)
    finally:
        official._safe_reply, official._safe_send_group = real_reply, real_group
    check("不支持的纯表情包自动降级为情绪文字", official_sent == ["行吧", "（无语）"], str(official_sent))

    # ---------- 上下文净化 ----------
    print("\n10. 上下文净化")
    check("图片描述里的标签行被剥掉",
          vu.strip_tag_line(DESC_STICKER)
          == "类型：表情包 ｜ 意图：表达无语和「你在说什么」\n"
             "一只熊猫捂着脸，图上有「我裂开了」几个字。",
          repr(vu.strip_tag_line(DESC_STICKER)))
    brief_msg = IncomingMessage(channel="onebot", image_descs=[DESC_STICKER])
    check("缓冲里的图注不带标签行", "标签：" not in brief_msg.buffer_text(), brief_msg.buffer_text())

    # ---------- 10. 真图真模型（可选，会计费） ----------
    if "--live" in sys.argv:
        print("\n11. 真图真模型（--live：会调用文生图与视觉模型，会计费）")
        urls = await asyncio.to_thread(
            vu.text_to_image, "一只熊猫捂着脸、配字「我裂开了」的搞笑表情包", "1024*1024", 1
        )
        if not urls:
            print("  [SKIP] 文生图没返回图片（没配 DASHSCOPE 或调用失败）")
        else:
            before = stickers_utils.stats()["total"]
            live_desc = await asyncio.to_thread(vu.describe_image, urls[0])
            print(f"  —— VLM 原样返回 ——\n{live_desc}\n  --------------------")
            live_tags = stickers_utils.parse_tags(live_desc or "")
            check("真模型确实按格式给出了五个标签", live_tags is not None, str(live_tags))
            check("真图进了库（采集链路跑通）",
                  await asyncio.to_thread(stickers_utils.wait_idle, 20)
                  and stickers_utils.stats()["total"] > before)
            if live_tags:
                live_hit = stickers_utils.pick(live_tags[0])
                check("新图能按情绪词挑回来", live_hit is not None, getattr(live_hit, "name", "None"))

            # 库里有货之后，真跑一轮对话（真 DeepSeek，只打桩发送）：
            # 看模型会不会自己调 send_sticker 并把 [img] 放进回复 ——
            # 用不用是它的判断，所以只报告、不算失败（工具选中了谁，日志里会有「[表情包] 挑中 …」）
            print("\n  —— 真模型真对话（库里有货，看它用不用工具）——")
            live_routed: list[dict] = []

            class _LiveSender:
                max_text_len = 2000

                async def send(self, text, image_urls=None, reply_to=None, sticker_paths=None):
                    live_routed.append({"text": text, "stickers": [str(p) for p in (sticker_paths or [])]})

            live_msg = IncomingMessage(
                channel="onebot", conv_id="9999", user_id="1", user_name="张三",
                text="我刚发的那张捂脸的图，你觉得怎么样？",
                is_group=True, is_at_bot=True, message_id="1",
            )
            await core.handle_message(live_msg, _LiveSender())  # type: ignore[arg-type]
            if live_routed:
                print(f"  模型回：{live_routed[0]['text']!r}")
                names = [Path(p).name for p in live_routed[0]["stickers"]]
                print(f"  表情包：{names or '（这轮没带图：没调工具或挑了没给，是它的判断，不算失败）'}")
            else:
                print("  [SKIP] 模型这轮没开口（熔断或超时），跳过")

    print("\n" + "=" * 60)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项未通过：" + "、".join(_FAILED))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP_LIB, ignore_errors=True)
    sys.exit(code)
