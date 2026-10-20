"""Offline turn lifecycle checks; semantic decisions are tested separately with real models."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, trigger as tr, tools
from src.channels import core, onebot
from src.channels.base import IncomingMessage
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
