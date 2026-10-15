#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
降级链离线验证：视觉失败退避 + 占位、QQ 域名跳过直传、服务不可用熔断快速失败。

全部离线（不连 QQ、不调 DeepSeek）：视觉客户端与 core.chat 都是假实现，
只为验证「失败时到底会不会拖住对话、会不会反复重试」。

    python tests/_degrade_check.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src import config as cfg                              # noqa: E402
from src import deepseek_client as dc                      # noqa: E402
from src import vision_utils as vu                         # noqa: E402
from src.channels import core                              # noqa: E402
from src.channels.base import IncomingMessage, Sender      # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


# ============================================================
# 假视觉客户端
# ============================================================
class _FakeCompletions:
    def __init__(self, outer: "_FakeClient"):
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls += 1
        self._outer.blocks.append(kwargs["messages"][0]["content"][0])
        if self._outer.mode == "boom":
            raise RuntimeError("503 Service is too busy")
        if self._outer.mode == "none_choices":
            return types.SimpleNamespace(choices=None, model_dump=lambda: {"choices": None})
        if self._outer.mode == "empty":
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="   "), finish_reason="stop")])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=self._outer.text), finish_reason="stop")])


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.mode = "boom"
        self.text = "类型：表情包 ｜ 意图：无语，不想说话"
        self.blocks: list[dict] = []

    def __post_init__(self) -> None:  # pragma: no cover - 仅为可读性
        pass


def make_fake() -> _FakeClient:
    fake = _FakeClient()
    fake.chat = types.SimpleNamespace(completions=_FakeCompletions(fake))
    return fake


QQ_URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc&rkey=xyz"
WEB_URL = "https://example.com/cat-question-mark.jpg"


def reset_vision() -> None:
    vu._FAILED.clear()
    vu._IMG_CACHE.clear()
    vu._IMG_RATE.clear()


def main() -> int:
    print("=== 1. 客户端是「短超时 + 不重试」 ===")
    real = vu._get_client()
    check("读超时 = VISION_TIMEOUT_SEC", getattr(real, "timeout", None) is not None
          and real.timeout.read == cfg.VISION_TIMEOUT_SEC,
          f"read={getattr(getattr(real, 'timeout', None), 'read', None)} 配置={cfg.VISION_TIMEOUT_SEC}")
    check("不重试（max_retries=0）", real.max_retries == 0, f"max_retries={real.max_retries}")

    print("\n=== 2. QQ 系域名：跳过直传，一次请求就够 ===")
    check("QQ CDN 命中跳过直传", vu._skip_direct(QQ_URL) is True)
    check("普通域名仍走直传", vu._skip_direct(WEB_URL) is False)

    reset_vision()
    fake = make_fake()
    downloads = {"n": 0}

    def fake_download(url, level):
        downloads["n"] += 1
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}

    vu._get_client = lambda: fake
    vu._downloaded_block = fake_download

    t0 = time.monotonic()
    desc = vu.describe_image(QQ_URL)
    used = time.monotonic() - t0
    check("QQ 图识别失败返回 None", desc is None)
    check("只自己取图 1 次（没有先白跑一次直传）", downloads["n"] == 1, f"下载={downloads['n']}")
    check("只发起 1 次视觉请求", fake.calls == 1, f"请求={fake.calls}")
    check("失败是快速返回（<1s）", used < 1.0, f"{used:.2f}s")

    print("\n=== 3. 失败退避：窗口内不再下载、不再调用 ===")
    desc2 = vu.describe_image(QQ_URL)
    check("第二次仍返回 None", desc2 is None)
    check("没有重新下载", downloads["n"] == 1, f"下载={downloads['n']}")
    check("没有重新请求", fake.calls == 1, f"请求={fake.calls}")
    check("退避窗口 = VISION_FAIL_BACKOFF_SEC", vu.VISION_FAIL_BACKOFF_SEC == 600.0)

    print("\n=== 4. 上下文预识别：失败留占位，逐张尽力 ===")
    reset_vision()
    fake2 = make_fake()
    vu._get_client = lambda: fake2
    descs = asyncio.run(vu.describe_for_context("onebot_group_1", [QQ_URL]))
    check("返回占位而不是 None", descs == [vu.IMAGE_UNAVAILABLE], f"{descs}")
    check("占位不进缓存（窗口过后还能再试）", vu._cache_get(QQ_URL) is None)
    step = IncomingMessage(text="", image_urls=[QQ_URL], image_descs=list(descs or []))
    check("占位能写进接话上下文", "[图片1：[图片，未能读取]]" in step.buffer_text(),
          step.buffer_text())

    # 一张成功 + 一张失败：整组仍然返回（不因为一张失败就丢掉另一张）
    reset_vision()
    vu._get_client = lambda: fake2
    vu._cache_put(WEB_URL, "类型：内容图片 ｜ 意图：给你看报错")
    mixed = asyncio.run(vu.describe_for_context("onebot_group_2", [WEB_URL, QQ_URL]))
    check("一张好一张坏：好的照常给出、坏的留占位",
          mixed == ["类型：内容图片 ｜ 意图：给你看报错", vu.IMAGE_UNAVAILABLE], f"{mixed}")

    print("\n=== 5. 成功路径：清退避、写缓存 ===")
    reset_vision()
    fake3 = make_fake()
    fake3.mode = "ok"
    vu._get_client = lambda: fake3
    desc3 = vu.describe_image(WEB_URL)
    check("成功返回描述", bool(desc3 and desc3.startswith("类型：")), str(desc3)[:30])
    check("成功后不在退避里", vu._in_backoff(WEB_URL) is False)
    # describe_image 本身不写缓存（缓存属于「上下文预识别」那一层）
    descs2 = asyncio.run(vu.describe_for_context("onebot_group_1", [WEB_URL]))
    check("预识别拿到同一份描述", descs2 == [desc3] and fake3.calls == 1, f"请求={fake3.calls}")
    descs3 = asyncio.run(vu.describe_for_context("onebot_group_1", [WEB_URL]))
    check("同图第二次命中缓存（不重复计费）", descs3 == [desc3] and fake3.calls == 1,
          f"请求={fake3.calls}")

    print("\n=== 6. 200 但 choices 为 None：不再抛 'NoneType' 裸错 ===")
    reset_vision()
    fake4 = make_fake()
    fake4.mode = "none_choices"
    vu._get_client = lambda: fake4
    try:
        desc4 = vu.describe_image(WEB_URL)
        check("判空后返回 None（没抛异常）", desc4 is None)
    except Exception as e:  # noqa: BLE001
        check("判空后返回 None（没抛异常）", False, f"{type(e).__name__}: {e}")
    fake4.mode = "empty"
    vu._clear_failed(WEB_URL)
    check("空回答按失败处理", vu.describe_image(WEB_URL) is None)

    print("\n=== 7. 服务不可用：熔断 + 快速放弃本轮 ===")
    vu._get_client = lambda: None

    class StubSender(Sender):
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text, image_urls=None, reply_to=None, sticker_paths=None):
            self.sent.append(text)
            from src.channels.base import SendResult
            return SendResult("ok", text)

    dc.note_api_success()
    calls = {"n": 0}
    chat_mode = {"mode": "boom"}

    async def fake_chat(*args, **kwargs):
        calls["n"] += 1
        if chat_mode["mode"] == "boom":
            raise ConnectionError("503 Service is too busy")
        return "好的呀～"

    core.chat = fake_chat

    def direct_msg() -> IncomingMessage:
        return IncomingMessage(channel="onebot", conv_id="onebot_group_1", user_id="u1",
                               user_name="张三", text="在吗", is_at_bot=True)

    def wake_msg() -> IncomingMessage:
        return IncomingMessage(channel="onebot", conv_id="onebot_group_1", user_id="u2",
                               user_name="李四", text="你们昨天说的那个游戏好玩吗",
                               ai_decides_reply=True, is_interjection=True,
                               extra_ctx="【李四】你们昨天说的那个游戏好玩吗")

    # 第 1 次失败：直呼 → 给一句短提示，且必须快速返回
    s1 = StubSender()
    t0 = time.monotonic()
    asyncio.run(core.handle_message(direct_msg(), s1))
    cost1 = time.monotonic() - t0
    check("直呼失败：快速返回（<1s，不再等 60s）", cost1 < 1.0, f"{cost1:.2f}s")
    check("直呼失败：给一句短提示", len(s1.sent) == 1 and "连不上" in s1.sent[0],
          str(s1.sent))
    check("失败计入连续失败（1/3）", dc._fail_streak == 1, f"streak={dc._fail_streak}")

    # 第 2、3 次失败 → 触发熔断
    asyncio.run(core.handle_message(direct_msg(), StubSender()))
    asyncio.run(core.handle_message(direct_msg(), StubSender()))
    left = dc.api_cooldown_left()
    check("连续 3 次失败后熔断", left > 0, f"冷却剩余 {left:.0f}s")

    # 熔断期内：唤醒/接话完全静默，且根本不发请求
    before = calls["n"]
    s2 = StubSender()
    t0 = time.monotonic()
    asyncio.run(core.handle_message(wake_msg(), s2))
    cost2 = time.monotonic() - t0
    check("熔断期唤醒：不发请求", calls["n"] == before, f"新增请求={calls['n'] - before}")
    check("熔断期唤醒：一声不吭（不吓人）", s2.sent == [], str(s2.sent))
    check("熔断期唤醒：立即返回（<1s）", cost2 < 1.0, f"{cost2:.2f}s")

    # 熔断期内：直呼只回一句短提示，同样不发请求
    s3 = StubSender()
    asyncio.run(core.handle_message(direct_msg(), s3))
    check("熔断期直呼：不发请求", calls["n"] == before)
    check("熔断期直呼：一句短提示", len(s3.sent) == 1 and "连不上" in s3.sent[0], str(s3.sent))

    # 服务恢复：熔断解除，恢复正常回复
    chat_mode["mode"] = "ok"
    dc.note_api_success()
    check("恢复后冷却清零", dc.api_cooldown_left() == 0.0)
    s4 = StubSender()
    asyncio.run(core.handle_message(direct_msg(), s4))
    check("恢复后正常回话", s4.sent and "好的呀" in s4.sent[0], str(s4.sent))
    check("成功一次即清零失败计数", dc._fail_streak == 0)

    print("\n=== 8. 视觉失败不再污染上下文 ===")
    check('上下文里没有「识别失败」字样',
          "识别失败" not in vu.IMAGE_UNAVAILABLE
          and "[图片，未能读取]" == vu.IMAGE_UNAVAILABLE, vu.IMAGE_UNAVAILABLE)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
