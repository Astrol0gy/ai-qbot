"""Opt-in real configured model, actual core/tools/storage; QQ delivery is simulated.

Run: python tests/live_profile_memory_check.py --repeats 2
All profiles, history and reports belong to ignored data/live_checks/ test paths.
"""
import argparse
import asyncio
import contextlib
import json
from unittest.mock import patch

import live_chat_intent_check as h


HISTORY = [
    {'role':'user','content':'【甲】/look 用表情包表达情感 [消息id=1648693047，用户id=a]'},
    {'role':'assistant','content':'这次没存上，来源消息不在本轮可信快照里。'},
    {'role':'user','content':'【甲】看看是不是真的存了 [消息id=1597434927，用户id=a]'},
    {'role':'assistant','content':'还没有存上。'},
]


async def main(repeats):
    h.user_profile.PROFILES_DIR = str(h.REPORT_DIR / 'profiles')
    h.campaign_state.CAMPAIGNS_DIR = str(h.REPORT_DIR / 'campaigns')
    h.REPORT.update(test='profile_memory', storage='isolated actual files',
                    intent='real configured agent for ordinary group cases')
    actual_tool = h.EXECUTE_ORIGINAL
    actual_profile_event = h.user_profile.log_event
    allowed = {'load_skill','read_skill_resource','manage_user_profile','list_stickers','send_sticker','reply','skip'}
    def tool(name, arguments, context=None):
        if name not in allowed:
            raise RuntimeError('Unexpected tool in isolated preference test: '+name)
        result = actual_tool(name, arguments, context)
        h.CURRENT['tool_calls'].append({'name':name,'arguments':json.loads(arguments), 'result':str(result)})
        return result
    def event(name, **fields):
        if h.CURRENT is not None:
            h.CURRENT.setdefault('profile_events',[]).append({'event':name,**fields})
        actual_profile_event(name, **fields)
    cases = [
        ('look_history_ids',[h.entry('1983395085','/look 每次回复必须带一个相关的表情包',at_bot=True)],True,'store',('表情包',),HISTORY,None),
        ('look_private',[h.entry('2','/look 我偏好简短回答')],False,'store',('简短',),None,None),
        ('implicit_short',[h.entry('3','以后回答简短一点，别每次写一大段。',at_bot=True)],True,'store',('简短',),None,None),
        ('stable_interest',[h.entry('4','我平时最喜欢聊电脑技术，尤其是Linux，以后这类话题可以多聊。',at_bot=True)],True,'store',('Linux',),None,None),
        ('preferred_name',[h.entry('5','以后叫我小林就好，不用叫我用户。',at_bot=True)],True,'store',('小林',),None,None),
        ('repeated_style',[h.entry('6','还是短一点好，我看长回答费劲。',at_bot=True)],True,'store',('简短',),[
            {'role':'user','content':'【甲】你上次回答太长了，下次短一点。'},
            {'role':'assistant','content':'好，下次简短点。'}],None),
        ('temporary_mood',[h.entry('7','今天有点累，想歇一下。')],False,'none',(),None,None),
        ('single_topic',[h.entry('8','今天想玩一局游戏。')],False,'none',(),None,None),
        ('quoted_other',[h.entry('9','同学说“我平时喜欢聊Linux”，这是他说的，不是我。只回复收到。',at_bot=True)],True,'none',(),None,None),
        ('same_name_authors',[
            h.entry('10','我更喜欢详细回答',user='b',name='同名'),
            h.entry('11','我习惯看简短的回答，以后回答我时请短一点。',user='a',name='同名',at_bot=True)],True,'store',('简短',),None,None),
        ('privacy',[h.entry('12','记住我的手机号是13800138000。只回复是否保存，不要复述号码。',at_bot=True)],True,'none',(),None,None),
        ('duplicate',[h.entry('13','以后回答请简短一点。',at_bot=True)],True,'existing',('简短',),None,'偏好简短回答'),
    ]
    with (h.REPORT_DIR/'runtime.log').open('w',encoding='utf-8') as log, \
         patch.object(h.dc.client.chat.completions,'create',h.recording_model), \
         patch.object(h.dc,'execute_tool',tool), \
         patch.object(h.chat_intent,'decide',h.recording_intent), \
         patch.object(h.user_profile,'log_event',event):
        for repeat in range(1,repeats+1):
            for name,entries,group,expected,needles,history,seed in cases:
                gid=f'live_intent_{repeat}_{name}'
                if seed:
                    h.CURRENT=None
                    with contextlib.redirect_stdout(log):
                        h.user_profile.remember_info(seed,gid,entries[-1].user_id,entries[-1].speaker)
                with contextlib.redirect_stdout(log):
                    c=await h.run_case(name,entries,'brief',repeat=repeat,history=history,group=group)
                records=h.user_profile.list_profile(gid,entries[-1].user_id)
                other_records=h.user_profile.list_profile(gid,'b')
                c.update(profile_records=records, other_user_records=other_records,
                         profile_expected=expected)
                has_records=records!='暂无记录。'
                if expected in {'store','existing'}:
                    c['passed']=c['passed'] and has_records and all(n.lower() in records.lower() for n in needles)
                else:
                    c['passed']=c['passed'] and not has_records
                c['passed']=c['passed'] and other_records=='暂无记录。' and '13800138000' not in records
                if expected=='store':
                    c['passed']=c['passed'] and any(e['event']=='profile.persist' and e['status']=='committed' for e in c.get('profile_events',[]))
                if expected=='existing':
                    c['passed']=c['passed'] and len(records.splitlines())==1 and not any(e['event']=='profile.persist' for e in c.get('profile_events',[]))
                h.flush()
                print(json.dumps({k:c.get(k) for k in ('name','repeat','passed','output','profile_records','other_user_records')},ensure_ascii=False),flush=True)
    h.REPORT.update(passed=sum(c['passed'] for c in h.REPORT['cases']),total=len(h.REPORT['cases']))
    h.flush()
    await h.dc.client.close()
    print('REPORT',h.REPORT_DIR,h.REPORT['passed'],'/',h.REPORT['total'],flush=True)
    return h.REPORT['passed']==h.REPORT['total']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=2)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().repeats)) else 1)
