"""Opt-in real configured LLM/VLM checks. QQ delivery is always simulated.

Run: python tests/live_chat_intent_check.py --repeats 2
Reports and isolated fixture data are written under ignored data/live_checks/.
No API credentials are copied into the report.
"""
import argparse
import asyncio
import base64
import contextlib
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REPORT_DIR = ROOT / 'data' / 'live_checks' / datetime.now().strftime('%Y%m%d-%H%M%S')
REPORT_DIR.mkdir(parents=True, exist_ok=True)
os.environ['STICKER_DIR'] = str(REPORT_DIR / 'stickers')

from PIL import Image, ImageDraw, ImageFont
from src import config, deepseek_client as dc, runtime, stickers_utils, trigger as tr, chat_intent
from src import user_profile, campaign_state, vision_utils as vu
from src.channels import core, onebot
from src.channels.base import IncomingMessage
from src.observability import redact

CURRENT = None
CALL_ORIGINAL = dc.client.chat.completions.create
EXECUTE_ORIGINAL = dc.execute_tool
INTENT_ORIGINAL = chat_intent.decide
REPORT = {'model': config.DEEPSEEK_MODEL, 'vision_model': config.VISION_MODEL,
          'api_host': urlsplit(config.DEEPSEEK_BASE_URL).hostname,
          'thinking': config.DEEPSEEK_ENABLE_THINKING,
          'reasoning_effort': config.DEEPSEEK_REASONING_EFFORT,
          'qq_delivery': 'simulated', 'cases': []}


def flush():
    (REPORT_DIR / 'report.json').write_text(json.dumps(redact(REPORT), ensure_ascii=False, indent=2), encoding='utf-8')


async def recording_model(**kwargs):
    start = time.monotonic()
    try:
        response = await CALL_ORIGINAL(**kwargs)
    except Exception as exc:
        CURRENT['error'] = redact(type(exc).__name__ + ': ' + str(exc)[:300])
        raise
    msg = response.choices[0].message
    CURRENT['model_calls'].append({'at_sec': round(start - CURRENT['_start'], 2),
        'phase': 'intent' if kwargs.get('response_format') else 'answer',
        'elapsed_sec': round(time.monotonic() - start, 2), 'text': msg.content,
        'thinking': kwargs.get('extra_body', {}).get('thinking', {}).get('type'),
        'tool_choice': kwargs.get('tool_choice'),
        'tools': [{'name': t.function.name, 'arguments': json.loads(t.function.arguments)}
                  for t in (msg.tool_calls or [])],
        'usage': response.usage.model_dump() if response.usage else None})
    return response


async def recording_intent(*args, **kwargs):
    decision = await INTENT_ORIGINAL(*args, **kwargs)
    CURRENT.setdefault('intent_decisions', []).append(decision.__dict__)
    return decision


def skip_reasons(case):
    return ([{'WAIT':'wait','IGNORE':'irrelevant','END':'end'}[d['action']]
             for d in case.get('intent_decisions', []) if d['action'] != 'REPLY']
        + [t['arguments'].get('reason', 'irrelevant') for t in case['tool_calls'] if t['name'] == 'skip'])


def recording_tool(name, arguments, ctx):
    start = time.monotonic()
    # No unrelated external work or durable user state in this test.
    allowed = {'load_skill', 'read_skill_resource', 'reply', 'skip', 'list_stickers',
               'send_sticker', 'web_search', 'fetch_url', 'quote_message'}
    result = EXECUTE_ORIGINAL(name, arguments, ctx) if name in allowed else '本次测试不执行无关工具。'
    CURRENT['tool_calls'].append({'at_sec': round(start - CURRENT['_start'], 2),
        'elapsed_sec': round(time.monotonic() - start, 2), 'name': name,
        'arguments': json.loads(arguments), 'result_excerpt': str(result)[:350]})
    return result


class FakeQQ:
    async def call_api_ex(self, action, params):
        # The real OneBotSender builds payloads, but no platform connection exists.
        CURRENT['platform_payloads'].append({'at_sec': round(time.monotonic() - CURRENT['_start'], 2),
            'action': action, 'segments': [{**s, 'data': {'file': 'base64://<image bytes>'}}
                if s['type'] == 'image' else s for s in params['message']]})
        return {}, onebot.API_OK


def fixture_images():
    screenshot = Image.new('RGB', (1000, 500), '#20252b')
    draw = ImageDraw.Draw(screenshot)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    draw.text((30, 30), 'Python application - Connection error', font=font, fill='white')
    draw.multiline_text((30, 100), 'Traceback (most recent call last):\nConnectionRefusedError: [WinError 10061]\nNo connection could be made\nHost: 127.0.0.1    Port: 3001', font=font, fill='#ff8888', spacing=14)
    screenshot.save(REPORT_DIR / 'screenshot.png')
    meme = Image.new('RGB', (480, 480), 'white')
    draw = ImageDraw.Draw(meme)
    draw.ellipse((80, 70, 400, 350), fill='#fff09a', outline='black', width=8)
    draw.line((145, 165, 200, 165), fill='black', width=10)
    draw.line((280, 165, 335, 165), fill='black', width=10)
    draw.line((185, 270, 295, 270), fill='black', width=10)
    draw.text((140, 380), 'BRUH...', font=font, fill='black')
    meme.save(REPORT_DIR / 'meme.png')


def recognize(path):
    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
    start = time.monotonic()
    description = vu.describe_image('data:image/png;base64,' + encoded)
    REPORT.setdefault('vision', []).append({'image': path.name,
        'elapsed_sec': round(time.monotonic() - start, 2), 'description': description})
    flush()
    if not description:
        raise RuntimeError('真实识图失败：' + path.name)
    return description


async def run_case(name, entries, expected, *, repeat, history=None, group=True, conv_id=None):
    global CURRENT
    entries = copy.deepcopy(entries)
    gid = conv_id or f'live_intent_{repeat}_{name}'
    controller = tr.TriggerController()
    for entry in entries:
        controller.buffer.add(gid, entry)
    latest = entries[-1]
    if history:
        dc._conversations[gid] = history
    CURRENT = {'name': name, 'repeat': repeat, 'expected': expected,
        'input_context': '\n'.join(e.line() for e in entries),
        'seed_history': history or [],
        'model_calls': [], 'tool_calls': [], 'platform_payloads': [], '_start': time.monotonic()}
    REPORT['cases'].append(CURRENT)
    # Deliberately let the real model see even explicit other-recipient cases:
    # the hard trigger filter must not conceal semantic failures in this test.
    msg = IncomingMessage(channel='onebot', conv_id=gid, user_id=latest.user_id,
        user_name=latest.speaker, text=latest.text, message_id=latest.message_id,
        image_descs=[], is_group=group, is_at_bot=latest.at_bot, ai_decides_reply=group,
        is_interjection=not latest.at_bot, context_entries=tuple(entries))
    try:
        with patch.object(tr, 'trigger', controller):
            await asyncio.wait_for(core.handle_message(msg, onebot.OneBotSender(FakeQQ(), group, 1, 2)), 180)
    except Exception as exc:
        CURRENT['error'] = redact(type(exc).__name__ + ': ' + str(exc)[:300])
    CURRENT['elapsed_sec'] = round(time.monotonic() - CURRENT.pop('_start'), 2)
    output = [s['data']['text'] for p in CURRENT['platform_payloads'] for s in p['segments'] if s['type'] == 'text']
    images = sum(s['type'] == 'image' for p in CURRENT['platform_payloads'] for s in p['segments'])
    skips = skip_reasons(CURRENT)
    CURRENT.update(output=output, images=images, skip_reasons=skips)
    CURRENT['reply_committed'] = dc.get_history_length(gid) > len(history or []) // 2
    checks = {'silent': not output and not images, 'wait': not output and not images and 'wait' in skips,
        'reply': bool(output or images), 'brief': bool(output) and len(output) <= 2 and sum(map(len, output)) <= config.CHAT_BRIEF_MAX_CHARS,
        'sticker': images == 1, 'reference': bool(output) and not any(x in ''.join(output) for x in ['没收到图','没看到图'])}
    CURRENT['passed'] = not CURRENT.get('error') and checks[expected]
    if expected not in {'silent','wait'}:
        CURRENT['passed'] = CURRENT['passed'] and CURRENT['reply_committed']
    elif group:
        CURRENT['passed'] = CURRENT['passed'] and bool(CURRENT.get('intent_decisions') or skips)
    if name == 'search_brief':
        CURRENT['passed'] = CURRENT['passed'] and any(t['name']=='web_search' for t in CURRENT['tool_calls'])
    if name == 'natural_sticker_opportunity':
        CURRENT['passed'] = CURRENT['passed'] and len(output)<=2 and sum(map(len,output))<=config.CHAT_BRIEF_MAX_CHARS
    flush()
    return CURRENT


def entry(mid, text, user='a', name='甲', **kw):
    return tr.BufferEntry(message_id=mid, text=text, user_id=user, speaker=name, **kw)


async def main(repeats):
    user_profile.PROFILES_DIR = str(REPORT_DIR / 'profiles')
    campaign_state.CAMPAIGNS_DIR = str(REPORT_DIR / 'campaigns')
    fixture_images()
    raw_log = (REPORT_DIR / 'runtime.log').open('w', encoding='utf-8')
    with contextlib.redirect_stdout(raw_log):
        screenshot = recognize(REPORT_DIR / 'screenshot.png')
        meme = recognize(REPORT_DIR / 'meme.png')
        for tags in [('无语','摊手','黄脸','卡通','BRUH'), ('委屈','趴地','黄脸','卡通','累了'), ('开心','微笑','黄脸','卡通','好耶')]:
            stickers_utils.save((REPORT_DIR / 'meme.png').read_bytes() + tags[0].encode(), 'image/png', tags)
    print('VISION_DONE', flush=True)
    cases = [
        ('mention_other', [entry('1','小乙，你明天几点上班？', mention_user_ids=('b',))], 'silent'),
        ('quote_other', [entry('1','你这个报错怎么解决的？',quote='引用了乙的消息：「昨天程序连不上」',quote_author_id='b')], 'silent'),
        ('discuss_bot', [entry('1','小乙，这个bot有点话多，你觉得呢？',mention_user_ids=('b',))], 'silent'),
        ('pronoun_other', [entry('1','小乙你明天几点上班',mention_user_ids=('b',)), entry('2','九点，你呢？','b','乙'), entry('3','我也九点，明天见')], 'silent'),
        ('image_only', [entry('1','[图片1：'+screenshot+']')], 'silent'),
        ('unfinished_image_meme', [entry('1','[图片1：'+screenshot+']'),entry('2','我这个程序刚才'),entry('3','[图片1：'+meme+']')], 'wait'),
        ('unfinished_variant', [entry('1','[图片1：'+screenshot+']'),entry('2','刚运行到这里的时候'),entry('3','[图片1：'+meme+']')], 'wait'),
        ('at_bot_unfinished', [entry('1','[图片1：'+screenshot+']'),entry('2','等下，我还没说完，后面还有',at_bot=True)], 'wait'),
        ('complete_variant', [entry('1','[图片1：'+screenshot+']'),entry('2','帮我解释下错误码10061，一句话就行',at_bot=True)], 'brief'),
        ('recipient_variant', [entry('1','小乙，你发的截图我看到了，等下我把剩下的贴给你',mention_user_ids=('b',))], 'silent'),
        ('explicit_image_question', [entry('1','[图片1：'+screenshot+']'),entry('2','这个报错是什么意思？一句话说清楚',at_bot=True)], 'brief'),
        ('brief_banter', [entry('1','今天又加班了，随便吐槽一句就行',at_bot=True)], 'brief'),
        ('sticker_request', [entry('1','发一张无语的表情包就好，不要配字',at_bot=True)], 'sticker'),
        ('natural_sticker_opportunity', [entry('1','我刚写完的文件忘保存就关了，整个人无语住了',at_bot=True)], 'reply'),
        ('search_brief', [entry('1','帮我搜一下 GitHub Actions 是做什么的，只要一句话结论',at_bot=True)], 'brief'),
    ]
    with patch.object(dc.client.chat.completions, 'create', recording_model), patch.object(dc, 'execute_tool', recording_tool), patch.object(chat_intent, 'decide', recording_intent):
        for repeat in range(1, repeats + 1):
            for name, entries, expected in cases:
                with contextlib.redirect_stdout(raw_log):
                    result = await run_case(name, entries, expected, repeat=repeat)
                print(json.dumps({'case':name,'repeat':repeat,'passed':result['passed'],
                    'elapsed_sec':result['elapsed_sec'],'output':result['output'],
                    'images':result['images'],'skip':result['skip_reasons']},ensure_ascii=False),flush=True)
            history = [{'role':'user','content':'【甲】[图片1：'+screenshot+']'},
                       {'role':'assistant','content':'先等你补充问题。'}]
            with contextlib.redirect_stdout(raw_log):
                result = await run_case('image_reference_followup',[entry('2','刚才那个截图里的3001是什么意思？',at_bot=True)],'reference',repeat=repeat,history=history)
            print(json.dumps({'case':result['name'],'repeat':repeat,'passed':result['passed'],'output':result['output']},ensure_ascii=False),flush=True)
    raw_log.close()
    REPORT['passed'] = sum(c['passed'] for c in REPORT['cases'])
    REPORT['total'] = len(REPORT['cases'])
    flush()
    await dc.client.close()
    print('REPORT',str(REPORT_DIR / 'report.json'),REPORT['passed'], '/', REPORT['total'],flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repeats',type=int,default=2)
    args=parser.parse_args()
    asyncio.run(main(args.repeats))
