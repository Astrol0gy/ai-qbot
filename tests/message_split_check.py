"""Separate sticker delivery, precise receipts and shared round budgets."""
import asyncio
from collections import defaultdict
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import deepseek_client as dc, runtime, stickers_utils
from src.channels import core, onebot, official
from src.channels.base import IncomingMessage
from src.tools.chat_provider import ReplyItem
from src.trigger import BufferEntry


class DeliveryCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, name, value in (
            (stickers_utils, "resolve", lambda path: str(path)),
            (stickers_utils, "mark_used", lambda path: None),
            (stickers_utils, "fallback_text", lambda path: "（无语）"),
            (dc, "_conversations", defaultdict(list)),
            (dc, "_cooldown_until", 0),
        ):
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_group_and_private_stickers_are_separate_messages(self):
        for is_group in (True, False):
            with self.subTest(is_group=is_group):
                api = AsyncMock(return_value=({}, onebot.API_OK))
                sender = onebot.OneBotSender(NS(call_api_ex=api), is_group, 10, 20)
                with patch.object(stickers_utils, "mark_used") as used:
                    result = await sender.send("hello", reply_to="42", sticker_paths=["one.png", "two.png"])
                calls = api.call_args_list
                self.assertEqual(len(calls), 3)
                action = "send_group_msg" if is_group else "send_private_msg"
                self.assertTrue(all(call.args[0] == action for call in calls))
                messages = [call.args[1]["message"] for call in calls]
                self.assertEqual([segment["type"] for segment in messages[0]], ["reply", "text"])
                for message, path in zip(messages[1:], ("one.png", "two.png")):
                    self.assertEqual(message, [{"type": "reply", "data": {"id": "42"}},
                                               {"type": "image", "data": {"file": path}}])
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.text, "hello")
                self.assertEqual(result.stickers, ("one.png", "two.png"))
                self.assertEqual(used.call_count, 2)

    async def test_rejected_sticker_preserves_delivered_text(self):
        api = AsyncMock(side_effect=[({}, "ok"), (None, "rejected")])
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(stickers_utils, "mark_used") as used:
            receipt = await sender.send("hello", sticker_paths=["one.png"])
        self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("partial", "hello", ()))
        self.assertEqual(sender.interrupted_result(), receipt)
        used.assert_not_called()

    async def test_sticker_only_success_never_sends_text(self):
        for group in (True, False):
            api = AsyncMock(return_value=({}, "ok"))
            sender = onebot.OneBotSender(NS(call_api_ex=api), group, 10, 20)
            receipt = await sender.send("", sticker_paths=["one.png"])
            self.assertEqual(api.await_count, 1)
            self.assertEqual(api.call_args.args[1]["message"],
                             [{"type": "image", "data": {"file": "one.png"}}])
            self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("ok", "", ("one.png",)))

    async def test_sticker_only_failure_sends_text_once(self):
        for status in ("offline", "rejected"):
            for group in (True, False):
                with self.subTest(status=status, group=group):
                    api = AsyncMock(side_effect=[(None, status), ({}, "ok")])
                    sender = onebot.OneBotSender(NS(call_api_ex=api), group, 10, 20)
                    with patch.object(stickers_utils, "mark_used") as used:
                        receipt = await sender.send("", reply_to="42", sticker_paths=["one.png"])
                    self.assertEqual(api.await_count, 2)
                    self.assertEqual(api.call_args.args[1]["message"], [
                        {"type": "reply", "data": {"id": "42"}},
                        {"type": "text", "data": {"text": "（无语）"}}])
                    self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("ok", "（无语）", ()))
                    used.assert_not_called()

    async def test_sticker_and_text_failure_stop_without_retries(self):
        for status in ("offline", "rejected", "unknown"):
            api = AsyncMock(side_effect=[(None, "rejected"), (None, status)])
            sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
            receipt = await sender.send("", sticker_paths=["one.png", "two.png"])
            self.assertEqual(api.await_count, 2)
            self.assertEqual(receipt.status, status)
            self.assertEqual(receipt.stickers, ())
            self.assertEqual(receipt.text, "（无语）" if status == "unknown" else "")

    async def test_unknown_sticker_only_does_not_send_fallback(self):
        api = AsyncMock(return_value=(None, "unknown"))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        receipt = await sender.send("", sticker_paths=["one.png"])
        self.assertEqual(api.await_count, 1)
        self.assertEqual(receipt.status, "unknown")
        self.assertEqual(receipt.text, "")

    async def test_missing_sticker_falls_back_to_text(self):
        api = AsyncMock(return_value=({}, "ok"))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(stickers_utils, "resolve", return_value=None):
            receipt = await sender.send("", sticker_paths=["one.png"])
        self.assertEqual(api.await_count, 1)
        self.assertEqual(receipt.text, "（无语）")
        self.assertEqual(receipt.stickers, ())

    async def test_unsupported_official_sticker_uses_one_text_attempt(self):
        for status in ("ok", "rejected"):
            sender = official.OfficialSender(NS(group_openid="g", id="m"), None)
            from src.channels.base import SendResult
            fallback = AsyncMock(return_value=SendResult(status, "（无语）" if status == "ok" else ""))
            with patch.object(official, "_safe_send_group", fallback):
                receipt = await sender.send("", sticker_paths=["one.png"])
            self.assertEqual(fallback.await_count, 1)
            self.assertEqual(fallback.call_args.args[3], "（无语）")
            self.assertEqual(receipt.status, status)

    async def test_core_records_fallback_and_abandons_remaining_on_failure(self):
        for text_ok in (True, False):
            gid = f"fallback-{text_ok}"
            api = AsyncMock(side_effect=[(None, "rejected"),
                ({}, "ok") if text_ok else (None, "rejected")])
            sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
            async def model(*args, outbox=None, **kwargs):
                outbox.items.append(ReplyItem("[img]one.png[/img]", "42"))
                if not text_ok:
                    outbox.items.append(ReplyItem("must not send", "42"))
                runtime.current().history = (gid, [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "[img]one.png[/img]"}])
                return "pending"
            incoming = IncomingMessage(conv_id=gid, user_id="20", text="hello",
                ai_decides_reply=True, context_entries=(BufferEntry(user_id="20", text="hello", message_id="42"),))
            with patch.object(core, "chat", model), patch.object(core, "STICKER_MAX_PER_REPLY", 1):
                await core.handle_message(incoming, sender)
            self.assertEqual(api.await_count, 2)
            if text_ok:
                self.assertEqual(dc._conversations[gid][-1]["content"], "（无语）")
            else:
                self.assertNotIn(gid, dc._conversations)

    async def test_unknown_sticker_is_not_retried_or_followed(self):
        api = AsyncMock(side_effect=[({}, "ok"), (None, "unknown")])
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(stickers_utils, "mark_used") as used:
            receipt = await sender.send("hello", sticker_paths=["one.png", "two.png"])
        self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("unknown", "hello", ("one.png",)))
        self.assertEqual(api.await_count, 2)
        used.assert_not_called()

    async def test_offline_sticker_retry_never_resends_text(self):
        api = AsyncMock(side_effect=[({}, "ok"), (None, "offline"), ({}, "ok")])
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(onebot.asyncio, "sleep", new=AsyncMock()), \
             patch.object(stickers_utils, "mark_used") as used:
            receipt = await sender.send("hello", sticker_paths=["one.png"])
        self.assertEqual(receipt.status, "ok")
        messages = [call.args[1]["message"] for call in api.call_args_list]
        self.assertEqual(messages[0], [{"type": "text", "data": {"text": "hello"}}])
        self.assertEqual(messages[1:], [[{"type": "image", "data": {"file": "one.png"}}]] * 2)
        used.assert_called_once_with("one.png")

    async def test_cancelled_offline_wait_keeps_only_confirmed_text(self):
        api = AsyncMock(side_effect=[({}, "ok"), (None, "offline")])
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(onebot.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await sender.send("hello", sticker_paths=["one.png"])
        receipt = sender.interrupted_result()
        self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("partial", "hello", ()))

    async def test_interrupted_sticker_retains_earlier_text(self):
        calls = []
        async def send(action, params):
            calls.append(params)
            if len(calls) == 1:
                return {}, "ok"
            await asyncio.Event().wait()
        sender = onebot.OneBotSender(NS(call_api_ex=send), True, 10, 20)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(sender.send("hello", sticker_paths=["one.png"]), .05)
        receipt = sender.interrupted_result()
        self.assertEqual((receipt.status, receipt.text, receipt.stickers), ("unknown", "hello", ("one.png",)))
        self.assertEqual(len(calls), 2)

    async def test_round_budget_and_history_cover_split_deliveries(self):
        api = AsyncMock(return_value=({}, "ok"))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        async def model(*args, outbox=None, **kwargs):
            outbox.items.extend([ReplyItem("first [img]one.png[/img]", "42"),
                                 ReplyItem("second [img]two.png[/img]", "42")])
            runtime.current().history = ("split-test", [
                {"role": "user", "content": "hello"}, {"role": "assistant", "content": "pending"}])
            return "pending"
        incoming = IncomingMessage(conv_id="split-test", user_id="20", text="hello",
            ai_decides_reply=True, context_entries=(BufferEntry(user_id="20", text="hello", message_id="42"),))
        with patch.object(core, "chat", model), patch.object(core, "STICKER_MAX_PER_REPLY", 1):
            await core.handle_message(incoming, sender)
        self.assertEqual(api.await_count, 3)
        content = dc._conversations["split-test"][-1]["content"]
        self.assertIn("first", content)
        self.assertIn("second", content)
        self.assertIn("one.png", content)
        self.assertNotIn("two.png", content)
        for call in api.call_args_list:
            message = call.args[1]["message"]
            self.assertFalse(any(s["type"] == "image" for s in message)
                             and any(s["type"] == "text" for s in message))


if __name__ == "__main__":
    unittest.main(verbosity=2)
