"""Bounded disposable send derivatives; originals and library indexes stay read-only.

Use stickers_utils.prepare_sticker(path) from application code. Pillow is loaded
on demand. Limits are local safety defaults, not QQ/NapCat protocol guarantees.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import warnings

MAX_EDGE = 320
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_PIXELS = 16_000_000
MAX_DECODE_PIXELS = 64_000_000
MAX_INPUT_FRAMES = 120
MAX_OUTPUT_FRAMES = 60
MAX_CACHE_FILES = 128
MAX_CACHE_BYTES = 64 * 1024 * 1024
_FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}
_SUFFIXES = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif'})
_CACHE_NAME = '.send-cache'
_CACHE_ENTRY = re.compile(r'^[0-9a-f]{64}\.(?:jpg|png|webp|gif)$')
_LOCK = threading.RLock()


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _source(path: str | Path, library_dir: Path) -> tuple[Path, Path] | None:
    base = library_dir.resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        if candidate.name != str(candidate) or '/' in str(candidate) or '\\' in str(candidate):
            return None
        candidate = base / candidate
    if candidate.parent not in (base, library_dir.absolute()):
        return None
    if _linked(candidate) or candidate.suffix.lower() not in _SUFFIXES:
        return None
    source = candidate.resolve(strict=True)
    if source.parent != base or not source.is_file():
        return None
    return source, base


def _cache_dir(base: Path) -> Path:
    cache = base / _CACHE_NAME
    if _linked(cache):
        raise ValueError('linked sticker cache')
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.resolve(strict=True).parent != base:
        raise ValueError('sticker cache outside library')
    return cache


def _bounds(image, *, output: bool = False) -> int:
    pixels = image.width * image.height
    if not pixels or pixels > MAX_INPUT_PIXELS:
        raise ValueError('sticker canvas exceeds pixel budget')
    count = getattr(image, 'n_frames', 1)
    if count > (MAX_OUTPUT_FRAMES if output else MAX_INPUT_FRAMES):
        raise ValueError('sticker exceeds frame budget')
    if pixels * count > MAX_DECODE_PIXELS:
        raise ValueError('sticker exceeds decoded pixel budget')
    if output and max(image.size) > MAX_EDGE:
        raise ValueError('cached sticker exceeds dimensions')
    return count


def _valid_cache(target: Path, fmt: str, animated: bool, Image) -> bool:
    if not target.is_file() or _linked(target):
        return False
    if not 0 < target.stat().st_size <= MAX_OUTPUT_BYTES:
        return False
    try:
        with Image.open(target) as cached:
            count = _bounds(cached, output=True)
            if cached.format != fmt or (count > 1) != animated:
                return False
            for i in range(count):
                cached.seek(i)
                cached.load()
        return True
    except (OSError, ValueError, EOFError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False


def _frames(image, count: int, Image, ImageOps):
    frames, durations = [], []
    stride = math.ceil(count / MAX_OUTPUT_FRAMES)
    # APNG's optional default image is a poster, not an animation frame.
    start = 1 if image.format == 'PNG' and image.info.get('default_image') else 0
    decoded_pixels = 0
    for i in range(start, count):
        image.seek(i)
        pixels = image.width * image.height
        decoded_pixels += pixels
        if pixels > MAX_INPUT_PIXELS or decoded_pixels > MAX_DECODE_PIXELS:
            raise ValueError('sticker frame exceeds pixel budget')
        # WebP updates duration only when the selected frame has been loaded.
        image.load()
        duration = max(10, min(60000, int(image.info.get('duration', 100))))
        if (i - start) % stride == 0:
            # Decode all source frames for disposal/compositing, retaining only
            # thumbnails rather than full-size copies of the whole animation.
            frame = image.convert('RGBA')
            if count == 1:
                frame = ImageOps.exif_transpose(frame)
            frame.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            frame.info.clear()
            frames.append(frame)
            durations.append(duration)
        else:
            durations[-1] += duration
    if not frames:
        raise ValueError('empty sticker animation')
    return frames, durations


def _gif_frame(frame, Image):
    # Reserve a palette entry for transparency instead of quantizing alpha away.
    palette = frame.convert('RGB').quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    palette.paste(255, mask=frame.getchannel('A').point(lambda a: 255 if a < 128 else 0))
    palette.info['transparency'] = 255
    return palette


def _encode(frames, durations, fmt, loop, Image) -> bytes:
    for edge in (MAX_EDGE, 240, 160, 96, 64, 32):
        resized = []
        for frame in frames:
            item = frame.copy()
            item.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            if fmt == 'GIF':
                item = _gif_frame(item, Image)
            elif fmt == 'JPEG':
                item = item.convert('RGB')
            resized.append(item)
        options = {}
        if fmt == 'JPEG':
            options.update(quality=85, optimize=True)
        elif fmt == 'WEBP':
            options.update(lossless=True, method=4)
        if fmt == 'GIF':
            options.update(transparency=255, background=255, disposal=2, optimize=False)
        if len(frames) > 1:
            options.update(save_all=True, append_images=resized[1:], duration=durations)
            if loop is not None:
                options['loop'] = loop
        stream = io.BytesIO()
        resized[0].save(stream, format=fmt, **options)
        if stream.tell() <= MAX_OUTPUT_BYTES:
            stream.seek(0)
            with Image.open(stream) as output:
                _bounds(output, output=True)
                if len(frames) > 1 and not getattr(output, 'is_animated', False):
                    raise ValueError('sampling or encoding flattened the animation')
            return stream.getvalue()
    raise ValueError('sticker cannot fit send byte budget')


def _prune(cache: Path, keep: Path) -> None:
    entries = []
    for path in cache.iterdir():
        if _CACHE_ENTRY.fullmatch(path.name) and not _linked(path) and path.is_file():
            stat = path.stat()
            entries.append((stat.st_mtime_ns, path, stat.st_size))
    total = sum(item[2] for item in entries)
    count = len(entries)
    for _, path, size in sorted(entries):
        if count <= MAX_CACHE_FILES and total <= MAX_CACHE_BYTES:
            break
        if path != keep:
            path.unlink(missing_ok=True)
            total -= size
            count -= 1


def prepare_sticker(path: str | Path, *, library_dir: str | Path) -> str | None:
    """Return an absolute cached derivative, or None; never return the original.

    Only direct files in library_dir are accepted (including legacy filenames).
    JPEG/PNG/WebP/GIF are identified by content. GIF/WebP/APNG stay animated;
    unsafe input, missing codecs, and unwritable caches fail closed. Content hashes
    invalidate replacements even with identical size and timestamps. This blocks
    on local I/O; async callers should use asyncio.to_thread.
    """
    try:
        from PIL import Image, ImageOps, __version__
    except ImportError:
        return None
    try:
        with _LOCK, warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            checked = _source(path, Path(library_dir))
            if checked is None:
                return None
            source, base = checked
            with source.open('rb') as stream:
                if os.fstat(stream.fileno()).st_size > MAX_INPUT_BYTES:
                    return None
                data = stream.read(MAX_INPUT_BYTES + 1)
            if len(data) > MAX_INPUT_BYTES:
                return None
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format
                if fmt not in _FORMATS:
                    return None
                count = _bounds(image)
                cache = _cache_dir(base)
                policy = (f'v1:{__version__}:{MAX_EDGE}:{MAX_OUTPUT_BYTES}:'
                          f'{MAX_OUTPUT_FRAMES}:{MAX_INPUT_FRAMES}:{MAX_INPUT_PIXELS}:'
                          f'{MAX_DECODE_PIXELS}:{MAX_INPUT_BYTES}').encode()
                key = hashlib.sha256(policy + b'\0' + data).hexdigest()
                target = cache / (key + _FORMATS[fmt])
                if _linked(target):
                    return None
                if _valid_cache(target, fmt, count > 1, Image):
                    _prune(cache, target)
                    return str(target)
                loop = image.info.get('loop')
                frames, durations = _frames(image, count, Image, ImageOps)
                encoded = _encode(frames, durations, fmt, loop, Image)
            # Other processes must never observe a partially written cache entry.
            fd, name = tempfile.mkstemp(prefix='sticker-', suffix='.tmp', dir=cache)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                if _linked(cache) or _linked(target) or cache.resolve().parent != base:
                    return None
                os.replace(name, target)
            finally:
                Path(name).unlink(missing_ok=True)
            _prune(cache, target)
            return str(target)
    except (ImportError, OSError, ValueError, TypeError, EOFError, SyntaxError, OverflowError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
