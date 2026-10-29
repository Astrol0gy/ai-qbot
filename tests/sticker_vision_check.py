"""Main-model visual inspection of retrieved stickers; no real API or QQ calls."""
import base64
import copy
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('DEEPSEEK_API_KEY', 'offline-test-key')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, stickers_utils
from src.channels import core
from src.channels.base import IncomingMessage
from src.tools.chat_provider import ChatProvider, ReplyOutbox
from src.tools import ChatContext
from PIL import Image


class StickerVisionCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit.AuditCases.setUp(self)
        self.path = Path(self.tmp.name) / 'happy.png'
        Image.new('RGB', (600, 400), 'red').save(self.path)
        self.marker = f'[img]{self.path}[/img]'
        self.resolve = patch.object(stickers_utils, 'resolve',
            side_effect=lambda p: str(self.path) if str(p) == str(self.path) else None)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    async def test_main_request_receives_real_candidate_image_before_final_selection(self):
        model = audit.Model([
            audit.response(calls=[('load_skill', {'name': '图片理解与表情包'})]),
            audit.response(calls=[('send_sticker', {'query': '开心'})]),
            audit.response(self.marker)])
        create = model.create
        async def capture(**kw):
            return await create(**copy.deepcopy(kw))
        model.create = capture
        real_execute = dc.execute_tool
        def execute(name, args, ctx):
            return '候选1 标签：开心\n' + self.marker if name == 'send_sticker' else real_execute(name, args, ctx)
        sender = audit.Sender()
        with patch.object(dc, 'client', model), patch.object(dc, 'execute_tool', execute):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='a',
                text='开心，发张表情', is_group=False), sender)
        blocks = [b for m in model.requests[-1]['messages'] if isinstance(m, dict)
                  and isinstance(m.get('content'), list) for b in m['content']
                  if b.get('type') == 'image_url']
        self.assertEqual(len(blocks), 1, 'RAG结果必须作为图片输入同一主模型，而不是只给标签')
        url = blocks[0]['image_url']['url']
        self.assertTrue(url.startswith('data:image/'))
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
            self.assertLessEqual(max(image.size), 512)
        self.assertEqual(sender.sent[0]['stickers'], [str(self.path)])
        self.assertNotIn('data:image', str(dc._conversations['g']))

    async def test_unseen_sticker_cannot_be_submitted(self):
        ctx = ChatContext(outbox=ReplyOutbox(), extra={'sticker_previews': frozenset()})
        out = ChatProvider._reply(ctx, text=self.marker)
        self.assertFalse(ctx.outbox.has_items)
        self.assertIn('看图', out)

    async def test_long_animation_is_rejected_before_preview_decode(self):
        from src.sticker_preview import build_previews
        self.path = Path(self.tmp.name) / 'too-many-frames.gif'
        frames = [Image.new('RGB', (2, 2), 'red' if i % 2 else 'blue') for i in range(121)]
        frames[0].save(self.path, save_all=True, append_images=frames[1:], duration=20)
        preview, viewed = build_previews(f'[img]{self.path}[/img]')
        self.assertIsNone(preview)
        self.assertFalse(viewed)

    async def test_unreadable_candidate_is_not_sent(self):
        self.path.write_bytes(b'not an image')
        model = audit.Model([
            audit.response(calls=[('load_skill', {'name': '图片理解与表情包'})]),
            audit.response(calls=[('send_sticker', {'query': '开心'})]),
            audit.response(self.marker), audit.response('这次用文字表达开心')])
        real_execute = dc.execute_tool
        def execute(name, args, ctx):
            return self.marker if name == 'send_sticker' else real_execute(name, args, ctx)
        sender = audit.Sender()
        with patch.object(dc, 'client', model), patch.object(dc, 'execute_tool', execute):
            await core.handle_message(IncomingMessage(conv_id='g', user_id='a',
                text='发张开心表情', is_group=False), sender)
        self.assertEqual(sender.sent[0]['text'], '这次用文字表达开心')
        self.assertFalse(sender.sent[0]['stickers'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
