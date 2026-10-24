"""Storage/source contracts only; semantic preference decisions use live_profile_memory_check."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import runtime, tools, user_profile as up
from src.tools import profile_provider


class ProfileMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.events = []
        def event(name, **fields):
            self.events.append(dict(event=name, **fields))
        for target, name, value in ((up, 'PROFILES_DIR', str(self.root)),
                                    (up, 'log_event', event), (profile_provider, 'log_event', event)):
            manager = patch.object(target, name, value)
            manager.start()
            self.addCleanup(manager.stop)
        self.context = tools.ChatContext(conv_id='g', user_id='a', user_name='同名',
            extra={'current_message_id':'current', 'actors':{'current':('a','同名'),'other':('b','同名')}},
            allowed_tools=frozenset({'manage_user_profile'}))

    def call(self, **args):
        return tools.execute('manage_user_profile', json.dumps({'action':'remember','info':'偏好简短回答',**args}), self.context)

    def test_current_source_defaults_correctly_with_multiple_authors(self):
        self.assertIn('已记住', self.call())
        self.assertIn('偏好简短回答', up.list_profile('g','a'))
        self.assertEqual('暂无记录。', up.list_profile('g','b'))

    def test_historical_source_rejected_then_correct_current_source_succeeds(self):
        result = self.call(source_message_id='old_history_id')
        self.assertIn('未执行', result)
        self.assertIn('current', result)
        self.assertFalse(list(self.root.rglob('*.md')))
        self.assertEqual('rejected', self.events[-1]['status'])
        self.assertIn('已记住', self.call(source_message_id='current'))

    def test_other_author_cannot_be_selected_even_with_valid_message_id(self):
        self.assertIn('未执行', self.call(source_message_id='other'))
        self.assertFalse(list(self.root.rglob('*.md')))

    def test_ambiguous_context_without_current_message_still_requires_source(self):
        self.context.extra['current_message_id'] = ''
        self.assertIn('未执行', self.call())
        self.assertFalse(list(self.root.rglob('*.md')))

    def test_persist_event_only_after_commit(self):
        lease = runtime.RoundLease('g', None)
        self.addCleanup(lease.revoke)
        with runtime.use(lease):
            self.call()
            self.assertFalse(list(self.root.rglob('*.md')))
            self.assertEqual('staged', self.events[-1]['status'])
            self.assertFalse(any(e['event']=='profile.persist' for e in self.events))
            lease.commit()
        self.assertEqual('profile.persist', self.events[-1]['event'])
        self.assertIn('偏好简短回答', up.list_profile('g','a'))

    def test_revoked_write_never_reports_persisted(self):
        lease = runtime.RoundLease('g', None)
        with runtime.use(lease):
            self.call()
            lease.revoke()
        self.assertFalse(list(self.root.rglob('*.md')))
        self.assertFalse(any(e['event']=='profile.persist' for e in self.events))

    def test_conflict_prevents_persist_event_and_preserves_external_change(self):
        lease = runtime.RoundLease('g', None)
        self.addCleanup(lease.revoke)
        with runtime.use(lease):
            self.call()
            path = Path(up._get_profile_path('g','a'))
            path.parent.mkdir(parents=True)
            path.write_text('external change', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                lease.commit()
        self.assertEqual('external change', path.read_text(encoding='utf-8'))
        self.assertFalse(any(e['event']=='profile.persist' for e in self.events))

    def test_duplicate_does_not_write_or_report_another_persist(self):
        self.call()
        self.events.clear()
        self.assertIn('已经记录', self.call())
        self.assertEqual('duplicate', self.events[-1]['status'])
        self.assertFalse(any(e['event']=='profile.persist' for e in self.events))

    def test_privacy_rejected_without_values_in_telemetry(self):
        self.assertIn('隐私保护', self.call(info='手机号13800138000'))
        self.assertEqual('privacy', self.events[-1]['reason'])
        self.assertNotIn('13800138000', json.dumps(self.events))
        self.assertFalse(list(self.root.rglob('*.md')))

    def test_io_failure_is_logged_without_success(self):
        with patch.object(runtime, 'write_text', side_effect=PermissionError('blocked')):
            self.call()
        self.assertEqual('failed', self.events[-1]['status'])
        self.assertFalse(any(e['event']=='profile.persist' for e in self.events))

    def test_forget_has_real_persist_event(self):
        self.call()
        self.events.clear()
        self.assertIn('已删除', self.call(action='forget', info='简短'))
        self.assertEqual('forget', self.events[-1]['action'])
        self.assertEqual('profile.persist', self.events[-1]['event'])
        self.assertEqual('暂无记录。', up.list_profile('g','a'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
