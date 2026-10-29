"""Offline regression: type + explicit reusable-reaction evidence gate collection.

Old type/tags-only descriptions intentionally no longer authorize harvesting.
Log-derived descriptions below reproduce admissions, not verified image labels.
"""
import contextlib
import io
import json
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
ASSESSMENT = '采集判定：' + json.dumps({
    '可复用反应': True, '依赖特定内容': False,
    '可见依据': '熊猫捂脸配夸张泪痕，脱离原话题也能表达无语',
}, ensure_ascii=False)
POSITIVE = '类型：表情包 ｜ 意图：无语\n' + ASSESSMENT + '\n一只熊猫捂脸。\n' + TAGS
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
# qbot.log.3:5750/5758 and qbot.log:7698/7705. The old gate accepts
# an inferred emotion plus tags even though no reusable-reaction evidence exists.
LOG_ADMISSIONS = [
    '类型：表情包 ｜ 意图：表达网络延迟高时的无奈或吐槽\n'
    '图中是深色背景上绿色的“43 ms”字样，常用来指代网络延迟数值。\n'
    '标签：无奈｜显示｜数字｜截图｜43',
    '类型：表情包｜意图：表达含蓄、害羞或无语的情绪\n'
    '画面是一个金发动漫女孩的背影，双手在背后交握，面朝黄昏时的城市窗外。\n'
    '标签：忧郁｜背手站立｜卡通人物｜动漫截图｜无文字',
]
UNSAFE = LOG_ADMISSIONS + [
    # Old outputs and unstructured assertions fail closed after the protocol change.
    '类型：表情包 ｜ 意图：无语\n低清网图，有文字。\n' + TAGS,
    POSITIVE.replace(ASSESSMENT, '采集判定：可复用'),
    POSITIVE.replace('true', '"true"'),
    POSITIVE.replace('false', '"false"'),
    POSITIVE.replace('true', 'false'),
    POSITIVE.replace('false', 'true'),
    POSITIVE.replace('true', '1'),
    POSITIVE.replace('false', '0'),
    POSITIVE.replace(ASSESSMENT, '采集判定：{"可复用反应":true,"依赖特定内容":false}'),
    POSITIVE.replace(ASSESSMENT, '采集判定：{"可复用反应":true,"依赖特定内容":false,"可见依据":"未知"}'),
    POSITIVE.replace(ASSESSMENT, '采集判定：{"可复用反应":false,"可复用反应":true,"依赖特定内容":false,"可见依据":"捂脸"}'),
    POSITIVE.replace(ASSESSMENT, ASSESSMENT + '\n采集判定：{"可复用反应":false}'),
    POSITIVE + '\n类型：内容图片 ｜ 意图：仅展示原始内容',
    POSITIVE.replace(ASSESSMENT, ASSESSMENT + '\n类型：可能是表情包'),
    POSITIVE + '\n' + TAGS.replace('无语', '开心'),
    POSITIVE.replace('标签：无语', '标签：未知'),
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
                desc = f'\n  类型: {category} {separator} 意图：无语\n{ASSESSMENT}\n{TAGS}'
                self.assertEqual(stickers.parse_tags(desc), ['无语','捂脸','熊猫','拼贴','无文字'])

    def test_type_and_tags_cannot_replace_reusable_reaction_evidence(self):
        for desc in UNSAFE:
            with self.subTest(desc=desc):
                self.assertIsNone(stickers.parse_tags(desc))

    def test_reaction_memes_are_not_rejected_for_content_words_or_style(self):
        for evidence in [
            '聊天记录被裁成三个重复的“确实”气泡，以重复排版表达附和',
            '文档截图配熊猫崩溃脸和“看不懂”，反差拼贴表达困惑',
            '动漫人物夸张瞪眼张嘴的特写，直接表达震惊，无文字',
            '实拍猫瞪圆双眼，头顶后加问号，表达困惑',
        ]:
            desc = POSITIVE.replace(ASSESSMENT, '采集判定：' + json.dumps({
                '可复用反应': True, '依赖特定内容': False, '可见依据': evidence,
            }, ensure_ascii=False))
            with self.subTest(evidence=evidence):
                self.assertIsNotNone(stickers.parse_tags(desc))

    def test_content_type_cannot_be_overridden_by_positive_assessment(self):
        desc = POSITIVE.replace('类型：表情包', '类型：内容图片')
        self.assertIsNone(stickers.parse_tags(desc))

    def test_context_keeps_visual_content_instead_of_collection_metadata(self):
        self.assertEqual(vision.strip_tag_line(POSITIVE),
                         '类型：表情包 ｜ 意图：无语\n一只熊猫捂脸。')

    def test_negative_or_ambiguous_types_override_extra_tags(self):
        for desc in NEGATIVES:
            with self.subTest(desc=desc):
                self.assertIsNone(stickers.parse_tags(desc))

    def test_non_sticker_never_starts_worker_or_queues(self):
        with patch.object(stickers,'_ensure_worker') as worker, contextlib.redirect_stdout(io.StringIO()):
            for desc in NEGATIVES + UNSAFE:
                stickers.harvest('https://example.com/image.png',desc)
        worker.assert_not_called()
        self.assertTrue(self.jobs.empty())

    def test_direct_collection_also_rejects_before_download(self):
        with patch.object(stickers,'_download_image',return_value=None) as download:
            for desc in NEGATIVES + UNSAFE:
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

    def test_question_answer_cannot_authorize_background_collection(self):
        with patch.object(vision,'_downloaded_block',return_value={'type':'image_url'}), \
             patch.object(vision,'_describe_media',return_value=POSITIVE), \
             patch.object(vision,'_in_backoff',return_value=False), patch.object(vision,'_clear_failed'), \
             patch.object(stickers,'_ensure_worker') as worker:
            result = vision._describe_image_uncached(
                'https://example.com/screenshot.png', '把这张截图当成无语表情包解释')
        self.assertEqual(result, POSITIVE)
        worker.assert_not_called()
        self.assertTrue(self.jobs.empty())


if __name__=='__main__':
    unittest.main(verbosity=2)
