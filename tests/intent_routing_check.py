"""Production intent/answer separation and delivery invariants (offline).

Model responses here test plumbing only; semantic checks use the opt-in live tests.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import chat_intent as intent, deepseek_client as dc, trigger as tr
from src.channels import core, onebot
from src.channels.base import IncomingMessage


def decision(action='REPLY', **extra):
    return {'action':action, 'recipient':'bot', 'expression':'complete',
        'target_message_ids':['1'] if action=='REPLY' else [], 'context_message_ids':[],
        'evidence_message_ids':['1'], 'reason':'当前消息的明确关系', **extra}


def response(action='REPLY', **extra):
    return audit.response(json.dumps(decision(action, **extra), ensure_ascii=False))


class RoutingCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        for target, attr in ((core,'CHAT_INTENT_ENABLE'), (tr,'CHAT_INTENT_ENABLE'), (onebot,'CHAT_INTENT_ENABLE')):
            p=patch.object(target,attr,True)
            p.start()
            self.addCleanup(p.stop)
        p=patch.object(core,'CHAT_SEGMENT_INTERVAL_SEC',0)
        p.start()
        self.addCleanup(p.stop)

    async def run_turn(self, replies, text='你在吗', newer=None):
        model=audit.Model(replies)
        sender=audit.Sender()
        msg=IncomingMessage(conv_id='g', user_id='a', user_name='甲', message_id='1',
            text=text, is_at_bot=True, ai_decides_reply=True, has_newer_input=newer)
        self.trigger.decide(msg)
        msg.context_entries=self.trigger.buffer.snapshot('g')
        with patch.object(dc,'client',model), contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(msg,sender)
        return model,sender

    async def test_reply_runs_tool_less_intent_then_answer_without_control_tools(self):
        model,sender=await self.run_turn([response(),audit.response('在呢～')])
        self.assertEqual(len(model.requests),2)
        self.assertNotIn('tools',model.requests[0])
        self.assertEqual(model.requests[0]['response_format'],{'type':'json_object'})
        names={t['function']['name'] for t in model.requests[1]['tools']}
        self.assertEqual(names,{'load_skill','read_skill_resource','quote_message'})
        self.assertNotIn('这一轮开不开口',model.requests[1]['messages'][-1]['content'])
        self.assertNotIn('群聊使用本轮提供的 reply/skip 决定参与',model.requests[1]['messages'][0]['content'])
        self.assertEqual(sender.sent[0]['text'],'在呢～')
        self.assertEqual(dc.get_history_length('g'),1)
        self.assertEqual(self.trigger.buffer.snapshot('g'),())
        self.assertEqual(len(self.trigger.buffer.recent('g')),1)

    async def test_non_reply_actions_never_start_answer_or_write_history(self):
        for action in ('WAIT','IGNORE','END'):
            self.trigger.clear_waiting('g')
            self.trigger.buffer.clear('g')
            model,sender=await self.run_turn([response(action)])
            self.assertEqual(len(model.requests),1)
            self.assertFalse(sender.sent)
            self.assertEqual(dc.get_history_length('g'),0)
            self.assertEqual(bool(self.trigger.waiting_for('g','a')),action=='WAIT')
            self.assertEqual(self.trigger.is_active('g'),action!='END')

    async def test_protocol_retry_never_falls_through_to_answer(self):
        model,sender=await self.run_turn([response(target_message_ids=['forged'])]*2)
        self.assertEqual(len(model.requests),2)
        self.assertTrue(all('response_format' in r for r in model.requests))
        self.assertEqual(dc.get_history_length('g'),0)
        self.assertNotIn('forged',''.join(s['text'] for s in sender.sent))

    async def test_protocol_correction_can_recover(self):
        model,sender=await self.run_turn([audit.response('not JSON'),response(),audit.response('在呢')])
        self.assertEqual(len(model.requests),3)
        self.assertEqual(sender.sent[0]['text'],'在呢')

    async def test_newer_input_during_router_never_starts_answer(self):
        checks=iter([False,True,True])
        model,sender=await self.run_turn([response()],newer=lambda:next(checks))
        self.assertEqual(len(model.requests),1)
        self.assertFalse(sender.sent)
        self.assertEqual(dc.get_history_length('g'),0)
        self.assertTrue(self.trigger.waiting_for('g','a'))

    async def test_long_answer_is_rewritten_before_delivery(self):
        model,sender=await self.run_turn([response(),audit.response('好'*161),audit.response('简短结论')])
        self.assertEqual(len(model.requests),3)
        self.assertEqual(sender.sent[0]['text'],'简短结论')
        self.assertFalse(any(m.get('role')=='assistant' and m.get('content')=='好'*161
            for m in model.requests[-1]['messages'] if isinstance(m,dict)))

    async def test_lost_file_banter_does_not_trigger_long_writing_task(self):
        model,sender=await self.run_turn([response(),audit.response('啊'*161),audit.response('先看看自动恢复，抱抱')],
            text='我刚写完的文件忘保存就关了，整个人无语住了')
        self.assertEqual(len(model.requests),3)
        self.assertEqual(sender.sent[0]['text'],'先看看自动恢复，抱抱')

    async def test_selected_image_keeps_full_facts_after_routing_summary(self):
        image=IncomingMessage(conv_id='g',user_id='a',message_id='image',image_urls=['unused'],
            image_descs=['截图前言。'*70+'ConnectionRefusedError 10061 Host 127.0.0.1 Port 3001'])
        self.trigger.decide(image)
        model,sender=await self.run_turn([response(context_message_ids=['image']),audit.response('3001是端口')])
        content=model.requests[1]['messages'][-1]['content']
        self.assertIn('Host 127.0.0.1 Port 3001',content)
        self.assertIn('Host 127.0.0.1 Port 3001',dc._conversations['g'][-2]['content'])

    async def test_search_guard_still_requires_a_real_tool_attempt(self):
        replies=[response(),audit.response('未搜索的草稿'),
            audit.response(calls=[('load_skill',{'name':'精准搜索'})]),
            audit.response(calls=[('web_search',{'query':'test'})]),audit.response('已查到')]
        real=dc.execute_tool
        def execute(name,args,ctx):
            return '测试搜索结果' if name=='web_search' else real(name,args,ctx)
        with patch.object(dc,'execute_tool',execute):
            model,sender=await self.run_turn(replies,text='帮我搜一下 test')
        self.assertTrue(sender.sent)
        self.assertEqual(sender.sent[0]['text'],'已查到')
        self.assertGreaterEqual(len(model.requests),4)

    def test_all_eligible_batches_route_without_sampling_or_length_drop(self):
        with patch.object(tr.random,'random',side_effect=AssertionError('must not sample')):
            msg=IncomingMessage(conv_id='g',user_id='a',text='你')
            self.assertEqual(self.trigger.decide(msg).decision,tr.Decision.WAKE)
            msg.mention_user_ids=('b',)
            self.assertEqual(self.trigger.decide(msg).decision,tr.Decision.IGNORE)

    def test_reset_clears_consumed_recipient_evidence(self):
        e=tr.BufferEntry(text='甲向乙提问',message_id='old')
        self.trigger.buffer.add('g',e)
        self.trigger.buffer.consume('g',[e])
        self.assertTrue(self.trigger.buffer.recent('g'))
        dc.clear_history('g')
        self.assertFalse(self.trigger.buffer.recent('g'))

    async def test_other_author_in_same_batch_does_not_swallow_explicit_request(self):
        model=audit.Model([response(),audit.response('给甲的答复'),
            response('IGNORE',evidence_message_ids=['2'])])
        bot=onebot.OneBotClient()
        sender=audit.Sender()
        a=IncomingMessage(conv_id='g',user_id='a',user_name='甲',message_id='1',text='给我一句回复',is_at_bot=True)
        b=IncomingMessage(conv_id='g',user_id='b',user_name='乙',message_id='2',text='我已经订好车了')
        try:
            with patch.object(dc,'client',model),contextlib.redirect_stdout(io.StringIO()):
                await bot._prepare_batch([(a,sender),(b,sender)])
            self.assertEqual(len(model.requests),3)
            self.assertEqual([s['text'] for s in sender.sent],['给甲的答复'])
            self.assertEqual(len(self.trigger.buffer.snapshot('g')),1)
            self.assertEqual(self.trigger.buffer.snapshot('g')[0].user_id,'b')
        finally:
            await bot.close()

    async def test_one_authors_answer_preserves_another_authors_wait(self):
        model=audit.Model([response('WAIT'),response(target_message_ids=['2'],evidence_message_ids=['2']),
                           audit.response('给乙的答复')])
        bot=onebot.OneBotClient()
        sender=audit.Sender()
        a=IncomingMessage(conv_id='g',user_id='a',message_id='1',text='等下我还没说完',is_at_bot=True)
        b=IncomingMessage(conv_id='g',user_id='b',message_id='2',text='你在吗',is_at_bot=True)
        try:
            with patch.object(dc,'client',model),contextlib.redirect_stdout(io.StringIO()):
                await bot._prepare_batch([(a,sender),(b,sender)])
            self.assertTrue(self.trigger.waiting_for('g','a'))
            self.assertIsNone(self.trigger.waiting_for('g','b'))
            self.assertEqual([e.user_id for e in self.trigger.buffer.snapshot('g')],['a'])
        finally:
            await bot.close()

    def test_validator_checks_action_consistency_and_references(self):
        entries=[tr.BufferEntry(message_id='1')]
        for extra in ({'recipient':'others'}, {'expression':'incomplete'},
                      {'evidence_message_ids':['missing']}, {'target_message_ids':[]}):
            with self.assertRaises(ValueError):
                intent.validate(decision(**extra),entries,'1')


if __name__=='__main__':
    unittest.main(verbosity=2)
