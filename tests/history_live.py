#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对话记忆压缩：**效果**评估（真打 DeepSeek，约 3 次对话调用 + 2 次摘要调用）

和 tests/history_compress_check.py 的分工：
  · history_compress_check.py 验**机制**（写回 / 增量 / 有界 / 冷却跳过），全离线打桩
  · 本脚本验**效果**——摘要到底保住了哪些信息，模型只靠摘要还能不能答对

做法：按线上格式往 _conversations 里灌 21 轮群聊（关键事实故意放在会被压掉的旧轮，
最近 4 轮只留寒暄），然后：
  1) 触发压缩，把摘要原文打出来；核对请求里旧原文确实已不在（只留摘要）
  2) 逐条核对关键事实是否留在摘要里，给命中表
  3) 只靠「摘要 + 最近 4 轮」问回忆题，看模型答得对不对（人会答错，所以逐条打分）
  4) 再灌一段新对话触发第二次压缩，检查是否把旧摘要合并进来（增量，不丢老信息）
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
from src.config import DEEPSEEK_API_KEY  # noqa: E402

if not DEEPSEEK_API_KEY:
    print("没配 DEEPSEEK_API_KEY：请在 .env 里填好再跑。")
    sys.exit(2)

FAILS: list[str] = []
GROUP = "live-compress-test"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  ← {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def note(text: str) -> None:
    print(f"  · {text}")


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


# ============================================================
# 记录模型实际看到的 messages（转发给真客户端，只多存一份入参）
# ============================================================
class _Recorder:
    def __init__(self, inner):
        self._inner = inner
        self.calls: list[list[dict]] = []
        self.times: list[float] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        started = time.monotonic()
        result = await self._inner.chat.completions.create(**kwargs)
        self.times.append(time.monotonic() - started)
        self.calls.append(kwargs.get("messages") or [])
        return result


# ============================================================
# 21 轮群聊：关键事实在前 6 轮（会被压掉），最近 4 轮只有寒暄
# ============================================================
ROUNDS = [
    ("【小林】下周五聚餐定哪儿？先说好我不吃辣，得有个鸳鸯锅",
     "收到～鸳鸯锅记上了，地点你们挑，别选全是辣的店。"),
    ("【阿May】那就海底捞春熙路店吧，另外我花生过敏，到店得跟服务员说一声",
     "海底捞春熙路店可以，花生过敏这点到店再强调一次。"),
    ("【小王】人数先按 8 个人算，人均 120 上下，超了我不请客啊",
     "8 个人、人均 120 左右，这样好算账。"),
    ("【小林】时间就 10 月 23 号晚上 7 点半，谁迟到谁请奶茶",
     "10 月 23 日 19:30，记下了。"),
    ("【阿May】张总说他 20:00 才能到，让我们先点菜别等他",
     "那张总 20:00 到，我们先点着。"),
    ("【小王】订座我来负责，提前一天打 028-8888 1234 那个号码",
     "订座交给小王，提前一天打电话。"),
    ("【小林】今天地铁 7 号线早高峰挤爆了，站了四十分钟",
     "7 号线早高峰确实劝退。"),
    ("【阿May】下午三点又要开会，烦",
     "开完会记得喝口水。"),
    ("【小王】周末那场球你们看吗",
     "看，几点开球来着。"),
    ("【小林】我先把周报写了，别催我",
     "行，不催。"),
    ("【阿May】楼下面馆涨价了两块",
     "两块也是钱。"),
    ("【小王】我工位空调太冷了，穿了两件还冷",
     "跟他们报修下出风口吧。"),
    ("【小林】刚吃完一碗甜水面，撑住了",
     "甜水面后劲大。"),
    ("【阿May】快递又放驿站了，懒得下去拿",
     "攒一起拿吧。"),
    ("【小王】打印机又卡纸了，这个月第三次",
     "该换纸了。"),
    ("【小林】晚上降温，记得加衣服",
     "好，你也是。"),
    ("【阿May】我把会议纪要发群里了，有空看看",
     "回头就看。"),
    ("【小王】周末那场球你们押谁赢",
     "我看主队状态好点。"),
    ("【小林】主队后腰伤了，别押太满",
     "那就小押一点。"),
    ("【阿May】看球那家烧烤摊还开着吗",
     "应该还开，去前问一声。"),
    ("【小王】开球是周日晚上八点，别记错日子",
     "周日晚八点，记住了。"),
]

HISTORY = []
for user_msg, bot_msg in ROUNDS:
    HISTORY.append({"role": "user", "content": user_msg})
    HISTORY.append({"role": "assistant", "content": bot_msg})

# 关键事实：(描述, 摘要里应该出现的任一关键词)
FACTS = [
    ("聚餐日期时间 10 月 23 日 19:30", ["10 月 23", "10月23", "23 号", "23号"]),
    ("时间点 19:30 / 7 点半", ["19:30", "7 点半", "七点半"]),
    ("地点海底捞春熙路店", ["海底捞"]),
    ("具体门店春熙路", ["春熙路"]),
    ("人数 8 人", ["8 人", "8人", "8个", "8 个"]),
    ("人均 120", ["120"]),
    ("小林不吃辣 / 鸳鸯锅", ["鸳鸯锅"]),
    ("阿May 花生过敏", ["花生"]),
    ("小王负责订座", ["订座"]),
    ("订座电话 028-8888 1234", ["028-8888", "8888"]),
    ("张总 20:00 到", ["张总"]),
]

def has_24(text: str) -> bool:
    """改期后的日期（10 月 24 日）在不在文本里 —— 写法有好几种，逐一看"""
    return any(k in text for k in ("24 号", "24号", "10 月 24", "10月24", "24 日", "24日"))


def summary_text() -> str:
    """历史第 1 条就是摘要（带标记），剥掉标记返回正文"""
    history = dc._conversations[GROUP]
    if not history:
        return ""
    return history[0]["content"].replace(dc._SUMMARY_MARKER, "").strip()


def fact_table(text: str, title: str) -> list[str]:
    print(f"\n  ── {title}：关键词命中表 ──")
    missed = []
    for desc, keys in FACTS:
        hit = any(k in text for k in keys)
        print(f"     {'✔' if hit else '✘'} {desc}")
        if not hit:
            missed.append(desc)
    return missed


async def main() -> None:
    recorder = _Recorder(dc.client)
    dc.client = recorder
    dc.MAX_HISTORY_LENGTH = 20          # 与线上 .env 一致：阈值 40 条、保留最近 4 轮
    dc.DEEPSEEK_ENABLE_SEARCH = False
    dc.get_definitions = lambda whitelist=None: []  # 本测试只评记忆，显式禁用含技能加载在内的全部工具。
    dc._conversations[GROUP].clear()
    dc._conversations[GROUP].extend([dict(m) for m in HISTORY])

    raw_chars = sum(len(m["content"]) for m in HISTORY[: -8])
    print(f"灌入 {len(ROUNDS)} 轮 / {len(HISTORY)} 条历史；其中会被压缩的旧部分 {raw_chars} 字")

    # ============================================================
    section("1. 触发压缩：摘要原文 + 请求里旧原文是否真的消失")
    # ============================================================
    started = time.monotonic()
    await dc.chat(GROUP, "u-xiaowang", "在的，说个事", "小王")
    elapsed = time.monotonic() - started
    summary = summary_text()
    history = dc._conversations[GROUP]

    print(f"\n  ── 摘要原文（{len(summary)} 字，原始 {raw_chars} 字，"
          f"压到约 {len(summary) / max(raw_chars, 1) * 100:.0f}%）──")
    print("  " + summary.replace("\n", "\n  "))
    print(f"\n  · 触发压缩那一轮共 {elapsed:.1f} 秒 = 摘要调用 {recorder.times[0]:.1f} 秒"
          f" + 对话调用 {recorder.times[1]:.1f} 秒")
    print(f"  · 摘要调用入参 {sum(len(m['content']) for m in recorder.calls[0])} 字"
          f"（不含 system，只有旧历史 + 压缩指令）")
    check("慢的是摘要调用本身，老行为下每轮都要重付一次",
          recorder.times[0] > recorder.times[1],
          f"摘要 {recorder.times[0]:.1f}s vs 对话 {recorder.times[1]:.1f}s")

    check("压缩已发生：历史开头是摘要", history[0]["content"].startswith(dc._SUMMARY_MARKER))
    check("历史被真正缩短（42 条 → 12 条）", len(history) == 12, f"实测 {len(history)} 条")
    check("最近 4 轮原文保留（寒暄没被压掉）",
          "周日晚八点" not in summary and "开球" in str(history[-8:]),
          str(history[-1]["content"])[:30])

    seen = recorder.calls[-1]
    check("模型这轮看到的第一条是摘要素材（system 之后）",
          seen[0]["role"] == "system" and seen[1]["content"].startswith(dc._SUMMARY_MARKER))
    check("请求里只剩摘要+最近几轮，几十条旧原文不在了",
          len(seen) <= 14, f"实测 {len(seen)} 条（压缩前历史 {len(HISTORY)} 条）")
    hist_chars = sum(len(m["content"]) for m in seen[1:])   # 去掉 system，只算历史部分
    check("请求里的历史部分确实比原文短",
          hist_chars < raw_chars, f"{hist_chars} 字 vs 原文 {raw_chars} 字")
    print(f"  · 这轮请求：历史部分 {hist_chars} 字（原文 {raw_chars} 字）"
          f"，整个请求 {sum(len(m['content']) for m in seen)} 字（system 占大头）")

    # ============================================================
    section("2. 关键事实保留情况（摘要层）")
    # ============================================================
    missed = fact_table(summary, "摘要")
    check("事实保留率 ≥ 80%", len(missed) <= 2, f"丢了 {len(missed)} 条：{missed}")

    # ============================================================
    section("3. 只靠摘要+最近 4 轮问回忆题（模型层）")
    # ============================================================
    question = ("下周聚餐是哪天、几点、在哪家店？几个人、人均多少？"
                "谁不能吃辣、谁过敏、谁去订座，张总几点到？")
    print(f"\n  提问：{question}")
    started = time.monotonic()
    reply = await dc.chat(GROUP, "u-xiaowang", question, "小王")
    print(f"\n  ── 模型回答（{time.monotonic() - started:.1f} 秒）──")
    print("  " + reply.replace("\n", "\n  "))

    print("\n  ── 回答里的关键信息核对 ──")
    ANSWER_KEYS = [
        ("日期 10 月 23", ["23"]),
        ("时间 19:30 / 7 点半", ["19:30", "7 点半", "七点半"]),
        ("地点海底捞春熙路店", ["海底捞"]),
        ("人数 8 人", ["8"]),
        ("人均 120", ["120"]),
        ("小林不吃辣/鸳鸯锅", ["小林"]),
        ("阿May 花生过敏", ["花生"]),
        # 提问的人本身就是小王，模型答成「订座是你负责」也算答对（你=小王）
        ("小王订座", ["小王", "你负责", "订座是你"]),
        ("张总 20:00 到", ["20:00", "8 点", "八点"]),
    ]
    answer_missed = []
    for desc, keys in ANSWER_KEYS:
        hit = any(k in reply for k in keys)
        print(f"     {'✔' if hit else '✘'} {desc}")
        if not hit:
            answer_missed.append(desc)
    check("回答答对 ≥ 7 条关键信息", len(answer_missed) <= 2,
          f"漏了 {len(answer_missed)} 条：{answer_missed}")

    # ============================================================
    section("4. 第二次压缩：合并旧摘要（增量，不丢老信息）")
    # ============================================================
    NEW_ROUNDS = [
        ("【阿May】海底捞说 23 号那天的包间满了，只能改到 10 月 24 号同一时间",
         "那就 10 月 24 日 19:30，其余不变。"),
        ("【小王】改期后我重新订了，订座号 7712，还是报张总的名字",
         "订座号 7712 记下了。"),
        ("【小林】24 号我没问题",
         "好，那就这么定。"),
    ] + [(f"【小王】随便聊点别的第 {i} 句，今天不聊聚餐的事", "嗯嗯。")
         for i in range(1, 13)]

    for user_msg, bot_msg in NEW_ROUNDS:
        dc._conversations[GROUP].append({"role": "user", "content": user_msg})
        dc._conversations[GROUP].append({"role": "assistant", "content": bot_msg})
    note(f"再灌 {len(NEW_ROUNDS)} 轮新对话（含改期到 10 月 24 日、订座号 7712）；"
         f"历史涨到 {len(dc._conversations[GROUP])} 条，再次越过 40 条阈值")

    old_summary = summary_text()
    raw2 = sum(len(m["content"]) for m in dc._conversations[GROUP][:-8])
    await dc.chat(GROUP, "u-xiaowang", "在的", "小王")
    merged = summary_text()
    print(f"\n  ── 合并后的摘要（{len(merged)} 字；旧部分原文 {raw2} 字，"
          f"压到约 {len(merged) / max(raw2, 1) * 100:.0f}%）──")
    print("  " + merged.replace("\n", "\n  "))

    check("第二次压缩是合并：新事实进了摘要（改到 10 月 24 日）", has_24(merged), merged[:60])
    check("新事实：订座号 7712", "7712" in merged)
    check("老事实仍在：海底捞 / 8 人 / 花生",
          all(k in merged for k in ("海底捞", "8", "花生")),
          f"海底捞={'海底捞' in merged} 8={'8' in merged} 花生={'花生' in merged}")
    check("摘要没有无限膨胀（合并后仍是百来字级）", len(merged) < len(old_summary) * 2.5,
          f"{len(old_summary)} → {len(merged)} 字")

    # 只靠合并后的摘要再问一次改期这件事
    reply2 = await dc.chat(GROUP, "u-xiaowang", "聚餐最后定的是哪天？订座号多少？", "小王")
    print(f"\n  ── 改期后的回答 ──\n  " + reply2.replace("\n", "\n  "))
    check("能答出改期后的日期", has_24(reply2), reply2[:80])
    check("能答出订座号", "7712" in reply2)


asyncio.run(main())

print()
if FAILS:
    print(f"没通过 {len(FAILS)} 条：")
    for name in FAILS:
        print(f"  - {name}")
    sys.exit(1)
print("压缩效果评估：全部通过")
