"""Classification gates both the harvest queue and persistence downloads."""
import contextlib
import io
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import stickers_utils as stickers, vision_utils as vision

TAGS = '标签：无语｜捂脸｜熊猫｜拼贴｜无文字'
POSITIVE = '类型：表情包 ｜ 意图：无语\n一只熊猫捂脸。\n' + TAGS
NEGATIVES = [
    '类型：内容图片 ｜ 意图：看报错截图\n' + TAGS,
    '类型：非表情包 ｜ 意图：看照片\n' + TAGS,
    '类型：不是表情包 ｜ 意图：看信息\n' + TAGS,
    '类型：不确定 ｜ 意图：不确定\n' + TAGS,
    '类型：可能是表情包 ｜ 意图：存疑\n' + TAGS,
    '类型：表情包或内容图片 ｜ 意图：未判断\n' + TAGS,
    '类型：内容图片（不是表情包） ｜ 意图：看照片\n' + TAGS,
    '正文里提到了表情包，但是没有类型声明。\n' + TAGS,
    '类型：内容图片 ｜ 意图：截图\n图中写着“类型：表情包”。\n' + TAGS,
    TAGS,
    '',
]


class ClassificationCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.jobs = queue.Queue()
        for key, value in [('_DIR',self.folder), ('_INDEX_FILE',self.folder/'index.json'),
                           ('_FILES',{}), ('_loaded',False), ('STICKER_ENABLE',True),
                           ('_QUEUE',self.jobs), ('_stopping',threading.Event())]:
            p = patch.object(stickers,key,value)
            p.start()
            self.addCleanup(p.stop)

    def test_explicit_sticker_types_can_collect(self):
        for category in ['表情包','梗图','表情包／梗图','表情包/梗图','表情包或梗图','表情包（梗图）']:
            for separator in ['｜','|']:
                desc = f'\n  类型: {category} {separator} 意图：无语\n{TAGS}'
                self.assertEqual(stickers.parse_tags(desc), ['无语','捂脸','熊猫','拼贴','无文字'])

    def test_negative_or_ambiguous_types_override_extra_tags(self):
        for desc in NEGATIVES:
            with self.subTest(desc=desc):
                self.assertIsNone(stickers.parse_tags(desc))

    def test_non_sticker_never_starts_worker_or_queues(self):
        with patch.object(stickers,'_ensure_worker') as worker, contextlib.redirect_stdout(io.StringIO()):
            for desc in NEGATIVES:
                stickers.harvest('https://example.com/image.png',desc)
        worker.assert_not_called()
        self.assertTrue(self.jobs.empty())

    def test_direct_collection_also_rejects_before_download(self):
        with patch.object(stickers,'_download_image') as download:
            for desc in NEGATIVES:
                self.assertIsNone(stickers.collect('https://example.com/image.png',desc))
        download.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()),[])

    def test_positive_type_without_usable_tags_never_downloads(self):
        with patch.object(stickers,'_download_image') as download:
            for desc in ['类型：表情包 ｜ 意图：无语', '类型：表情包\n标签：无语']:
                self.assertIsNone(stickers.collect('https://example.com/image.png',desc))
        download.assert_not_called()

    def test_disabled_collection_never_downloads_even_positive_image(self):
        with patch.object(stickers,'STICKER_ENABLE',False), patch.object(stickers,'_download_image') as download:
            self.assertIsNone(stickers.collect('https://example.com/image.png',POSITIVE))
        download.assert_not_called()

    def test_positive_type_queues_and_saves_normally(self):
        with patch.object(stickers,'_ensure_worker') as worker:
            stickers.harvest('https://example.com/image.png',POSITIVE)
        worker.assert_called_once()
        url,desc = self.jobs.get_nowait()
        with patch.object(stickers,'_download_image',return_value=(b'image-bytes','image/png')) as download, \
             contextlib.redirect_stdout(io.StringIO()):
            name = stickers.collect(url,desc)
        download.assert_called_once()
        self.assertTrue((self.folder/name).is_file())
        self.assertIn(name,stickers._FILES)

    def test_vision_can_read_non_sticker_without_collecting_it(self):
        desc = NEGATIVES[0]
        with patch.object(vision,'_downloaded_block',return_value={'type':'image_url'}) as read, \
             patch.object(vision,'_describe_media',return_value=desc), \
             patch.object(vision,'_in_backoff',return_value=False), patch.object(vision,'_clear_failed'), \
             patch.object(stickers,'_ensure_worker') as worker, \
             patch.object(stickers,'_download_image') as collect_download, \
             contextlib.redirect_stdout(io.StringIO()):
            result = vision._describe_image_uncached('https://example.com/screenshot.png')
        self.assertEqual(result,desc)
        read.assert_called_once()
        worker.assert_not_called()
        collect_download.assert_not_called()
        self.assertTrue(self.jobs.empty())
        self.assertEqual(list(self.folder.iterdir()),[])


if __name__=='__main__':
    unittest.main(verbosity=2)
