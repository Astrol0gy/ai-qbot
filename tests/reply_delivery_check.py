"""Quote semantics and pending input must hold across the production delivery path."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DEEPSEEK_API_KEY', 'offline-test-key')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
import intent_routing_check as routing
from src import deepseek_client as dc, stickers_utils
from src.channels import core, onebot
from src.channels.base import IncomingMessage


class ReplyDeliveryCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp

    async def test_default_agent_answer_does_not_quote_latest_message(self):
        model = audit.Model([routing.response(), audit.response('在呢')])
        api = AsyncMock(return_value=({}, onebot.API_OK))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        msg = IncomingMessage(conv_id='g', user_id='a', text='在吗', message_id='1',
                              is_at_bot=True, ai_decides_reply=True)
        self.trigger.decide(msg)
        with patch.object(core, 'CHAT_INTENT_ENABLE', True), patch.object(dc, 'client', model):
            await core.handle_message(msg, sender)
        segments = api.call_args.args[1]['message']
        self.assertFalse(any(s['type'] == 'reply' for s in segments))

    async def test_sticker_segment_never_inherits_text_quote(self):
        api = AsyncMock(return_value=({}, onebot.API_OK))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        with patch.object(onebot, '_sticker_base64', return_value=('a.gif', 'base64://abc')):
            await sender.send('针对你那句话', reply_to='1', sticker_paths=['a.gif'])
        messages = [call.args[1]['message'] for call in api.call_args_list]
        self.assertTrue(any(s['type'] == 'reply' for s in messages[0]))
        self.assertFalse(any(s['type'] == 'reply' for s in messages[1]))
        image = next(s for s in messages[1] if s['type'] == 'image')
        self.assertEqual(image['data']['sub_type'], 1)

    async def test_main_can_explicitly_quote_a_specific_message(self):
        model = audit.Model([routing.response(),
            audit.response(calls=[('quote_message', {'message_id': '1'})]),
            audit.response('你那条消息的意思是这样')])
        api = AsyncMock(return_value=({}, onebot.API_OK))
        sender = onebot.OneBotSender(NS(call_api_ex=api), True, 10, 20)
        msg = IncomingMessage(conv_id='g', user_id='a', text='请引用这条解释', message_id='1',
                              is_at_bot=True, ai_decides_reply=True)
        self.trigger.decide(msg)
        with patch.object(core, 'CHAT_INTENT_ENABLE', True), patch.object(dc, 'client', model):
            await core.handle_message(msg, sender)
        segments = api.call_args.args[1]['message']
        self.assertEqual(segments[0], {'type': 'reply', 'data': {'id': '1'}})

    async def test_followup_during_sticker_tool_suppresses_old_draft(self):
        newer = False
        model = audit.Model([
            audit.response(calls=[('load_skill', {'name': '图片理解与表情包'})]),
            audit.response(calls=[('send_sticker', {'query': '开心'})]),
            audit.response(calls=[('reply', {'text': '旧草稿'})])])
        real_execute = dc.execute_tool
        def execute(name, args, ctx):
            nonlocal newer
            if name == 'send_sticker':
                newer = True
                return '本次没有找到表情'
            return real_execute(name, args, ctx)
        msg = IncomingMessage(conv_id='g', user_id='a', text='先听我说', message_id='1',
            is_at_bot=True, ai_decides_reply=True, has_newer_input=lambda: newer)
        self.trigger.decide(msg)
        sender = audit.Sender()
        with patch.object(dc, 'client', model), patch.object(dc, 'execute_tool', execute):
            await core.handle_message(msg, sender)
        self.assertFalse(sender.sent, '工具调用不能使旧草稿绕过补充消息复核')
        self.assertNotIn('旧草稿', str(dc._conversations['g']))

    async def test_completed_paid_image_is_preserved_when_followup_arrives(self):
        newer = False
        calls = []
        model = audit.Model([
            audit.response(calls=[('load_skill', {'name': '图片创作'})]),
            audit.response(calls=[('text_to_image', {'prompt': 'a cat'})]),
            audit.response(calls=[('reply', {'text': '[img]https://example.com/cat.png[/img]'})])])
        real_execute = dc.execute_tool
        def execute(name, args, ctx):
            nonlocal newer
            if name == 'text_to_image':
                calls.append(args)
                newer = True
                return '[img]https://example.com/cat.png[/img]'
            return real_execute(name, args, ctx)
        msg = IncomingMessage(conv_id='g', user_id='a', text='画只猫', message_id='1',
            is_at_bot=True, ai_decides_reply=True, has_newer_input=lambda: newer)
        self.trigger.decide(msg)
        sender = audit.Sender()
        with patch.object(dc,'client',model), patch.object(dc,'execute_tool',execute):
            await core.handle_message(msg,sender)
        self.assertEqual(len(calls),1)
        self.assertEqual(sender.sent[0]['images'],['https://example.com/cat.png'])
        self.assertIn('cat.png',str(dc._conversations['g']))

    async def test_compatibility_mode_keeps_other_authors_background(self):
        from src import trigger as tr
        model = audit.Model([audit.response(calls=[('reply',{'text':'连接被拒绝'})])])
        bot = onebot.OneBotClient()
        self.addAsyncCleanup(bot.close)
        bot._queue.collect_sec = 0
        sender = audit.Sender()
        first = IncomingMessage(conv_id='g', user_id='a', user_name='甲', message_id='1',
                                text='程序报错 ConnectionRefusedError 10061')
        second = IncomingMessage(conv_id='g', user_id='b', user_name='乙', message_id='2',
                                 text='上面那个错误是什么原因？', is_at_bot=True)
        with patch.object(dc,'client',model), patch.object(tr.random,'random',return_value=1):
            await bot._prepare_batch([(first,sender),(second,sender)])
        self.assertIn('ConnectionRefusedError',str(model.requests[0]['messages']))
        self.assertTrue(any(e.user_id=='a' for e in self.trigger.buffer.snapshot('g')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
