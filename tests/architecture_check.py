"""Offline behavioral checks for the audit's additional architecture findings."""
import asyncio
import io
import ipaddress
import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review_regressions as audit
from review_regressions import Sender, Model, response, event
from src import runtime, campaign_state as cs, user_profile as up, deepseek_client as dc
from src import tools, config, vision_utils as vu, net_safety as ns
from src.channels import core, onebot, official
from src.channels.base import IncomingMessage, SendResult
from src.channels.scheduler import ConversationQueue
from src.trigger import BufferEntry
from src.tools.chat_provider import ReplyItem


class ArchitectureCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp
    campaign = audit.AuditCases.campaign

    def test_round_changes_are_tentative_and_reset_revokes_them(self):
        lease = runtime.RoundLease("g", 10)
        with runtime.use(lease):
            up.remember_info("暂存偏好", "g", "a")
            self.assertIn("暂存偏好", up.list_profile("g", "a"))
        self.assertNotIn("暂存偏好", up.list_profile("g", "a"))
        dc.clear_history("g")
        with runtime.use(lease), self.assertRaises(runtime.RoundExpired):
            lease.commit()
        self.assertNotIn("暂存偏好", up.list_profile("g", "a"))

    def test_concurrent_local_change_rejects_stale_round_commit(self):
        self.campaign()
        lease = runtime.RoundLease("g", 10)
        with runtime.use(lease):
            cs.append_fact("模型旧版本", "g", "a")
        cs.append_fact("本地新版本", "g", "b")
        with runtime.use(lease), self.assertRaises(RuntimeError):
            lease.commit()
        facts = cs._get_active("g")[1]["facts"]
        self.assertEqual(len(facts), 1)
        self.assertIn("本地新版本", facts[0])
        lease.revoke()

    def test_multiple_state_files_roll_back_when_commit_fails(self):
        up.remember_info("原始甲", "g", "a")
        up.remember_info("原始乙", "g", "b")
        lease = runtime.RoundLease("g", 10)
        with runtime.use(lease):
            up.remember_info("新甲", "g", "a")
            up.remember_info("新乙", "g", "b")
        original = runtime._atomic
        calls = 0
        def fail_second(path, text):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(path, text)
        with runtime.use(lease), patch.object(runtime, "_atomic", fail_second), self.assertRaises(OSError):
            lease.commit()
        lease.revoke()
        self.assertNotIn("新甲", up.list_profile("g", "a"))
        self.assertNotIn("新乙", up.list_profile("g", "b"))

    def test_tool_error_rolls_back_its_tentative_writes(self):
        registry = tools.ToolRegistry()
        class Broken(tools.ToolProvider):
            name = "broken"
            def get_tools(self):
                def fail():
                    up.remember_info("不应存在", "g", "a")
                    raise ValueError("bad")
                return [dict(name="broken", description="", parameters={"type":"object"}, handler=fail)]
        registry.register_provider(Broken())
        lease = runtime.RoundLease("g", 10)
        with runtime.use(lease):
            self.assertIn("失败", registry.execute("broken", "{}"))
            lease.commit()
        self.assertNotIn("不应存在", up.list_profile("g", "a"))

    def test_same_name_authors_require_verified_message_source(self):
        context = tools.ChatContext(conv_id="g", user_id="b", user_name="同名",
            extra={"actors":{"m1":("a", "同名"), "m2":("b", "同名")}})
        for source in (None, "forged"):
            args = {"action":"remember", "info":"甲的偏好"}
            if source:
                args["source_message_id"] = source
            self.assertIn("失败", tools.execute("manage_user_profile", json.dumps(args), context))
        args["source_message_id"] = "m1"
        self.assertIn("已记住", tools.execute("manage_user_profile", json.dumps(args), context))
        self.assertIn("甲的偏好", up.list_profile("g", "a"))
        self.assertNotIn("甲的偏好", up.list_profile("g", "b"))

    def test_schema_checks_required_enum_boolean_and_nonfinite_numbers(self):
        definitions = tools.get_definitions()
        route = next(d for d in definitions if d["function"]["name"] == "plan_route")
        prepare = tools.ToolRegistry._prepare_args
        for args in ({"origin":"x"}, {"origin":"x","destination":"y","mode":"flying"},
                     {"origin":"x","destination":"y","strategy":True},
                     {"origin":"x","destination":"y","strategy":float("nan")},
                     {"origin":"x","destination":"y","strategy":1.5}):
            self.assertIsNotNone(prepare(route, args)[1], args)

    def test_undo_rejects_overlapping_later_edits_without_spending_count(self):
        self.campaign()
        cs.update_state('{"hp":10}', "g", "a")
        cs.update_state('{"hp":8}', "g", "b")
        before = cs._get_active("g")[1]
        self.assertIn("冲突", cs.undo_last("g", "a"))
        self.assertEqual(cs._get_active("g")[1], before)

    def test_one_decision_log_and_update_undo_together(self):
        self.campaign()
        lease = runtime.RoundLease("g", 10)
        with runtime.use(lease):
            cs.append_fact("甲打开门", "g", "a")
            cs.update_state('{"scene":"门后"}', "g", "a")
            lease.commit()
        self.assertEqual(len(cs._get_active("g")[1]["history_stack"]), 1)
        cs.undo_last("g", "a")
        campaign = cs._get_active("g")[1]
        self.assertEqual(campaign["facts"], [])
        self.assertEqual(campaign["state"], {})
        self.assertEqual(campaign["players"]["g:a"]["again_remaining"], 2)

    def test_prompt_prioritizes_dice_over_long_rules_and_lore(self):
        self.campaign()
        cs.set_rules("规则" * 5000, "g")
        cs.append_lore("背景" * 5000, "g")
        cs.record_roll("1d20", "真实骰点=17", "g", "a", "甲")
        text = cs.get_state_for_prompt("g")
        self.assertIn("真实骰点=17", text)
        self.assertLess(len(text), 6200)
        full = cs.view_state("g", "lore", 3)
        self.assertIn("第 3/", full)

    async def test_quote_enrichment_cannot_overtake_earlier_event(self):
        client = onebot.OneBotClient()
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def quote(_):
            started.set()
            await release.wait()
            return None, []
        async def handle(msg, sender):
            seen.append(msg.text)
        first = event(1, "先来", group=False)
        first["message"].insert(0, {"type":"reply", "data":{"id":"7"}})
        with patch.object(client, "_quote_info", quote), patch.object(onebot, "handle_message", handle):
            task = asyncio.create_task(client._process(first))
            await started.wait()
            await client._process(event(2, "后来", group=False))
            self.assertEqual(seen, [])
            release.set()
            await task
        self.assertEqual(seen, ["先来", "后来"])

    async def test_queue_capacity_is_explicit_and_does_not_overwrite(self):
        queue = ConversationQueue(per_conversation=1)
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def handle(value):
            seen.append(value)
            if value == 1:
                started.set()
                await release.wait()
        task = asyncio.create_task(queue.submit("g", 1, handle))
        await started.wait()
        await queue.submit("g", 2, handle)
        with self.assertRaises(asyncio.QueueFull):
            await queue.submit("g", 3, handle)
        release.set()
        await task
        self.assertEqual(seen, [1, 2])
        self.assertEqual(queue.queues, {})

    async def test_command_with_images_never_calls_vision_or_model(self):
        msg = IncomingMessage(conv_id="g", user_id="a", text="/help",
            image_urls=["https://invalid.example/x"], is_at_bot=True, ai_decides_reply=True)
        with patch.object(core, "_image_text", new_callable=AsyncMock) as vision, patch.object(core, "chat", new_callable=AsyncMock) as model:
            sender = Sender()
            await core.handle_message(msg, sender)
            self.assertEqual(sender.sent[0]["text"], core.HELP_TEXT)
            vision.assert_not_called()
            model.assert_not_called()

    async def test_partial_reply_batch_consumes_only_confirmed_source(self):
        for mid in ("1", "2"):
            self.trigger.buffer.add("g", BufferEntry(text=f"问题{mid}", message_id=mid, user_id=mid))
        model = Model([response(calls=[("reply",{"text":"答一","reply_to":"1"}),
                                     ("reply",{"text":"答二","reply_to":"2"})]), response()])
        class Delivery:
            calls = 0
            async def send(self, text, **kwargs):
                self.calls += 1
                return SendResult("ok", text) if self.calls == 1 else SendResult("rejected")
        with patch.object(dc, "client", model):
            await core.handle_message(IncomingMessage(conv_id="g", user_id="2", text="问题2", ai_decides_reply=True), Delivery())
        self.assertEqual(self.trigger.buffer.ids("g"), {"2"})
        self.assertEqual(dc._conversations["g"][-1]["content"], "答一")

    async def test_unknown_delivery_is_recorded_and_never_retried(self):
        model = Model([response("【甲】" + "长" * 100)])
        calls = []
        async def api(action, params):
            calls.append(params)
            return None, onebot.API_UNKNOWN
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, "g", "a")
        sender.max_text_len = 20
        with patch.object(dc, "client", model):
            await core.handle_message(IncomingMessage(conv_id="g", user_id="a", user_name="甲", text="问题", is_at_bot=True), sender)
        self.assertEqual(len(calls), 1)
        saved = dc._conversations["g"][-1]["content"]
        self.assertIn("unknown", saved)
        self.assertNotIn("【甲】", saved)
        self.assertIn(calls[0]["message"][-1]["data"]["text"], saved)

    async def test_official_sequences_increment_and_timeout_has_no_retry(self):
        calls = []
        async def send(**kwargs):
            calls.append(kwargs)
            return {}
        sender = official.OfficialSender(NS(group_openid="g", id="m"), NS(post_group_message=send))
        await sender.send("一")
        await sender.send("二")
        self.assertEqual([call["msg_seq"] for call in calls], [1, 2])
        async def timeout():
            calls.append({})
            raise asyncio.TimeoutError()
        result = await official._send_once(timeout, "未知")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(calls), 3)

    async def test_official_write_transport_bypasses_sdk_hidden_retries(self):
        calls = []
        class Manager:
            async def __aenter__(self):
                raise ConnectionResetError("reset after write")
            async def __aexit__(self, *args):
                return False
        def post(*args, **kwargs):
            calls.append(kwargs)
            return Manager()
        token = NS(check_token=AsyncMock(), get_string=lambda:"fake-token", app_id="fake-id")
        sdk_request = AsyncMock(side_effect=AssertionError("must not call SDK retry transport"))
        api = NS(_http=NS(_token=token, is_sandbox=False, request=sdk_request),
                 _qbot_write_session=NS(closed=False, post=post))
        receipt = await official._safe_send_group(api, "g", "m", "hello")
        self.assertEqual(receipt.status, "unknown")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["allow_redirects"])
        sdk_request.assert_not_called()

    async def test_missing_official_receipt_is_never_success(self):
        async def missing():
            return None
        receipt = await official._send_once(missing, "hello")
        self.assertEqual(receipt.status, "unknown")

    async def test_websocket_cancel_releases_echo_future(self):
        sent = asyncio.Event()
        async def write(_):
            sent.set()
        client = onebot.OneBotClient()
        client._ws = NS(closed=False, send_str=write)
        task = asyncio.create_task(client.call_api_ex("get_status", {}))
        await sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client._pending, {})

    async def test_websocket_reader_exception_cleans_connection_and_waiters(self):
        client = onebot.OneBotClient()
        future = asyncio.get_running_loop().create_future()
        client._pending["old"] = future
        class WebSocket:
            closed = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise RuntimeError("reader failed")
        class Manager:
            def __init__(self, value):
                self.value = value
            async def __aenter__(self):
                return self.value
            async def __aexit__(self, *args):
                return False
        session = NS(ws_connect=lambda *a, **kw: Manager(WebSocket()))
        with patch.object(onebot.aiohttp, "ClientSession", return_value=Manager(session)):
            with self.assertRaisesRegex(RuntimeError, "reader failed"):
                await client._connect_and_serve()
        self.assertIsNone(client._ws)
        self.assertEqual(client._pending, {})
        self.assertIsInstance(future.exception(), ConnectionError)

    def test_tls_pinning_preserves_hostname_verification(self):
        with patch.object(ns.urllib3, "HTTPSConnectionPool") as pool:
            ns._pool("https", "93.184.216.34", 443, "example.com")
        args, kwargs = pool.call_args
        self.assertEqual(args[:2], ("93.184.216.34", 443))
        self.assertEqual(kwargs["assert_hostname"], "example.com")
        self.assertEqual(kwargs["server_hostname"], "example.com")
        self.assertEqual(kwargs["cert_reqs"], "CERT_REQUIRED")

    def test_network_size_and_encoding_limits_close_transport(self):
        for headers, body in (({}, b"x"*5), ({"Content-Length":"5"}, b""), ({"Content-Encoding":"gzip"}, b"")):
            stream = NS(status=200, headers=headers, connection=None,
                read1=lambda *a, **kw: body, close=lambda: None)
            with patch.object(stream, "close") as close, patch.object(ns, "_resolve", return_value=("http://x/", NS(scheme="http",path="/",query="",port=None),"x",80,["93.184.216.34"])), patch.object(ns, "_pool") as create_pool, patch.dict(os.environ, {"TRUST_PROXY_URL":""}):
                create_pool.return_value.urlopen.return_value = stream
                with self.assertRaises(ns.UnsafeURLError):
                    ns.safe_get(None, "http://x/", max_bytes=4)
                close.assert_called_once()
                create_pool.return_value.close.assert_called_once()

    def test_fake_ip_requires_explicit_trusted_proxy(self):
        dns = [(2,1,6,"",("198.18.0.1",80))]
        with patch.object(ns.socket, "getaddrinfo", return_value=dns):
            with patch.dict(os.environ, {"TRUST_PROXY_URL":""}), self.assertRaises(ns.UnsafeURLError):
                ns.check_url("http://proxy.example/")
            with patch.dict(os.environ, {"TRUST_PROXY_URL":"http://trusted-proxy:8080"}):
                self.assertEqual(ns.check_url("http://proxy.example/"), "http://proxy.example/")

    def test_concurrent_image_requests_share_one_vlm_call(self):
        vu._IMG_CACHE.clear()
        calls = []
        def recognize(*args):
            calls.append(args)
            time.sleep(.02)
            return "同一个描述"
        with patch.object(vu, "_describe_image_uncached", recognize), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: vu.describe_image("data:test", "同一个问题"), range(4)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, ["同一个描述"]*4)
        vu._IMG_CACHE.clear()

    def test_history_cache_and_rate_cache_are_bounded(self):
        for index in range(501):
            dc._store_history(str(index), [{"role":"user","content":"x"}]*100)
        self.assertEqual(len(dc._conversations), 500)
        self.assertLessEqual(len(dc._conversations["500"]), dc.MAX_HISTORY_LENGTH*2+2)
        from collections import deque
        with patch.object(vu, "_IMG_RATE", {str(i):deque([time.monotonic()-61]) for i in range(201)}):
            vu._take_rate("new", 1)
            self.assertEqual(len(vu._IMG_RATE), 1)

    def test_invalid_configuration_and_explicit_thinking_disable(self):
        for name, value in (("MAX_HISTORY_LENGTH",0),("TRIGGER_CONTEXT_MESSAGES",-1),("ONEBOT_MAX_CONCURRENCY",0),("TRIGGER_PROBABILITY",float("nan"))):
            with patch.object(config, name, value), self.assertRaises(ValueError):
                config.validate()
        with patch.object(dc, "DEEPSEEK_ENABLE_THINKING", False):
            self.assertEqual(dc._build_request_kwargs([], None)["extra_body"]["thinking"], {"type":"disabled"})

    def test_legacy_migration_keeps_original_and_refuses_overwrite(self):
        from src.migrate_data import migrate
        source = Path(self.tmp.name) / "legacy.md"
        source.write_text("# 用户画像\n- [旧时间] 旧偏好\n", encoding="utf-8")
        target = migrate(source, "a_b", "c:d", "甲")
        self.assertTrue(source.exists())
        self.assertIn("旧偏好", up.get_profile_for_prompt("a_b"))
        self.assertNotIn("旧偏好", up.get_profile_for_prompt("a"))
        with self.assertRaises(FileExistsError):
            migrate(source, "a_b", "c:d", "甲")
        self.assertNotEqual(up._get_profile_path("a_b", "c"), up._get_profile_path("a", "b_c"))
        self.assertNotEqual(cs._get_file_path("a:b"), cs._get_file_path("a%3Ab"))
        self.assertTrue(target.exists())

    def test_legacy_campaign_migration_does_not_reset_staged_updates(self):
        group_id = "onebot_group_123"
        legacy = Path(cs.CAMPAIGNS_DIR)/(group_id + ".json")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        data = cs._empty_group_state()
        data["dm_mode"] = True
        data["active_campaign"] = "old"
        data["campaigns"]["old"] = cs._empty_campaign("old")
        legacy.write_text(json.dumps(data), encoding="utf-8")
        lease = runtime.RoundLease(group_id, 10)
        with runtime.use(lease):
            cs.append_fact("第一条", group_id, "a")
            cs.append_fact("第二条", group_id, "a")
            cs.update_state('{"hp":8}', group_id, "a")
            lease.commit()
        loaded = cs._get_active(group_id)[1]
        self.assertEqual(len(loaded["facts"]), 2)
        self.assertEqual(loaded["state"]["hp"], 8)
        self.assertTrue(legacy.exists())

    async def test_fallback_never_exposes_reasoning(self):
        result = response()
        result.choices[0].message.reasoning_content = "PRIVATE_REASONING"
        with patch.object(dc, "client", Model([result])), self.assertRaises(RuntimeError):
            await dc._fallback_summarize([])

    def test_recovery_probe_has_only_one_owner(self):
        with patch.object(dc, "_cooldown_until", time.monotonic()-1), patch.object(dc, "_recovery_probe", False):
            self.assertTrue(dc.acquire_service_probe())
            self.assertFalse(dc.acquire_service_probe())
            dc.release_service_probe()
            self.assertTrue(dc.acquire_service_probe())
        self.assertFalse(dc.is_service_failure(ValueError("bad argument")))
        self.assertFalse(dc.is_service_failure(PermissionError("disk")))
        self.assertTrue(dc.is_service_failure(ConnectionError("offline")))

    async def test_cancelled_worker_retains_io_slot_until_it_finishes(self):
        release = threading.Event()
        tasks = [asyncio.create_task(runtime.run_blocking(release.wait, 2)) for _ in range(8)]
        try:
            await asyncio.sleep(.05)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with self.assertRaises(RuntimeError):
                await runtime.run_blocking(lambda: "should not start")
        finally:
            release.set()
            await asyncio.sleep(.05)
        self.assertEqual(await runtime.run_blocking(lambda: "done"), "done")

    async def test_unsubmitted_media_timeout_is_not_unknown_delivery(self):
        sender = onebot.OneBotSender(NS(), True, "g", "a")
        with patch.object(runtime, "run_blocking", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            receipt = await core._deliver(sender, "[img]https://example.com/x.png[/img]", None, [1, "甲"])
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(receipt.images, ())

    async def test_total_queue_budget_counts_active_conversations(self):
        queue = ConversationQueue(total=1)
        queue.owners.add(object())
        with self.assertRaises(asyncio.QueueFull):
            await queue.submit("another", "new", AsyncMock())

    def test_corrupt_backup_failure_preserves_original(self):
        path = Path(cs._get_file_path("g"))
        path.write_text('{"broken":', encoding="utf-8")
        with patch.object(cs.os, "replace", side_effect=PermissionError("denied")):
            result = cs.create_campaign("new", "g")
        self.assertIn("失败", result)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"broken":')

    async def test_slow_response_headers_respect_download_deadline(self):
        import socketserver
        received = threading.Event()
        class Slow(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                received.set()
                try:
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                        self.request.sendall(bytes([byte]))
                        time.sleep(.02)
                except OSError:
                    pass
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Slow)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        start = time.monotonic()
        try:
            with patch.object(ns, "_is_public_ip", return_value=True), patch.dict(os.environ, {"TRUST_PROXY_URL":""}):
                with self.assertRaises((ns.urllib3.exceptions.HTTPError, OSError, TimeoutError)):
                    await asyncio.to_thread(ns.safe_get, None, f"http://127.0.0.1:{server.server_address[1]}/", timeout=.15)
            self.assertLess(time.monotonic()-start, .6)
            self.assertTrue(received.is_set())
            self.assertGreater(time.monotonic()-start, .1)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()

    def test_rotating_logger_preserves_console_and_limits_files(self):
        from src.runner import LoggedStream
        from logging.handlers import RotatingFileHandler
        import logging
        path = Path(self.tmp.name)/"test.log"
        logger = logging.getLogger("architecture-test")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(path, maxBytes=80, backupCount=2, encoding="utf-8")
        logger.addHandler(handler)
        console = io.StringIO()
        stream = LoggedStream(console, logger)
        try:
            for _ in range(20):
                stream.write("a line of output\n")
            stream.flush()
        finally:
            handler.close()
            logger.removeHandler(handler)
        self.assertEqual(console.getvalue().count("a line"), 20)
        self.assertLessEqual(len(list(path.parent.glob("test.log*"))), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
