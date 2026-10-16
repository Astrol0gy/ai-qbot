"""Offline admin authorization, privacy, file transactions and detached control tests."""
import asyncio
from collections import defaultdict
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import admin, admin_job, config, stickers_utils as stickers
from src.channels import onebot
from src.channels.base import IncomingMessage, SendResult


def message(text, uid='523155725', *, group=False, channel='onebot'):
    return IncomingMessage(channel=channel, conv_id='onebot_private_'+uid, user_id='onebot_'+uid,
        text=text, is_group=group, raw={'user_id': uid, 'message_type': 'group' if group else 'private'})


class Sender:
    def __init__(self, status='ok'):
        self.sent = []
        self.status = status

    async def send(self, text, **kwargs):
        self.sent.append(text)
        return SendResult(self.status, text)


class AdminCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / '.env'
        self.env.write_text("ADMIN_QQ=523155725\nOFFICIAL_ENABLE=false\nONEBOT_ENABLE=true\n"
                            "DEEPSEEK_API_KEY=original-secret-123456\nTOOL_CALL_NOTICE_SEC=30\n", encoding='utf-8')
        self.images = self.root / 'stickers'
        self.images.mkdir()
        for module, key, value in [(config, 'ADMIN_QQ', '523155725'), (admin, 'ENV_FILE', self.env),
            (admin, 'DATA_DIR', self.root / 'admin'), (admin, '_lock', asyncio.Lock()),
            (stickers, '_DIR', self.images), (stickers, '_INDEX_FILE', self.images/'index.json'),
            (stickers, '_FILES', {}), (stickers, '_loaded', False), (stickers, 'STICKER_ENABLE', True)]:
            p = patch.object(module, key, value)
            p.start()
            self.addCleanup(p.stop)

    async def request(self, text, **kwargs):
        sender = Sender()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handled = await admin.handle(message(text, **kwargs), sender)
        return handled, '\n'.join(sender.sent), output.getvalue()

    def image(self):
        return stickers.save(b'fake-image-content', 'image/gif', ['无语','捂脸','熊猫','拼贴','无文字'])

    def test_commands_and_natural_language(self):
        cases = {
            '/config set TOOL_CALL_NOTICE_SEC 45': ('config.set', ('TOOL_CALL_NOTICE_SEC','45')),
            '请帮我把工具提示时间改为45': ('config.set', ('工具提示时间','45')),
            '请把.env里的温度调到0.8': ('config.set', ('温度','0.8')),
            'ADMIN_QQ=123456': ('config.set', ('ADMIN_QQ','123456')),
            'DASHSCOPE_API_KEY=secret!': ('config.set', ('DASHSCOPE_API_KEY','secret!')),
            '把BOT_SYSTEM_PROMPT改为你好。': ('config.set', ('BOT_SYSTEM_PROMPT','你好。')),
            '查看env中配置': ('config.list', ()),
            '查看生图模型': ('config.get', ('生图模型',)),
            '看最近20行日志': ('bot.log', ('20',)),
            '重载机器人': ('bot.reload', ()),
            '停止机器人': ('bot.stop', ()),
            '启动机器人': ('bot.start', ()),
            '删除表情包 old.gif': ('sticker.delete', ('old.gif',)),
            '把表情包 old.gif 改名为 new.gif': ('sticker.rename', ('old.gif','new.gif')),
        }
        for text, expected in cases.items():
            req = admin.parse(text)
            self.assertEqual((req.action, req.args), expected, text)
        self.assertIsNone(admin.parse('今天心情不错，给我讲个故事'))
        self.assertIsNone(admin.parse('群友说“停止机器人”，是什么意思'))

    async def test_other_users_groups_and_official_cannot_manage(self):
        for kwargs in [{'uid':'999999'}, {'group':True}, {'channel':'official'}]:
            before = self.env.read_bytes()
            handled, reply, _ = await self.request('/config set TOOL_CALL_NOTICE_SEC 45', **kwargs)
            self.assertTrue(handled)
            self.assertEqual(self.env.read_bytes(), before)
        forged = message('/config list')
        forged.raw['user_id'] = '999999'
        self.assertFalse(admin.authorized(forged))

    async def test_admin_identity_is_readonly_in_all_spellings(self):
        for text in ['/config set ADMIN_QQ 999999', '/config set admin_qq 999999',
                     '把管理员账号改为999999', 'ADMIN_QQ=999999']:
            _, reply, logs = await self.request(text)
            self.assertIn('只能在部署端修改', reply)
            self.assertIn('ADMIN_QQ=523155725', self.env.read_text())
            self.assertNotIn('999999', logs)

    async def test_config_update_validates_then_saves_without_changing_runtime(self):
        runtime_value = config.TOOL_CALL_NOTICE_SEC
        _, reply, _ = await self.request('把工具提示时间改为45')
        self.assertIn('已保存', reply)
        self.assertEqual(admin.dotenv_values(self.env)['TOOL_CALL_NOTICE_SEC'], '45')
        self.assertEqual(config.TOOL_CALL_NOTICE_SEC, runtime_value)
        backups = list((admin.DATA_DIR/'env-backups').glob('*.env'))
        self.assertEqual(len(backups), 1)
        self.assertIn('TOOL_CALL_NOTICE_SEC=30', backups[0].read_text())

    async def test_invalid_or_unknown_config_does_not_replace_file(self):
        before = self.env.read_bytes()
        for text in ['/config set TOOL_CALL_NOTICE_SEC -1', '/config set TOOL_CALL_NOTICE_SEC nan',
                     '/config set DEEPSEEK_ENABLE_THINKING perhaps', '/config set PATH arbitrary']:
            _, reply, _ = await self.request(text)
            self.assertNotIn('已保存', reply)
            self.assertEqual(self.env.read_bytes(), before)

    async def test_secret_setting_never_echoes_or_logs_value(self):
        value = 'new-admin-secret-987654'
        _, reply, logs = await self.request('/config set DASHSCOPE_API_KEY '+value)
        self.assertIn('已保存', reply)
        self.assertNotIn(value, reply+logs)
        self.assertEqual(admin.dotenv_values(self.env)['DASHSCOPE_API_KEY'], value)
        _, reply, logs = await self.request('/config get DASHSCOPE_API_KEY')
        self.assertNotIn(value, reply+logs)

    def test_edit_restores_index_and_file_on_index_failure(self):
        name = self.image()
        before = dict(stickers._FILES)
        real_replace = os.replace
        def fail_index(src, dest):
            if Path(dest) == stickers._INDEX_FILE:
                raise OSError('disk full')
            return real_replace(src, dest)
        with patch.object(stickers.os, 'replace', side_effect=fail_index):
            with self.assertRaises(OSError):
                stickers.admin_edit(name, 'renamed.gif')
        self.assertEqual(stickers._FILES, before)
        self.assertTrue((self.images/name).is_file())
        self.assertFalse((self.images/'renamed.gif').exists())

    def test_rename_and_delete_sync_memory_disk_and_preserve_metadata(self):
        name = self.image()
        metadata = dict(stickers._FILES[name])
        stickers.admin_edit(name, 'renamed.gif')
        self.assertNotIn(name, stickers._FILES)
        self.assertEqual(stickers._FILES['renamed.gif'], metadata)
        index = json.loads(stickers._INDEX_FILE.read_text())
        self.assertEqual(index['files']['renamed.gif'], metadata)
        self.assertFalse((self.images/name).exists())
        stickers.admin_edit('renamed.gif')
        self.assertFalse((self.images/'renamed.gif').exists())
        self.assertNotIn('renamed.gif', json.loads(stickers._INDEX_FILE.read_text())['files'])
        self.assertEqual(len(list((self.images/'.admin-trash').iterdir())), 1)

    def test_traversal_and_overwrite_are_rejected(self):
        name = self.image()
        for target in ['../outside.gif', '/outside.gif', 'folder\\image.gif', 'index.json', '.hidden.gif']:
            with self.assertRaises(ValueError):
                stickers.admin_edit(name, target)
        (self.images/'exists.gif').write_bytes(b'existing')
        with self.assertRaises(ValueError):
            stickers.admin_edit(name, 'exists.gif')
        self.assertEqual((self.images/'exists.gif').read_bytes(), b'existing')

    async def test_control_ack_precedes_detached_launch(self):
        sender = Sender()
        for action in ['reload', 'stop']:
            def launch(op, job_id):
                self.assertIn('已受理', sender.sent[-1])
                self.assertEqual(op, action)
            with patch.object(admin.sys, 'platform', 'linux'), patch.object(admin, 'launch_control', side_effect=launch), \
                 contextlib.redirect_stdout(io.StringIO()):
                await admin.handle(message('/bot '+action), sender)

    async def test_failed_ack_does_not_launch_control(self):
        with patch.object(admin.sys, 'platform', 'linux'), patch.object(admin, 'launch_control') as launch, \
             contextlib.redirect_stdout(io.StringIO()):
            await admin.handle(message('/bot reload'), Sender('unknown'))
        launch.assert_not_called()
        statuses = [json.loads(p.read_text())['status'] for p in (admin.DATA_DIR/'jobs').glob('*.json')]
        self.assertEqual(statuses, ['ack_failed'])

    def test_detached_launch_uses_fixed_argv_and_fresh_config(self):
        with patch.object(admin.sys, 'platform', 'linux'):
            job = admin.prepare_control('reload')
        with patch.object(admin.subprocess, 'Popen') as spawn, patch.dict(os.environ, {'TOOL_CALL_NOTICE_SEC':'old'}):
            admin.launch_control('reload', job)
        args, kwargs = spawn.call_args
        self.assertEqual(args[0][-2:], ['reload', job])
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('TOOL_CALL_NOTICE_SEC', kwargs['env'])

    async def test_onebot_admin_bypasses_queue_and_does_not_log_secret(self):
        client = onebot.OneBotClient()
        event = dict(message_id=1, message_type='private', user_id=523155725, self_id=9,
                     message=[{'type':'text','data':{'text':'/config set ADMIN_QQ hidden-secret'}}])
        output = io.StringIO()
        with patch.object(onebot.OneBotSender, 'send', new=AsyncMock(return_value=SendResult('ok'))), \
             patch.object(client._queue, 'submit', new=AsyncMock()) as queue, contextlib.redirect_stdout(output):
            await client._process(event)
        queue.assert_not_called()
        self.assertNotIn('hidden-secret', output.getvalue())
        self.assertNotIn('message.received', output.getvalue())

    async def test_log_snapshot_is_finite_and_redacted(self):
        fake_root = self.root/'project'
        (fake_root/'logs').mkdir(parents=True)
        (fake_root/'logs'/'qbot.log').write_text('old\noriginal-secret-123456\nlatest\n')
        with patch.object(admin, 'ROOT', fake_root):
            text = admin.recent_logs(2)
        self.assertNotIn('original-secret-123456', text)
        self.assertNotIn('old', text)
        self.assertIn('latest', text)
        with self.assertRaises(ValueError):
            admin.recent_logs(101)

    async def test_start_is_not_exposed(self):
        _, reply, _ = await self.request('/bot start')
        self.assertIn('不提供私聊启动', reply)

    async def test_help_includes_admin_commands_only_in_admin_private_chat(self):
        handled, reply, _ = await self.request('/help')
        self.assertTrue(handled)
        for command in ['/reset', '/config set', '/sticker rename', '/sticker delete',
                        '/bot reload', '/bot log', '/bot stop']:
            self.assertIn(command, reply)
        for kwargs in [{'uid':'999999'}, {'group':True}, {'channel':'official'}]:
            handled, reply, _ = await self.request('/help', **kwargs)
            self.assertFalse(handled, kwargs)
            self.assertEqual(reply, '')

    def test_candidate_validation_does_not_overwrite_external_edits(self):
        def validate(*args, **kwargs):
            self.env.write_text('external edit', encoding='utf-8')
            return NS(returncode=0)
        with patch.object(admin.subprocess, 'run', side_effect=validate):
            with self.assertRaisesRegex(ValueError, '其他操作修改'):
                admin.update_config('TOOL_CALL_NOTICE_SEC', '45')
        self.assertEqual(self.env.read_text(), 'external edit')

    def test_delete_rolls_back_on_index_failure(self):
        name = self.image()
        real_replace = os.replace
        def fail_index(src, dest):
            if Path(dest) == stickers._INDEX_FILE:
                raise OSError('disk full')
            return real_replace(src, dest)
        with patch.object(stickers.os, 'replace', side_effect=fail_index):
            with self.assertRaises(OSError):
                stickers.admin_edit(name)
        self.assertTrue((self.images/name).is_file())
        self.assertIn(name, stickers._FILES)
        self.assertIn(name, json.loads(stickers._INDEX_FILE.read_text())['files'])

    def test_worker_runs_fixed_script_and_records_result(self):
        jobs = self.root/'data'/'admin'/'jobs'
        jobs.mkdir(parents=True)
        fake_fcntl = NS(LOCK_EX=1, LOCK_NB=2, flock=Mock())
        for action, exit_code, expected in [('reload',0,'ok'), ('stop',1,'failed')]:
            jid = ('a' if action=='reload' else 'b') * 12
            with patch.dict(sys.modules, {'fcntl': fake_fcntl}), patch.object(admin_job, 'ROOT', self.root), \
                 patch.object(admin_job.subprocess, 'run', return_value=NS(returncode=exit_code)) as run:
                admin_job.run(action, jid)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['bash', str(self.root/'start.sh'), action])
            self.assertEqual(kwargs['env']['PYTHON_CMD'], sys.executable)
            record = json.loads((jobs/(jid+'.json')).read_text())
            self.assertEqual(record['status'], expected)
        fake_fcntl.flock.side_effect = BlockingIOError
        with patch.dict(sys.modules, {'fcntl': fake_fcntl}), patch.object(admin_job, 'ROOT', self.root), \
             patch.object(admin_job.subprocess, 'run') as run:
            admin_job.run('reload', 'c'*12)
        run.assert_not_called()
        self.assertEqual(json.loads((jobs/('c'*12+'.json')).read_text())['status'], 'busy')

    def test_log_preserves_control_status_when_body_is_truncated(self):
        jobs = admin.private_dir(admin.DATA_DIR/'jobs')
        (jobs/'sample.json').write_text(json.dumps({'id':'sample','action':'reload','status':'ok'}))
        fake_root = self.root/'project'
        (fake_root/'logs').mkdir(parents=True)
        (fake_root/'logs'/'qbot.log').write_text('x'*15000)
        with patch.object(admin, 'ROOT', fake_root):
            text = admin.recent_logs()
        self.assertIn('sample reload：ok', text)
        self.assertLess(len(text), 12000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
