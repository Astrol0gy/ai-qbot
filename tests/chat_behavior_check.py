"""Offline chat pacing, selective participation and persona integration tests."""
import asyncio
import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, tools
from src.channels import core, onebot, official
from src.channels.base import IncomingMessage, SendResult
from src.trigger import BufferEntry

TEXT = '我觉得\n\n你说的有道理，但是\n\n事情真的是这样吗'
PARTS = ['我觉得', '你说的有道理，但是', '事情真的是这样吗']


class ChatCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        for key, value in [('CHAT_MAX_SEGMENTS',3), ('CHAT_SEGMENT_INTERVAL_SEC',0)]:
            p = patch.object(core, key, value)
            p.start()
            self.addCleanup(p.stop)

    async def run_model(self, model, *, msg=None, sender=None):
        sender = sender or audit.Sender()
        msg = msg or IncomingMessage(conv_id='g', user_id='a', text='聊聊', is_group=False)
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(msg, sender)
        return sender

    def test_semantic_boundaries_only_and_no_content_lost(self):
        self.assertEqual(core._chat_parts(TEXT, 3), PARTS)
        self.assertEqual(core._chat_parts('一\n\n二', 3), ['一', '二'])
        self.assertEqual(core._chat_parts('很长的一句话'*100, 3), ['很长的一句话'*100])
        text = '一\n\n二\n\n三\n\n四'
        self.assertEqual(core._chat_parts(text, 3), ['一','二','三\n\n四'])
        for protected in ['https://example.com\n\n说明', '1. 第一步\n\n2. 第二步',
                          '```python\na=1\n\nb=2\n```', '| 项目 | 说明 |\n| --- | --- |\n\n解释',
                          '[img]https://example.com/a.png[/img]\n\n图片']:
            self.assertEqual(core._chat_parts(protected, 3), [protected])

    async def test_private_chat_sends_parts_and_records_delivered_text(self):
        sender = await self.run_model(audit.Model([audit.response(TEXT)]))
        self.assertEqual([x['text'] for x in sender.sent], PARTS)
        self.assertEqual(dc._conversations['g'][-1]['content'], '\n'.join(PARTS))

    async def test_onebot_quotes_selected_source_only_on_first_part(self):
        for mid in ('1','2'):
            self.trigger.buffer.add('g', BufferEntry(text='消息'+mid, message_id=mid, user_id=mid))
        model = audit.Model([audit.response(calls=[('reply',{'text':TEXT,'reply_to':'2'})]), audit.response()])
        api = AsyncMock(return_value=({}, onebot.API_OK))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        await self.run_model(model, sender=sender, msg=IncomingMessage(conv_id='g',user_id='2',
            text='新消息', message_id='2', is_at_bot=True, ai_decides_reply=True))
        messages = [call.args[1]['message'] for call in api.call_args_list]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0][0], {'type':'reply','data':{'id':'2'}})
        self.assertTrue(all(not any(s['type']=='reply' for s in m) for m in messages[1:]))
        self.assertEqual([m[-1]['data']['text'] for m in messages], PARTS)

    async def test_official_parts_increment_sequence(self):
        calls = []
        async def send(api, gid, mid, text, seq=1):
            calls.append((text,seq))
            return SendResult('ok', text)
        sender = official.OfficialSender(NS(group_openid='g',id='m'), None)
        with patch.object(official, '_safe_send_group', send):
            await self.run_model(audit.Model([audit.response(TEXT)]), sender=sender)
        self.assertEqual(calls, list(zip(PARTS,[1,2,3])))

    async def test_short_reply_stays_one_message(self):
        sender = await self.run_model(audit.Model([audit.response('你说得有道理。')]))
        self.assertEqual(len(sender.sent),1)

    async def test_maximum_can_disable_splitting(self):
        with patch.object(core, 'CHAT_MAX_SEGMENTS',1):
            sender = await self.run_model(audit.Model([audit.response(TEXT)]))
        self.assertEqual([x['text'] for x in sender.sent], [TEXT])

    async def test_pacing_between_parts_only(self):
        sender = audit.Sender()
        sends_at_wait = []
        real_sleep = asyncio.sleep
        async def sleep(delay):
            sends_at_wait.append((delay,len(sender.sent)))
            await real_sleep(0)
        with patch.object(core, 'CHAT_SEGMENT_INTERVAL_SEC',.6), patch.object(core.asyncio, 'sleep', sleep):
            await self.run_model(audit.Model([audit.response(TEXT)]), sender=sender)
        self.assertEqual(sends_at_wait, [(.6,1),(.6,2)])

    async def run_tool_reply(self, text, *, tool='web_search', skill='精准搜索', args=None):
        model = audit.Model([audit.response(calls=[('load_skill',{'name':skill})]),
            audit.response(calls=[(tool,args or {'query':'测试问题'})]), audit.response(text)])
        def execute(name, arguments, ctx):
            if name == tool:
                self.assertIn(name, ctx.allowed_tools)
                return '工具查询结果'
            return tools.execute(name,arguments,ctx)
        with patch.object(dc, 'execute_tool', execute):
            return await self.run_model(model)

    async def test_tool_assisted_plain_explanations_are_split(self):
        for tool, skill, args in [('web_search','精准搜索',{'query':'测试问题'}),
                                 ('plan_route','地图导航',{'origin':'成都','destination':'北京'})]:
            with self.subTest(tool=tool):
                sender = await self.run_tool_reply(TEXT,tool=tool,skill=skill,args=args)
                self.assertEqual([x['text'] for x in sender.sent], PARTS)

    async def test_tool_assisted_structured_replies_stay_intact(self):
        for text in ['说明\n\nhttps://example.com', '步骤\n\n1. 第一步\n\n2. 第二步',
                     '代码\n\n```python\na=1\n\nb=2\n```',
                     '表格\n\n| 项目 | 说明 |\n| --- | --- |']:
            with self.subTest(text=text):
                sender = await self.run_tool_reply(text)
                self.assertEqual([x['text'] for x in sender.sent], [text])

    async def test_two_paragraphs_send_no_empty_third_segment(self):
        sender = await self.run_tool_reply('第一段\n\n第二段')
        self.assertEqual([x['text'] for x in sender.sent], ['第一段','第二段'])
        self.assertEqual(dc._conversations['g'][-1]['content'], '第一段\n第二段')

    async def test_dm_response_is_not_split_or_given_casual_persona(self):
        model = audit.Model([audit.response(TEXT)])
        with patch.object(dc.campaign_state, 'is_dm_mode', return_value=True):
            sender = await self.run_model(model)
        self.assertEqual([x['text'] for x in sender.sent], [TEXT])
        self.assertNotIn('【兴趣与相处分寸】', model.requests[0]['messages'][0]['content'])

    async def test_rejected_segment_stops_and_history_is_not_fabricated(self):
        class Delivery:
            calls = 0
            async def send(self, text, **kwargs):
                self.calls += 1
                return SendResult('ok', text) if self.calls==1 else SendResult('rejected')
        sender = Delivery()
        await self.run_model(audit.Model([audit.response(TEXT)]), sender=sender)
        self.assertEqual(sender.calls,2)
        self.assertEqual(dc._conversations['g'][-1]['content'], PARTS[0])

    async def test_unknown_segment_stops_without_retry(self):
        class Delivery:
            calls = 0
            async def send(self, text, **kwargs):
                self.calls += 1
                return SendResult('unknown', text, detail='timeout')
        sender = Delivery()
        await self.run_model(audit.Model([audit.response(TEXT)]), sender=sender)
        self.assertEqual(sender.calls,1)
        self.assertIn('unknown', dc._conversations['g'][-1]['content'])
        self.assertNotIn(PARTS[1], dc._conversations['g'][-1]['content'])

    async def test_cancel_during_pause_records_only_delivered_segment(self):
        paused = asyncio.Event()
        sender = audit.Sender()
        real_sleep = asyncio.sleep
        async def wait(delay):
            paused.set()
            await real_sleep(100)
        model = audit.Model([audit.response(TEXT)])
        with patch.object(dc,'client',model), patch.object(core,'CHAT_SEGMENT_INTERVAL_SEC',.6), \
             patch.object(core.asyncio,'sleep',wait), contextlib.redirect_stdout(io.StringIO()):
            task = asyncio.create_task(core.handle_message(IncomingMessage(conv_id='g',user_id='a',
                text='问题',is_group=False),sender))
            await asyncio.wait_for(paused.wait(),2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([x['text'] for x in sender.sent],[PARTS[0]])
        self.assertEqual(dc._conversations['g'][-1]['content'],PARTS[0])

    async def test_select_one_message_without_answering_every_person(self):
        for mid in ('old','new'):
            self.trigger.buffer.add('g', BufferEntry(text='话题'+mid, message_id=mid, user_id=mid))
        model = audit.Model([audit.response(calls=[('reply',{'text':'只接当前话题','reply_to':'new'})]),audit.response()])
        sender = await self.run_model(model, msg=IncomingMessage(conv_id='g',user_id='new',text='话题new',
            message_id='new', is_at_bot=True, ai_decides_reply=True))
        self.assertEqual([x['text'] for x in sender.sent],['只接当前话题'])
        prompt = next(m['content'] for m in model.requests[0]['messages']
                      if isinstance(m,dict) and m.get('role')=='user')
        self.assertIn('当前触发消息id：new', prompt)
        self.assertNotIn('凡是跟你说话的都要照顾到', prompt)
        self.assertEqual(self.trigger.buffer.ids('g'),set())

    async def test_skipped_messages_remain_marked_background_next_round(self):
        self.trigger.buffer.add('g', BufferEntry(text='旧话题',message_id='old',user_id='old'))
        first = audit.Model([audit.response(calls=[('skip',{})]),audit.response()])
        sender = await self.run_model(first, msg=IncomingMessage(conv_id='g',user_id='old',text='旧话题',
            message_id='old', ai_decides_reply=True))
        self.assertEqual(sender.sent,[])
        self.assertTrue(self.trigger.buffer.snapshot('g')[0].reviewed)
        self.trigger.buffer.add('g', BufferEntry(text='当前话题',message_id='new',user_id='new'))
        second = audit.Model([audit.response(calls=[('reply',{'text':'接当前话题','reply_to':'new'})]),audit.response()])
        await self.run_model(second, msg=IncomingMessage(conv_id='g',user_id='new',text='当前话题',
            message_id='new',ai_decides_reply=True))
        prompt = next(m['content'] for m in second.requests[0]['messages']
                      if isinstance(m,dict) and m.get('role')=='user')
        self.assertIn('旧话题', prompt)
        self.assertIn('已看过并略过，仅作背景，无需单独补答；仍可用于理解后续补充', prompt)
        self.assertIn('当前触发消息id：new', prompt)

    async def test_skip_does_not_mark_messages_arriving_during_thinking(self):
        old = BufferEntry(text='旧',message_id='old')
        new = BufferEntry(text='新',message_id='new')
        self.trigger.buffer.add('g',old)
        async def chat(*args,**kwargs):
            self.trigger.buffer.add('g',new)
            kwargs['outbox'].skipped=True
            return ''
        with patch.object(core,'chat',chat), contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(IncomingMessage(conv_id='g',user_id='a',text='旧',ai_decides_reply=True),audit.Sender())
        self.assertTrue(old.reviewed)
        self.assertFalse(new.reviewed)

    async def test_wait_preserves_images_for_followup_without_reviewed_mark(self):
        image = BufferEntry(text='[图片1：报错截图，连接失败]', message_id='image',user_id='a')
        explanation = BufferEntry(text='我这个程序刚才',message_id='half',user_id='a')
        for entry in (image,explanation):
            self.trigger.buffer.add('g',entry)
        model = audit.Model([audit.response(calls=[('skip',{'reason':'wait'})]),audit.response()])
        sender = await self.run_model(model,msg=IncomingMessage(conv_id='g',user_id='a',text='我这个程序刚才',
            message_id='half',ai_decides_reply=True))
        self.assertEqual(sender.sent,[])
        self.assertFalse(image.reviewed)
        self.assertFalse(explanation.reviewed)
        self.assertNotIn('g',dc._conversations)
        self.trigger.buffer.add('g',BufferEntry(text='为什么连不上？',message_id='question',user_id='a'))
        followup = audit.Model([audit.response(calls=[('reply',{'text':'先检查连接地址','reply_to':'question'})]),audit.response()])
        sender = await self.run_model(followup,msg=IncomingMessage(conv_id='g',user_id='a',text='为什么连不上？',
            message_id='question',ai_decides_reply=True))
        prompt = next(m['content'] for m in followup.requests[0]['messages'] if isinstance(m,dict) and m.get('role')=='user')
        self.assertIn('报错截图，连接失败',prompt)
        self.assertIn('我这个程序刚才',prompt)
        self.assertNotIn('已看过并略过',prompt)
        self.assertEqual([x['text'] for x in sender.sent],['先检查连接地址'])

    async def test_explicit_irrelevant_marks_background_and_skip_after_reply_does_not_cancel(self):
        entry=BufferEntry(text='完整闲聊',message_id='old',user_id='a')
        self.trigger.buffer.add('g',entry)
        model=audit.Model([audit.response(calls=[('skip',{'reason':'irrelevant'})]),audit.response()])
        await self.run_model(model,msg=IncomingMessage(conv_id='g',user_id='a',text='完整闲聊',ai_decides_reply=True))
        self.assertTrue(entry.reviewed)
        model=audit.Model([audit.response(calls=[('reply',{'text':'已选回复'}),('skip',{'reason':'wait'})]),audit.response()])
        sender=await self.run_model(model,msg=IncomingMessage(conv_id='g',user_id='a',text='新话题',ai_decides_reply=True))
        self.assertEqual([x['text'] for x in sender.sent],['已选回复'])

    async def test_chat_policy_is_consistent_and_dm_avoids_casual_rules(self):
        model=audit.Model([audit.response('好的')])
        await self.run_model(model)
        prompt=model.requests[0]['messages'][0]['content']
        for fragment in ['skip(reason=wait)','skip(reason=irrelevant)','不代表之前没收到图','私聊直接回答','技术任务确实需要代码']:
            self.assertIn(fragment,prompt)
        model=audit.Model([audit.response('剧情继续')])
        with patch.object(dc.campaign_state,'is_dm_mode',return_value=True):
            await self.run_model(model)
        prompt=model.requests[0]['messages'][0]['content']
        self.assertNotIn('默认就回一两句',prompt)
        self.assertNotIn('skip(reason=wait)',prompt)

    async def test_partial_delivery_marks_unselected_background_not_failed_source(self):
        ignored = BufferEntry(text='旧',message_id='old')
        selected = BufferEntry(text='新',message_id='new')
        for entry in (ignored,selected):
            self.trigger.buffer.add('g',entry)
        model = audit.Model([audit.response(calls=[('reply',{'text':'答当前问题','reply_to':'new'})]),audit.response()])
        sender = NS(send=AsyncMock(return_value=SendResult('rejected')))
        await self.run_model(model,sender=sender,msg=IncomingMessage(conv_id='g',user_id='a',text='新',ai_decides_reply=True))
        self.assertTrue(ignored.reviewed)
        self.assertFalse(selected.reviewed)
        self.assertEqual(self.trigger.buffer.ids('g'),{'old','new'})

    async def test_persona_augments_custom_prompt_and_deescalates(self):
        model = audit.Model([audit.response('随口聊聊')])
        with patch.object(dc,'BOT_SYSTEM_PROMPT','自定义机器人名字与身份'):
            await self.run_model(model)
        prompt = model.requests[0]['messages'][0]['content']
        for part in ['自定义机器人名字与身份','游戏','音乐','电影','动漫','漫画','ACG','电脑技术',
                     '宗教','哲学','政治话题保持审慎','萌混过关','不对骂','不阴阳怪气']:
            self.assertIn(part,prompt)


if __name__=='__main__':
    unittest.main(verbosity=2)
