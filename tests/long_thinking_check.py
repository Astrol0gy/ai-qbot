"""Long thinking notices use a scaled clock and fake APIs, never real QQ."""
import asyncio
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, runtime
from src.channels import core, official
from src.channels.base import IncomingMessage, SendResult


class NoticeSender:
    def __init__(self, fail_notice=False):
        self.sent = []
        self.notice = asyncio.Event()
        self.fail_notice = fail_notice

    async def send(self, text, **kwargs):
        self.sent.append(text)
        if text.startswith('正在思考'):
            self.notice.set()
            if self.fail_notice:
                raise ConnectionError('notice delivery failed')
        return SendResult('ok', text)


class ThinkingCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp

    async def exercise_wait(self, *, private=False, fail_notice=False):
        release = asyncio.Event()
        leases = []
        async def create(**kwargs):
            leases.append(runtime.current())
            await release.wait()
            return audit.response('最终答案')
        sender = NoticeSender(fail_notice)
        model = NS(chat=NS(completions=NS(create=create)))
        with patch.object(dc, 'client', model), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01):
            processing = asyncio.create_task(core.handle_message(IncomingMessage(
                conv_id='g', user_id='a', text='问题', is_group=not private, is_at_bot=not private), sender))
            try:
                await asyncio.wait_for(sender.notice.wait(), 1)
                await asyncio.sleep(.04)
                self.assertFalse(processing.done(), 'notice must not cancel the model')
                self.assertTrue(leases[0].valid)
                self.assertTrue(math.isinf(leases[0].deadline))
                self.assertEqual(sender.sent, ['正在思考中，请稍等……'])
            finally:
                release.set()
                await asyncio.wait_for(processing, 1)
        self.assertEqual(sender.sent, ['正在思考中，请稍等……', '最终答案'])
        history = dc._conversations['g']
        self.assertEqual(history[-1]['content'], '最终答案')
        self.assertNotIn('正在思考', str(history))

    async def test_at_group_continues_after_one_notice(self):
        await self.exercise_wait()

    async def test_private_chat_continues_after_one_notice(self):
        await self.exercise_wait(private=True)

    async def test_failed_notice_does_not_cancel_final_answer(self):
        await self.exercise_wait(fail_notice=True)

    async def test_unsolicited_wake_stays_silent_during_wait(self):
        async def model(*args, **kwargs):
            await asyncio.sleep(.04)
            return '[skip]'
        sender = NoticeSender()
        with patch.object(core, 'chat', model), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='a',
                text='群友聊天', ai_decides_reply=True, is_interjection=True), sender)
        self.assertEqual(sender.sent, [])

    async def test_real_error_ends_round_without_committing_history(self):
        async def create(**kwargs):
            await asyncio.sleep(.04)
            raise ConnectionError('upstream disconnected')
        sender = NoticeSender()
        output = io.StringIO()
        with patch.object(dc, 'client', NS(chat=NS(completions=NS(create=create)))), \
             patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01), contextlib.redirect_stdout(output):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='a', text='问题', is_at_bot=True), sender)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn('连不上模型', sender.sent[-1])
        self.assertEqual(dc.get_history_length('g'), 0)
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertTrue(any(e['event'] == 'model.round.finish' and e['status'] == 'error' for e in events))
        self.assertFalse(any(e['event'] == 'model.round.timeout' for e in events))

    async def test_other_conversation_can_answer_while_one_waits(self):
        release = asyncio.Event()
        async def model(gid, *args, **kwargs):
            if gid == 'slow':
                await release.wait()
            return '答案'
        slow, fast = NoticeSender(), NoticeSender()
        with patch.object(core, 'chat', model), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01):
            pending = asyncio.create_task(core.handle_message(IncomingMessage(
                conv_id='slow', user_id='a', text='问题', is_at_bot=True), slow))
            try:
                await asyncio.wait_for(slow.notice.wait(), 1)
                await asyncio.wait_for(core.handle_message(IncomingMessage(
                    conv_id='fast', user_id='b', text='问题', is_at_bot=True), fast), 1)
                self.assertEqual(fast.sent, ['答案'])
                self.assertFalse(pending.done())
            finally:
                release.set()
                await pending

    async def test_official_notice_and_final_have_distinct_sequences(self):
        async def model(*args, **kwargs):
            await asyncio.sleep(.04)
            return '最终答案'
        calls = []
        async def send(api, gid, mid, text, seq=1):
            calls.append((text, seq))
            return SendResult('ok', text)
        sender = official.OfficialSender(NS(group_openid='g', id='m'), None)
        with patch.object(core, 'chat', model), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01), \
             patch.object(official, '_safe_send_group', send):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='a', text='问题', is_at_bot=True), sender)
        self.assertEqual(calls, [('正在思考中，请稍等……', 1), ('最终答案', 2)])

    def test_model_response_has_no_read_deadline(self):
        self.assertIsNone(dc.client.timeout.read)
        self.assertGreater(dc.client.timeout.connect, 0)
        self.assertGreater(dc.client.timeout.write, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
