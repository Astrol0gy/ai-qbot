"""Offline delivery contracts for brevity and the imported progressive style skill."""
import contextlib
import hashlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import review_regressions as audit
from src import deepseek_client as dc, skills
from src.channels import core
from src.channels.base import IncomingMessage


class StyleCases(unittest.IsolatedAsyncioTestCase):
    setUp = audit.AuditCases.setUp

    async def run_chat(self,responses,text='今天又加班了',*,style=True,group=False):
        model=audit.Model(responses)
        sender=audit.Sender()
        msg=IncomingMessage(conv_id='g',user_id='a',text=text,is_group=group,is_at_bot=True,
                            ai_decides_reply=group)
        with patch.object(dc,'client',model),patch.object(dc,'CHAT_STYLE_SKILL_ENABLE',style),contextlib.redirect_stdout(io.StringIO()):
            await core.handle_message(msg,sender)
        return model,sender

    async def test_private_draft_needs_style_load_before_delivery(self):
        model,sender=await self.run_chat([audit.response('未加载的草稿'),
            audit.response(calls=[('load_skill',{'name':skills.STYLE_SKILL_NAME})]),audit.response('早点歇会儿，辛苦了')])
        self.assertEqual(sender.sent[0]['text'],'早点歇会儿，辛苦了')
        self.assertEqual(len(model.requests),3)
        initial=model.requests[0]['messages'][0]['content']
        self.assertNotIn('# 去AI味：聊天适配',initial)
        self.assertNotIn('### 11. 段首零主语评论',initial)
        self.assertIn('# 去AI味：聊天适配',str(model.requests[-1]['messages']))
        self.assertNotIn('未加载的草稿',str(dc._conversations['g']))

    async def test_private_long_reply_rewritten_instead_of_truncated(self):
        _,sender=await self.run_chat([audit.response('长'*81),audit.response('先歇一下，文件可以晚点再写')],style=False)
        self.assertEqual(sender.sent[0]['text'],'先歇一下，文件可以晚点再写')
        self.assertEqual(dc._conversations['g'][-1]['content'],'先歇一下，文件可以晚点再写')

    async def test_technical_nouns_do_not_bypass_brief_budget(self):
        for text in ('我电脑配置太差了','代码又报错了，烦','10061是什么意思','安装真费劲','我刚写完的文件没保存'):
            model,sender=await self.run_chat([audit.response('长'*81),audit.response('一句简短回应')],text=text,style=False)
            self.assertEqual(len(model.requests),2,text)
            self.assertEqual(sender.sent[0]['text'],'一句简短回应')

    async def test_explicit_detail_and_complete_artifact_keep_full_output(self):
        for text in ('请详细解释操作步骤','给我完整Python脚本','请把下面原文去AI味，保留全部内容','翻译这段文章'):
            model,sender=await self.run_chat([audit.response('完整内容'*40)],text=text,style=False)
            self.assertEqual(len(model.requests),1,text)
            self.assertEqual(sender.sent[0]['text'],'完整内容'*40)

    async def test_simple_answer_request_overrides_request_to_expand(self):
        model,sender=await self.run_chat([audit.response('长'*81),audit.response('只说结论')],
            text='不用详细解释，简单说配置要怎么改',style=False)
        self.assertEqual(len(model.requests),2)
        self.assertEqual(sender.sent[0]['text'],'只说结论')

    async def test_legacy_group_reply_requires_the_style_skill_too(self):
        model,sender=await self.run_chat([
            audit.response(calls=[('reply',{'text':'未加载不发送'})]),
            audit.response(calls=[('load_skill',{'name':skills.STYLE_SKILL_NAME})]),
            audit.response(calls=[('reply',{'text':'现在可以了'})])],group=True)
        self.assertEqual([s['text'] for s in sender.sent],['现在可以了'])

    async def test_same_response_cannot_load_style_and_submit_an_unread_draft(self):
        _,sender=await self.run_chat([
            audit.response(calls=[('load_skill',{'name':skills.STYLE_SKILL_NAME}),('reply',{'text':'尚未读到正文的草稿'})]),
            audit.response(calls=[('reply',{'text':'读过规则后的回复'})])],group=True)
        self.assertEqual([s['text'] for s in sender.sent],['读过规则后的回复'])

    def test_full_upstream_is_a_separate_resource_without_tool_grants(self):
        session=skills.SkillSession(set(dc.get_tool_names()),set(dc.get_tool_names()))
        before=session.allowed_tools(set(skills.SKILL_TOOL_NAMES))
        body=session.load(skills.STYLE_SKILL_NAME)
        self.assertNotIn('### 11. 段首零主语评论',body)
        full=session.read_resource(skills.STYLE_SKILL_NAME,'references/upstream-SKILL.md')
        self.assertIn('### 11. 段首零主语评论',full)
        self.assertEqual(session.allowed_tools(set(skills.SKILL_TOOL_NAMES)),before)
        skill=skills.get_skill(skills.STYLE_SKILL_NAME)
        raw=(skill['resource_dir']/'references/upstream-SKILL.md').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'c55dec1765c2d6ce31f9e03e62876b36a3a65af830350d4d0d5bab6df766b042')
        self.assertIn('Copyright (c) 2026 shiujan',(skill['resource_dir']/'LICENSE').read_text(encoding='utf-8'))

    async def test_dm_path_does_not_require_style_loading_or_brief_budget(self):
        with patch.object(dc.campaign_state,'is_dm_mode',return_value=True):
            model,sender=await self.run_chat([audit.response('剧情'*100)],style=True)
        self.assertEqual(len(model.requests),1)
        self.assertEqual(sender.sent[0]['text'],'剧情'*100)


if __name__=='__main__':
    unittest.main(verbosity=2)
