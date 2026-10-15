#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对话历史压缩：离线验证（不联网，打桩 DeepSeek 客户端）

钉住 2026-10-15 修的坑：压缩结果必须**写回** _conversations。老实现只改本次请求的
messages，history 原地不动 —— 于是从超阈值那轮起，每一轮都要把不断变长的全部旧历史
重新摘要一遍（不是增量），白烧一次 API 调用和延迟，而且越滚越贵。

场景（把 MAX_HISTORY_LENGTH 调小以便快速触发；语义与线上一致）：
    MAX_HISTORY_LENGTH=6 → 阈值 12 条、保留最近 4 轮（8 条）
    第 8 轮触发第一次压缩，之后每 2 轮压一次
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import src.deepseek_client as dc  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  ← {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 打桩 DeepSeek 客户端
# ============================================================
SUMMARIZER_INPUTS: list[list[dict]] = []   # 摘要请求的入参
CHAT_INPUTS: list[list[dict]] = []         # 正常请求的 messages


class _Msg:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_calls = None
        self.reasoning_content = None


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("Choice", (), {"message": _Msg(content)})()]


class _Completions:
    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last = messages[-1].get("content", "") if messages else ""
        if "压缩成一段简洁的摘要" in last:      # 摘要请求：没有 system，最后一条是压缩指令
            SUMMARIZER_INPUTS.append(messages)
            return _Resp("摘要：聊过成都的路线，约好周四见。")
        CHAT_INPUTS.append(messages)
        return _Resp("收到")


class _FakeClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _Completions()})()


dc.client = _FakeClient()
dc.MAX_HISTORY_LENGTH = 6        # 只为让测试跑得短：阈值 12 条、保留最近 4 轮
MARKER = dc._SUMMARY_MARKER
KEY = "g1"

HISTORY_LENS: list[int] = []


async def turn(i: int) -> str:
    reply = await dc.chat(KEY, "u1", f"第{i}句", "测试")
    HISTORY_LENS.append(len(dc._conversations[KEY]))
    return reply


async def main() -> None:
    # ============================================================
    section("压缩确实发生，且写回 _conversations")
    # ============================================================
    for i in range(1, 9):
        await turn(i)
    history = dc._conversations[KEY]
    print(f"  轮次 → 历史条数：{HISTORY_LENS}")
    print(f"  摘要调用次数：{len(SUMMARIZER_INPUTS)}")
    check("第 8 轮触发了压缩（超阈值 12 条）", len(SUMMARIZER_INPUTS) == 1)
    check("压缩后历史真的变短（14 条 → 10 条 + 本轮 2 条 = 12）",
          HISTORY_LENS[7] == 12, f"实测 {HISTORY_LENS[7]}")
    check("摘要在历史第 1 条（写回生效）",
          history[0]["role"] == "user" and history[0]["content"].startswith(MARKER),
          history[0]["content"][:30])
    check("历史第 2 条是配套的 assistant 确认", history[1]["role"] == "assistant")
    check("压缩窗口内保留最近 4 轮原文，更早的已进摘要",
          MARKER not in str(history[-8:])
          and "第8句" in history[-2]["content"] and "第4句" in str(history[2])
          and "第1句" not in str(history),
          str([m["content"][:14] for m in history]))

    # ============================================================
    section("下一轮不再重压（老实现会在这里再压一遍）")
    # ============================================================
    await turn(9)
    check("又来一轮后压缩次数仍是 1（没重复压同一段旧历史）",
          len(SUMMARIZER_INPUTS) == 1, f"实测 {len(SUMMARIZER_INPUTS)} 次")
    check("历史条数是 14（12 + 本轮 2），没有被重复压缩", HISTORY_LENS[8] == 14,
          f"实测 {HISTORY_LENS[8]}")
    check("摘要出现在本轮请求里（模型确实看得到）",
          CHAT_INPUTS[-1][0]["role"] == "system"
          and CHAT_INPUTS[-1][1]["content"].startswith(MARKER))

    # ============================================================
    section("第二次压缩是增量的（把旧摘要合并，而不是重压全量）")
    # ============================================================
    before = len(SUMMARIZER_INPUTS)
    await turn(10)
    check("第 10 轮触发第二次压缩", len(SUMMARIZER_INPUTS) == before + 1)
    merged = SUMMARIZER_INPUTS[-1]
    check("待压部分以【更早的对话摘要】开头（拿去合并）",
          merged[0]["content"].startswith(MARKER), merged[0]["content"][:24])
    check("待压部分是固定的一小段（6 条 + 1 条指令），没有随轮次变长",
          len(merged) == 7, f"实测 {len(merged)} 条")
    check("压缩指令要求合并旧摘要，别丢关键信息",
          "合并" in merged[-1]["content"])

    # ============================================================
    section("长时间跑：历史长度有界，压缩次数线性而不是每轮一次")
    # ============================================================
    for i in range(11, 15):
        await turn(i)
    check("历史长度始终有界（≤ 14 条）", max(HISTORY_LENS) <= 14, f"峰值 {max(HISTORY_LENS)}")
    # 14 轮里：第 8、10、12、14 轮各压一次 = 4 次；老实现会到 7 次（8 轮之后每轮都压）
    check("14 轮只压了 4 次（= 触发次数，不是轮数）", len(SUMMARIZER_INPUTS) == 4,
          f"实测 {len(SUMMARIZER_INPUTS)} 次")

    # ============================================================
    section("熔断冷却期：跳过摘要，不去白等一次超时")
    # ============================================================
    calls_before = len(SUMMARIZER_INPUTS)
    dc._cooldown_until = time.monotonic() + 60
    try:
        await turn(15)
        await turn(16)          # 这一轮本该触发压缩（第 16 轮 = 14 条 > 阈值）
    finally:
        dc._cooldown_until = 0.0
    check("冷却期一次摘要都没发（不白等超时）",
          len(SUMMARIZER_INPUTS) == calls_before, f"实测 {len(SUMMARIZER_INPUTS)} 次")
    check("冷却期照常回答（退回硬截断，不是不干活）",
          dc._conversations[KEY][-1]["content"] == "收到")
    check("冷却期不改动历史（下轮还能重试压缩）",
          HISTORY_LENS[-1] > 14, f"实测 {HISTORY_LENS[-1]}")

    # 冷却结束后能正常再压
    await turn(17)
    check("冷却结束后恢复压缩", len(SUMMARIZER_INPUTS) == calls_before + 1,
          f"实测 {len(SUMMARIZER_INPUTS)} 次")
    check("恢复压缩后历史重新变短", HISTORY_LENS[-1] <= 14, f"实测 {HISTORY_LENS[-1]}")


async def counterfactual() -> None:
    """
    对照实验：用**真实的** _compress_history，但每次调用后把 history 复原成调用前的样子
    —— 这就精确复刻了老实现（只改本次请求的 messages，历史原地不动）。
    用来量化「白烧多少次调用 + 历史会涨到多长」。
    """
    section("对照：老行为（不写回）到底白烧了多少")
    real = dc._compress_history

    async def old_style(history, messages, threshold, keep_recent_rounds):
        snapshot = list(history)
        out = await real(history, messages, threshold, keep_recent_rounds)
        history[:] = snapshot          # 老实现：历史不动，只影响本次请求
        return out

    dc._compress_history = old_style
    dc._conversations[KEY].clear()
    SUMMARIZER_INPUTS.clear()
    lengths: list[int] = []
    try:
        for i in range(1, 15):
            await real_turn(i, lengths)
    finally:
        dc._compress_history = real

    print(f"  老行为 轮次 → 历史条数：{lengths}")
    print(f"  老行为 摘要调用次数：{len(SUMMARIZER_INPUTS)}")
    check("老行为：超阈值后每轮都压（14 轮压 7 次，第 8 轮起每轮一次）",
          len(SUMMARIZER_INPUTS) == 7, f"实测 {len(SUMMARIZER_INPUTS)} 次")
    check("老行为：历史一直涨（14 轮涨到 28 条，从没缩短）",
          lengths[-1] == 28 and max(lengths) == 28, f"实测 {lengths[-1]}")
    check("对照成立：同样 14 轮，写回版 4 次 / 老版 7 次",
          len(SUMMARIZER_INPUTS) > 4)


async def real_turn(i: int, sink: list[int]) -> None:
    await dc.chat(KEY, "u1", f"第{i}句", "测试")
    sink.append(len(dc._conversations[KEY]))


asyncio.run(main())
asyncio.run(counterfactual())

print()
if FAILS:
    print(f"没通过 {len(FAILS)} 条：")
    for name in FAILS:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
