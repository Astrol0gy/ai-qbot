"""
表情包库 —— 采集、入库、检索（``tools/image_provider.py`` 里 list_stickers / send_sticker 两个工具的后端）

VLM 认出「表情包」时按 ``vision_utils._TYPE_GUIDE`` 的要求补一行五个标签（情绪｜动作｜主体｜风格｜文字），
这里把原图下载到 ``data/stickers/``，文件名就是那五个标签加一个序号：

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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import STICKER_DIR, STICKER_ENABLE, STICKER_MAX_BYTES, STICKER_MAX_FILES
from .vision_utils import _download_image

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

# 「标签：无语｜捂脸｜熊猫｜拼贴｜无文字」——只认这一行的第一个匹配
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

def parse_tags(desc: str) -> list[str] | None:
    """
    从 VLM 描述里取出五个标签；不是表情包（没有标签行）时返回 None。

    「有没有标签行」就是「是不是表情包」的判据 —— 提示词只要求表情包输出这一行，
    所以不需要再去看第一行的类型字样（模型偶尔把类型写歪，标签行反而更稳）。
    """
    if not desc:
        return None
    match = _TAG_LINE.search(desc)
    if match is None:
        return None
    tags = [_clean_tag(part) for part in _TAG_SPLIT.split(match.group(1))]
    tags = [t for t in tags if t]
    if len(tags) < 3:  # 少于三个多半是那句「标签：」被模型写歪了，宁可不收
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
        return
    _ensure_worker()
    try:
        _QUEUE.put_nowait((url, desc))
    except queue.Full:
        print("[表情包] 采集队列积压，丢掉这一张")


def collect(url: str, desc: str) -> str | None:
    """真下载并入库（后台线程里跑）。返回文件名，失败返回 None"""
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


def stats() -> dict:
    """库统计（诊断用）"""
    with _LOCK:
        _load()
        return {
            "dir": str(_DIR),
            "total": len(_FILES),
            "used": sum(int(i.get("used") or 0) for i in _FILES.values()),
        }
