"""Opt-in real main-model look-before-send checks. No QQ sends; use a copied gallery.

python tests/live_sticker_vision_check.py --repeats 2
Uses the configured API and writes only bounded visible results under data/live_checks.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import deepseek_client as dc, runtime, stickers_utils, sticker_rag
from src.tools.chat_provider import ReplyOutbox
from PIL import Image, ImageDraw


async def run(repeats):
    output = Path('data/live_checks') / ('sticker-vision-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    # A copied gallery is required; never mutate or prune the deployment's originals.
    root = stickers_utils._DIR
    if root.resolve() == (Path('/opt/project_workspace/ai-qbot/data/stickers')).resolve():
        raise RuntimeError('Run this check with STICKER_DIR set to a disposable copied gallery')
    stickers_utils.snapshot()
    rag = sticker_rag.get_rag()
    rag._ensure_worker = lambda: None
    mismatch = root / 'visual-mismatch.png'
    image = Image.new('RGB', (640, 480), 'white')
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(['SERVER STATUS', 'Latency: 43 ms', 'CPU: 12 percent', 'Memory: 48 percent']):
        draw.text((40, 40 + index * 75), line, fill='black', font_size=28)
    image.save(mismatch)
    stickers_utils._FILES[mismatch.name] = {'tags':['无语','捂脸','猫','手绘','无文字'],
        'hash': hashlib.sha1(mismatch.read_bytes()).hexdigest()[:16], 'added':0, 'used':0, 'last_used':0}
    rows = []
    original_create, original_execute = dc.client.chat.completions.create, dc.execute_tool
    cases = [('real_rag', '只发一张无语的表情包，不要配字'),
             ('wrong_tags', '发一张表达无语的表情；先看实际图片，如果只是普通内容截图就不要发，用文字说没有合适表情'),
             ('direct_text', '只用文字回答：在吗？'),
             ('explicit_quote', '请引用当前这条消息回答“收到”，不要发表情')]
    try:
        for repeat in range(1, repeats + 1):
            for name, text in cases:
                row = {'case':name, 'repeat':repeat, 'tools':[], 'visual_requests':0, 'requests':0}
                async def create(**kw):
                    row['requests'] += 1
                    if any(isinstance(m,dict) and isinstance(m.get('content'),list)
                           and any(b.get('type')=='image_url' for b in m['content']) for m in kw['messages']):
                        row['visual_requests'] += 1
                    return await original_create(**kw)
                def execute(tool, args, ctx):
                    row['tools'].append(tool)
                    if tool not in {'load_skill','read_skill_resource','send_sticker','list_stickers','quote_message'}:
                        return '只读测试中未开放此工具。'
                    if tool == 'send_sticker' and name == 'wrong_tags':
                        return f'候选1：标签无语、捂脸、猫；[img]{mismatch}[/img]'
                    return original_execute(tool,args,ctx)
                lease = runtime.RoundLease('vision-check-' + name + str(repeat), None)
                lease.reply_only = True
                lease.focus_text = text
                lease.current_message_id = '1'
                lease.actors = {'1':('test-user','测试用户')}
                lease.intent_decision = NS(recipient='bot')
                box = ReplyOutbox(known_ids={'1'}, max_text_chars=80)
                try:
                    with runtime.use(lease), patch.object(dc.client.chat.completions,'create',create), \
                            patch.object(dc,'execute_tool',execute):
                        await asyncio.wait_for(dc.chat(lease.conv_id,'test-user',text,'测试用户',outbox=box),180)
                    row['output'] = box.as_history_text()
                    row['quotes'] = [item.reply_to for item in box.items]
                    has_image = '[img]' in row['output']
                    row['passed'] = bool(box.items) and (
                        name == 'real_rag' and has_image and row['visual_requests'] > 0
                        or name == 'wrong_tags' and not has_image and row['visual_requests'] > 0
                        or name == 'direct_text' and not has_image and all(q is None for q in row['quotes'])
                        or name == 'explicit_quote' and row['quotes'] == ['1'])
                except Exception as exc:
                    row['error_type'] = type(exc).__name__
                    row['passed'] = False
                finally:
                    lease.revoke()
                rows.append(row)
                (output/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
                print(json.dumps(row,ensure_ascii=False),flush=True)
    finally:
        await dc.client.close()
    print('REPORT', output, sum(row['passed'] for row in rows), '/', len(rows))
    return all(row['passed'] for row in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats',type=int,default=2)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.repeats)) else 1)
