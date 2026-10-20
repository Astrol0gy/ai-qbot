"""Offline regression cases from unsolicited group replies and missing stickers."""
import contextlib
import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, tools, trigger as tr, stickers_utils, vision_utils
from src.channels import core, onebot
from src.channels.base import IncomingMessage, QuoteInfo


class ParticipationCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        p = patch.object(core, 'CHAT_SEGMENT_INTERVAL_SEC', 0)
        p.start()
        self.addCleanup(p.stop)

    async def chat(self, responses, text='你好', group=True):
        sender = audit.Sender()
        model = audit.Model(responses)
        msg = IncomingMessage(conv_id='g', user_id='a', text=text,
                              is_group=group, ai_decides_reply=group)
        with patch.object(dc, 'client', model), contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(msg, sender)
        return sender, model

    def test_real_mentions_and_quote_authors_override_activity(self):
        self.trigger.mark_answered('g', 'a')
        cases = [IncomingMessage(conv_id='g', user_id='a', text='你怎么看', mention_user_ids=('onebot_b',)),
                 IncomingMessage(conv_id='g', user_id='a', text='你怎么看',
                                 quote=QuoteInfo(author_id='b', author_name='乙', text='旧话题'))]
        for msg in cases:
            result = self.trigger.decide(msg)
            self.assertEqual(result.reason, '对其他人说话')
            self.assertEqual(result.decision, tr.Decision.IGNORE)
        context = self.trigger.buffer.peek('g')
        self.assertIn('明确@对象=onebot_b', context)
        self.assertIn('引用作者id=b（非机器人）', context)
        cases[0].is_at_bot = True
        self.assertEqual(self.trigger.decide(cases[0]).reason, '@')

    def test_adapter_preserves_structured_mentions_only(self):
        self.assertEqual(onebot.mention_ids([{'type':'at','data':{'qq':'7'}},
            {'type':'at','data':{'qq':'8'}}], '7'), ('onebot_8',))
        self.assertEqual(onebot.mention_ids('[CQ:at,qq=8]你好', '7'), ('onebot_8',))
        self.assertEqual(onebot.mention_ids('普通文字 @8', '7'), ())

    async def test_quoted_author_uses_same_identity_as_message_sender(self):
        client = onebot.OneBotClient.__new__(onebot.OneBotClient)
        client._self_id = '7'
        client.call_api = AsyncMock(return_value={'sender':{'user_id':8},
            'message':[{'type':'text','data':{'text':'我自己的问题'}}]})
        quote, _ = await client._quote_info('42')
        msg = IncomingMessage(user_id='onebot_8', quote=quote)
        self.assertEqual(quote.author_id, msg.user_id)
        self.assertFalse(msg.addressed_to_others)

    def test_activity_does_not_slide_or_apply_to_every_member(self):
        self.trigger.mark_answered('g', 'a')
        deadline = self.trigger._active_until['g']
        result = self.trigger.decide(IncomingMessage(conv_id='g', user_id='a', text='接着说'))
        self.assertEqual(result.reason, '互动中')
        self.assertEqual(self.trigger._active_until['g'], deadline)
        with patch.object(tr.random, 'random', return_value=1):
            result = self.trigger.decide(IncomingMessage(conv_id='g', user_id='b', text='别的话题'))
        self.assertEqual(result.reason, '概率未命中')
        self.assertEqual(self.trigger._active_until['g'], deadline)
        result = self.trigger.decide(IncomingMessage(conv_id='g', user_id='b', text='再问你',
                                                    quote=QuoteInfo(author_id='bot', is_bot=True)))
        self.assertEqual(result.reason, '互动中')
        self.trigger._active_until['g'] = time.monotonic() - 1
        with patch.object(tr.random, 'random', return_value=1):
            result = self.trigger.decide(IncomingMessage(conv_id='g', user_id='a', text='接着说'))
        self.assertEqual(result.reason, '概率未命中')

    async def test_only_explicit_end_closes_fixed_activity_window(self):
        for reason in ('wait', 'irrelevant', 'end'):
            self.trigger.mark_answered('g', 'a')
            deadline = self.trigger._active_until['g']
            sender, _ = await self.chat([audit.response(calls=[('skip', {'reason':reason})]), audit.response()])
            self.assertEqual(sender.sent, [])
            if reason != 'end':
                self.assertEqual(self.trigger._active_until['g'], deadline)
                with patch.object(tr.random, 'random', return_value=1):
                    self.assertEqual(self.trigger.decide(IncomingMessage(conv_id='g', user_id='a', text='接着说')).decision,
                                     tr.Decision.WAKE)
            else:
                self.assertFalse(self.trigger.is_active('g'))
                self.assertNotIn('g', self.trigger._session_users)

    def test_bare_mention_keeps_actor_message_id_and_invitation(self):
        result = self.trigger.decide(IncomingMessage(conv_id='g',user_id='a',user_name='甲',
                                    message_id='bare',is_at_bot=True))
        self.assertEqual(result.decision, tr.Decision.WAKE)
        self.assertIn('仅@机器人', result.context)
        self.assertIn('@了你', result.context)
        self.assertEqual(self.trigger.buffer.ids('g'), {'bare'})
        self.assertEqual(self.trigger.buffer.snapshot('g')[0].user_id, 'a')

    async def test_irrelevant_does_not_extend_deadline_or_wake_other_users(self):
        self.trigger.mark_answered('g', 'a')
        deadline = self.trigger._active_until['g']
        await self.chat([audit.response(calls=[('skip', {'reason':'irrelevant'})])])
        self.assertEqual(self.trigger._active_until['g'], deadline)
        with patch.object(tr.random, 'random', return_value=1):
            other = self.trigger.decide(IncomingMessage(conv_id='g',user_id='b',text='另一个话题'))
            self.assertEqual(other.reason, '概率未命中')
            self.trigger._active_until['g'] = time.monotonic() - 1
            expired = self.trigger.decide(IncomingMessage(conv_id='g',user_id='a',text='过期后的消息'))
            self.assertEqual(expired.reason, '概率未命中')

    async def test_direct_prose_requires_decision_and_never_leaks(self):
        sender, model = await self.chat([audit.response('不应发出的抢话'),
            audit.response(calls=[('reply', {'text':'已判断后回应'})]), audit.response()])
        self.assertEqual([x['text'] for x in sender.sent], ['已判断后回应'])
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn('不应发出的抢话', dc._conversations['g'][-1]['content'])
        correction = model.requests[1]['messages'][0]['content']
        self.assertIn('本轮还没有任何回复发送', correction)
        self.assertEqual(model.requests[1]['tool_choice'], 'required')
        self.assertEqual(model.requests[1]['extra_body']['thinking']['type'], 'disabled')
        self.assertEqual(model.requests[0]['extra_body']['thinking']['type'],
                         'enabled' if dc.DEEPSEEK_ENABLE_THINKING else 'disabled')
        self.assertFalse(any(m.get('role')=='assistant' and m.get('content')=='不应发出的抢话'
                             for m in model.requests[1]['messages'] if isinstance(m,dict)))

    async def test_two_missing_decisions_stay_silent_without_history(self):
        sender, model = await self.chat([audit.response('第一次抢话'), audit.response('第二次抢话')])
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn('g', dc._conversations)
        sender, _ = await self.chat([audit.response('私聊正常直接回答')], group=False)
        self.assertEqual(sender.sent[0]['text'], '私聊正常直接回答')

    async def test_non_thinking_tool_correction_can_resume_thinking(self):
        sender, model = await self.chat([
            audit.response('草稿不发送'),
            audit.response(calls=[('reply', {'text':'错误对象', 'recipient':'others'})]),
            audit.response(calls=[('reply', {'text':'正确对象', 'recipient':'bot'})])])
        self.assertEqual(sender.sent[0]['text'], '正确对象')
        self.assertEqual(len(model.requests), 3)
        previous = [m for m in model.requests[2]['messages'] if getattr(m,'tool_calls',None)]
        self.assertEqual(previous[0].reasoning_content, '')
        self.assertEqual(model.requests[2]['extra_body']['thinking']['type'],
                         'enabled' if dc.DEEPSEEK_ENABLE_THINKING else 'disabled')

    async def test_control_decision_finishes_without_an_extra_model_turn(self):
        for call in [('reply', {'text':'一句回复'}), ('skip', {'reason':'wait'}),
                     ('skip', {'reason':'irrelevant'})]:
            dc._conversations.pop('g', None)
            sender, model = await self.chat([audit.response(calls=[call])])
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(bool(sender.sent), call[0]=='reply')

    async def test_sticker_draft_is_not_mistaken_for_delivered_history(self):
        responses = [audit.response(calls=[('load_skill',{'name':'图片理解'})]),
                     audit.response(calls=[('send_sticker',{'query':'无语'})]),
                     audit.response('[img]chosen.gif[/img]'),
                     audit.response(calls=[('reply',{'text':'[img]chosen.gif[/img]'})])]
        execute = tools.execute
        def fake_tool(name, args, ctx):
            return '[img]chosen.gif[/img]' if name=='send_sticker' else execute(name,args,ctx)
        with patch.object(dc,'execute_tool',fake_tool), patch.object(stickers_utils,'resolve',return_value='chosen.gif'):
            sender, model = await self.chat(responses,text='发一张表情包')
        self.assertEqual(sender.sent[0]['stickers'], ['chosen.gif'])
        self.assertEqual(len(model.requests), 4)
        self.assertFalse(any(m.get('role')=='assistant' and m.get('content')=='[img]chosen.gif[/img]'
                             for m in model.requests[-1]['messages'] if isinstance(m,dict)))

    async def test_long_casual_reply_is_rejected_then_rewritten_not_truncated(self):
        sender, _ = await self.chat([
            audit.response(calls=[('reply',{'text':'长'*161})]),
            audit.response(calls=[('reply',{'text':'简短结论'})]), audit.response()])
        self.assertEqual([x['text'] for x in sender.sent], ['简短结论'])

    async def test_explicit_search_rejects_answer_until_attempted(self):
        execute = tools.execute
        def fake_tool(name, args, ctx):
            return '搜索服务暂时失败' if name == 'web_search' else execute(name, args, ctx)
        with patch.object(dc, 'execute_tool', fake_tool):
            sender, model = await self.chat([
                audit.response(calls=[('reply', {'text':'未经搜索的答案'})]),
                audit.response(calls=[('load_skill', {'name':'精准搜索'})]),
                audit.response(calls=[('web_search', {'query':'GitHub Actions'})]),
                audit.response(calls=[('reply', {'text':'搜索暂时失败，请稍后再试。'})])],
                text='帮我搜一下 GitHub Actions')
        self.assertEqual([x['text'] for x in sender.sent], ['搜索暂时失败，请稍后再试。'])
        self.assertEqual(len(model.requests), 4)

    async def test_local_lookup_and_search_opt_out_do_not_require_web(self):
        for text in ('不用搜索，直接说结论', '查一下本地配置'):
            dc._conversations.pop('g', None)
            sender, _ = await self.chat([audit.response(calls=[('reply', {'text':'本地结果'})])], text=text)
            self.assertEqual(sender.sent[0]['text'], '本地结果')

    async def test_disabled_search_can_report_unavailability(self):
        with patch.object(dc, 'DEEPSEEK_ENABLE_SEARCH', False):
            sender, _ = await self.chat([audit.response(calls=[('reply', {'text':'搜索功能未启用。'})])],
                                       text='帮我搜一下 GitHub Actions')
        self.assertEqual(sender.sent[0]['text'], '搜索功能未启用。')

    async def test_total_budget_cannot_be_bypassed_by_multiple_calls(self):
        sender, _ = await self.chat([audit.response(calls=[
            ('reply',{'text':'甲'*60}), ('reply',{'text':'乙'*60})]), audit.response()])
        self.assertEqual([x['text'] for x in sender.sent], ['甲'*60])

    async def test_detailed_task_can_exceed_casual_budget(self):
        text = '说明'*120
        sender, _ = await self.chat([audit.response(calls=[('reply',{'text':text})]), audit.response()],
                                    text='请详细解释操作步骤')
        self.assertEqual(sender.sent[0]['text'], text)

    async def test_sticker_selection_still_loads_skill_progressively(self):
        responses = [audit.response(calls=[('load_skill',{'name':'图片理解'})]),
                     audit.response(calls=[('send_sticker',{'query':'无语'})]),
                     audit.response(calls=[('reply',{'text':'[img]chosen.gif[/img]'})]), audit.response()]
        execute = tools.execute
        def fake_tool(name, args, ctx):
            if name == 'send_sticker':
                self.assertIn(name, ctx.allowed_tools)
                return '[img]chosen.gif[/img]'
            return execute(name,args,ctx)
        with patch.object(dc,'execute_tool',fake_tool), patch.object(stickers_utils,'resolve',return_value='chosen.gif'):
            sender, model = await self.chat(responses, text='发个表情包')
        self.assertEqual(sender.sent[0]['stickers'], ['chosen.gif'])
        first = model.requests[0]
        self.assertNotIn('send_sticker', {t['function']['name'] for t in first['tools']})
        self.assertIn('load_skill(name=图片理解)', first['messages'][0]['content'])
        self.assertNotIn('## 场景分类', first['messages'][0]['content'])

    def test_vision_guide_does_not_assume_a_recipient(self):
        self.assertIn('不推断是在对机器人说话', vision_utils._TYPE_GUIDE)
        self.assertIn('意图只是图像层面的假设', vision_utils._TYPE_GUIDE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
