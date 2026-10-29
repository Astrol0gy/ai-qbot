"""
表情包库 —— 采集、入库、检索（``tools/image_provider.py`` 里 list_stickers / send_sticker 两个工具的后端）

VLM 按 ``vision_utils._TYPE_GUIDE`` 判为「表情包」，给出可独立复用的可见依据与五个标签
（情绪｜动作｜主体｜风格｜文字）后，这里把原图下载到 ``data/stickers/``，文件名就是标签加序号：

    无语_捂脸_熊猫_拼贴_无文字_1.gif
    ^^^^^^^^ ^^^^ ^^^^^ ^^^^^^ ^^^^^^^ ^ 同一组五个标签里的第 1 张

:func:`pick` 挑库里最贴切的一张，工具把它的本地路径包成 ``[img]…[/img]`` 交回给模型；
模型放进回复后，core 解析 ``[img]`` 时用 :func:`resolve` 认出这是库里的文件，随那句话一起发出去
（用法见 ``src/skills/image.md``）。

这个模块只干「管库」和「挑图」两件事，不碰通道、不碰工具注册 —— 工具壳留在工具那一层。
两条约定：**采集不阻塞对话**（下载丢给后台线程，一张图一次，失败就算了）；
**只复用视觉模块的下载函数**（``vision_utils._download_image``），索引与文件自成一套，
库坏了也不影响「看图」这条主链路。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import STICKER_DIR, STICKER_ENABLE, STICKER_MAX_BYTES, STICKER_MAX_FILES
from .vision_utils import _download_image
from .observability import log_event

# ---- 路径与常量 -----------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DIR = Path(STICKER_DIR).expanduser() if STICKER_DIR else _ROOT / "data" / "stickers"
if not _DIR.is_absolute():
    _DIR = _ROOT / _DIR
_INDEX_FILE = _DIR / "index.json"
_INDEX_VERSION = 1

_NONE = "无"          # 某一维缺失时的占位
_NO_TEXT = "无文字"    # 「文字」这一维没有文字时的占位
_SKIP_TAGS = frozenset({_NONE, _NO_TEXT})  # 这两个不参与检索匹配（"无"会匹配到一切）

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 分类协议各只允许一行；不能从矛盾输出中挑一个肯定结论入库。
_TYPE_LINE = re.compile(r"^\s*类型\s*[：:]", re.MULTILINE)
_ASSESSMENT_LINE = re.compile(r"^\s*采集判定\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
_TAG_LINE = re.compile(r"^\s*(?:[-·•*]|\d+[.、])?\s*标\s*签\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
_TAG_SPLIT = re.compile(r"[｜|/\\、,，;；:：\s_]+")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')

_MAX_QUEUE = 64       # 采集队列长度，满了就丢（表情包不值得为它积压内存）
_MAX_TAG_CHARS = 8    # 单个标签最长字数


# ---- 数据结构 -------------------------------------------------------------

@dataclass
class Sticker:
    """挑中的一张表情包"""
    name: str                 # 文件名（含序号）
    tags: list[str]           # 五个标签
    path: str                 # 本地绝对路径

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0]


# ---- 标签解析与文件名规则 --------------------------------------------------

def _unique_fields(pairs: list[tuple]) -> dict:
    """JSON duplicate keys are contradictory evidence, not last-value-wins."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate collection assessment field')
        result[key] = value
    return result


def _explicit_sticker(desc: str) -> bool:
    """Require an unambiguous type AND visible evidence of a reusable reaction.

    Legacy type/tags-only descriptions remain readable, but cannot add new files.
    Evidence is judged by the visual model; words such as 截图 are not a blacklist.
    """
    if not desc or not desc.strip():
        return False
    lines = [line.strip() for line in desc.splitlines() if line.strip()]
    if len(lines) < 2 or len(_TYPE_LINE.findall(desc)) != 1:
        return False
    header = lines[0]
    match = re.match(r'^类型\s*[：:]\s*([^｜|\r\n]+)', header)
    if match is None:
        return False
    category = re.sub(r'\s+', '', match.group(1))
    if category not in {'表情包', '梗图', '表情包/梗图', '表情包／梗图', '表情包或梗图', '表情包（梗图）'}:
        return False
    if len(_ASSESSMENT_LINE.findall(desc)) != 1:
        return False
    assessment = _ASSESSMENT_LINE.fullmatch(lines[1])
    if assessment is None:
        return False
    try:
        fields = json.loads(assessment.group(1), object_pairs_hook=_unique_fields)
    except (ValueError, TypeError):
        return False
    if not isinstance(fields, dict) or set(fields) != {'可复用反应', '依赖特定内容', '可见依据'}:
        return False
    evidence = fields['可见依据']
    return (fields['可复用反应'] is True and fields['依赖特定内容'] is False
            and isinstance(evidence, str) and bool(evidence.strip())
            and evidence.strip() not in {_NONE, '未知', '不确定', _NO_TEXT})


def parse_tags(desc: str) -> list[str] | None:
    """
    必须明确判定为可独立复用的反应图，且有唯一有效标签行，才返回五个标签。
    内容图片、缺少可见依据、否定/不确定/矛盾的判读都不采集。
    """
    if not _explicit_sticker(desc):
        return None
    matches = list(_TAG_LINE.finditer(desc))
    if len(matches) != 1:
        return None
    match = matches[0]
    tags = [_clean_tag(part) for part in _TAG_SPLIT.split(match.group(1))]
    tags = [t for t in tags if t]
    if len(tags) < 3:  # 少于三个多半是那句「标签：」被模型写歪了，宁可不收
        return None
    if tags[0] in {_NONE, _NO_TEXT, '未知', '不确定'}:
        return None
    tags = tags[:5]
    while len(tags) < 5:
        tags.append(_NONE)
    return tags


def _clean_tag(tag: str) -> str:
    """
    洗掉标签里不能进文件名的字符。

    下划线是字段分隔符（``五个标签_序号``），标签内出现会把名字切成七段，
    所以哪怕是全角的也一律抹掉 —— 文件名里的下划线数量必须是「5 个标签 + 1 个序号」。
    """
    text = _ILLEGAL.sub("", (tag or "").strip())
    text = text.replace("_", "").replace("＿", "")
    return text.strip(".·、。，,　 ")[:_MAX_TAG_CHARS]


def _file_stem(tags: list[str]) -> str:
    return "_".join(tags)


def _tags_from_name(name: str) -> list[str] | None:
    """从文件名反解五个标签（重建索引用）"""
    if name == _INDEX_FILE.name:
        return None
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("_")
    if len(parts) < 6:
        return None
    return parts[:5]


# ---- 索引与库文件 ---------------------------------------------------------

_LOCK = threading.RLock()
_FILES: dict[str, dict] = {}   # 文件名 -> {tags, hash, added, used, last_used}
_loaded = False
_worker: threading.Thread | None = None
_stopping = threading.Event()
_QUEUE: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=_MAX_QUEUE)


def _ensure_dir() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)


def _load() -> None:
    """载入索引；索引缺失或损坏就按文件名把库扫回来（标签就在文件名里）"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    _ensure_dir()
    raw: dict = {}
    try:
        raw = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"[表情包] 索引读不出来（{e}），按文件名重建")

    if isinstance(raw, dict) and raw.get("version") == _INDEX_VERSION:
        for name, info in (raw.get("files") or {}).items():
            path = _DIR / name
            if (Path(name).name != name or path.suffix.lower() not in _EXT_BY_MIME.values()
                    or not path.is_file() or path.is_symlink()):
                continue
            tags = info.get("tags") or _tags_from_name(name)
            if not tags:
                continue
            _FILES[name] = {
                "tags": list(tags)[:5],
                "hash": info.get("hash") or hashlib.sha1(path.read_bytes()).hexdigest()[:16],
                "added": int(info.get("added") or 0),
                "used": int(info.get("used") or 0),
                "last_used": int(info.get("last_used") or info.get("added") or 0),
            }

    # 索引与目录对不上的部分（手工丢进去的图、索引丢了）：按文件名补回来
    for path in _DIR.iterdir():
        if not path.is_file() or path.is_symlink() or path.name in _FILES or path.suffix.lower() not in _EXT_BY_MIME.values():
            continue
        tags = _tags_from_name(path.name)
        if tags:
            _FILES[path.name] = {"tags": tags, "hash": hashlib.sha1(path.read_bytes()).hexdigest()[:16], "added": int(path.stat().st_mtime),
                                 "used": 0, "last_used": int(path.stat().st_mtime)}
    if _FILES:
        _flush()
        print(f"[表情包] 库就绪：{len(_FILES)} 张（{_DIR}）")


def _flush() -> None:
    """原子写索引：先写临时文件再替换，避免中途崩掉留下半个 JSON"""
    payload = {"version": _INDEX_VERSION, "files": _FILES}
    tmp = _INDEX_FILE.with_suffix(".json.tmp")
    try:
        _ensure_dir()
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, _INDEX_FILE)
    except OSError as e:
        print(f"[表情包] 索引写入失败：{e}")


def _find_hash(digest: str) -> str | None:
    """同一张图只存一份（按内容 sha1 去重，改名不算）"""
    if not digest:
        return None
    for name, info in _FILES.items():
        if info.get("hash") == digest:
            return name
    return None


def _next_seq(stem: str) -> int:
    """同一组五个标签的第几张 —— 序号从 1 开始，重名就往后排"""
    used = []
    for name in _FILES:
        if not name.startswith(stem + "_"):
            continue
        tail = name[len(stem) + 1:].rsplit(".", 1)[0]
        if tail.isdigit():
            used.append(int(tail))
    return max(used) + 1 if used else 1


def _evict() -> None:
    """超出容量时淘汰最久没用过的"""
    while len(_FILES) > STICKER_MAX_FILES:
        name = min(_FILES, key=lambda n: (_FILES[n].get("last_used") or 0, _FILES[n].get("added") or 0))
        _FILES.pop(name, None)
        try:
            (_DIR / name).unlink(missing_ok=True)
        except OSError as e:
            print(f"[表情包] 淘汰 {name} 失败：{e}")
        print(f"[表情包] 库满了（{STICKER_MAX_FILES} 张），淘汰最久未用的 {name}")


def save(data: bytes, mime: str, tags: list[str]) -> str | None:
    """
    把一张表情包写进库，返回文件名。

    同一张图（内容哈希相同）重复出现时返回已经存着的那张，不重复占坑。
    格式不认识、超过大小上限、或库不可写时返回 None —— 采集失败不该惊动任何人。
    """
    ext = _EXT_BY_MIME.get(mime)
    if not STICKER_ENABLE or not data or ext is None:
        return None
    if len(data) > STICKER_MAX_BYTES:
        print(f"[表情包] 太大（{len(data) / 1048576:.1f}MB > "
              f"{STICKER_MAX_BYTES / 1048576:.1f}MB），不收")
        return None
    if len(tags) != 5:
        return None

    digest = hashlib.sha1(data).hexdigest()[:16]
    with _LOCK:
        _load()
        dup = _find_hash(digest)
        if dup:
            return dup
        stem = _file_stem(tags)
        name = f"{stem}_{_next_seq(stem)}{ext}"
        try:
            (_DIR / name).write_bytes(data)
        except OSError as e:
            print(f"[表情包] 写文件失败：{e}")
            return None
        now = int(time.time())
        _FILES[name] = {"tags": list(tags), "hash": digest, "added": now,
                        "used": 0, "last_used": now}
        _evict()
        _flush()
        # 新图只补这一张的 CLIP 向量，不重建全库；模型/索引未就绪时由 RAG 自己回退。
        try:
            from . import sticker_rag
            sticker_rag.notify_added(name)
        except Exception as exc:
            print(f"[表情包] RAG 增量索引通知失败：{exc}")
        return name


# ---- 采集（后台线程，不阻塞对话） ------------------------------------------

def harvest(url: str, desc: str) -> None:
    """
    排队采集一张表情包。**立即返回**，下载与落盘都在后台线程里做。

    url 只认 http(s) 直链：file-api-xxx 与 data: 开头的是我们喂给模型的引用，
    不是能重新下载的地址。
    """
    if _stopping.is_set() or not STICKER_ENABLE or not url or not url.startswith(("http://", "https://")):
        return
    if parse_tags(desc) is None:
        log_event('sticker.harvest.skipped', explicit_sticker=_explicit_sticker(desc),
                  reason='classification_or_tags_invalid')
        return
    _ensure_worker()
    try:
        _QUEUE.put_nowait((url, desc))
    except queue.Full:
        print("[表情包] 采集队列积压，丢掉这一张")


def collect(url: str, desc: str) -> str | None:
    """真下载并入库（后台线程里跑）。返回文件名，失败返回 None"""
    if not STICKER_ENABLE or _stopping.is_set():
        return None
    tags = parse_tags(desc)
    if tags is None:
        return None
    got = _download_image(url, STICKER_MAX_BYTES)
    if got is None:
        return None
    data, mime = got
    name = save(data, mime, tags)
    if name:
        print(f"[表情包] 入库 {name}（{len(data) / 1024:.0f}KB）")
    return name


def _ensure_worker() -> None:
    global _worker
    with _LOCK:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="sticker-harvest", daemon=True)
        _worker.start()


def _loop() -> None:
    while not _stopping.is_set():
        try:
            url, desc = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            collect(url, desc)
        except Exception as e:  # 采集是顺手活，出错绝不能让线程死掉
            print(f"[表情包] 采集异常：{type(e).__name__}: {e}")
        finally:
            _QUEUE.task_done()


def shutdown(timeout=3):
    _stopping.set()
    while True:
        try:
            _QUEUE.get_nowait()
            _QUEUE.task_done()
        except queue.Empty:
            break
    if _worker is not None:
        _worker.join(timeout)


def wait_idle(timeout: float = 30.0) -> bool:
    """等采集队列跑空（测试与退出时用）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _QUEUE.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return False


# ---- 检索 -----------------------------------------------------------------

def pick(query: str) -> Sticker | None:
    """
    按情绪/场景词挑一张表情包。挑不到就返回 None（调用方宁可不发，也不要乱发）。

    匹配靠标签子串：查询词与某个标签完全相等 +3，两个字的互相包含 +2，
    再给「命中了几种查询词」加一点分 —— 否则同一句里的多个词会堆在同一张图上。
    """
    tokens = [t for t in _TAG_SPLIT.split((query or "").strip()) if t]
    if not tokens:
        return None
    with _LOCK:
        _load()
        if not _FILES:
            return None
        scored = [(_score(info["tags"], tokens), name) for name, info in _FILES.items()]
        scored = [item for item in scored if item[0] > 0]
        if not scored:
            return None
        top = max(score for score, _ in scored)
        name = random.choice([n for score, n in scored if score == top])
        info = _FILES[name]
        return Sticker(name=name, tags=list(info["tags"]), path=str(_DIR / name))


def mark_used(path):
    with _LOCK:
        info = _FILES.get(Path(path).name)
        if info:
            info["used"] = int(info.get("used") or 0) + 1
            info["last_used"] = int(time.time())
            _flush()


def _score(tags: list[str], tokens: list[str]) -> int:
    score = 0
    hits: set[str] = set()
    for tag in tags:
        if tag in _SKIP_TAGS:
            continue
        for token in tokens:
            if token == tag:
                score += 3
                hits.add(token)
            elif len(token) >= 2 and len(tag) >= 2 and (token in tag or tag in token):
                score += 2
                hits.add(token)
    return score + len(hits)


# ---- 回复里的本地路径 → 库文件 ---------------------------------------------

def prepare_sticker(path: str | Path) -> str | None:
    """发送前派生至多 320px 的缓存图；失败返回 None，原图/索引/RAG 不变。

    不调用 _load/resolve，兼容旧库且不因准备发送重写索引。调用方保留原路径
    做 receipt、mark_used；这里只返回发送文件。异步通道请用 asyncio.to_thread。
    """
    if not STICKER_ENABLE:
        return None
    from .sticker_media import prepare_sticker as prepare
    return prepare(path, library_dir=_DIR)


def resolve(payload: str) -> str | None:
    """
    把回复里 ``[img]`` 的内容解析成库里的文件（是表情包就返回本地绝对路径，否则 None）。

    只认库里的东西：绝对路径必须落在库目录内，非绝对路径按文件名在库里查找。
    库外的一律不认 —— 模型抄错或编出来的路径（乃至被用户诱导出的系统文件路径）
    绝不能进发送环节。库关掉时同样返回 None。
    """
    text = (payload or "").strip().strip("\"'")
    if not STICKER_ENABLE or not text:
        return None
    with _LOCK:
        _load()
        path = Path(text)
        if path.is_absolute():
            try:
                real = path.resolve()
                base = _DIR.resolve()
            except (OSError, ValueError):
                return None
            if real.parent == base and real.name in _FILES and real.is_file() and real.suffix.lower() in _EXT_BY_MIME.values() and not path.is_symlink():
                return str(real)
            return None
        # 模型可能只留了文件名（把 send_sticker 给的路径截短了）
        if text in _FILES and (_DIR / text).is_file() and not (_DIR / text).is_symlink():
            path = (_DIR / text).resolve()
            if path.parent == _DIR.resolve():
                return str(path)
    return None


def fallback_text(payload: str) -> str:
    """Express an indexed sticker's emotion as text, without exposing its path."""
    default = "（表情）"
    with _LOCK:
        _load()
        try:
            path = Path(str(payload or ""))
            if path.is_absolute():
                if path.resolve().parent != _DIR.resolve():
                    return default
            elif path.name != str(path):
                return default
        except (OSError, ValueError):
            return default
        tags = (_FILES.get(path.name) or {}).get("tags") or []
        emotion = _clean_tag(str(tags[0]))[:20] if tags else ""
        return f"（{emotion}）" if emotion and emotion not in _SKIP_TAGS else default


# ---- 库现状（list_stickers 工具用） -----------------------------------------

def tag_summary(limit: int = 12) -> str:
    """库里最常用的情绪/动作标签（空库返回空串）"""
    with _LOCK:
        _load()
        total = len(_FILES)
        counter: Counter = Counter()
        for info in _FILES.values():
            for tag in info["tags"][:2]:  # 只统计「情绪」「动作」两维，主体/风格对选图意义不大
                if tag not in _SKIP_TAGS:
                    counter[tag] += 1
    if not total:
        return ""
    top = "、".join(tag for tag, _ in counter.most_common(limit))
    return f"库里现有 {total} 张表情包，常用标签：{top}。" if top else f"库里现有 {total} 张表情包。"


def snapshot() -> list[dict]:
    """当前库的只读快照，供 CLIP 索引按 hash 做增量更新。"""
    with _LOCK:
        _load()
        return [
            {
                "name": name,
                "tags": tuple(info.get("tags") or ()),
                "hash": str(info.get("hash") or ""),
                "path": str(_DIR / name),
            }
            for name, info in sorted(_FILES.items())
        ]


def stats() -> dict:
    """库统计（诊断用）"""
    with _LOCK:
        _load()
        return {
            "dir": str(_DIR),
            "total": len(_FILES),
            "used": sum(int(i.get("used") or 0) for i in _FILES.values()),
        }


def admin_list(query: str = "") -> list[str]:
    with _LOCK:
        _load()
        return sorted(name for name, info in _FILES.items()
                      if not query or query in name or any(query in t for t in info['tags']))


def admin_edit(name: str, new_name: str | None = None) -> None:
    """Rename or quarantine one indexed image; keep disk and memory in sync."""
    with _LOCK:
        _load()
        if (not name or Path(name).name != name or '/' in name or '\\' in name
                or name not in _FILES):
            raise ValueError("请提供索引中的完整表情包文件名")
        source = _DIR / name
        if source.is_symlink() or source.resolve().parent != _DIR.resolve() or not source.is_file():
            raise ValueError("表情包文件不存在或路径不可用")
        if new_name is not None:
            if (not new_name or new_name in ('.', '..') or _ILLEGAL.search(new_name)
                    or Path(new_name).name != new_name or len(new_name) > 180
                    or new_name.startswith('.') or Path(new_name).suffix != source.suffix):
                raise ValueError("新名称必须是同扩展名的图片文件名，不能包含目录或特殊字符")
            if new_name == name:
                return
            target = _DIR / new_name
            if target.exists() or target.is_symlink() or new_name in _FILES:
                raise ValueError("新文件名已存在")
        else:
            trash = _DIR / '.admin-trash'
            if trash.is_symlink():
                raise ValueError("回收目录不可用")
            trash.mkdir(mode=0o700, exist_ok=True)
            target = trash / (uuid.uuid4().hex + '-' + name)
        old_files = dict(_FILES)
        # A same-directory temporary index and os.replace make index writes atomic.
        temp_index = _DIR / ('.admin-index-' + uuid.uuid4().hex)
        if new_name is not None:
            # Exclusive creation prevents a concurrent external file from being overwritten.
            os.link(source, target)
            try:
                source.unlink()
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        else:
            os.replace(source, target)
        _FILES.pop(name)
        if new_name is not None:
            _FILES[new_name] = old_files[name]
        try:
            temp_index.write_text(json.dumps({'version': _INDEX_VERSION, 'files': _FILES},
                                            ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(temp_index, _INDEX_FILE)
        except BaseException:
            _FILES.clear()
            _FILES.update(old_files)
            os.replace(target, source)
            raise
        finally:
            temp_index.unlink(missing_ok=True)
        if new_name is not None:
            try:
                from . import sticker_rag
                sticker_rag.notify_added(new_name)
            except Exception as exc:
                print(f"[表情包] RAG 增量索引通知失败：{exc}")
