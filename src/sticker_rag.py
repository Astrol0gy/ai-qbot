"""表情包语义检索：Chinese-CLIP 图片/标签 embedding + 余弦相似度 top3。

设计要点：
- 图片用 ONNX Chinese-CLIP 的 image encoder，标签与名称用 text encoder；
- 每个表情包保留 image/text 两份向量，检索分数 = 0.7*图片余弦 + 0.3*文字余弦；
- 缓存写在表情包库的 ``embeddings.json``，按图片 hash 和标签 hash 增量更新；
- 新表情包入库时由 ``stickers_utils.save`` 调 ``notify_added``，只补这一张；
- 模型未加载、索引未就绪或推理失败时抛 ``RagUnavailable``，调用方回退到标签匹配。

ONNX 模型来自 Xenova/chinese-clip-vit-base-patch16 的量化版本（190MB），
运行时只需 onnxruntime/numpy/Pillow/tokenizers，不依赖 torch。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import (
    STICKER_RAG_ENABLE,
    STICKER_RAG_MIN_SCORE,
    STICKER_RAG_MODEL_DIR,
    STICKER_RAG_TOP_K,
)
from .observability import log_event

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "chinese-clip-vit-base-patch16-quantized"
CACHE_VERSION = 1
IMAGE_WEIGHT = 0.7
TEXT_WEIGHT = 1.0 - IMAGE_WEIGHT
MAX_TEXT_TOKENS = 52
IMAGE_SIZE = 224
# CLIP 的 text encoder 是图文对齐训练的，纯文本 query 与标签的余弦不够锐利；
# 给命中标签的候选一个小加分，保证「卖萌」「无语」这类明确标签不会被图片向量挤掉。
TAG_BOOST = 0.12
_TAG_SPLIT = re.compile(r"[｜|/\\、,，;；:：\s_]+")
_SKIP_TAGS = frozenset({"无", "无文字"})


class RagUnavailable(RuntimeError):
    """语义索引暂时不可用；调用方应回退到标签检索。"""


@dataclass(frozen=True)
class Candidate:
    rank: int
    name: str
    tags: tuple[str, ...]
    path: str
    score: float


def _model_dir() -> Path:
    path = Path(STICKER_RAG_MODEL_DIR).expanduser() if STICKER_RAG_MODEL_DIR else (
        ROOT / "data" / "models" / "chinese-clip-vit-base-patch16"
    )
    return path if path.is_absolute() else ROOT / path


def default_cache_path() -> Path:
    from . import stickers_utils

    return Path(stickers_utils.stats()["dir"]) / "embeddings.json"


def default_snapshot() -> list[dict]:
    from . import stickers_utils

    return stickers_utils.snapshot()


def sticker_text(entry: dict) -> str:
    """用于 text encoder 的文本：标签放前面，避免长文件名把标签截断。"""
    stem = str(entry.get("name") or "").rsplit(".", 1)[0]
    tags = " ".join(str(t) for t in (entry.get("tags") or ()))
    return f"{tags} {stem}".strip()


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _lexical_boost(query_text: str, tags: Sequence[str]) -> float:
    """query 与标签完全相等 +0.12，两个字以上的互相包含 +0.06。"""
    if not query_text:
        return 0.0
    tokens = [token for token in _TAG_SPLIT.split(query_text.strip()) if token]
    best = 0.0
    for tag in tags:
        if not tag or tag in _SKIP_TAGS:
            continue
        for token in tokens:
            if token == tag:
                best = max(best, 1.0)
            elif len(token) >= 2 and len(tag) >= 2 and (token in tag or tag in token):
                best = max(best, 0.5)
    return TAG_BOOST * best


def rank_candidates(
    query_vec: Sequence[float],
    entries: Sequence[dict],
    *,
    query_text: str = "",
    top_k: int = 3,
    image_weight: float = IMAGE_WEIGHT,
    min_score: float = -1.0,
) -> list[Candidate]:
    """entries 需带 image_vec/text_vec；返回带 rank 的 top-k。"""
    if top_k <= 0 or not entries:
        return []
    text_weight = 1.0 - image_weight
    scored = []
    for entry in entries:
        image_vec = entry.get("image_vec")
        text_vec = entry.get("text_vec")
        score = 0.0
        weight = 0.0
        if image_vec:
            score += image_weight * _cos(query_vec, image_vec)
            weight += image_weight
        if text_vec:
            score += text_weight * _cos(query_vec, text_vec)
            weight += text_weight
        if weight:
            score /= weight
        score += _lexical_boost(query_text, entry.get("tags") or ())
        if score < min_score:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [
        Candidate(
            rank=index + 1,
            name=entry["name"],
            tags=tuple(entry.get("tags") or ()),
            path=entry["path"],
            score=score,
        )
        for index, (score, entry) in enumerate(scored[:top_k])
    ]


class ClipOnnxEncoder:
    """Chinese-CLIP 量化 ONNX 的薄封装；导入重依赖延迟到第一次使用。"""

    def __init__(self, model_dir: Path | None = None):
        self.model_dir = Path(model_dir or _model_dir())
        self._session = None
        self._tokenizer = None
        self._mean = None
        self._std = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self.model_dir / "model_quantized.onnx"

    def _load(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            for name in ("model_quantized.onnx", "tokenizer.json", "preprocessor_config.json"):
                if not (self.model_dir / name).is_file():
                    raise RagUnavailable(f"缺少模型文件：{self.model_dir / name}")
            try:
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as exc:
                raise RagUnavailable(
                    "缺少 CLIP 运行时依赖，请安装 onnxruntime/numpy/Pillow/tokenizers"
                ) from exc

            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self._input_names = {item.name for item in self._session.get_inputs()}
            self._output_index = {
                item.name: index for index, item in enumerate(self._session.get_outputs())
            }
            tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=MAX_TEXT_TOKENS)
            tokenizer.enable_padding(length=MAX_TEXT_TOKENS)
            self._tokenizer = tokenizer
            config = json.loads(
                (self.model_dir / "preprocessor_config.json").read_text(encoding="utf-8")
            )
            self._mean = np.asarray(config.get("image_mean", [0.48145466, 0.4578275, 0.40821073]),
                                    dtype=np.float32)
            self._std = np.asarray(config.get("image_std", [0.26862954, 0.26130258, 0.27577711]),
                                   dtype=np.float32)
            self._np = np

    def _run(self, feed: dict) -> dict:
        with self._lock:
            outputs = self._session.run(None, feed)
        return {
            name: outputs[index]
            for name, index in self._output_index.items()
        }

    def _text_feed(self, texts: list[str]) -> tuple[dict, "object"]:
        self._load()
        encodings = self._tokenizer.encode_batch(texts)
        ids = self._np.asarray([item.ids for item in encodings], dtype=self._np.int64)
        mask = self._np.asarray([item.attention_mask for item in encodings], dtype=self._np.int64)
        dummy_image = self._np.zeros((1, 3, IMAGE_SIZE, IMAGE_SIZE), dtype=self._np.float32)
        return {
            "input_ids": ids,
            "attention_mask": mask,
            "pixel_values": dummy_image,
        }, ids

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            feed, _ = self._text_feed(texts)
            result = self._run(feed)
            vectors = result["text_embeds"]
            return self._normalize(vectors)
        except RagUnavailable:
            raise
        except Exception as exc:
            raise RagUnavailable(f"文本向量计算失败：{exc}") from exc

    def encode_images(self, paths: list[str]) -> list[list[float]]:
        if not paths:
            return []
        try:
            vectors = []
            for chunk_start in range(0, len(paths), 4):
                chunk = paths[chunk_start:chunk_start + 4]
                feed = self._image_feed(chunk)
                result = self._run(feed)
                vectors.extend(self._normalize(result["image_embeds"]))
            return vectors
        except RagUnavailable:
            raise
        except Exception as exc:
            raise RagUnavailable(f"图片向量计算失败：{exc}") from exc

    def _image_feed(self, paths: list[str]) -> dict:
        self._load()
        from PIL import Image

        pixels = []
        for path in paths:
            with Image.open(path) as image:
                image = image.convert("RGB").resize(
                    (IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BICUBIC
                )
                array = self._np.asarray(image, dtype=self._np.float32) / 255.0
            array = (array - self._mean) / self._std
            pixels.append(self._np.transpose(array, (2, 0, 1)))
        pixel_values = self._np.stack(pixels, axis=0).astype(self._np.float32)
        dummy_ids = self._np.zeros((1, MAX_TEXT_TOKENS), dtype=self._np.int64)
        dummy_ids[0, 0] = 101  # [CLS]
        dummy_ids[0, 1] = 102  # [SEP]
        dummy_mask = self._np.zeros((1, MAX_TEXT_TOKENS), dtype=self._np.int64)
        dummy_mask[0, :2] = 1
        return {
            "input_ids": dummy_ids,
            "attention_mask": dummy_mask,
            "pixel_values": pixel_values,
        }

    def _normalize(self, vectors) -> list[list[float]]:
        norms = self._np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vectors / norms).astype(self._np.float32).tolist()


def default_encoder_factory() -> ClipOnnxEncoder:
    return ClipOnnxEncoder()


def model_available() -> bool:
    return (_model_dir() / "model_quantized.onnx").is_file()


class StickerRag:
    """单进程内的表情包向量索引；缓存与增量更新都在这里。"""

    def __init__(
        self,
        cache_path: Path | str,
        *,
        encoder_factory: Callable[[], ClipOnnxEncoder] | None = None,
        background: bool = True,
        image_weight: float = IMAGE_WEIGHT,
        top_k: int = STICKER_RAG_TOP_K,
        min_score: float = STICKER_RAG_MIN_SCORE,
        model_id: str = MODEL_ID,
    ):
        self.cache_path = Path(cache_path)
        self._encoder_factory = encoder_factory or default_encoder_factory
        self._background = background
        self._image_weight = image_weight
        self._top_k = top_k
        self._min_score = min_score
        self._model_id = model_id
        self._encoder = None
        self._cache = self._load_cache()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending: set[str] = set()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._model_ready = False

    # ---------- 缓存 ----------
    def _load_cache(self) -> dict:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if (raw.get("version") == CACHE_VERSION and raw.get("model") == self._model_id
                and isinstance(raw.get("items"), dict)):
            return raw
        return {"version": CACHE_VERSION, "model": self._model_id, "items": {}}

    def _save_cache_locked(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.cache_path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(self._cache, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temp, self.cache_path)
        except OSError as exc:
            print(f"[表情包RAG] 索引写入失败：{exc}")

    def _get_encoder(self):
        with self._lock:
            if self._encoder is None:
                self._encoder = self._encoder_factory()
            return self._encoder

    def _cached_vector(self, name: str, field: str) -> list[float] | None:
        item = self._cache.get("items", {}).get(name) or {}
        value = item.get(field)
        return value if isinstance(value, list) and value else None

    def _available_entries(self, entries: Sequence[dict]) -> list[dict]:
        available = []
        for entry in entries:
            image_vec = self._cached_vector(entry["name"], "image")
            text_vec = self._cached_vector(entry["name"], "text")
            if image_vec is None and text_vec is None:
                continue
            available.append({**entry, "image_vec": image_vec, "text_vec": text_vec})
        return available

    # ---------- 增量建索引 ----------
    def build(self, snapshot: Sequence[dict], *, force: bool = False) -> tuple[int, int]:
        """只计算缺失/变化的向量；返回 (图片编码数, 文本编码数)。"""
        entries = list(snapshot)
        with self._lock:
            items = self._cache.setdefault("items", {})
            need_image: list[dict] = []
            need_text: list[dict] = []
            for entry in entries:
                cached = items.get(entry["name"]) or {}
                image_hash = str(entry.get("hash") or "")
                text = sticker_text(entry)
                text_hash = _text_hash(text)
                if force or not cached.get("image") or cached.get("image_hash") != image_hash:
                    need_image.append(entry)
                if force or not cached.get("text") or cached.get("text_hash") != text_hash:
                    need_text.append(entry)
        if not need_image and not need_text:
            with self._lock:
                self._model_ready = True
            return 0, 0

        encoder = self._get_encoder()
        image_vectors = encoder.encode_images([entry["path"] for entry in need_image]) if need_image else []
        text_vectors = encoder.encode_texts([sticker_text(entry) for entry in need_text]) if need_text else []

        with self._lock:
            items = self._cache.setdefault("items", {})
            for entry, vector in zip(need_image, image_vectors):
                item = items.setdefault(entry["name"], {})
                item["image_hash"] = str(entry.get("hash") or "")
                item["image"] = vector
            for entry, vector in zip(need_text, text_vectors):
                item = items.setdefault(entry["name"], {})
                item["text_hash"] = _text_hash(sticker_text(entry))
                item["text"] = vector
            keep = {entry["name"] for entry in entries}
            for name in list(items):
                if name not in keep:
                    items.pop(name, None)
            self._save_cache_locked()
            self._model_ready = True
        log_event("sticker.rag.build", images=len(need_image), texts=len(need_text), total=len(entries))
        return len(need_image), len(need_text)

    # ---------- 查询 ----------
    def search(
        self,
        query: str,
        *,
        snapshot: Sequence[dict] | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[Candidate]:
        entries = list(snapshot) if snapshot is not None else default_snapshot()
        if not entries:
            return []
        if self._background:
            with self._lock:
                available = self._available_entries(entries)
                missing = [entry for entry in entries if self._cached_vector(entry["name"], "image") is None
                           and self._cached_vector(entry["name"], "text") is None]
                if missing:
                    self._pending.update(entry["name"] for entry in missing)
            if missing:
                self._ensure_worker()
                with self._cond:
                    self._cond.notify_all()
            if not available:
                raise RagUnavailable("表情包语义索引正在建立")
        else:
            self.build(entries)
            with self._lock:
                available = self._available_entries(entries)
        try:
            encoder = self._get_encoder()
            query_vec = encoder.encode_texts([query])[0]
        except RagUnavailable:
            raise
        except Exception as exc:
            raise RagUnavailable(f"查询向量计算失败：{exc}") from exc
        with self._lock:
            self._model_ready = True
        return rank_candidates(
            query_vec,
            available,
            query_text=query,
            top_k=top_k or self._top_k,
            image_weight=self._image_weight,
            min_score=self._min_score if min_score is None else min_score,
        )

    # ---------- 后台增量 ----------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._closed or (self._worker is not None and self._worker.is_alive()):
                return
            self._worker = threading.Thread(target=self._worker_loop, name="sticker-rag", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        try:
            self.build(default_snapshot())
        except Exception as exc:
            print(f"[表情包RAG] 初始索引失败：{exc}")
            log_event("sticker.rag.error", stage="initial", detail=str(exc)[:200])
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                pending = set(self._pending)
                self._pending.clear()
            if not pending:
                continue
            try:
                # 新图入库后走全量 snapshot 的增量检查：只补缺失/变化的条目。
                self.build(default_snapshot())
            except Exception as exc:
                print(f"[表情包RAG] 增量索引失败：{exc}")
                log_event("sticker.rag.error", stage="incremental", detail=str(exc)[:200])

    def notify_added(self, name: str) -> None:
        if not name:
            return
        with self._cond:
            self._pending.add(name)
            self._cond.notify_all()
        if self._background:
            self._ensure_worker()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_RAG: StickerRag | None = None


def get_rag() -> StickerRag:
    global _DEFAULT_RAG
    with _DEFAULT_LOCK:
        if _DEFAULT_RAG is None:
            _DEFAULT_RAG = StickerRag(default_cache_path())
        return _DEFAULT_RAG


def search(query: str, *, top_k: int | None = None, snapshot: Sequence[dict] | None = None):
    if not STICKER_RAG_ENABLE:
        raise RagUnavailable("表情包语义检索已关闭")
    try:
        return get_rag().search(query, snapshot=snapshot, top_k=top_k)
    except RagUnavailable:
        raise
    except Exception as exc:
        raise RagUnavailable(f"表情包语义检索失败：{exc}") from exc


def notify_added(name: str) -> None:
    if not STICKER_RAG_ENABLE or not model_available():
        return
    try:
        get_rag().notify_added(name)
    except Exception as exc:
        print(f"[表情包RAG] 增量索引通知失败：{exc}")


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="构建/刷新表情包 CLIP 向量索引")
    parser.add_argument("--build", action="store_true", help="立即构建索引")
    parser.add_argument("--force", action="store_true", help="忽略缓存，重算全部向量")
    parser.add_argument("--cache", default="", help="缓存文件路径")
    parser.add_argument("--model-dir", default="", help="ONNX 模型目录")
    args = parser.parse_args()
    if not args.build:
        parser.print_help()
        return 1
    cache = Path(args.cache) if args.cache else default_cache_path()
    factory = (lambda: ClipOnnxEncoder(Path(args.model_dir))) if args.model_dir else default_encoder_factory
    rag = StickerRag(cache, encoder_factory=factory, background=False)
    images, texts = rag.build(default_snapshot(), force=args.force)
    print(f"表情包索引完成：图片 {images} 张，文本 {texts} 条，缓存 {cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
