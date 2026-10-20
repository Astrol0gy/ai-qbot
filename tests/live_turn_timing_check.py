"""Opt-in real LLM/VLM tests through OneBot intake and its actual queue.

Only QQ delivery is simulated. Wait expiry is shortened to two seconds; collection
windows use production configuration. Reports share live_chat_intent_check format.
"""
import argparse
import asyncio
import base64
import contextlib
import json
import time
from unittest.mock import patch

import live_chat_intent_check as h


def image(path):
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


async def until(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError('等待测试阶段超时')
        await asyncio.sleep(.02)


async def run_case(name, repeat, screenshot, meme):
    controller = h.tr.TriggerController()
    bot = h.onebot.OneBotClient()
    bot.call_api_ex = h.FakeQQ().call_api_ex
    started = asyncio.Event()
    gid = 800000000 + repeat
    conv = f'onebot_group_{gid}'
    case = {'name':name, 'repeat':repeat, 'model_calls':[], 'tool_calls':[],
            'platform_payloads':[], 'arrivals':[], '_start':time.monotonic()}
    h.CURRENT = case
    h.REPORT['cases'].append(case)
    mid = 0

    async def send(text='', *, at=False, picture=None, user=1, other=None):
        nonlocal mid
        mid += 1
        segments = []
        if at or other:
            segments.append({'type':'at','data':{'qq':str(other or 9)}})
        if text:
            segments.append({'type':'text','data':{'text':text}})
        if picture:
            segments.append({'type':'image','data':{'url':picture}})
        case['arrivals'].append({'at_sec':round(time.monotonic()-case['_start'],2),
            'message_id':str(mid), 'user':user, 'at_bot':at, 'other':other,
            'text':text, 'image':bool(picture)})
        await bot._process({'self_id':9,'message_type':'group','group_id':gid,
            'user_id':user,'message_id':mid,'sender':{'nickname':'甲' if user==1 else '乙'},
            'message':segments})

    async def model(**kwargs):
        case.setdefault('requests', []).append({'at_sec':round(time.monotonic()-case['_start'],2),
            'user_context':[m['content'] for m in kwargs['messages'] if isinstance(m,dict) and m.get('role')=='user']})
        started.set()
        return await h.recording_model(**kwargs)

    tasks = []
    try:
        with patch.object(h.tr,'trigger',controller), patch.object(h.onebot,'trigger',controller), \
             patch.object(h.tr,'CHAT_WAIT_TIMEOUT_SEC',2), \
             patch.object(h.dc.client.chat.completions,'create',model), \
             patch.object(h.dc,'execute_tool',h.recording_tool), \
             patch.object(h.tr.random,'random',return_value=1):
            if name == 'burst_image_question':
                tasks.append(asyncio.create_task(send(at=True,picture=screenshot)))
                await asyncio.sleep(.1)
                await send('我这个程序刚运行到这里的时候')
                await send(picture=meme)
                await send('我想问：截图里的3001是什么意思？一句话解释就行')
            elif name == 'wait_then_followup':
                tasks.append(asyncio.create_task(send('我这个程序刚才',at=True,picture=screenshot)))
                await asyncio.sleep(.1)
                await send(picture=meme)
                await tasks[0]
                case['waiting_observed'] = controller.waiting_for(conv,'onebot_1') is not None
                case['payloads_before_followup'] = len(case['platform_payloads'])
                controller._active_until[conv] = 0  # Wait state must bypass activity and sampling.
                tasks.append(asyncio.create_task(send('现在说完了，刚才的3001是端口号吗？简短解释')))
            elif name in ('wait_timeout_once', 'wait_with_other_topic'):
                tasks.append(asyncio.create_task(send('我刚才写到这里，然后',at=True)))
                await tasks[0]
                case['waiting_observed'] = controller.waiting_for(conv,'onebot_1') is not None
                if name == 'wait_with_other_topic':
                    await send('明天大家九点集合，我已经订好车了',user=2)
                await until(lambda: not bot._wait_tasks)
            elif name == 'inflight_followup':
                tasks.append(asyncio.create_task(send('错误码10061是什么意思？一句话解释',at=True)))
                await started.wait()
                await send('补充：不用解释错误了，请只回复「收到补充」')
            elif name == 'other_recipient_inflight':
                tasks.append(asyncio.create_task(send('请只回复「给甲的答复」',at=True)))
                await started.wait()
                await send('小丙，明天九点见',user=2,other=3)
            await asyncio.gather(*tasks)
            # Allow canceled wait timers to finish; successful replies clear their tokens.
            await asyncio.sleep(.05)
    except Exception as exc:
        case['error'] = h.redact(type(exc).__name__+': '+str(exc)[:300])
    finally:
        await bot.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    output = [s['data']['text'] for p in case['platform_payloads'] for s in p['segments'] if s['type']=='text']
    skips = [t['arguments'].get('reason','irrelevant') for t in case['tool_calls'] if t['name']=='skip']
    case.update(output=output, skip_reasons=skips,
        elapsed_sec=round(time.monotonic()-case.pop('_start'),2))
    answer = ''.join(output)
    checks = {
        'burst_image_question': bool(output) and '3001' in answer and len(output)<=2,
        'wait_then_followup': case.get('waiting_observed') and not case.get('payloads_before_followup') and bool(output) and 'wait' in skips,
        'wait_timeout_once': case.get('waiting_observed') and not output and len(case['requests'])>=2 and len(case['requests'])<=4,
        'wait_with_other_topic': case.get('waiting_observed') and not output and 'wait' in skips,
        'inflight_followup': bool(output) and '收到补充' in answer and '10061' not in answer,
        'other_recipient_inflight': answer.strip('。！ ')=='给甲的答复',
    }
    case['passed'] = not case.get('error') and bool(checks[name])
    h.flush()
    return case


async def main(repeats):
    h.user_profile.PROFILES_DIR = str(h.REPORT_DIR/'profiles')
    h.campaign_state.CAMPAIGNS_DIR = str(h.REPORT_DIR/'campaigns')
    h.fixture_images()
    h.REPORT['suite'] = 'actual_onebot_intake_timing'
    h.REPORT['wait_timeout_sec'] = 2
    h.REPORT['collection_sec'] = h.config.CHAT_COLLECT_SEC
    h.REPORT['max_collection_sec'] = h.config.CHAT_MAX_COLLECT_SEC
    screenshot, meme = image(h.REPORT_DIR/'screenshot.png'), image(h.REPORT_DIR/'meme.png')
    with (h.REPORT_DIR/'runtime.log').open('w',encoding='utf-8') as log:
        for repeat in range(1,repeats+1):
            for name in ('burst_image_question','wait_then_followup','wait_timeout_once','wait_with_other_topic',
                         'inflight_followup','other_recipient_inflight'):
                with contextlib.redirect_stdout(log):
                    result = await asyncio.wait_for(run_case(name,repeat,screenshot,meme),180)
                print(json.dumps({k:result.get(k) for k in ('name','repeat','passed','output','skip_reasons','elapsed_sec','error')},ensure_ascii=False),flush=True)
    h.REPORT['passed'] = sum(c['passed'] for c in h.REPORT['cases'])
    h.REPORT['total'] = len(h.REPORT['cases'])
    h.flush()
    await h.dc.client.close()
    print('REPORT',h.REPORT_DIR,h.REPORT['passed'],'/',h.REPORT['total'],flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats',type=int,default=2)
    asyncio.run(main(parser.parse_args().repeats))
