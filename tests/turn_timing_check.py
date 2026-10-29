"""Offline turn lifecycle checks; semantic decisions are tested separately with real models."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, trigger as tr, tools
from src.channels import core, onebot
from src.channels import scheduler
from src.channels.base import IncomingMessage, QuoteInfo
from src.channels.scheduler import ConversationQueue
from src.tools import ChatContext
from src.tools.chat_provider import ReplyOutbox


class TimingCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        self.bot = onebot.OneBotClient()
        self.bot._queue.collect_sec = .015
        self.bot._queue.max_collect_sec = .05
        self.sender = audit.Sender()
        for target, key, value in [(core, 'CHAT_SEGMENT_INTERVAL_SEC', 0),
                                   (tr, 'CHAT_WAIT_TIMEOUT_SEC', .08)]:
            p = patch.object(target, key, value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.bot.close()

    def msg(self, mid, text, *, at=False, user='a', mentions=()):
        return IncomingMessage(conv_id='g', user_id=user, user_name=user, message_id=mid,
            text=text, is_group=True, is_at_bot=at, mention_user_ids=mentions)

    async def submit(self, msg):
        await self.bot._queue.submit(msg.conv_id, (msg, self.sender), self.bot._prepare_and_dispatch,
                                     batch_handler=self.bot._prepare_batch)

    async def test_burst_is_one_model_turn_with_all_messages(self):
        model = audit.Model([audit.response(calls=[('reply', {'text':'完整回复'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.create_task(self.submit(self.msg('1', '截图说明', at=True)))
            await asyncio.sleep(.005)
            await self.submit(self.msg('2', '我想问的是'))
            await self.submit(self.msg('3', '如何解决？'))
            await first
        self.assertEqual(len(model.requests), 1)
        prompt = next(m['content'] for m in reversed(model.requests[0]['messages']) if isinstance(m,dict) and m['role']=='user')
        for text in ('截图说明', '我想问的是', '如何解决？'):
            self.assertIn(text, prompt)
        self.assertEqual([x['text'] for x in self.sender.sent], ['完整回复'])

    async def test_inflight_followup_discards_only_unsubmitted_reply(self):
        started, release = asyncio.Event(), asyncio.Event()
        model = audit.Model([audit.response(calls=[('reply', {'text':'旧草稿'})]),
                             audit.response(calls=[('reply', {'text':'根据补充回答'})])])
        create = model.create
        async def slow(**kw):
            if not model.requests:
                started.set()
                await release.wait()
            return await create(**kw)
        model.create = slow
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.create_task(self.submit(self.msg('1', '解释错误', at=True)))
            await started.wait()
            await self.submit(self.msg('2', '补充：只解释含义，不要修改配置'))
            release.set()
            await first
        self.assertEqual(len(model.requests), 2)
        self.assertEqual([x['text'] for x in self.sender.sent], ['根据补充回答'])
        self.assertNotIn('旧草稿', str(dc._conversations['g']))
        self.assertIn('只解释含义', str(model.requests[1]['messages']))

    async def test_quoted_self_followup_supersedes_before_quote_enrichment(self):
        started, release = asyncio.Event(), asyncio.Event()
        model = audit.Model([audit.response(calls=[('reply', {'text': '过时的草稿'})]),
                             audit.response(calls=[('reply', {'text': '合并引用的补充'})])])
        create = model.create
        async def controlled(**kw):
            if not model.requests:
                started.set()
                await release.wait()
            return await create(**kw)
        model.create = controlled
        followup = self.msg('2', '补充：不要改配置')
        followup.raw['reply_id'] = '1'
        quote = QuoteInfo(message_id='1', author_id='a', text='解释错误')
        with patch.object(dc, 'client', model), \
             patch.object(self.bot, '_quote_info', AsyncMock(return_value=(quote, []))), \
             contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.create_task(self.submit(self.msg('1', '解释错误', at=True)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.submit(followup)
                release.set()
                await first
            finally:
                release.set()
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
        self.assertEqual([x['text'] for x in self.sender.sent], ['合并引用的补充'])
        self.assertIn('解释错误', str(model.requests[-1]['messages']))
        self.assertIn('不要改配置', str(model.requests[-1]['messages']))

    async def test_wait_bypasses_probability_and_expired_activity_for_same_user(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason':'wait'})]),
                             audit.response(calls=[('reply', {'text':'接着回答'})])])
        with patch.object(dc, 'client', model), patch.object(tr.random, 'random', return_value=1), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1', '还没说完', at=True))
            self.trigger._active_until['g'] = 0
            await self.submit(self.msg('2', '现在说完了：帮我说明这个错误'))
            await asyncio.sleep(.12)
        self.assertEqual(len(model.requests), 2)
        self.assertIsNone(self.trigger.waiting_for('g', 'a'))
        self.assertEqual([x['text'] for x in self.sender.sent], ['接着回答'])

    async def test_wait_timeout_rechecks_once_without_new_input(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason':'wait'})]),
                             audit.response(calls=[('skip', {'reason':'wait'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1', '然后', at=True))
            await asyncio.sleep(.22)
        self.assertEqual(len(model.requests), 2)
        self.assertIn('等待补充截止', str(model.requests[1]['messages']))
        self.assertIsNone(self.trigger.waiting_for('g', 'a'))
        self.assertEqual(self.sender.sent, [])
        self.assertTrue(all(entry.reviewed for entry in self.trigger.buffer.snapshot('g')))

    async def test_other_recipient_does_not_suppress_current_answer(self):
        started, release = asyncio.Event(), asyncio.Event()
        model = audit.Model([audit.response(calls=[('reply', {'text':'给甲的回复'})])])
        create = model.create
        async def slow(**kw):
            started.set()
            await release.wait()
            return await create(**kw)
        model.create = slow
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.create_task(self.submit(self.msg('1', '请回答', at=True)))
            await started.wait()
            await self.submit(self.msg('2', '小乙明天见', user='b', mentions=('c',)))
            release.set()
            await first
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.sender.sent[0]['text'], '给甲的回复')

    async def test_other_user_topic_does_not_satisfy_wait(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason':'wait'})]),
                             audit.response(calls=[('skip', {'reason':'wait'})])])
        with patch.object(dc, 'client', model), patch.object(tr.random,'random',return_value=1), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1', '然后', at=True))
            await self.submit(self.msg('2', '明天九点集合', user='b'))
            await asyncio.sleep(.18)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(self.sender.sent, [])
        self.assertIn('不借机换对象接话', str(model.requests[1]['messages']))

    async def test_mixed_author_batch_preserves_wait_and_answers_other_author(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason': 'wait'})]),
                             audit.response(calls=[('reply', {'text': '给乙的答复'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_batch([
                (self.msg('1', '还没说完', at=True), self.sender),
                (self.msg('2', '请回答我的问题', at=True, user='b'), self.sender)])
        self.assertEqual([x['text'] for x in self.sender.sent], ['给乙的答复'])
        self.assertIsNotNone(self.trigger.waiting_for('g', 'a'))
        self.assertEqual([e.message_id for e in self.trigger.buffer.snapshot('g')], ['1'])

    async def test_collected_batch_keeps_expression_beyond_context_cache_limit(self):
        model = audit.Model([audit.response(calls=[('reply', {'text': '完整回答'})])])
        # Production cache is eight entries; a collected batch can hold 32.
        items = [(self.msg(str(i), '第一条说明' if i == 1 else f'补充{i}', at=i == 1), self.sender)
                 for i in range(1, 10)]
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_batch(items)
        prompt = str(model.requests[0]['messages'])
        self.assertIn('第一条说明', prompt)
        self.assertIn('补充9', prompt)
        self.assertEqual([x['text'] for x in self.sender.sent], ['完整回答'])

    async def test_early_author_in_batch_never_falls_back_to_other_authors_context(self):
        self.trigger.buffer = tr.MessageBuffer(max_per_conv=1)
        model = audit.Model([audit.response(calls=[('reply', {'text': '甲的回答'})]),
                             audit.response(calls=[('reply', {'text': '乙的回答'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_batch([
                (self.msg('1', '甲的问题', at=True), self.sender),
                (self.msg('2', '乙的问题', at=True, user='b'), self.sender)])
        self.assertIn('甲的问题', str(model.requests[0]['messages']))
        self.assertEqual([x['text'] for x in self.sender.sent], ['甲的回答', '乙的回答'])

    async def test_intent_mode_retains_collected_input_without_consuming_other_recipient(self):
        intent = dict(action='REPLY', recipient='bot', expression='complete',
            target_message_ids=['9'], context_message_ids=['1'], evidence_message_ids=['9'],
            reason='同一作者的完整问题')
        model = audit.Model([audit.response(json.dumps(intent, ensure_ascii=False)),
                             audit.response('结合开头和补充回答')])
        items = [(self.msg(str(i), '第一条说明' if i == 1 else f'补充{i}', at=i == 1), self.sender)
                 for i in range(1, 10)]
        items.append((self.msg('10', '小乙明天见', mentions=('b',)), self.sender))
        with patch.object(core, 'CHAT_INTENT_ENABLE', True), \
             patch.object(tr, 'CHAT_INTENT_ENABLE', True), \
             patch.object(onebot, 'CHAT_INTENT_ENABLE', True), \
             patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_batch(items)
        self.assertEqual([x['text'] for x in self.sender.sent], ['结合开头和补充回答'])
        self.assertIn('第一条说明', str(model.requests[-1]['messages']))
        self.assertIn('补充9', str(model.requests[-1]['messages']))
        self.assertNotIn('小乙明天见', str(model.requests[-1]['messages']))
        self.assertEqual([e.message_id for e in self.trigger.buffer.snapshot('g')], ['10'])

    async def test_same_authors_other_recipient_is_not_merged_into_focus(self):
        model = audit.Model([audit.response(calls=[('reply', {'text': '给甲的答复'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_batch([
                (self.msg('1', '请回答我的问题', at=True), self.sender),
                (self.msg('2', '小乙明天见', mentions=('b',)), self.sender)])
        self.assertEqual([e.message_id for e in self.trigger.buffer.snapshot('g')], ['2'])
        self.assertEqual([x['text'] for x in self.sender.sent], ['给甲的答复'])

    async def test_timeout_recheck_does_not_review_another_authors_unsent_input(self):
        original = self.msg('1', '然后', at=True)
        self.trigger.decide(original)
        self.trigger.decide(self.msg('2', '尚未处理的乙问题', user='b'))
        state = self.trigger.mark_waiting('g', 'a', '1')
        retry = self.msg('1', '然后')
        retry.ai_decides_reply = retry.wait_recheck = True
        retry.waiting_token = state
        model = audit.Model([audit.response(calls=[('skip', {'reason': 'wait'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.bot._prepare_and_dispatch((retry, self.sender))
        entries = self.trigger.buffer.snapshot('g')
        self.assertTrue(entries[0].reviewed)
        self.assertFalse(entries[1].reviewed, '甲的超时复核不能标记乙的问题为已略过')

    async def test_recheck_owner_survives_followup_that_waits_again(self):
        # A timeout recheck owns the *real* queue; a followup arrives while its
        # model is running. Replacing the timer must not cancel the queue owner.
        started, release, followup_started, drain = (asyncio.Event() for _ in range(4))
        original = self.msg('1', '然后', at=True)
        await self.bot._prepare(original)
        state = self.trigger.mark_waiting('g', 'a', '1')
        state = (time.monotonic() - 1, *state[1:])
        self.trigger._waiting['g', 'a'] = state
        model = audit.Model([audit.response(calls=[('skip', {'reason': 'wait'})]),
                             audit.response(calls=[('skip', {'reason': 'wait'})]),
                             audit.response(calls=[('reply', {'text': '终于说完了'})])])
        create = model.create
        async def controlled(**kw):
            if len(model.requests) == 0:
                started.set()
                await release.wait()
            else:
                followup_started.set()
            return await create(**kw)
        model.create = controlled
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            owner = asyncio.create_task(self.bot._recheck_wait(original, self.sender, state))
            self.bot._wait_tasks['g', 'a'] = owner
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.submit(self.msg('2', '还要补充一点'))
                release.set()
                await asyncio.wait_for(followup_started.wait(), 2)
                # A non-batched sentinel stays behind the followup and proves
                # cancellation did not silently discard the remainder.
                async def sentinel(_):
                    await asyncio.sleep(0)
                    drain.set()
                await self.bot._queue.submit('g', (self.msg('sentinel', '/sentinel'), self.sender), sentinel)
                result = await asyncio.gather(owner, return_exceptions=True)
                self.assertFalse(isinstance(result[0], BaseException), result)
                self.assertTrue(drain.is_set(), '等待任务取消丢弃了队列尾部')
                await self.submit(self.msg('3', '现在说完了，请回答'))
            finally:
                release.set()
                if not owner.done():
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)
        self.assertEqual([x['text'] for x in self.sender.sent], ['终于说完了'])
        self.assertIn('还要补充一点', str(model.requests[-1]['messages']))

    async def test_command_is_batch_boundary(self):
        queue = ConversationQueue(collect_sec=.01, max_collect_sec=.03)
        seen = []
        async def single(item): seen.append(item)
        async def batch(items): seen.append(items)
        first = asyncio.create_task(queue.submit('g', 1, single, batch_handler=batch))
        await asyncio.sleep(0)
        await queue.submit('g', 2, single)
        await queue.submit('g', 3, single, batch_handler=batch)
        await first
        self.assertEqual(seen, [[1], 2, [3]])
        self.assertEqual(queue.size, 0)

    async def test_production_collect_windows_use_quiet_gap_and_hard_deadline(self):
        # Advance only the scheduler's clock; no wall-time sleeps or live APIs.
        for gaps, expected in [([.9, .9], 3.0), ([.9, .9, .9, .9], 4.0)]:
            with self.subTest(gaps=gaps):
                queue = ConversationQueue(collect_sec=1.2, max_collect_sec=4)
                clock = NS(now=100.0)
                arrivals = iter(gaps)
                dispatches, submitted = [], [0]
                async def single(item):
                    self.fail('batchable input dispatched singly')
                async def batch(items):
                    dispatches.append((clock.now - 100, items))
                async def wait_for(awaitable, timeout):
                    gap = next(arrivals, None)
                    if gap is None or gap >= timeout:
                        clock.now += timeout
                        awaitable.close()
                        raise asyncio.TimeoutError
                    clock.now += gap
                    submitted.append(len(submitted))
                    await queue.submit('g', submitted[-1], single, batch_handler=batch)
                    return await awaitable
                fake_asyncio = NS(**{name: getattr(asyncio, name) for name in (
                    'Event', 'current_task', 'QueueFull', 'TimeoutError', 'CancelledError', 'gather')},
                    get_running_loop=lambda: NS(time=lambda: clock.now), wait_for=wait_for)
                with patch.object(scheduler, 'asyncio', fake_asyncio), contextlib.redirect_stdout(io.StringIO()):
                    await queue.submit('g', 0, single, batch_handler=batch)
                self.assertEqual(len(dispatches), 1)
                self.assertAlmostEqual(dispatches[0][0], expected)
                self.assertEqual(dispatches[0][1], submitted)
                self.assertEqual((queue.size, queue.owners, queue.inflight_extra), (0, set(), {}))

    async def test_reset_cancels_waiting_recheck_without_model_call(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason':'wait'})])])
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1', '然后', at=True))
            reset = self.msg('2', '/reset', at=True)
            await self.bot._queue.submit('g', (reset, self.sender), self.bot._prepare_and_dispatch)
            await asyncio.sleep(.12)
        self.assertIsNone(self.trigger.waiting_for('g','a'))
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.sender.sent[0]['text'], '记忆已清除，让我们重新开始吧！')

    async def test_bare_mention_then_skipped_ack_still_accepts_followup(self):
        model = audit.Model([audit.response(calls=[('reply', {'text':'在的'})]),
                             audit.response(calls=[('skip', {'reason':'irrelevant'})]),
                             audit.response(calls=[('reply', {'text':'当然不会烦'})])])
        with patch.object(dc,'client',model), patch.object(tr.random,'random',return_value=1), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1','',at=True))
            deadline = self.trigger._active_until['g']
            await self.submit(self.msg('2','没什么'))
            self.assertEqual(self.trigger._active_until['g'],deadline)
            await self.submit(self.msg('3','你会不会觉得我烦'))
        self.assertEqual(len(model.requests),3)
        self.assertIn('仅@机器人', str(model.requests[0]['messages']))
        self.assertEqual([x['text'] for x in self.sender.sent],['在的','当然不会烦'])

    async def test_explicit_end_returns_to_probability(self):
        model = audit.Model([audit.response(calls=[('skip', {'reason':'end'})])])
        with patch.object(dc,'client',model), patch.object(tr.random,'random',return_value=1), contextlib.redirect_stdout(io.StringIO()):
            await self.submit(self.msg('1','再见，先聊到这里',at=True))
            self.assertFalse(self.trigger.is_active('g'))
            await self.submit(self.msg('2','稍后再说'))
        self.assertEqual(len(model.requests),1)
        self.assertEqual(self.sender.sent,[])

    def test_structured_reply_rejects_incomplete_or_other_recipient(self):
        box = ReplyOutbox()
        ctx = ChatContext(conv_id='g', outbox=box)
        for recipient, expression in [('others', 'complete'), ('bot', 'incomplete')]:
            result = tools.execute('reply', json.dumps(dict(text='不能发送', recipient=recipient,
                                   expression=expression)), ctx)
            self.assertIn('没有记录', result)
        missing = tools.execute('reply', '{"text":"缺少判断"}', ctx)
        self.assertIn('缺少必填参数', missing)
        self.assertFalse(box.has_items)


if __name__ == '__main__':
    unittest.main(verbosity=2)
