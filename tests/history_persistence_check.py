"""Delivered history and compressed summaries survive reload; drafts never do."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('DEEPSEEK_API_KEY', 'offline-test-key')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, runtime
from src.history_store import HistoryStore


class HistoryPersistenceCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        self.root = Path(self.tmp.name) / 'history'
        self.store = HistoryStore(self.root)
        p = patch.object(dc, '_history_store', self.store)
        p.start()
        self.addCleanup(p.stop)

    async def test_compressed_summary_and_actual_delivery_survive_reload(self):
        summary = [{'role': 'user', 'content': dc._SUMMARY_MARKER + '\n约好周四见'},
                   {'role': 'assistant', 'content': '已了解'}]
        dc._store_history('g', summary)
        lease = runtime.RoundLease('g', None)
        lease.history = ('g', summary + [{'role': 'user', 'content': '今天几号'},
                                       {'role': 'assistant', 'content': '未投递草稿'}])
        dc.commit_delivery(lease, '实际已发送文本')
        restored = HistoryStore(self.root).load_all()['g']
        self.assertIn('约好周四见', restored[0]['content'])
        self.assertEqual(restored[-1]['content'], '实际已发送文本')
        self.assertNotIn('未投递草稿', str(restored))

    async def test_revoked_draft_never_replaces_saved_delivery(self):
        dc._store_history('g', [{'role': 'user', 'content': '旧问题'},
                                {'role': 'assistant', 'content': '已发送'}])
        lease = runtime.RoundLease('g', None)
        with runtime.use(lease):
            dc._stage_history('g', [{'role': 'assistant', 'content': '不能入库的草稿'}])
        lease.revoke()
        self.assertEqual(self.store.load_all()['g'][-1]['content'], '已发送')

    async def test_reset_does_not_resurrect_history_on_restart(self):
        dc._store_history('g', [{'role': 'user', 'content': '记住这句'},
                                {'role': 'assistant', 'content': '已回答'}])
        dc.clear_history('g')
        self.assertNotIn('g', self.store.load_all())

    async def test_corrupt_file_does_not_prevent_other_conversations_loading(self):
        dc._store_history('g', [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}])
        (self.root / 'bad.json').write_text('{broken', encoding='utf8')
        self.assertEqual(self.store.load_all()['g'][-1]['content'], 'b')

    async def test_history_failure_does_not_reclassify_a_delivered_reply(self):
        with patch.object(self.store, 'save', side_effect=OSError('full disk')):
            dc._store_history('g', [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}])
        self.assertEqual(dc._conversations['g'][-1]['content'], 'b')


if __name__ == '__main__':
    unittest.main(verbosity=2)
