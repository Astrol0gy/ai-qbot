"""Program/storage/scheduling contracts. Semantic checks live in live_group_memory_check."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src import group_memory as gm
from src.channels.base import IncomingMessage
from src.group_memory.store import MemoryStore, RAW_TTL, FACT_TTL
from src.group_memory.agent import analyze


def message(mid, uid='a', text='收到捏，晚上开月亮船？', **kw):
    return dict(id=mid,user_id=uid,name='同名',text=text,time=time.time(),mentions=[],at_bot=False,**kw)


def habit(ids=('1','2','3'), **kw):
    return dict(user_id='a',category='expression',description='确认时常用“收到捏”',record_id='',
                confidence=0.9,uncertain=False,evidence_ids=list(ids),**kw)


def word(ids=('1','2'), **kw):
    result=dict(term='月亮船',meaning='本群合作游戏的代称',scope='讨论游戏时',referent_type='topic',
                referent_user_id='',record_id='',confidence=0.9,uncertain=False,evidence_ids=list(ids))
    result.update(kw)
    return result


class StoreCases(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=MemoryStore(tmp.name)
        for mid in ('1','2','3'):
            self.store.append('g',message(mid))

    def save(self, **output):
        data=self.store.snapshot('g')
        facts,rejected=self.store.validate(data,{'habits':[],'glossary':[],**output})
        self.store.complete('g',data,facts)
        return facts,rejected

    def test_evidence_and_owner_validation(self):
        bad=habit()
        bad['user_id']='b'
        short=habit(ids=('1','2'))
        invented=habit(ids=('1','2','invented'))
        facts,bad_count=self.save(habits=[bad,short,invented,habit()])
        self.assertEqual(3,bad_count)
        self.assertEqual(1,len(facts['habits']))
        self.assertEqual('a',facts['habits'][0]['user_id'])

    def test_groups_and_same_name_users_do_not_mix(self):
        self.save(habits=[habit()])
        self.assertTrue(self.store.relevant('g',['a'],'')['users'])
        self.assertEqual({},self.store.relevant('g',['b'],''))
        self.assertEqual({},self.store.relevant('other-group',['a'],''))

    def test_new_arrivals_survive_completed_batch(self):
        snapshot=self.store.snapshot('g')
        facts,_=self.store.validate(snapshot,{'habits':[habit()],'glossary':[]})
        self.store.append('g',message('4'))
        self.store.complete('g',snapshot,facts)
        self.assertEqual(['4'],self.store.snapshot('g')['pending_ids'])

    def test_restart_preserves_pending_and_deduplicates_delivery(self):
        reopened=MemoryStore(self.store.root)
        self.assertEqual(['1','2','3'],reopened.snapshot('g')['pending_ids'])
        self.assertFalse(reopened.append('g',message('2')))
        self.assertEqual(3,len(reopened.snapshot('g')['messages']))

    def test_uncertain_and_low_confidence_facts_are_not_injected(self):
        unsure=habit()
        unsure['uncertain']=True
        self.save(habits=[unsure],glossary=[word(confidence=0.7)])
        self.assertEqual({},self.store.relevant('g',['a'],'月亮船'))

    def test_matching_glossary_only_and_multiple_senses(self):
        self.save(glossary=[word(),word(meaning='群里的同名服务器',scope='讨论运维时')])
        self.assertEqual({},self.store.relevant('g',[],'无关主题'))
        self.assertEqual(2,len(self.store.relevant('g',[],'月亮船')['glossary']))

    def test_alias_target_must_have_evidence(self):
        facts,rejected=self.save(glossary=[word(referent_type='user',referent_user_id='missing'),word(referent_type='bot')])
        self.assertEqual(2,rejected)
        self.assertEqual([],facts['glossary'])

    def test_invented_term_not_present_in_sources_is_rejected(self):
        facts,rejected=self.save(glossary=[word(term='凭空编造的词')])
        self.assertEqual(1,rejected)
        self.assertEqual([],facts['glossary'])

    def test_known_record_can_be_corrected_or_marked_uncertain(self):
        facts,_=self.save(habits=[habit()])
        original=facts['habits'][0]
        self.store.append('g',message('4','a','我不是每次都这样说'))
        candidate=habit(ids=('2','3','4'))
        candidate.update(record_id=original['id'],uncertain=True)
        self.save(habits=[candidate])
        rows=self.store.snapshot('g')['habits']
        self.assertEqual(1,len(rows))
        self.assertEqual(original['id'],rows[0]['id'])
        self.assertEqual({},self.store.relevant('g',['a'],''))

    def test_expiry_prunes_disk_not_just_prompt(self):
        self.save(habits=[habit()])
        data=self.store.snapshot('g')
        for m in data['messages']:
            m['time']=time.time()-RAW_TTL-1
        data['habits'][0]['updated_at']=time.time()-FACT_TTL-1
        self.store._write('g',data)
        self.store.snapshot('g',prune=True)
        disk=json.loads(self.store.path('g').read_text(encoding='utf-8'))
        self.assertEqual([],disk['messages'])
        self.assertEqual([],disk['habits'])

    def test_frontend_reads_do_not_mutate_expired_files(self):
        self.save(habits=[habit()])
        data=self.store.snapshot('g')
        data['habits'][0]['updated_at']=time.time()-FACT_TTL-1
        self.store._write('g',data)
        before=self.store.path('g').read_bytes()
        self.assertEqual({},self.store.relevant('g',['a'],''))
        self.assertEqual(before,self.store.path('g').read_bytes())

    def test_corrupt_state_is_not_overwritten(self):
        self.store.path('g').write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.append('g',message('4'))
        self.assertEqual('{broken',self.store.path('g').read_text(encoding='utf-8'))

    def test_retrieval_is_bounded_valid_json(self):
        self.save(habits=[habit()],glossary=[word()])
        result=self.store.relevant('g',['a'],'月亮船',budget=180)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),180)

    def test_no_new_evidence_does_not_inflate_observation_count(self):
        self.save(habits=[habit()])
        self.store.append('g',message('4'))
        facts,rejected=self.store.validate(self.store.snapshot('g'),{'habits':[habit()],'glossary':[]})
        self.assertEqual([],facts['habits'])
        self.assertEqual(1,rejected)

    def test_rejection_diagnostics_explain_counts_without_raw_chat(self):
        snapshot=self.store.snapshot('g')
        bad_owner=habit()
        bad_owner['user_id']='b'
        output={'habits':[bad_owner,habit(ids=('missing',))], 'glossary':[]}
        log=io.StringIO()
        with contextlib.redirect_stdout(log):
            facts,rejected=self.store.validate(snapshot,output)
        self.assertEqual({'habits':[], 'glossary':[]},facts)
        self.assertEqual(2,rejected)
        events=[json.loads(line) for line in log.getvalue().splitlines()]
        self.assertTrue(any(e['event']=='memory.validation.rejected' for e in events))
        diagnostic=next(e for e in events if e['event']=='memory.validation.rejected')
        self.assertEqual({'habit_owner_or_samples':1,'evidence':1},diagnostic['reasons'])
        self.assertNotIn('收到捏',log.getvalue())

    def test_identical_fact_and_delivery_do_not_duplicate_after_restart(self):
        first,_=self.save(habits=[habit()])
        reopened=MemoryStore(self.store.root)
        self.assertFalse(reopened.append('g',message('3')))
        reopened.append('g',message('4'))
        snapshot=reopened.snapshot('g')
        facts,rejected=reopened.validate(snapshot,{'habits':[habit(ids=('2','3','4'))],'glossary':[]})
        self.assertEqual(0,rejected)
        reopened.complete('g',snapshot,facts)
        rows=reopened.snapshot('g')['habits']
        self.assertEqual(1,len(rows))
        self.assertEqual(first['habits'][0]['id'],rows[0]['id'])


class ServiceCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(self.tmp.name)

    async def test_observation_never_waits_for_analyzer_and_private_bot_commands_are_excluded(self):
        calls=[]
        async def analyzer(store,snapshot):
            calls.append(snapshot)
            return {'habits':[],'glossary':[]}
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyzer)
        for mid,text,group,uid in [('1','普通聊天',True,'a'),('2','私聊',False,'a'),
                                   ('3','/config set x y',True,'a'),('4','机器人回复',True,'bot'),
                                   ('5','手机号13800138000',True,'a')]:
            await service.observe(IncomingMessage(conv_id='g',user_id=uid,user_name='甲',
                message_id=mid,text=text,is_group=group),bot_user_id='bot')
        self.assertEqual([],calls)
        self.assertEqual(['1'],self.store.snapshot('g')['pending_ids'])

    async def test_failed_or_cancelled_batch_keeps_pending_for_restart(self):
        for mid in ('1','2','3'):
            self.store.append('g',message(mid))
        async def failed(*args):
            raise ConnectionError('offline')
        service=gm.MemoryService(self.store,batch_size=3,analyzer=failed)
        self.assertFalse(await service.process('g'))
        self.assertEqual(['1','2','3'],self.store.snapshot('g')['pending_ids'])
        async def cancelled(*args):
            raise asyncio.CancelledError()
        service.analyzer=cancelled
        with self.assertRaises(asyncio.CancelledError):
            await service.process('g',force=True)
        self.assertEqual(['1','2','3'],MemoryStore(self.tmp.name).snapshot('g')['pending_ids'])

    async def test_threshold_timer_and_successful_processing(self):
        calls=[]
        async def analyzer(store,snapshot):
            calls.append(snapshot)
            return {'habits':[],'glossary':[]}
        for mid in ('1','2'):
            self.store.append('g',message(mid))
        service=gm.MemoryService(self.store,batch_size=3,interval=180,analyzer=analyzer)
        self.assertFalse(await service.process('g'))
        self.assertTrue(await service.process('g',force=True))
        self.assertEqual(1,len(calls))
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])

    async def test_shutdown_cancels_background_wait_without_losing_raw_messages(self):
        started=asyncio.Event()
        async def analyzer(*args):
            started.set()
            await asyncio.Event().wait()
        for mid in ('1','2','3'):
            self.store.append('g',message(mid))
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyzer)
        service.start()
        await asyncio.wait_for(started.wait(),2)
        await service.close()
        self.assertIsNone(service.worker)
        self.assertEqual(['1','2','3'],self.store.snapshot('g')['pending_ids'])

    async def test_ingress_preserves_order_and_drains_on_shutdown(self):
        service=gm.MemoryService(self.store,batch_size=96)
        for mid in ('1','2','3'):
            service.enqueue(IncomingMessage(conv_id='g',user_id='a',user_name='甲',
                message_id=mid,text='普通聊天'+mid,is_group=True))
        self.assertFalse(self.store.path('g').exists())
        # Start only persistence here: no model request is relevant to this contract.
        service.ingester=asyncio.create_task(service.ingest())
        await service.close()
        self.assertEqual(['1','2','3'],self.store.snapshot('g')['pending_ids'])

    async def test_twenty_pending_triggers_before_timer_but_nineteen_does_not(self):
        batches=[]
        async def analyzer(store,snapshot):
            batches.append(snapshot['pending_ids'])
            return {'habits':[],'glossary':[]}
        service=gm.MemoryService(self.store,batch_size=20,interval=180,analyzer=analyzer)
        for mid in range(1,20):
            self.store.append('g',message(str(mid)))
        self.assertFalse(await service.process('g'))
        self.store.append('g',message('20'))
        self.assertTrue(await service.process('g'))
        self.assertEqual([list(map(str,range(1,21)))],batches)
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])

    async def test_interval_flushes_one_new_message_with_historical_evidence(self):
        for mid in ('1','2','3'):
            self.store.append('g',message(mid))
        old=self.store.snapshot('g')
        self.store.complete('g',old,{'habits':[],'glossary':[]})
        now=time.time()
        self.store.append('g',message('4') | {'time':now})
        async def analyzer(store,snapshot):
            facts,rejected=store.validate(snapshot,{'habits':[habit(ids=('2','3','4'))],'glossary':[]})
            self.assertEqual(0,rejected)
            return facts
        service=gm.MemoryService(self.store,batch_size=20,interval=180,analyzer=analyzer)
        with patch.object(gm.time,'time',return_value=now+179):
            self.assertFalse(await service.process('g'))
        with patch.object(gm.time,'time',return_value=now+180):
            self.assertTrue(await service.process('g'))
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])
        self.assertTrue(self.store.relevant('g',['a'],'')['users'])


class AnalyzerCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=MemoryStore(tmp.name)
        for mid in ('1','2','3'):
            self.store.append('g',message(mid))

    def response(self,output):
        return NS(choices=[NS(message=NS(content=json.dumps(output)),finish_reason='stop')],usage=None)

    async def test_all_rejected_output_preserves_pending_after_correction_fails(self):
        bad=habit()
        bad['user_id']='wrong-author'
        create=AsyncMock(return_value=self.response({'habits':[bad],'glossary':[]}))
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyze)
        with patch('src.deepseek_client.client.chat.completions.create',create):
            self.assertFalse(await service.process('g'))
        reopened=MemoryStore(self.store.root)
        self.assertEqual(['1','2','3'],reopened.snapshot('g')['pending_ids'])
        self.assertEqual(0,reopened.snapshot('g')['processed_at'])
        self.assertEqual({},reopened.relevant('g',['a'],''))
        self.assertIn('g',service.retry_at)

    async def test_all_rejected_output_can_be_corrected_into_valid_memory(self):
        bad=habit(ids=('invented',))
        create=AsyncMock(side_effect=[
            self.response({'habits':[bad],'glossary':[]}),
            self.response({'habits':[habit()],'glossary':[]})])
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyze)
        with patch('src.deepseek_client.client.chat.completions.create',create):
            self.assertTrue(await service.process('g'))
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])
        self.assertEqual(1,len(self.store.snapshot('g')['habits']))
        self.assertEqual('确认时常用“收到捏”',self.store.relevant('g',['a'],'')['users'][0]['habits'][0]['description'])

    async def test_empty_conclusion_is_a_successful_batch(self):
        create=AsyncMock(return_value=self.response({'habits':[],'glossary':[]}))
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyze)
        with patch('src.deepseek_client.client.chat.completions.create',create):
            self.assertTrue(await service.process('g'))
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])

    async def test_partial_rejection_keeps_valid_facts(self):
        create=AsyncMock(return_value=self.response({'habits':[habit(),habit(ids=('invented',))],'glossary':[]}))
        service=gm.MemoryService(self.store,batch_size=3,analyzer=analyze)
        with patch('src.deepseek_client.client.chat.completions.create',create):
            self.assertTrue(await service.process('g'))
        self.assertEqual(1,len(self.store.snapshot('g')['habits']))
        self.assertEqual([],self.store.snapshot('g')['pending_ids'])


if __name__=='__main__':
    unittest.main(verbosity=2)
