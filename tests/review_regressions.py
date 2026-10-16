"""审查发现的回归用例：假模型、临时数据，不调用 QQ 或付费 API。

运行：python -u tests/review_regressions.py
断言固定审查发现的缺陷；修复后应全部通过。与架构及技能专项检查互补。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import campaign_state as cs, deepseek_client as dc, trigger as tr, user_profile as up
from src import tools, vision_utils as vu
from src.channels import core, onebot
from src.channels.base import IncomingMessage
from src.tools.chat_provider import ReplyItem, ReplyOutbox

from src.channels import official


def response(text="", calls=()):
    msg = NS(content=text, tool_calls=[
        NS(id=f"c{i}", function=NS(name=name, arguments=json.dumps(args, ensure_ascii=False)))
        for i, (name, args) in enumerate(calls)
    ], reasoning_content=None)
    return NS(choices=[NS(message=msg)])


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.chat = NS(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.replies)


class Sender:
    def __init__(self):
        self.sent = []

    async def send(self, text, image_urls=None, reply_to=None, sticker_paths=None):
        self.sent.append(dict(text=text, images=image_urls or [], stickers=sticker_paths or []))


def event(mid, text, *, group=True, at=False, image=False):
    segments = [{"type": "text", "data": {"text": text}}]
    if at:
        segments.insert(0, {"type": "at", "data": {"qq": "9"}})
    if image:
        segments.append({"type": "image", "data": {"url": "https://example.com/a.png"}})
    return dict(message_id=mid, message_type="group" if group else "private",
                self_id=9, group_id=100, user_id=1,
                sender={"nickname": "甲"}, message=segments)


class AuditCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qbot_review_")
        self.addCleanup(self.tmp.cleanup)
        for target, attr, value in [
            (cs, "CAMPAIGNS_DIR", str(Path(self.tmp.name) / "campaigns")),
            (up, "PROFILES_DIR", str(Path(self.tmp.name) / "profiles")),
            (dc, "_conversations", defaultdict(list)),
            (dc, "_fail_streak", 0), (dc, "_cooldown_until", 0.0),
            (dc, "DEEPSEEK_ENABLE_SEARCH", True),
            (core, "DEEPSEEK_THINKING_NOTICE_SEC", 1.0),
            (tr, "ONEBOT_GROUP_BLACKLIST", set()), (tr, "ONEBOT_GROUP_WHITELIST", set()),
            (tr, "TRIGGER_RECOGNIZE_IMAGES", True),
        ]:
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.trigger = tr.TriggerController()
        for target in (tr, onebot):
            p = patch.object(target, "trigger", self.trigger)
            p.start()
            self.addCleanup(p.stop)

    def campaign(self, gid="g"):
        cs.create_campaign("测试团", gid)
        cs.ensure_player(gid, "a", "甲")
        cs.ensure_player(gid, "b", "乙")
        cs.set_initial_again(3, gid)

    async def test_onebot_local_commands_before_wake_prompt(self):
        """普通群聊 @ /help 必须走本地指令，不能请求模型。"""
        model_calls = []

        async def fake_chat(*args, **kwargs):
            model_calls.append(args)
            return "模型回答"

        sender = Sender()
        with patch.object(core, "chat", fake_chat), patch.object(onebot, "OneBotSender", return_value=sender):
            await onebot.OneBotClient()._process(event(1, "/help", at=True))
        self.assertEqual(len(model_calls), 0, "@ /help 被包装成 WAKE_PROMPT，未走本地指令")
        self.assertEqual(sender.sent[0]["text"], core.HELP_TEXT)

    async def test_official_burst_preserves_every_message(self):
        """首条处理期间到来的第二、第三条都必须保留。"""
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def handle(msg, sender):
            seen.append(msg.text)
            if msg.text == "第一条":
                started.set()
                await release.wait()

        bot = NS(_locks={}, _pending={}, api=NS())

        def message(mid, text):
            return NS(id=str(mid), content=text, group_openid="g", author=NS(member_openid="a"), attachments=[])

        with patch.object(official, "handle_message", handle):
            first = asyncio.create_task(official.QBot.on_group_at_message_create(bot, message(1, "第一条")))
            await started.wait()
            await official.QBot.on_group_at_message_create(bot, message(2, "第二条"))
            await official.QBot.on_group_at_message_create(bot, message(3, "第三条"))
            release.set()
            await first
        combined = "\n".join(seen)
        self.assertTrue(all(t in combined for t in ["第一条", "第二条", "第三条"]), seen)

    async def burst(self, *, group, dm=False):
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        if dm:
            cs.create_campaign("测试团", "onebot_group_100")

        async def handle(msg, sender):
            seen.append(msg.text)
            if msg.text == "第一条":
                started.set()
                await release.wait()

        client = onebot.OneBotClient()
        with patch.object(onebot, "handle_message", handle):
            first = asyncio.create_task(client._process(event(1, "第一条", group=group, at=dm)))
            await started.wait()
            await client._process(event(2, "第二条", group=group, at=dm))
            await client._process(event(3, "第三条", group=group, at=dm))
            release.set()
            await first
        combined = "\n".join(seen)
        self.assertTrue(all(t in combined for t in ["第一条", "第二条", "第三条"]), seen)

    async def test_onebot_private_burst_preserves_every_message(self):
        await self.burst(group=False)

    async def test_onebot_dm_burst_preserves_every_message(self):
        await self.burst(group=True, dm=True)

    async def test_search_switch_does_not_disable_local_tools(self):
        model = Model([response(calls=[("load_skill", {"name": "用户偏好"})]), response("收到")])
        with patch.object(dc, "client", model), patch.object(dc, "DEEPSEEK_ENABLE_SEARCH", False):
            await dc.chat("g", "a", "记住偏好")
        names = {t["function"]["name"] for t in model.requests[1].get("tools", [])}
        self.assertIn("manage_user_profile", names)
        self.assertNotIn("manage_campaign", names)  # 未加载的跑团技能仍不开放。
        self.assertNotIn("web_search", names)

    async def test_unadvertised_tools_cannot_execute(self):
        model = Model([response(calls=[("manage_user_profile", {"action": "remember", "info": "未授权写入"})]), response("好")])
        with patch.object(dc, "client", model), patch.object(dc, "get_definitions", return_value=[]):
            await dc.chat("g", "a", "只聊天")
        self.assertNotIn("未授权写入", up.list_profile("g", "a"))

    async def test_late_model_cannot_commit_history(self):
        """用真实 chat，模拟模型客户端吞掉取消后返回。"""
        class LateModel:
            async def create(self, **kwargs):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.02)
                return response("晚到回答")

        sender = Sender()
        with patch.object(dc, "client", NS(chat=NS(completions=LateModel()))), patch.object(core, "DEEPSEEK_THINKING_NOTICE_SEC", 0.02):
            processing = asyncio.create_task(core.handle_message(IncomingMessage(conv_id="g", user_id="a", text="在吗", is_at_bot=True), sender))
            await asyncio.sleep(0.01)
            processing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await processing
            await asyncio.sleep(0.06)
        self.assertFalse(sender.sent)
        self.assertEqual(dc.get_history_length("g"), 0, "超时后真实 chat 仍提交历史")

    async def test_cancelled_thread_cannot_write_profile(self):
        release, finished, started = threading.Event(), threading.Event(), threading.Event()
        real_execute = tools.execute

        def slow_execute(*args):
            if args[0] != "manage_user_profile":
                return real_execute(*args)
            started.set()
            release.wait(2)
            try:
                return real_execute(*args)
            finally:
                finished.set()

        model = Model([response(calls=[("load_skill", {"name": "用户偏好"})]), response(calls=[("manage_user_profile", {"action": "remember", "info": "超时后才写入"})])])
        with patch.object(dc, "client", model), patch.object(dc, "execute_tool", slow_execute), patch.object(core, "DEEPSEEK_THINKING_NOTICE_SEC", 0.1):
            try:
                processing = asyncio.create_task(core.handle_message(IncomingMessage(conv_id="g", user_id="a", text="记偏好", is_at_bot=True), Sender()))
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                processing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await processing
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 3)
        self.assertNotIn("超时后才写入", up.list_profile("g", "a"))

    async def test_rejected_send_keeps_unanswered_buffer(self):
        gid = "g"
        self.trigger.buffer.add(gid, tr.BufferEntry(speaker="甲", text="问题", message_id="1"))
        model = Model([response(calls=[("reply", {"text": "答案", "reply_to": "1"})]), response()])

        async def reject(*args, **kwargs):
            return None, onebot.API_REJECTED

        msg = IncomingMessage(channel="onebot", conv_id=gid, user_id="a", text="问题", is_at_bot=True,
                              ai_decides_reply=True, extra_ctx=self.trigger.buffer.peek(gid), wake_started_at=time.monotonic())
        sender = onebot.OneBotSender(NS(call_api_ex=reject), True, 100, 1)
        with patch.object(dc, "client", model):
            await core.handle_message(msg, sender)
        self.assertIn("问题", self.trigger.buffer.peek(gid), "拒绝发送也被当作已回答，缓冲被清空")

    async def test_multi_reply_sticker_budget_is_per_round(self):
        sender = Sender()

        async def fake_chat(*args, outbox=None, **kwargs):
            outbox.items.extend([ReplyItem("甲[img]local.png[/img]"), ReplyItem("乙[img]local.png[/img]")])
            return outbox.as_history_text()

        with patch.object(core, "chat", fake_chat), patch.object(core, "STICKER_MAX_PER_REPLY", 1), patch.object(core.stickers_utils, "resolve", return_value="local.png"):
            await core.handle_message(IncomingMessage(conv_id="g", user_id="a", text="问题", ai_decides_reply=True), sender)
        self.assertEqual(sum(len(s["stickers"]) for s in sender.sent), 1)

    async def test_image_rate_limit_cannot_fall_back_to_full_recognition(self):
        gid = "onebot_group_100"
        self.trigger._enter_session(gid)
        recognized = []

        def describe(*args):
            recognized.append(args)
            return "内容图片"

        async def fake_chat(*args, **kwargs):
            return "好"

        with patch.object(vu, "vision_available", return_value=True), patch.object(core, "vision_available", return_value=True), patch.object(vu, "_IMG_CACHE", OrderedDict()), patch.object(vu, "TRIGGER_IMAGE_RATE_PER_MIN", 1), patch.object(vu, "_IMG_RATE", {gid: deque([time.monotonic()])}), patch.object(core, "describe_image", describe), patch.object(core, "chat", fake_chat), patch.object(onebot, "OneBotSender", return_value=Sender()):
            await onebot.OneBotClient()._process(event(1, "看这张图", image=True))
        self.assertFalse(recognized, "预识别频控返回 None 后 core 又进行完整识别")

    def test_undo_fact_preserves_other_players_later_facts(self):
        self.campaign()
        cs.append_fact("甲打开门", "g", "a")
        cs.append_fact("乙拿起钥匙", "g", "b")
        cs.undo_last("g", "a")
        facts = cs._get_active("g")[1]["facts"]
        self.assertTrue(any("乙拿起钥匙" in f for f in facts), facts)

    def test_undo_state_preserves_other_players_later_changes(self):
        self.campaign()
        cs.update_state('{"甲HP": 8}', "g", "a")
        cs.update_state('{"乙HP": 6}', "g", "b")
        cs.undo_last("g", "a")
        self.assertEqual(cs._get_active("g")[1]["state"].get("乙HP"), 6)

    def test_load_campaign_clears_normal_chat_context(self):
        self.campaign()
        cs.exit_dm("g")
        self.trigger.buffer.add("g", tr.BufferEntry(text="普通闲聊"))
        self.trigger._enter_session("g")
        cs.load_campaign("测试团", "g")
        self.assertEqual(self.trigger.buffer.peek("g"), "")
        self.assertFalse(self.trigger.is_active("g"))

    def test_switching_mode_does_not_keep_previous_mode_history(self):
        dc._conversations["g"] = [{"role": "user", "content": "普通聊天旧记忆"}]
        cs.enter_dm("g")
        self.assertFalse(dc._conversations.get("g"))

    def test_campaign_view_can_read_beyond_prompt_limit(self):
        self.campaign()
        cs.append_lore("很长的设定" * 1500, "g")
        cs.update_state('{"关键HP": 7}', "g", "a")
        from src.tools.campaign_provider import _view_state
        self.assertTrue("关键HP" in _view_state("g"), "view 仍返回截断的 prompt，长设定挤掉了角色状态")

    def test_persistence_failure_is_not_reported_as_success(self):
        with patch.object(cs.os, "replace", side_effect=PermissionError("模拟写入失败")):
            result = cs.create_campaign("无法保存", "g")
        self.assertNotIn("已创建", result)

    def test_reset_clears_buffer_as_well_as_history(self):
        dc._conversations["g"] = [{"role": "user", "content": "旧记忆"}]
        self.trigger.buffer.add("g", tr.BufferEntry(text="旧记忆"))
        dc.clear_history("g")
        self.assertNotIn("旧记忆", self.trigger.buffer.peek("g"))

    def test_ssrf_check_and_request_share_the_same_dns_result(self):
        """假 DNS 与假 transport，展示两次解析之间的空隙；不联网。"""
        from src import net_safety as ns
        answers = iter(["93.184.216.34", "127.0.0.1"])

        def dns(*args, **kwargs):
            return [(2, 1, 6, "", (next(answers), 80))]

        reached = []
        class Body:
            status = 200
            headers = {}
            connection = None
            def read1(self, *args, **kwargs):
                return b""
            def close(self):
                pass
        class Pool:
            def urlopen(self, *args, **kwargs):
                return Body()
            def close(self):
                pass
        def connect(scheme, ip, port, host):
            reached.append((ip, host))
            return Pool()
        with patch.object(ns.socket, "getaddrinfo", side_effect=dns) as lookup, patch.object(ns, "_pool", connect), patch.dict(os.environ, {"TRUST_PROXY_URL": ""}):
            ns.safe_get(None, "http://changing.example/image.png")
        self.assertEqual(reached, [("93.184.216.34", "changing.example")])
        self.assertEqual(lookup.call_count, 1)

    def test_profile_scope_cannot_match_another_group_prefix(self):
        up.remember_info("乙群独有偏好", "a_b", "u", "乙")
        self.assertNotIn("乙群独有偏好", up.get_profile_for_prompt("a"))

    def test_atomic_replace_also_needs_transaction_serialization(self):
        self.campaign()
        first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        read_count = 0
        original = cs._get_active

        def overlapping_reads(gid):
            nonlocal read_count
            data = original(gid)
            with counter_lock:
                read_count += 1
                first = read_count == 1
            if first:
                first_read.set()
                release.wait(2)
            else:
                second_read.set()
            return data

        with patch.object(cs, "_get_active", overlapping_reads), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cs.append_fact, "甲的事实", "g", "a")
            first_read.wait(1)
            second = pool.submit(cs.append_fact, "乙的事实", "g", "b")
            # 正确的事务锁会让第二次读取等第一笔提交；无锁时两边先读到同一版本。
            second_read.wait(0.1)
            release.set()
            futures = [first, second]
            for future in futures:
                future.result(timeout=3)
        self.assertEqual(len(cs._get_active("g")[1]["facts"]), 2, "原子替换未保护读改写事务，两次成功返回只保存了一条")

    async def test_transient_status_timeout_does_not_disable_online_probes(self):
        client = onebot.OneBotClient()

        async def call(action, params):
            return None if action == "get_status" else {"user_id": 9}

        with patch.object(client, "call_api", call):
            await client._probe()
        self.assertTrue(client._probe_online, "一次 get_status 超时、get_login_info 成功就永久停止在线状态检测")

    def test_reference_cache_total_limit_also_applies_on_load(self):
        path = Path(self.tmp.name) / "refs.jsonl"
        path.write_text("\n".join(json.dumps({"k": str(i), "v": {"owner": str(i), "timestamp": str(i)}}) for i in range(3)), encoding="utf-8")
        with patch.object(official, "_REF_INDEX_FILE", str(path)), patch.object(official, "_MSG_IDX_CACHE_MAX_TOTAL", 2):
            loaded = official._load_ref_index()
        self.assertLessEqual(len(loaded), 2)

    def test_campaign_mutation_registers_its_actor(self):
        cs.create_campaign("测试团", "g")
        tools.execute("manage_campaign", '{"action":"log","content":"甲进了房间"}', tools.ChatContext(conv_id="g", user_id="a", user_name="甲"))
        self.assertIn(cs._player_key("g", "a"), cs._get_active("g")[1]["players"], "只有第一次 /again 才会登记玩家，正常推进后仍无法奖励玩家")


if __name__ == "__main__":
    unittest.main(verbosity=2)
