#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
真实链路聊天模拟器：只把「把消息发到 QQ」这一步打桩，其余全走项目里的真实代码。

    OneBot 事件 → extract_message → trigger 唤醒 → 合并/补一轮
                → core.handle_message → 真·DeepSeek（reply / skip 工具）
                → ReplyOutbox → OneBotSender.send（打桩：只记录，不发送）

用来回答三个问题：
  1. 多人 / 连发时，交给模型的上下文是否完整（谁说的、@ 没 @、引用了谁）
  2. DeepSeek 是否老实调 reply / skip；几个人同时跟它说话时是否各回一条
  3. 真正发出去几条、引用（reply_to）是否都来自本轮上下文 —— 即「消息收发」是否正确

不连真 QQ、不写群数据；DeepSeek 是真实调用（整轮跑下来约 8~12 次，会计费）。

用法：
    python tests/sim_chat.py            # 跑全部场景
    python tests/sim_chat.py 1 3        # 只跑场景 1 和 3
    python tests/sim_chat.py --list     # 列出场景
完整输出（含项目自身日志）写入 tests/sim_chat_last.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，打 emoji 会直接抛 UnicodeEncodeError（沿用 main.py 的做法）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

BOT_NAME = "蓝色大肥鱼"
BOT_SELF_ID = "900000001"
START = time.monotonic()


def elapsed() -> float:
    return round(time.monotonic() - START, 1)


class Tee:
    """输出同时进控制台和日志文件（项目自己的日志也一并留档）"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # 追加模式：分批跑不同场景时，前一次的记录不会丢
        self._fh = path.open("a", encoding="utf-8")
        self._out = sys.stdout
        self._closed = False

    def write(self, text: str) -> int:
        if self._closed:
            return 0
        self._fh.write(text)
        return self._out.write(text)

    def flush(self) -> None:
        # 解释器退出时会再 flush 一次 stdout，那时文件已经关了 —— 静默跳过，
        # 否则结尾会甩一行 "I/O operation on closed file" 的报错
        if self._closed:
            return
        self._fh.flush()
        self._out.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fh.flush()
        self._fh.close()


LOG_PATH = ROOT / "tests" / "sim_chat_last.log"
LOG = Tee(LOG_PATH)
sys.stdout = LOG  # 项目里的 print 也进日志


def say(text: str = "") -> None:
    LOG.write(text + "\n")
    LOG.flush()


# ============================================
# 打桩：只替换「发到 QQ」和「get_msg 回查」
# ============================================
SIMS: dict[int, "Sim"] = {}          # group_id → 场景
SIMS_BY_CONV: dict[str, "Sim"] = {}  # conv_id  → 场景

from src.channels import core                            # noqa: E402
from src.channels import onebot as ob                    # noqa: E402
from src.channels.onebot import OneBotClient             # noqa: E402
from src.tools import execute as execute_tool            # noqa: E402
from src.tools.chat_provider import ReplyItem            # noqa: E402
from src.trigger import trigger                          # noqa: E402

_REAL_CHAT = core.chat


class Sim:
    """一个模拟群：事件构造、打桩发送记录、每轮模型调用的记录"""

    def __init__(self, group: int, title: str):
        self.group = group
        self.title = title
        self.conv_id = f"onebot_group_{group}"
        self.seq = 500000
        self.sent: list[dict] = []         # 机器人真正发出去的（打桩记录）
        self.bot_db: dict[str, dict] = {}  # 机器人发过的消息，供 get_msg 回查（验证「引用自己」）
        self.rounds: list[dict] = []       # 每一轮模型调用的记录
        self._snapped = 0
        self.client = OneBotClient()       # 只为复用真实的事件解析与合并派发，不联网
        self.client._ws = None
        SIMS[group] = self
        SIMS_BY_CONV[self.conv_id] = self

    # ---------- 构造入站事件（与 NapCat 推的格式一致）----------
    def next_id(self) -> int:
        self.seq += 1
        return self.seq

    def event(self, user: int, name: str, text: str, *, at: bool = False, quote: int | None = None) -> dict:
        segments: list[dict] = []
        if at:
            segments.append({"type": "at", "data": {"qq": BOT_SELF_ID}})
        if quote:
            segments.append({"type": "reply", "data": {"id": str(quote)}})
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": self.group,
            "user_id": user,
            "message_id": self.next_id(),
            "self_id": BOT_SELF_ID,
            "sender": {"nickname": name, "user_id": user},
            "message": segments,
        }

    # ---------- 取数 ----------
    def round_sent(self, idx: int) -> list[dict]:
        """
        第 idx 轮（0 基）实际发出去的消息。

        core 是在 chat() 返回**之后**才发消息的，所以在 chat() 的 wrapper 里切片永远
        是空的：只能按「下一轮开始前」的发送计数切一刀，两轮之间的发送都归上一轮。
        """
        start = self.rounds[idx]["before"]
        end = self.rounds[idx + 1]["before"] if idx + 1 < len(self.rounds) else len(self.sent)
        return self.sent[start:end]

    async def user_says(
        self,
        user: int,
        name: str,
        text: str,
        *,
        delay: float = 0.0,
        at: bool = False,
        quote: int | None = None,
    ) -> None:
        """按真实时序投递一条群消息，等它跑完（含可能的补一轮）后立刻快照缓冲"""
        if delay:
            await asyncio.sleep(delay)
        say(
            f"  {elapsed():>6}s  [群消息] {name}{' @机器人' if at else ''}: {text}"
            + (f"   （引用 #{quote}）" if quote else "")
        )
        await self.client._process(self.event(user, name, text, at=at, quote=quote))
        self.snapshot(immediate=True)

    def snapshot(self, *, immediate: bool) -> None:
        """记录每轮结束时的缓冲状态，供后面断言 skip / reply 的收尾行为"""
        for r in self.rounds[self._snapped:]:
            r["buf_after"] = trigger.buffer.peek(self.conv_id)
            r["ids_after"] = trigger.buffer.ids(self.conv_id)
            r["active"] = trigger.is_active(self.conv_id)
            r["immediate"] = immediate
        self._snapped = len(self.rounds)


async def _stub_send(self, text, image_urls=None, reply_to=None) -> None:  # noqa: ANN001
    """替换 OneBotSender.send：真实链路的最后一步，只记录不发送"""
    sim = SIMS.get(int(getattr(self, "_group_id", 0) or 0))
    clean = (text or "").strip()
    if sim is None:
        say(f"  [!!] 有条消息发往了未注册的会话：{clean}")
        return
    mid = sim.next_id()
    rec = {"id": mid, "text": clean, "images": list(image_urls or ()), "reply_to": reply_to, "t": elapsed()}
    sim.sent.append(rec)
    sim.bot_db[str(mid)] = {
        "message": [{"type": "text", "data": {"text": clean}}],
        "sender": {"nickname": BOT_NAME, "user_id": int(BOT_SELF_ID)},
    }
    head = f"[BOT→群] 引用#{reply_to} " if reply_to else "[BOT→群] "
    say(f"  {rec['t']:>6}s  {head}{clean}")


async def _stub_get_msg(self, action, params, timeout=None):  # noqa: ANN001
    """只让 get_msg 生效（验证「引用机器人的话」这条回查路径），其余 API 一律失败"""
    if action == "get_msg":
        for sim in SIMS.values():
            rec = sim.bot_db.get(str(params.get("message_id")))
            if rec:
                return rec
    return None


async def _recording_chat(
    conv_id: str,
    user_id: str,
    user_message: str,
    user_name: str = "",
    outbox=None,  # noqa: ANN001
    history_text: str | None = None,
):
    """包住真实的 chat()：进模型前记下「本轮上下文有哪些消息 id」，出来后记录它怎么表态"""
    sim = SIMS_BY_CONV.get(conv_id)
    known = set(map(str, outbox.known_ids)) if outbox is not None else set()
    # 只记下当时的发送条数：真正发出发生在本函数返回之后（core 里），
    # 所以具体归属哪几条由 round_sent() 在报告阶段切
    before = len(sim.sent) if sim else 0
    started = elapsed()

    result = await _REAL_CHAT(
        conv_id, user_id, user_message, user_name, outbox=outbox, history_text=history_text
    )

    if sim is not None:
        sim.rounds.append(
            {
                "t": started,
                "user": user_name,
                "context": history_text or "",
                "known": known,
                "outbox": [(i.text, i.reply_to) for i in outbox.items] if outbox is not None else None,
                "skipped": bool(outbox.skipped) if outbox is not None else False,
                "sent": sim.sent[before:],  # 占位，报告阶段用 round_sent() 覆盖
                "before": before,
                "result": result,
                "buf_after": "",
                "ids_after": set(),
                "active": trigger.is_active(conv_id),
                "immediate": False,
            }
        )
    return result


def install_stubs() -> None:
    ob.OneBotSender.send = _stub_send
    ob.OneBotClient.call_api = _stub_get_msg
    core.chat = _recording_chat
    # 群黑白名单是给真群用的，测试群直接放行
    trigger._group_allowed = lambda raw: True  # type: ignore[method-assign]


# ============================================
# 场景
# ============================================
ZHANG, LI, WANG, ZHAO = 111111, 222222, 333333, 444444


def new_sim(group: int, title: str) -> Sim:
    sim = Sim(group, title)
    trigger.buffer.clear(sim.conv_id)
    trigger.leave_session(sim.conv_id)
    say(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    return sim


async def scene_1() -> Sim:
    """@ 开局 → 多人接话 → 无关闲聊 → 引用机器人的话 → 收尾"""
    sim = new_sim(990001, "场景 1：@ 开局，之后多人接话 / 无关闲聊 / 引用机器人的话")

    await sim.user_says(
        ZHANG, "张三", "肥鱼，问你个事：你平时是怎么决定要不要在群里冒泡的？被@了就一定回吗？", at=True
    )
    await sim.user_says(LI, "李四", "我也想知道，感觉你平时都不说话", delay=0.5)
    await sim.user_says(WANG, "王五", "对了，今晚谁打排位", delay=0.5)
    await sim.user_says(ZHAO, "赵六", "我我我，等我吃完饭", delay=0.5)
    await sim.user_says(ZHANG, "张三", "是吧肥鱼，你自己说说", delay=0.5)
    if sim.sent:
        await sim.user_says(LI, "李四", "那你倒是具体说说看啊", delay=0.5, quote=sim.sent[0]["id"])
    await sim.user_says(ZHANG, "张三", "行，那就先这样，拜拜", delay=0.5, at=True)
    return sim


async def scene_2() -> Sim:
    """@ 之后 1s / 2s 各来一条：思考期间的消息应合并进补一轮，而不是一人一轮"""
    sim = new_sim(990002, "场景 2：@ 之后紧接着两条（思考期间合并；投递间隔 1s / 2s）")

    await asyncio.gather(
        sim.user_says(
            ZHANG, "张三", "肥鱼，「有人在群里@你」和「没人@你但聊到你」，你分别会怎么反应？", at=True
        ),
        sim.user_says(LI, "李四", "他肯定说看心情", delay=1.0),
        sim.user_says(WANG, "王五", "笑死，别欺负鱼了", delay=2.0),
    )
    sim.snapshot(immediate=False)
    return sim


async def scene_3() -> Sim:
    """两个人几乎同时 @ 机器人，各问各的"""
    sim = new_sim(990003, "场景 3：两个人几乎同时 @ 机器人（各问各的，看是否各回一条）")

    await asyncio.gather(
        sim.user_says(ZHANG, "张三", "肥鱼，你觉得写周报最烦的是哪一步？", at=True),
        sim.user_says(LI, "李四", "肥鱼，午饭吃啥好，给我出个主意", delay=0.4, at=True),
    )
    sim.snapshot(immediate=False)
    return sim


# 场景 4 的观测点（打桩模型，不需要 API）
SLOW: dict[str, object] = {}


async def scene_4() -> Sim:
    """场景 4：思考超过时限 → 放弃本轮（晚到的回答也不许进群；打桩，不调 API）"""
    sim = new_sim(
        990004,
        "场景 4：AI 思考超时 —— 放弃本轮（打桩模型：即使它在第 12 秒才返回，也不许发消息）",
    )
    cap = 1.0  # 把单轮时限压到 1 秒，假装模型很慢

    async def slow_chat(conv_id, user_id, user_message, user_name="", outbox=None, history_text=None):
        """慢模型：真时限 1s，但它要 6s 才有结果，而且「吞掉取消」继续跑完"""
        SLOW["started"] = True
        try:
            await asyncio.sleep(6.0)
        except asyncio.CancelledError:
            # 最坏情况：取消没拦住，请求其实还在跑，晚点才返回
            SLOW["cancel_seen"] = True
            await asyncio.sleep(6.0)
        if outbox is not None:
            # 晚到的回答试图说话 —— 这句绝对不许进群
            outbox.items.append(ReplyItem(text="这句晚到的回答绝对不能出现在群里"))
        SLOW["late_returned"] = True
        SLOW["sent_when_late"] = len(sim.sent)
        return "这句晚到的回答绝对不能出现在群里"

    async def fast_chat(conv_id, user_id, user_message, user_name="", outbox=None, history_text=None):
        """下一轮：正常答，同时记下它看到的上下文"""
        SLOW["next_ctx"] = history_text or ""
        if outbox is not None:
            outbox.items.append(ReplyItem(text="在的，刚走神了一下"))
            return "在的，刚走神了一下"
        return "在的，刚走神了一下"

    real_chat, real_cap = core.chat, core.DEEPSEEK_ROUND_TIMEOUT_SEC
    core.chat, core.DEEPSEEK_ROUND_TIMEOUT_SEC = slow_chat, cap
    try:
        await sim.user_says(ZHANG, "张三", "肥鱼，在线吗？问你个事：周末有空一起吃饭不", at=True)

        say(
            f"  {elapsed():>6}s  [超时瞬间] 发出 {len(sim.sent)} 条 / "
            f"互动窗口={'开着' if trigger.is_active(sim.conv_id) else '已关闭'} / "
            f"缓冲残留「{trigger.buffer.peek(sim.conv_id)}」"
        )
        check("场景4：超时后一条消息都没发", not sim.sent, f"实际 {len(sim.sent)} 条")
        check("场景4：超时后退出互动窗口（要再开口得等下一次唤醒）", not trigger.is_active(sim.conv_id))
        check(
            "场景4：超时的片段留在缓冲里（和 skip 一致，不丢上下文）",
            bool(trigger.buffer.peek(sim.conv_id).strip()),
        )

        # 等那个「取消没拦住」的请求真的返回
        await asyncio.sleep(7.0)
        say(
            f"  {elapsed():>6}s  [晚到的回答返回] late_returned={SLOW.get('late_returned')} "
            f"那一刻群里已有 {SLOW.get('sent_when_late')} 条 / 现在 {len(sim.sent)} 条"
        )
        check(
            "场景4：晚到的回答（第 12 秒）一条都没进群",
            SLOW.get("late_returned") is True and not sim.sent,
            f"late_returned={SLOW.get('late_returned')}，发出 {len(sim.sent)} 条",
        )
        check("场景4：取消确实送到了（它吞掉取消也没能拖住这一轮）", SLOW.get("cancel_seen") is True)

        # 下一次唤醒：@ 重新开局，且没丢前面那段
        core.chat = fast_chat
        await sim.user_says(LI, "李四", "在不在？我也想问周末的事", at=True)
        ctx = str(SLOW.get("next_ctx") or "")
        say(f"  {elapsed():>6}s  [下一轮上下文]\n" + "\n".join(f"        {ln}" for ln in ctx.splitlines()))
        check("场景4：超时之后的 @ 能正常接上", len(sim.sent) == 1, f"发出 {len(sim.sent)} 条")
        check(
            "场景4：下一轮上下文里张三那条还在（超时没丢上下文）",
            "张三" in ctx and "李四" in ctx,
            ctx.replace("\n", " | "),
        )
    finally:
        core.chat, core.DEEPSEEK_ROUND_TIMEOUT_SEC = real_chat, real_cap
    return sim


# 场景 5 的观测点（打桩模型，不需要 API）
FLIP: dict[str, object] = {}


async def scene_5() -> Sim:
    """场景 5：reply 之后又调 skip —— 已排队的回复不许被撤掉（打桩，不调 API）"""
    sim = new_sim(
        990005,
        "场景 5：reply×2 之后再调 skip —— 已排队的回复必须照常发出（打桩模型）",
    )

    async def flip_chat(conv_id, user_id, user_message, user_name="", outbox=None, history_text=None):
        """
        复刻真机上的那次来回：先排两条回复，模型随后又把 skip 当「我说完了」用了。

        旧实现里 skip 会清空 items，于是群里一条都收不到（真机上它还因此撤了又重发，
        白烧一次模型调用）；这里走真实的注册表入口（tools.execute + 本轮上下文），
        钉的就是注册之后 handler 的语义。
        """
        FLIP["ctx"] = history_text or ""
        if outbox is None:
            FLIP["no_outbox"] = True
            return ""
        ids = sorted(outbox.known_ids)
        FLIP["r1"] = execute_tool(
            "reply",
            json.dumps({"text": "先回一条：在的", "reply_to": ids[0] if ids else ""}),
            context=outbox,
        )
        FLIP["r2"] = execute_tool(
            "reply", json.dumps({"text": "第二条：刚翻了下记录"}), context=outbox
        )
        FLIP["skip"] = execute_tool("skip", "{}", context=outbox)
        FLIP["items_after_skip"] = len(outbox.items)
        FLIP["skipped_flag"] = outbox.skipped
        return ""

    real_chat = core.chat
    core.chat = flip_chat
    try:
        await sim.user_says(ZHANG, "张三", "肥鱼，你刚说的那个记录里到底有啥？", at=True)
    finally:
        core.chat = real_chat

    items = int(FLIP.get("items_after_skip") or 0)
    say(
        f"  {elapsed():>6}s  [skip 之后] 队列里还剩 {items} 条 / "
        f"skipped={FLIP.get('skipped_flag')} / skip 返回：「{FLIP.get('skip')}」"
    )
    say(
        f"  {elapsed():>6}s  [群里收到 {len(sim.sent)} 条] "
        + "；".join(
            (f"{m['text']}（引用 #{m['reply_to']}）" if m["reply_to"] else m["text"]) for m in sim.sent
        )
    )

    check("场景5：唤醒轮确实带上了 outbox", not FLIP.get("no_outbox"))
    check("场景5：skip 不撤销已排队的回复（队列仍是 2 条）", items == 2, f"实际 {items} 条")
    check("场景5：有回复时不该被标成「本轮不开口」", FLIP.get("skipped_flag") is False)
    check(
        "场景5：skip 的返回文案明确说了不撤销",
        "不会撤销" in str(FLIP.get("skip") or ""),
        str(FLIP.get("skip")),
    )
    check(
        "场景5：两条回复都发进群了（旧实现这里会是 0 条）",
        [m["text"] for m in sim.sent] == ["先回一条：在的", "第二条：刚翻了下记录"],
        f"实际 {[m['text'] for m in sim.sent]}",
    )
    check(
        "场景5：第一条仍带着引用，第二条是普通发言",
        len(sim.sent) == 2 and bool(sim.sent[0]["reply_to"]) and not sim.sent[1]["reply_to"],
        f"reply_to={[m['reply_to'] for m in sim.sent]}",
    )
    check("场景5：本轮结束顺手清了缓冲（回过的不会再被答一次）", not trigger.buffer.ids(sim.conv_id))
    return sim


SCENES = {"1": scene_1, "2": scene_2, "3": scene_3, "4": scene_4, "5": scene_5}


# ============================================
# 断言与总结
# ============================================
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    CHECKS.append((name, bool(ok), str(extra)))
    say(("  OK   " if ok else "  FAIL ") + name + (f"   [{extra}]" if extra else ""))


def report(sim: Sim) -> None:
    say(f"\n  ── 回放：{sim.title}")
    for i, r in enumerate(sim.rounds, 1):
        ctx_lines = [ln for ln in r["context"].splitlines() if ln.strip()]
        sent = sim.round_sent(i - 1)
        if r["skipped"]:
            action = "skip（本轮不开口）"
        elif r["outbox"]:
            action = f"reply ×{len(r['outbox'])}"
        else:
            action = "没调工具 → 退回普通文本"
        say(f"    第 {i} 轮 [{r['t']}s] 上下文 {len(ctx_lines)} 条 → {action}")
        for ln in ctx_lines:
            say(f"          · {ln}")
        for m in sent:
            head = f"（引用 #{m['reply_to']}）" if m["reply_to"] else ""
            say(f"        → 发出{head}（{len(m['text'])} 字）：{m['text']}")
        if not sent:
            say("        → 本轮一条都没发")
        elif len(sent) > 1:
            say(f"        （本轮共 {sum(len(m['text']) for m in sent)} 字）")

    say("")
    tag = f"场景{sim.group}"
    for i, r in enumerate(sim.rounds, 1):
        t = f"{tag} 第{i}轮"
        sent = sim.round_sent(i - 1)
        known = {str(x) for x in r["known"]}

        empty = [s["text"] for s in sent if not s["text"]]
        check(f"{t}：发出的内容都非空", not empty, str(empty))

        stray = [s["reply_to"] for s in sent if s["reply_to"] and str(s["reply_to"]) not in known]
        check(f"{t}：引用都来自本轮上下文", not stray, f"越界引用={stray}")

        # 模型会照抄上下文的【说话者】格式，发送前必须被 core 剥掉（实测踩过）
        human_names = set()
        for line in (r["context"] or "").splitlines():
            m = re.match(r"^【(.{1,12}?)】", line.strip())
            if m:
                human_names.add(m.group(1))
        leak = [s["text"] for s in sent if any(s["text"].startswith(f"【{n}】") for n in human_names)]
        check(f"{t}：回复开头没抄【说话者】标签", not leak, str(leak))

        if r["skipped"]:
            check(f"{t}：skip 后一条都没发出去", not sent, f"实际发了 {len(sent)} 条")
            if r["immediate"]:
                check(
                    f"{t}：skip 后片段留在缓冲里（下条消息继续用）",
                    bool(r["buf_after"].strip()),
                    r["buf_after"].replace("\n", " | "),
                )
        elif r["outbox"]:
            check(
                f"{t}：reply 调用条数 == 实际发送条数",
                len(sent) == len(r["outbox"]),
                f"工具 {len(r['outbox'])} 条 / 实际 {len(sent)} 条",
            )
            check(f"{t}：回复后互动窗口仍开着", bool(r["active"]))

    # 回过的消息不该再残留在缓冲里（跨轮验证：写进下一轮上下文会重复回答）
    replied = {x for i, r in enumerate(sim.rounds) if sim.round_sent(i) for x in r["known"]}
    left = replied & trigger.buffer.ids(sim.conv_id)
    check(f"{tag}：回过的消息不再残留缓冲（不会重复回答）", not left, f"残留={sorted(left)}")

    # 同一条入站消息不该出现在两个「发过言」的轮次里 —— 模型没用工具那轮必须同样收尾
    seen: dict[str, int] = {}
    dup: list[str] = []
    for i, r in enumerate(sim.rounds):
        if not sim.round_sent(i):
            continue
        for x in map(str, r["known"]):
            if x in seen:
                dup.append(f"#{x} 在第{seen[x]}轮和第{i + 1}轮都被回答")
            seen[x] = i + 1
    check(f"{tag}：同一条消息不会被回答两次", not dup, "; ".join(dup))

    # 场景专属观察项
    all_ctx = "\n".join(r["context"] for r in sim.rounds)
    if sim.group == 990001:
        check("场景1：引用机器人的话，模型收到「引用了你的消息」", "引用了你的消息" in all_ctx)
    if sim.group == 990002:
        check("场景2：连发被合并成补一轮，不是一人一轮", len(sim.rounds) == 2, f"实际 {len(sim.rounds)} 轮")
        if len(sim.rounds) >= 2:
            ctx2 = sim.rounds[-1]["context"]
            check("场景2：补一轮的上下文同时含李四、王五", "李四" in ctx2 and "王五" in ctx2)
    if sim.group == 990003:
        check(
            "场景3：两人的 @ 都进了模型上下文",
            ("写周报" in all_ctx) and ("午饭" in all_ctx) and all_ctx.count("@了你") >= 2,
            f"@了你 出现 {all_ctx.count('@了你')} 次",
        )


async def run(scene_ids: list[str]) -> None:
    install_stubs()
    from src import config

    say(f"模型：{config.DEEPSEEK_MODEL}  温度：{config.DEEPSEEK_TEMPERATURE}")
    say(
        "唤醒参数：TRIGGER_PROBABILITY={}  TRIGGER_ACTIVE_TIMEOUT_SEC={}  TRIGGER_MIN_LEN={}".format(
            config.TRIGGER_PROBABILITY, config.TRIGGER_ACTIVE_TIMEOUT_SEC, config.TRIGGER_MIN_LEN
        )
    )
    say("只打桩了「发到 QQ」：唤醒、合并、提示词、DeepSeek、outbox 全部是真实代码。")

    sims: list[Sim] = []
    for sid in scene_ids:
        sims.append(await SCENES[sid]())

    say(f"\n{'=' * 78}\n回放与断言\n{'=' * 78}")
    for sim in sims:
        report(sim)

    failed = [n for n, ok, _ in CHECKS if not ok]
    total_calls = sum(len(s.rounds) for s in sims)
    total_sent = sum(len(s.sent) for s in sims)
    say(f"\n{'=' * 78}")
    say(
        f"断言：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过    "
        f"模型调用：{total_calls} 次    实际发出：{total_sent} 条    耗时：{elapsed()}s"
    )
    lengths = [len(m["text"]) for s in sims for m in s.sent]
    if lengths:
        say(
            f"回复长度：平均 {sum(lengths) // len(lengths)} 字    "
            f"最长 {max(lengths)} 字    最短 {min(lengths)} 字"
        )
    if failed:
        say("失败项：")
        for n in failed:
            say("  - " + n)
    say(f"完整日志：{LOG_PATH}")
    if failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="真实链路聊天模拟器")
    parser.add_argument("scenes", nargs="*", help="要跑的场景编号（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出场景")
    args = parser.parse_args()

    if args.list:
        for sid, fn in SCENES.items():
            say(f"  {sid}  {(fn.__doc__ or '').strip().splitlines()[0]}")
        return

    ids = args.scenes or list(SCENES)
    unknown = [s for s in ids if s not in SCENES]
    if unknown:
        say(f"未知场景：{unknown}（可用：{list(SCENES)}）")
        sys.exit(2)

    try:
        asyncio.run(run(ids))
    finally:
        LOG.close()


if __name__ == "__main__":
    main()
