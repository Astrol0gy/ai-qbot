"""Scaled, offline message timelines. No real API calls or QQ messages."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import runtime, deepseek_client as dc, tools, vision_utils as vision
from src.channels import core, official
from src.channels.base import IncomingMessage, SendResult

SCALE = .02  # 60 simulated seconds = 1.2 seconds
TIMELINES = []


class TimingCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp

    async def run_case(self, title, phases, expected, *, direct=True, fail_notice=False, cancel=False):
        sent, attempts = [], []
        start = time.monotonic()

        class Sender:
            async def send(self, text, **kwargs):
                attempts.append(text)
                if fail_notice and text.startswith('正在'):
                    raise ConnectionError('notice rejected')
                sent.append((round((time.monotonic() - start) / SCALE, 1), text))
                return SendResult('ok', text)

        async def chat(*args, **kwargs):
            for kind, tag, seconds, error in phases:
                with runtime.phase(kind, tag):
                    await asyncio.sleep(seconds * SCALE)
                    if error:
                        return '生图失败：服务返回错误。'
            return '最终答案'

        with patch.object(core, 'chat', chat), \
             patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', 60 * SCALE), \
             patch.object(core, 'TOOL_CALL_NOTICE_SEC', 30 * SCALE), \
             contextlib.redirect_stdout(io.StringIO()):
            task = asyncio.create_task(core.handle_message(IncomingMessage(conv_id='g', user_id='u',
                text='测试', is_group=True, is_at_bot=direct, ai_decides_reply=not direct, is_interjection=not direct), Sender()))
            if cancel:
                await asyncio.sleep(45 * SCALE)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                await task
        self.assertEqual([text for _, text in sent], expected)
        TIMELINES.append(dict(case=title, output=sent, attempts=attempts))

    async def test_fast(self):
        await self.run_case('快速完成', [('model', '', 5, False), ('tool', '生图', 10, False)], ['最终答案'])

    async def test_slow_model(self):
        await self.run_case('长考', [('model', '', 80, False)], ['正在思考中...', '最终答案'])

    async def test_slow_tool(self):
        await self.run_case('慢生图', [('model', '', 5, False), ('tool', '生图', 80, False)],
            ['正在调用生图技能', '最终答案'])

    async def test_both(self):
        await self.run_case('长考后慢生图', [('model', '', 80, False), ('tool', '生图', 50, False)],
            ['正在思考中...', '正在调用生图技能', '最终答案'])

    async def test_tool_then_model(self):
        await self.run_case('慢工具后长考', [('tool', '生图', 50, False), ('model', '', 80, False)],
            ['正在调用生图技能', '正在思考中...', '最终答案'])

    async def test_two_tools(self):
        await self.run_case('连续慢工具', [('tool', '生图', 50, False), ('tool', '导航', 50, False)],
            ['正在调用生图技能', '正在调用导航技能', '最终答案'])

    async def test_repeated_model_notice_is_suppressed(self):
        await self.run_case('多次长考只提示一次', [('model', '', 80, False),
            ('tool', '生图', 50, False), ('model', '', 80, False)],
            ['正在思考中...', '正在调用生图技能', '最终答案'])

    async def test_same_tool_called_twice(self):
        await self.run_case('两次独立生图', [('tool', '生图', 50, False), ('tool', '生图', 50, False)],
            ['正在调用生图技能', '正在调用生图技能', '最终答案'])

    async def test_error_before_notice(self):
        await self.run_case('工具提示前报错', [('tool', '生图', 10, True)], ['生图失败：服务返回错误。'])

    async def test_error_after_notice(self):
        await self.run_case('工具提示后报错', [('tool', '生图', 50, True)],
            ['正在调用生图技能', '生图失败：服务返回错误。'])

    async def test_notice_failure(self):
        await self.run_case('提示发送失败', [('tool', '生图', 50, False)], ['最终答案'], fail_notice=True)

    async def test_unsolicited(self):
        await self.run_case('普通群聊未被直接询问', [('tool', '导航', 50, False)], ['最终答案'], direct=False)

    async def test_cancel(self):
        await self.run_case('提示后主动取消', [('tool', '生图', 200, False)], ['正在调用生图技能'], cancel=True)

    async def test_real_model_tool_stage(self):
        """Exercise real orchestration, with fake model and thread-executed tool."""
        calls = iter([audit.response(calls=[('text_to_image', {'prompt': '测试'})]), audit.response('最终答案')])
        async def create(**kwargs):
            return next(calls)
        def execute(*args):
            time.sleep(.12)
            return '生成结果'
        sender = audit.Sender()
        with patch.object(dc, 'client', NS(chat=NS(completions=NS(create=create)))), \
             patch.object(dc, 'get_definitions', return_value=[{'type': 'function', 'function': {'name': 'text_to_image'}}]), \
             patch.object(dc, 'execute_tool', execute), \
             patch.object(core, 'TOOL_CALL_NOTICE_SEC', .02), \
             patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .04), \
             contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='u', text='测试', is_at_bot=True), sender)
        self.assertEqual([e['text'] for e in sender.sent], ['正在调用生图技能', '最终答案'])

    async def test_official_notice_sequences(self):
        async def chat(*args, **kwargs):
            with runtime.phase('model'):
                await asyncio.sleep(.1)
            with runtime.phase('tool', '生图'):
                await asyncio.sleep(.1)
            return '最终答案'
        calls = []
        async def send(api, gid, mid, text, seq=1):
            calls.append((text, seq))
            return SendResult('ok', text)
        sender = official.OfficialSender(NS(group_openid='g', id='m'), None)
        with patch.object(core, 'chat', chat), patch.object(official, '_safe_send_group', send), \
             patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01), \
             patch.object(core, 'TOOL_CALL_NOTICE_SEC', .01), contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='u', text='测试', is_at_bot=True), sender)
        self.assertEqual(calls, [('正在思考中...', 1), ('正在调用生图技能', 2), ('最终答案', 3)])

    def test_all_tools_have_tags_without_changing_model_schema(self):
        for name in tools.get_tool_names():
            self.assertNotEqual(tools.get_tag(name), '工具', name)
        self.assertTrue(all('tag' not in d['function'] for d in tools.get_definitions()))

    def test_image_request_keeps_connect_timeout_without_read_deadline(self):
        result = NS(raise_for_status=lambda: None, json=lambda: {'output': {'choices': [
            {'message': {'content': [{'image': 'https://example.com/image.png'}]}}]}})
        with patch.object(vision, 'DASHSCOPE_API_KEY', 'fake'), patch.object(vision.requests, 'post', return_value=result) as post:
            self.assertEqual(vision.text_to_image('测试'), ['https://example.com/image.png'])
        self.assertEqual(post.call_args.kwargs['timeout'], (5, None))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TimingCases))
    target = Path(__file__).with_name('progress_timing_last.log')
    target.write_text(json.dumps(TIMELINES, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(TIMELINES, ensure_ascii=False, indent=2))
    sys.exit(not result.wasSuccessful())
