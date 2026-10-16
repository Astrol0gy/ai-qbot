"""Offline checks for correlated, bounded runtime logs and quote diagnostics."""
import asyncio
import contextlib
import io
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review_regressions as audit
from src import deepseek_client as dc
from src.channels import core, onebot
from src.channels.base import IncomingMessage, SendResult
from src.channels.scheduler import ConversationQueue
from src.observability import redact
from src.runner import LoggedStream


def records(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith('{')]


class LoggingCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp

    async def test_skill_body_reaches_model_but_not_logs(self):
        secret_body = 'SKILL_BODY_MUST_NOT_APPEAR_IN_LOGS'
        first = audit.response(calls=[('load_skill', {'name': 'test'})])
        first.choices[0].message.reasoning_content = 'PRIVATE_REASONING_BODY'
        model = audit.Model([first, audit.response('收到')])
        output = io.StringIO()
        with patch.object(dc, 'client', model), patch.object(dc, 'execute_tool', return_value=secret_body), contextlib.redirect_stdout(output):
            await core.handle_message(IncomingMessage(channel='onebot', conv_id='g', user_id='a',
                message_id='101', text='看看技能', is_at_bot=True), audit.Sender())
        text = output.getvalue()
        self.assertNotIn(secret_body, text)
        self.assertNotIn('PRIVATE_REASONING_BODY', text)
        self.assertTrue(any(m.get('content') == secret_body for m in model.requests[1]['messages'] if isinstance(m, dict)))
        events = records(text)
        self.assertTrue(any(e['event'] == 'tool.finish' and e['tool'] == 'load_skill' for e in events))

    async def test_long_thinking_has_one_terminal_event_after_completion(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(.04)
            return '答案'
        output = io.StringIO()
        with patch.object(core, 'chat', slow), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .01), contextlib.redirect_stdout(output):
            await core.handle_message(IncomingMessage(channel='onebot', conv_id='g', user_id='a',
                message_id='102', text='在吗', is_at_bot=True), audit.Sender())
            await asyncio.sleep(.01)
        ends = [e for e in records(output.getvalue()) if e['event'] == 'message.finish']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['status'], 'ok')
        self.assertEqual(ends[0]['message_id'], '102')
        self.assertTrue(any(e['event'] == 'model.round.long_running' for e in records(output.getvalue())))

    async def test_parallel_messages_have_separate_trace_ids(self):
        async def answer(*args, **kwargs):
            await asyncio.sleep(.01)
            return '你好'
        output = io.StringIO()
        with patch.object(core, 'chat', answer), contextlib.redirect_stdout(output):
            await asyncio.gather(*[core.handle_message(IncomingMessage(channel='onebot', conv_id=f'g{i}',
                user_id='a', message_id=str(i), text='在吗', is_at_bot=True), audit.Sender()) for i in range(2)])
        ends = [e for e in records(output.getvalue()) if e['event'] == 'message.finish']
        self.assertEqual(len(ends), 2)
        self.assertEqual(len({e['trace_id'] for e in ends}), 2)
        self.assertEqual({e['message_id'] for e in ends}, {'0', '1'})

    async def test_quote_lookup_failure_is_visible(self):
        client = onebot.OneBotClient()
        output = io.StringIO()
        with patch.object(client, 'call_api', return_value=None), contextlib.redirect_stdout(output):
            quote, images = await client._quote_info('123')
        self.assertIsNone(quote)
        self.assertEqual(images, [])
        self.assertTrue(any(e['event'] == 'quote.lookup' and e['status'] == 'unavailable' for e in records(output.getvalue())))

    def test_runner_normalizes_legacy_and_redacts_both_outputs(self):
        console, file_output = io.StringIO(), io.StringIO()
        logger = logging.Logger('offline-logging-test')
        logger.addHandler(logging.StreamHandler(file_output))
        stream = LoggedStream(console, logger)
        with patch.dict('os.environ', {'TEST_API_KEY': 'sensitive-12345'}):
            stream.write('old output sensitive-12345 https://example.com/image?signature=unsafe\n')
        self.assertEqual(console.getvalue(), file_output.getvalue())
        event = json.loads(console.getvalue())
        self.assertEqual(event['event'], 'legacy')
        self.assertNotIn('sensitive-12345', console.getvalue())
        self.assertNotIn('unsafe', console.getvalue())

    async def test_real_model_request_error_identifies_active_stage(self):
        from types import SimpleNamespace as NS
        async def slow(**kwargs):
            await asyncio.sleep(.04)
            raise ConnectionError('service disconnected')
        output = io.StringIO()
        with patch.object(dc, 'client', NS(chat=NS(completions=NS(create=slow)))), patch.object(core, 'DEEPSEEK_THINKING_NOTICE_SEC', .02), contextlib.redirect_stdout(output):
            await core.handle_message(IncomingMessage(channel='onebot', conv_id='g', user_id='a',
                message_id='103', text='在吗', is_at_bot=True), audit.Sender())
            await asyncio.sleep(.01)
        events = records(output.getvalue())
        self.assertTrue(any(e['event'] == 'model.request.start' for e in events))
        end = next(e for e in events if e['event'] == 'message.finish')
        self.assertTrue(any(e['event'] == 'model.round.finish' and e.get('status') == 'error' for e in events))
        self.assertEqual(end['status'], 'error')

    async def test_local_command_records_failed_delivery(self):
        class RejectedSender:
            async def send(self, *args, **kwargs):
                return SendResult('rejected', detail='offline')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await core.handle_message(IncomingMessage(conv_id='g', message_id='104', text='/help', is_at_bot=True), RejectedSender())
        events = records(output.getvalue())
        self.assertTrue(any(e['event'] == 'delivery.receipt' and e['status'] == 'rejected' for e in events))
        self.assertEqual(next(e for e in events if e['event'] == 'message.finish')['status'], 'rejected')

    def test_redaction_keeps_token_usage_but_hides_credentials(self):
        with patch.dict('os.environ', {'AMAP_KEY': 'amap-test-credential'}):
            for text in ['amap-test-credential', '{"api_key": "user-credential"}', 'ws://user:pass@host/path?signature=unsafe']:
                clean = redact(text)
                self.assertFalse(any(s in clean for s in ['amap-test-credential', 'user-credential', 'user:pass', 'unsafe']))
        self.assertEqual(redact({'max_tokens': 2048, 'usage': {'completion_tokens': 30}}),
            {'max_tokens': 2048, 'usage': {'completion_tokens': 30}})

    async def test_queue_rejection_has_terminal_trace(self):
        queue = ConversationQueue(total=0)
        msg = IncomingMessage(conv_id='g', message_id='105')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(asyncio.QueueFull):
            await queue.submit('g', (msg, None), None)
        end = next(e for e in records(output.getvalue()) if e['event'] == 'message.finish')
        self.assertEqual(end['trace_id'], msg.trace_id)
        self.assertEqual(end['status'], 'rejected')

    def test_report_reads_rotations_and_excludes_finished_from_unfinished(self):
        from src.log_report import read_events, report
        path = Path(self.tmp.name) / 'qbot.log'
        Path(str(path)+'.1').write_text(json.dumps({'event': 'message.received', 'trace_id': 't', 'message_id': '1'})+'\nold log\n', encoding='utf-8')
        path.write_text(json.dumps({'event': 'message.finish', 'trace_id': 't', 'status': 'timeout', 'stage': 'model.request'})+'\n', encoding='utf-8')
        result = report(read_events(path))
        self.assertEqual(result['outcomes'], {'timeout': 1})
        self.assertEqual(result['unfinished'], [])
        self.assertEqual(result['timeouts'][0]['stage'], 'model.request')

    def test_runner_entry_writes_identical_jsonl_file_and_console(self):
        from src import runner
        from src.observability import log_event
        path = Path(self.tmp.name) / 'run.log'
        async def fake_main():
            print('legacy startup')
            log_event('smoke', status='ok')
        console = io.StringIO()
        with patch.dict('os.environ', {'QBOT_LOG': str(path)}), patch.object(runner, '_run', fake_main), contextlib.redirect_stdout(console):
            runner.run()
        self.assertEqual(path.read_text(encoding='utf-8'), console.getvalue())
        self.assertEqual([e['event'] for e in records(console.getvalue())], ['legacy', 'smoke'])

    def test_runner_fatal_error_is_logged_before_nonzero_exit(self):
        from src import runner
        path = Path(self.tmp.name) / 'fatal.log'
        async def broken_main():
            raise RuntimeError('startup TEST_SECRET_VALUE')
        console, errors = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'QBOT_LOG': str(path), 'TEST_SECRET': 'TEST_SECRET_VALUE'}), patch.object(runner, '_run', broken_main), contextlib.redirect_stdout(console), contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            runner.run()
        self.assertEqual(raised.exception.code, 1)
        events = records(path.read_text(encoding='utf-8'))
        self.assertTrue(any(e['event'] == 'process.error' for e in events))
        self.assertNotIn('TEST_SECRET_VALUE', console.getvalue()+errors.getvalue()+path.read_text(encoding='utf-8'))

    def test_start_process_detection_does_not_match_admin_command_text(self):
        source = (Path(__file__).resolve().parent.parent/'start.sh').read_text(encoding='utf-8-sig')
        body = source.split('bot_running() {', 1)[1].split("<<'PY'", 1)[1].split('\n', 1)[1].split('\nPY', 1)[0]
        for argv, expected in [([b'python', b'-c', b'print("src.runner")'], 1),
                ([b'python', b'-u', b'-m', b'src.runner'], 0)]:
            with self.subTest(argv=argv), patch.dict('os.environ', {'QBOT_PROJECT_DIR': '/project'}), patch('os.listdir', return_value=['101']), patch('os.path.realpath', return_value='/project'), patch('builtins.open', return_value=io.BytesIO(b'\0'.join(argv))), self.assertRaises(SystemExit) as result:
                exec(compile(body, '<actual start.sh process check>', 'exec'), {})
            self.assertEqual(result.exception.code, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
