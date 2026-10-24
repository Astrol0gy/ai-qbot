"""离线验证表情包 CLIP RAG：排序、权重、增量 embedding、缓存、top3 输出与回退。

不下载 CLIP 模型、不启动 ONNX；只用一个假 encoder 验证索引与检索逻辑。
运行：python -u tests/sticker_rag_check.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-key")

from src import sticker_rag

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 32
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 32


def entry(name, tags, *, hash_=None, path=None):
    return {
        "name": name,
        "tags": tuple(tags),
        "hash": hash_ or f"hash-{name}",
        "path": path or f"/lib/{name}",
    }


def vectors(entries):
    return [
        {
            **item,
            "image_vec": [1.0, 0.0, 0.0] if item["name"].startswith("a") else [0.0, 1.0, 0.0],
            "text_vec": [1.0, 0.0, 0.0],
        }
        for item in entries
    ]


class FakeEncoder:
    def __init__(self, image_vectors, text_vectors):
        self.image_vectors = image_vectors
        self.text_vectors = text_vectors
        self.image_batches = []
        self.text_batches = []

    def _text(self, text):
        for key, vec in self.text_vectors.items():
            if key in text:
                return vec
        return [0.0, 0.0, 1.0]

    def encode_images(self, paths):
        self.image_batches.append(list(paths))
        return [self.image_vectors[path] for path in paths]

    def encode_texts(self, texts):
        self.text_batches.append(list(texts))
        return [self._text(text) for text in texts]


class RankTests(unittest.TestCase):
    def test_rank_candidates_orders_by_weighted_similarity_and_returns_top3(self):
        entries = [
            {"name": "a.gif", "tags": ("无语",), "path": "/a", "image_vec": [1, 0, 0], "text_vec": [0.8, 0.6, 0]},
            {"name": "b.gif", "tags": ("震惊",), "path": "/b", "image_vec": [0.6, 0.8, 0], "text_vec": [1, 0, 0]},
            {"name": "c.gif", "tags": ("开心",), "path": "/c", "image_vec": [-1, 0, 0], "text_vec": [-1, 0, 0]},
            {"name": "d.gif", "tags": ("其他",), "path": "/d", "image_vec": [0, 1, 0], "text_vec": [0, 1, 0]},
        ]
        out = sticker_rag.rank_candidates([1, 0, 0], entries, top_k=3)
        self.assertEqual([c.name for c in out], ["a.gif", "b.gif", "d.gif"])
        self.assertEqual([c.rank for c in out], [1, 2, 3])
        self.assertAlmostEqual(out[0].score, 0.7 * 1 + 0.3 * 0.8, places=6)
        self.assertAlmostEqual(out[1].score, 0.7 * 0.6 + 0.3 * 1.0, places=6)

    def test_image_and_text_weights_control_ranking(self):
        entries = [
            {"name": "image.gif", "tags": ("图片像",), "path": "/i", "image_vec": [1, 0], "text_vec": [0, 1]},
            {"name": "text.gif", "tags": ("文字像",), "path": "/t", "image_vec": [0, 1], "text_vec": [1, 0]},
        ]
        image_first = sticker_rag.rank_candidates([1, 0], entries, image_weight=0.8)
        text_first = sticker_rag.rank_candidates([1, 0], entries, image_weight=0.2)
        self.assertEqual(image_first[0].name, "image.gif")
        self.assertEqual(text_first[0].name, "text.gif")

    def test_exact_tag_match_gets_lexical_boost(self):
        entries = [
            {"name": "generic.gif", "tags": ("搞笑",), "path": "/g", "image_vec": [1, 0], "text_vec": [1, 0]},
            {"name": "exact.gif", "tags": ("无语",), "path": "/e", "image_vec": [0.9, 0.435], "text_vec": [0.9, 0.435]},
        ]
        without = sticker_rag.rank_candidates([1, 0], entries, top_k=2)
        with_boost = sticker_rag.rank_candidates([1, 0], entries, top_k=2, query_text="无语")
        self.assertEqual(without[0].name, "generic.gif")
        self.assertEqual(with_boost[0].name, "exact.gif")

    def test_min_score_filters_weak_candidates(self):
        entries = [
            {"name": "good.gif", "tags": (), "path": "/g", "image_vec": [1, 0], "text_vec": [1, 0]},
            {"name": "weak.gif", "tags": (), "path": "/w", "image_vec": [0, 1], "text_vec": [0, 1]},
        ]
        out = sticker_rag.rank_candidates([1, 0], entries, min_score=0.5)
        self.assertEqual([c.name for c in out], ["good.gif"])

    def test_empty_entries_returns_empty(self):
        self.assertEqual(sticker_rag.rank_candidates([1, 0], []), [])


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sticker_rag_")
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "embeddings.json"

    def rag(self, encoder):
        return sticker_rag.StickerRag(
            self.cache, encoder_factory=lambda: encoder, background=False, min_score=-1.0
        )

    def test_new_sticker_is_embedded_incrementally_without_touching_old_entries(self):
        first = [entry("a.gif", ("无语",)), entry("b.gif", ("震惊",))]
        encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0], "/lib/c.gif": [0, 0, 1]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0], "开心": [0, 0, 1]},
        )
        rag = self.rag(encoder)
        rag.search("无语", snapshot=first)
        self.assertEqual(encoder.image_batches, [["/lib/a.gif", "/lib/b.gif"]])
        self.assertEqual(len(encoder.text_batches[0]), 2)  # 两张图的名称+标签
        encoder.image_batches.clear()
        encoder.text_batches.clear()

        rag.notify_added("c.gif")
        out = rag.search("开心", snapshot=first + [entry("c.gif", ("开心",))])
        self.assertEqual(out[0].name, "c.gif")
        self.assertEqual(encoder.image_batches, [["/lib/c.gif"]])
        self.assertEqual(len(encoder.text_batches[0]), 1)  # 只编码新图，不碰旧图
        self.assertEqual(encoder.text_batches[1], ["开心"])  # 只编码查询词

    def test_notify_added_starts_background_worker_for_new_entry(self):
        first = [entry("a.gif", ("无语",))]
        new = entry("b.gif", ("震惊",))
        build_encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0]},
        )
        self.rag(build_encoder).search("无语", snapshot=first)

        encoder = FakeEncoder(
            {"/lib/b.gif": [0, 1, 0]},
            {"震惊": [0, 1, 0]},
        )
        rag = sticker_rag.StickerRag(
            self.cache, encoder_factory=lambda: encoder, background=True, min_score=-1.0
        )
        self.addCleanup(rag.close)
        with patch.object(sticker_rag, "default_snapshot", return_value=first + [new]):
            rag.notify_added("b.gif")
            deadline = time.monotonic() + 2.0
            while not rag._model_ready and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(rag._model_ready)
        self.assertEqual(encoder.image_batches, [["/lib/b.gif"]])
        self.assertIn("b.gif", json.loads(self.cache.read_text(encoding="utf-8"))["items"])

    def test_background_search_uses_complete_cache_without_falling_back(self):
        first = [entry("a.gif", ("无语",)), entry("b.gif", ("震惊",))]
        build_encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0]},
        )
        self.rag(build_encoder).search("无语", snapshot=first)

        encoder = FakeEncoder({}, {"无语": [1, 0, 0]})
        rag = sticker_rag.StickerRag(
            self.cache, encoder_factory=lambda: encoder, background=True, min_score=-1.0
        )
        self.addCleanup(rag.close)
        out = rag.search("无语", snapshot=first)
        self.assertEqual(out[0].name, "a.gif")
        self.assertEqual(encoder.image_batches, [])

    def test_cache_is_reused_without_reencoding_existing_stickers(self):
        first = [entry("a.gif", ("无语",)), entry("b.gif", ("震惊",))]
        encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0]},
        )
        self.rag(encoder).search("无语", snapshot=first)

        class CacheOnlyEncoder(FakeEncoder):
            def encode_images(self, paths):
                raise AssertionError("已有缓存不应重新编码图片")

        second_encoder = CacheOnlyEncoder({}, {"无语": [1, 0, 0]})
        out = self.rag(second_encoder).search("无语", snapshot=first)
        self.assertEqual(out[0].name, "a.gif")
        self.assertEqual(second_encoder.text_batches, [["无语"]])

    def test_changed_image_hash_reencodes_only_that_image(self):
        first = [entry("a.gif", ("无语",)), entry("b.gif", ("震惊",))]
        encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0]},
        )
        self.rag(encoder).search("无语", snapshot=first)

        changed = [entry("a.gif", ("无语",), hash_="new-image-hash"), entry("b.gif", ("震惊",))]
        encoder.image_batches.clear()
        encoder.text_batches.clear()
        self.rag(encoder).search("无语", snapshot=changed)
        self.assertEqual(encoder.image_batches, [["/lib/a.gif"]])
        self.assertEqual(encoder.text_batches, [["无语"]])

    def test_changed_tags_reencodes_text_only(self):
        first = [entry("a.gif", ("无语",)), entry("b.gif", ("震惊",))]
        encoder = FakeEncoder(
            {"/lib/a.gif": [1, 0, 0], "/lib/b.gif": [0, 1, 0]},
            {"无语": [1, 0, 0], "震惊": [0, 1, 0], "开心": [0, 0, 1]},
        )
        self.rag(encoder).search("无语", snapshot=first)

        changed = [entry("a.gif", ("开心",)), entry("b.gif", ("震惊",))]
        encoder.image_batches.clear()
        encoder.text_batches.clear()
        self.rag(encoder).search("开心", snapshot=changed)
        self.assertEqual(encoder.image_batches, [])
        self.assertEqual(len(encoder.text_batches[0]), 1)  # 只重算 a.gif 的标签
        self.assertEqual(encoder.text_batches[1], ["开心"])

    def test_notify_added_without_model_does_not_start_worker(self):
        with patch.object(sticker_rag, "_model_dir", return_value=Path(self.tmp.name)), \
                patch.object(sticker_rag, "get_rag") as get_rag:
            sticker_rag.notify_added("a.gif")
        get_rag.assert_not_called()

    def test_save_notifies_rag_for_incremental_embedding(self):
        from src import stickers_utils

        with tempfile.TemporaryDirectory(prefix="sticker_lib_") as lib:
            with patch.object(stickers_utils, "_DIR", Path(lib)), \
                    patch.object(stickers_utils, "_INDEX_FILE", Path(lib) / "index.json"), \
                    patch.object(stickers_utils, "_FILES", {}), \
                    patch.object(stickers_utils, "_loaded", False), \
                    patch("src.sticker_rag.notify_added") as notify:
                name = stickers_utils.save(PNG_A, "image/png", ["无语", "捂脸", "猫", "实拍", "无文字"])
                self.assertIsNotNone(name)
                notify.assert_called_once_with(name)


class SendStickerTests(unittest.TestCase):
    def setUp(self):
        from src.tools import image_provider

        self.provider = image_provider.ImageProvider
        self.image_provider = image_provider

    def test_send_sticker_returns_three_ranked_paths(self):
        candidates = [
            sticker_rag.Candidate(1, "a.gif", ("无语",), "/lib/a.gif", 0.82),
            sticker_rag.Candidate(2, "b.gif", ("尴尬",), "/lib/b.gif", 0.71),
            sticker_rag.Candidate(3, "c.gif", ("摆烂",), "/lib/c.gif", 0.63),
        ]
        with patch.object(self.image_provider.sticker_rag, "search", return_value=candidates):
            out = self.provider._send_sticker("无语")
        ranked = re.findall(r"^\d+\. \[img\].*?\[/img\]$", out, re.MULTILINE)
        self.assertEqual(len(ranked), 3)
        self.assertIn("1. [img]/lib/a.gif[/img]", out)
        self.assertIn("2. [img]/lib/b.gif[/img]", out)
        self.assertIn("3. [img]/lib/c.gif[/img]", out)
        self.assertIn("标签：无语", out)
        self.assertIn("都不贴合", out)

    def test_send_sticker_falls_back_to_tag_pick_when_rag_unavailable(self):
        picked = self.image_provider.stickers_utils.Sticker(
            name="a.gif", tags=["无语", "捂脸"], path="/lib/a.gif"
        )
        with patch.object(self.image_provider.sticker_rag, "search",
                          side_effect=sticker_rag.RagUnavailable("indexing")), \
                patch.object(self.image_provider.stickers_utils, "pick", return_value=picked):
            out = self.provider._send_sticker("无语")
        self.assertIn("[img]/lib/a.gif[/img]", out)
        self.assertIn("挑中", out)

    def test_send_sticker_reports_no_match_when_rag_and_tags_both_miss(self):
        with patch.object(self.image_provider.sticker_rag, "search",
                          side_effect=sticker_rag.RagUnavailable("indexing")), \
                patch.object(self.image_provider.stickers_utils, "pick", return_value=None):
            out = self.provider._send_sticker("十分罕见的心情")
        self.assertNotIn("[img]", out)
        self.assertIn("库里没有和", out)


class PromptAndSkillTests(unittest.TestCase):
    def test_skill_is_renamed_and_prompts_reference_it(self):
        from src import skills
        from src.tools import chat_provider

        self.assertIsNotNone(skills.get_skill("图片理解与表情包"))
        self.assertIsNone(skills.get_skill("图片理解"))
        self.assertIn("图片理解与表情包", chat_provider.CHAT_POLICY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
