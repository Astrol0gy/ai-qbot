"""Offline real-image checks. Run: .venv/Scripts/python.exe tests/sticker_media_check.py."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import stickers_utils as stickers


class StickerMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.root.mkdir()
        self.index = self.root / 'index.json'
        self.index.write_text(json.dumps({'version': 1, 'files': {}}), encoding='utf-8')
        self.embedding = self.root / 'clip-index.npz'
        self.embedding.write_bytes(b'unchanged legacy embedding')
        for name, value in [('_DIR', self.root), ('_INDEX_FILE', self.index),
                            ('_FILES', {}), ('_loaded', True), ('STICKER_ENABLE', True)]:
            p = patch.object(stickers, name, value)
            p.start()
            self.addCleanup(p.stop)

    def source(self, suffix='.png', size=(960, 480), color=(200, 40, 20, 255)):
        path = self.root / ('happy_wave_cat_draw_none_1' + suffix)
        image = Image.new('RGBA', size, color)
        if suffix in ('.jpg', '.jpeg'):
            image = image.convert('RGB')
        image.save(path)
        self.register(path)
        return path

    def register(self, path):
        stickers._FILES[path.name] = {'tags': ['happy', 'wave', 'cat', 'draw', 'none'],
                                      'hash': hashlib.sha1(path.read_bytes()).hexdigest()[:16]}

    def prepare(self, path):
        return stickers.prepare_sticker(str(path))

    def output(self, source):
        result = self.prepare(source)
        self.assertIsNotNone(result, 'valid local image must produce a sendable derivative')
        result = Path(result)
        self.assertNotEqual(result, source, 'sending must use a derivative, never rewrite originals')
        self.assertEqual(result.parent.parent, self.root)
        self.assertLessEqual(result.stat().st_size, 2 * 1024 * 1024)
        return result

    def test_static_formats_shrink_with_aspect_ratio_and_no_library_changes(self):
        for suffix, fmt in [('.jpg', 'JPEG'), ('.jpeg', 'JPEG'), ('.png', 'PNG'), ('.webp', 'WEBP')]:
            with self.subTest(format=fmt, suffix=suffix):
                path = self.source(suffix)
                before = (path.read_bytes(), path.stat().st_mtime_ns, self.index.read_bytes(),
                          self.embedding.read_bytes(), dict(stickers._FILES[path.name]))
                result = self.output(path)
                with Image.open(result) as im:
                    self.assertEqual(im.size, (320, 160))
                    self.assertEqual(im.format, fmt)
                self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns,
                                          self.index.read_bytes(), self.embedding.read_bytes(),
                                          stickers._FILES[path.name]))

    def test_small_images_are_not_upscaled(self):
        path = self.source(size=(40, 25))
        with Image.open(self.output(path)) as im:
            self.assertEqual(im.size, (40, 25))

    def test_transparency_survives_png_and_webp(self):
        for suffix in ('.png', '.webp'):
            with self.subTest(suffix=suffix):
                path = self.source(suffix, color=(0, 0, 0, 0))
                with Image.open(self.output(path)) as im:
                    self.assertEqual(im.convert('RGBA').getpixel((0, 0))[3], 0)

    def animation(self, suffix='.gif', count=4, size=(640, 320)):
        path = self.root / ('happy_wave_cat_draw_none_2' + suffix)
        frames = []
        for i in range(count):
            frame = Image.new('RGBA', size, (0, 0, 0, 0))
            ImageDraw.Draw(frame).rectangle((10 + i % 10, 10, 60 + i % 10, 60),
                                            fill=((i * 17) % 256, 150, 80, 255))
            frames.append(frame)
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=80,
                       loop=2, disposal=2, lossless=True)
        self.register(path)
        return path

    def test_animation_transparency_duration_loop_and_pixels_survive(self):
        for suffix, fmt in [('.gif', 'GIF'), ('.webp', 'WEBP'), ('.png', 'PNG')]:
            with self.subTest(format=fmt):
                path = self.animation(suffix)
                original = path.read_bytes()
                with Image.open(self.output(path)) as im:
                    self.assertEqual(im.format, fmt)
                    self.assertTrue(im.is_animated)
                    self.assertEqual(im.n_frames, 4)
                    self.assertEqual(im.size, (320, 160))
                    self.assertEqual(im.info.get('loop'), 2)
                    duration = 0
                    for i in range(im.n_frames):
                        im.seek(i)
                        rgba = im.convert('RGBA')
                        duration += im.info.get('duration', 0)
                        self.assertEqual(rgba.getpixel((0, 0))[3], 0)
                        self.assertGreater(rgba.getpixel((20, 20))[3], 200)
                    self.assertEqual(duration, 320)
                self.assertEqual(path.read_bytes(), original)

    def test_animation_sampling_bounds_output_frames_and_keeps_duration(self):
        path = self.animation(count=90, size=(96, 64))
        with Image.open(self.output(path)) as im:
            self.assertTrue(im.is_animated)
            self.assertLessEqual(im.n_frames, 60)
            duration = 0
            for i in range(im.n_frames):
                im.seek(i)
                im.load()
                duration += im.info['duration']
            self.assertEqual(duration, 7200)

    def test_noisy_animation_stays_within_byte_budget(self):
        path = self.root / 'noise_wave_cat_draw_none_1.gif'
        rng = random.Random(42)
        frames = [Image.frombytes('RGB', (320, 320), rng.randbytes(320 * 320 * 3))
                  for _ in range(30)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=40, loop=0)
        self.register(path)
        self.assertGreater(path.stat().st_size, 2 * 1024 * 1024)
        with Image.open(self.output(path)) as im:
            self.assertTrue(im.is_animated)
            self.assertLessEqual(max(im.size), 320)
            self.assertEqual(im.n_frames, 30)

    def test_cache_reuse_and_same_size_same_mtime_content_invalidation(self):
        path = self.source('.png')
        result = self.output(path)
        stamp = result.stat().st_mtime_ns
        self.assertEqual(Path(self.prepare(path)), result)
        self.assertEqual(result.stat().st_mtime_ns, stamp)
        old_stat = path.stat()
        replacement = io.BytesIO()
        Image.new('RGBA', (960, 480), (20, 40, 200, 255)).save(replacement, format='PNG')
        new_data = replacement.getvalue()
        # PNG allows trailing padding; keep stat size identical while changing pixels.
        old_data = path.read_bytes()
        length = max(len(old_data), len(new_data))
        path.write_bytes(old_data.ljust(length, b'\0'))
        result = self.output(path)
        path.write_bytes(new_data.ljust(length, b'\0'))
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        changed = self.output(path)
        self.assertNotEqual(changed, result)
        with Image.open(changed) as im:
            self.assertGreater(im.getpixel((20, 20))[2], 150)

    def test_corrupt_cache_is_rebuilt(self):
        path = self.source()
        result = self.output(path)
        result.write_bytes(b'corrupt cache')
        self.assertEqual(self.output(path), result)
        with Image.open(result) as im:
            im.load()
            self.assertEqual(im.size, (320, 160))

    def test_decompression_bomb_cache_is_rebuilt_without_decoding_it(self):
        path = self.source()
        result = self.output(path)
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        result.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB',
                           20000, 20000, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0')) + chunk(b'IEND', b''))
        self.assertEqual(self.output(path), result)

    def test_gif_disposal_does_not_leave_previous_frame_trails(self):
        path = self.root / 'move_wave_cat_draw_none_1.gif'
        frames = []
        for x in (20, 420, 220):
            frame = Image.new('RGBA', (640, 320))
            ImageDraw.Draw(frame).rectangle((x, 40, x + 80, 120), fill='red')
            frames.append(frame)
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=80,
                       loop=0, disposal=2, optimize=True)
        self.register(path)
        with Image.open(self.output(path)) as im:
            self.assertEqual(im.n_frames, 3)
            for i, current in enumerate((30, 230, 130)):
                im.seek(i)
                rgba = im.convert('RGBA')
                self.assertEqual(rgba.getpixel((current, 40))[3], 255)
                for previous in {30, 230, 130} - {current}:
                    self.assertEqual(rgba.getpixel((previous, 40))[3], 0)

    def test_legacy_filename_works_without_loading_or_rewriting_index(self):
        path = self.source()
        stickers._FILES.clear()
        stickers._loaded = False
        index = self.index.read_bytes()
        self.output(path.name)
        self.assertEqual(self.index.read_bytes(), index)
        self.assertFalse(stickers._loaded)

    def test_outside_traversal_nested_cache_and_disabled_paths_are_rejected(self):
        path = self.source()
        outside = Path(self.tmp.name) / path.name
        outside.write_bytes(path.read_bytes())
        for value in [outside, '../' + outside.name, self.root / 'sub' / path.name,
                      'https://example.com/a.png', self.root / 'missing.png']:
            with self.subTest(value=str(value)):
                self.assertIsNone(self.prepare(value))
        cached = self.output(path)
        self.assertIsNone(self.prepare(cached))
        with patch.object(stickers, 'STICKER_ENABLE', False):
            self.assertIsNone(self.prepare(path))

    def test_corrupt_unsupported_and_huge_pixel_headers_fail_closed(self):
        for data in [b'not an image', b'BM' + b'\0' * 100]:
            path = self.root / 'bad_wave_cat_draw_none_1.png'
            path.write_bytes(data)
            self.register(path)
            self.assertIsNone(self.prepare(path))
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        # Real PNG header with a 400-million-pixel canvas; no huge allocation in the test.
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB',
                         20000, 20000, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0')) + chunk(b'IEND', b''))
        self.assertIsNone(self.prepare(path))

    def test_excessive_animation_input_is_rejected_without_static_fallback(self):
        path = self.animation(count=125, size=(32, 32))
        self.assertIsNone(self.prepare(path))

    def test_sampling_cannot_silently_turn_animation_into_a_static_image(self):
        path = self.root / 'blink_wave_cat_draw_none_1.gif'
        frames = [Image.new('RGB', (32, 32), 'red' if i % 2 == 0 else 'blue')
                  for i in range(90)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
        self.register(path)
        result = self.prepare(path)
        # Temporal sampling may alias alternating frames. Either retain motion or
        # refuse the derivative; sending a static poster is never a safe fallback.
        if result is not None:
            with Image.open(result) as im:
                self.assertTrue(im.is_animated)

    def test_apng_poster_is_not_inserted_into_the_animation(self):
        path = self.root / 'poster_wave_cat_draw_none_1.png'
        poster = Image.new('RGBA', (640, 320), 'green')
        red = Image.new('RGBA', (640, 320), 'red')
        blue = Image.new('RGBA', (640, 320), 'blue')
        poster.save(path, save_all=True, append_images=[red, blue], default_image=True,
                    duration=[80, 120], loop=1)
        self.register(path)
        with Image.open(self.output(path)) as im:
            self.assertEqual(im.n_frames, 2)
            im.seek(0)
            self.assertEqual(im.convert('RGB').getpixel((20, 20)), (255, 0, 0))
            self.assertEqual(im.info['duration'], 80)
            im.seek(1)
            self.assertEqual(im.convert('RGB').getpixel((20, 20)), (0, 0, 255))
            self.assertEqual(im.info['duration'], 120)

    def test_decoded_pixel_and_source_byte_budgets_fail_closed(self):
        from src import sticker_media
        path = self.animation(count=4)
        with patch.object(sticker_media, 'MAX_DECODE_PIXELS', 640 * 320 * 3):
            self.assertIsNone(self.prepare(path))
        with patch.object(sticker_media, 'MAX_INPUT_BYTES', path.stat().st_size - 1):
            self.assertIsNone(self.prepare(path))

    def test_cache_policy_changes_invalidate_and_cache_retention_is_bounded(self):
        from src import sticker_media
        path = self.source()
        first = self.output(path)
        # A new processing policy must not reuse an existing 320px image.
        with patch.object(sticker_media, 'MAX_EDGE', 160):
            second = self.output(path)
            self.assertNotEqual(first, second)
            with Image.open(second) as im:
                self.assertEqual(im.size, (160, 80))
        cache = first.parent
        unrelated = cache / 'keep-me.txt'
        unrelated.write_text('unrelated')
        for i in range(4):
            (cache / (f'{i:064x}' + '.png')).write_bytes(b'old')
        with patch.object(sticker_media, 'MAX_CACHE_FILES', 2), \
             patch.object(sticker_media, 'MAX_CACHE_BYTES', first.stat().st_size + 3):
            self.assertEqual(self.output(path), first)
        entries = [p for p in cache.iterdir() if p.suffix == '.png']
        self.assertLessEqual(len(entries), 2)
        self.assertLessEqual(sum(p.stat().st_size for p in entries), first.stat().st_size + 3)
        self.assertEqual(unrelated.read_text(), 'unrelated')

    def test_unwritable_cache_and_missing_pillow_fail_closed(self):
        from src import sticker_media
        path = self.source()
        with patch.object(sticker_media, '_cache_dir', side_effect=PermissionError('read-only')):
            self.assertIsNone(self.prepare(path))
        with patch.dict(sys.modules, {'PIL': None}):
            self.assertIsNone(self.prepare(path))

    def test_linked_source_and_cache_cannot_escape_library(self):
        path = self.source()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        external = outside / path.name
        external.write_bytes(path.read_bytes())
        try:
            link = self.root / 'linked_wave_cat_draw_none_1.png'
            link.symlink_to(external)
        except OSError:
            link = None  # Windows may require administrator privilege for file symlinks.
        if link is not None:
            self.assertIsNone(self.prepare(link))
        cache = self.root / '.send-cache'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(cache), str(outside)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            cache.symlink_to(outside, target_is_directory=True)
        try:
            self.assertIsNone(self.prepare(path))
            self.assertEqual(list(outside.iterdir()), [external])
        finally:
            if os.name == 'nt':
                cache.rmdir()  # Remove only this junction; never recurse into its target.
            else:
                cache.unlink()

    def test_cache_file_symlink_is_rejected_without_changing_target(self):
        path = self.source()
        result = self.output(path)
        outside = Path(self.tmp.name) / 'outside.png'
        outside.write_bytes(b'outside target must remain untouched')
        result.unlink()
        try:
            result.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f'file symlinks unavailable: {exc}')
        self.assertIsNone(self.prepare(path))
        self.assertEqual(outside.read_bytes(), b'outside target must remain untouched')

    def test_exif_orientation_is_applied_and_metadata_is_removed(self):
        path = self.root / 'rotate_wave_cat_draw_none_1.jpg'
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = 'private source annotation'
        Image.new('RGB', (800, 400), 'red').save(path, exif=exif)
        self.register(path)
        with Image.open(self.output(path)) as im:
            self.assertEqual(im.size, (160, 320))
            self.assertFalse(im.getexif())

    def test_concurrent_preparation_returns_one_complete_cache_entry(self):
        path = self.source()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.prepare, [path] * 8))
        self.assertEqual(len(set(results)), 1)
        with Image.open(self.output(path)) as im:
            im.load()
        self.assertFalse(any(p.suffix == '.tmp' for p in Path(results[0]).parent.iterdir()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
