"""Opt-in real model checks for short group/private replies and imported style rules.

Uses local project API configuration and actual skill tools; only QQ delivery is simulated.
"""
import argparse
import asyncio
import contextlib
import json
import re
from unittest.mock import patch

import live_chat_intent_check as h

ORIGINAL = '''## 发布计划

先说结论：发布窗口是2026年11月3日20:30。预算是3200元，灰度比例保持在15%，可能调整，不能保证按时完成。

团队从9:00开始检查旧配置，检查结果记录在内部日志里。发布失败时会先恢复旧版本，再检查失败原因，不承诺立刻修好。

```
PORT=6099
RETRIES=3
```

引用：“这不是失败，而是一次机会。”
'''


async def main(repeats):
    h.user_profile.PROFILES_DIR=str(h.REPORT_DIR/'profiles')
    h.campaign_state.CAMPAIGNS_DIR=str(h.REPORT_DIR/'campaigns')
    h.fixture_images()
    h.stickers_utils.save((h.REPORT_DIR/'meme.png').read_bytes(),'image/png',('无语','摊手','黄脸','卡通','BRUH'))
    h.REPORT.update(test='chat_style', brief_max_chars=h.config.CHAT_BRIEF_MAX_CHARS,
        style_skill=h.dc.skills.STYLE_SKILL_NAME,style_enabled=h.dc.CHAT_STYLE_SKILL_ENABLE)
    cases=[
        ('lost_file','我刚写完的文件忘保存就关了，整个人无语住了',True,'brief'),
        ('simple_tech','代码报错10061是什么意思？简单说',True,'brief'),
        ('config_noun','我电脑配置不太行，玩游戏卡得难受',True,'brief'),
        ('game_banter','刚打游戏又输了，气死我了',True,'brief'),
        ('movie','这电影看得我一头雾水',True,'brief'),
        ('annoyed','你别一说话就长篇大论，烦死了',True,'brief'),
        ('private_emotion','今天好累啊',False,'brief'),
        ('private_tech','电脑配置是什么意思？一句话',False,'brief'),
        ('private_banter','刚打游戏又输了',False,'brief'),
        ('sticker_only','只发一张无语的表情包，不要配字',True,'sticker'),
        ('search_brief','帮我搜一下GitHub Actions干嘛的，给一句结论',True,'brief'),
        ('complete_script',"请给我一个完整Python脚本，包含import、main函数和if __name__ == '__main__'入口，将列表['甲','乙','丙']逐行写入utf-8文本文件，附运行命令。",True,'reply'),
        ('rewrite_article','请把下面这段成稿去AI味，保留全部信息、标题、段落、代码和引用，只输出改写结果：\n'+ORIGINAL,False,'reply'),
    ]
    with (h.REPORT_DIR/'runtime.log').open('w',encoding='utf-8') as log, \
         patch.object(h.dc.client.chat.completions,'create',h.recording_model), \
         patch.object(h.dc,'execute_tool',h.recording_tool), \
         patch.object(h.chat_intent,'decide',h.recording_intent):
        for repeat in range(1,repeats+1):
            for name,text,group,expected in cases:
                with contextlib.redirect_stdout(log):
                    c=await h.run_case(name,[h.entry('1',text,at_bot=True)],expected,repeat=repeat,group=group)
                c['text_chars']=sum(map(len,c['output']))
                c['style_loaded']=any(t['name']=='load_skill' and t['arguments'].get('name')==h.dc.skills.STYLE_SKILL_NAME
                    for t in c['tool_calls'])
                if c['output']:
                    c['passed']=c['passed'] and c['style_loaded']
                if name=='complete_script':
                    c['passed']=c['passed'] and c['text_chars']>h.config.CHAT_BRIEF_MAX_CHARS and all(s in ''.join(c['output']) for s in ('if __name__','utf-8','main'))
                if name=='rewrite_article':
                    answer='\n'.join(c['output'])
                    facts=('## 发布计划','2026年11月3日20:30','3200','15%','可能','不能保证','9:00','PORT=6099\nRETRIES=3','这不是失败，而是一次机会。')
                    c['facts_preserved']=all(s in answer for s in facts)
                    c['upstream_read']=any(t['name']=='read_skill_resource' and t['arguments'].get('path')=='references/upstream-SKILL.md' for t in c['tool_calls'])
                    c['passed']=c['passed'] and c['facts_preserved'] and c['upstream_read'] and c['text_chars']>80
                if name=='sticker_only':
                    c['passed']=c['passed'] and not c['output']
                if name=='simple_tech':
                    c['passed']=c['passed'] and '10061' in ''.join(c['output']) and '拒绝' in ''.join(c['output'])
                if name in {'config_noun','lost_file','game_banter','private_banter'}:
                    c['passed']=c['passed'] and not re.search(r'(?:看到|收到|发的).{0,10}(?:手机图|截图|图片)', ''.join(c['output']))
                h.flush()
                print(json.dumps({k:c.get(k) for k in ('name','repeat','passed','text_chars','output','images','style_loaded','facts_preserved','upstream_read')},ensure_ascii=False),flush=True)
    h.REPORT['passed']=sum(c['passed'] for c in h.REPORT['cases'])
    h.REPORT['total']=len(h.REPORT['cases'])
    h.flush()
    await h.dc.client.close()
    print('REPORT',h.REPORT_DIR,h.REPORT['passed'],'/',h.REPORT['total'],flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--repeats',type=int,default=2)
    asyncio.run(main(p.parse_args().repeats))
