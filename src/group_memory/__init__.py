"""Background group observation, independent of speaking decisions and round leases."""
import asyncio
from dataclasses import replace
from pathlib import Path
import re
import time

from .. import config
from ..observability import log_event
from .store import MemoryStore, safe_text
from .agent import analyze

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'group_memory'
_store = None
_service = None


def get_store():
    global _store
    if _store is None:
        _store=MemoryStore(ROOT)
    return _store


class MemoryService:
    def __init__(self, store, *, batch_size=None, interval=None, analyzer=analyze):
        self.store=store
        self.batch_size=batch_size or config.GROUP_MEMORY_BATCH_SIZE
        self.interval=interval if interval is not None else config.GROUP_MEMORY_INTERVAL_SEC
        self.analyzer=analyzer
        self.wake=asyncio.Event()
        self.worker=None
        self.ingester=None
        self.ingress=asyncio.Queue(maxsize=2048)
        self.retry_at={}

    def start(self):
        self.worker=asyncio.create_task(self.run(), name='group-memory')
        self.ingester=asyncio.create_task(self.ingest(), name='group-memory-ingest')

    def enqueue(self, msg, bot_user_id=''):
        # No I/O/await before the frontend queues its message: preserve arrival order.
        raw={key:msg.raw.get(key) for key in ('group_id','reply_id')}
        original=replace(msg,raw=raw)
        try:
            self.ingress.put_nowait((original,bot_user_id,time.time()))
        except asyncio.QueueFull:
            log_event('memory.observe.dropped',conversation_id=msg.conv_id,reason='ingress_full')

    async def ingest(self):
        while True:
            args=await self.ingress.get()
            try:
                await self.observe(*args)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event('memory.observe.failed',conversation_id=args[0].conv_id,error_type=type(exc).__name__)
            finally:
                self.ingress.task_done()

    async def observe(self, msg, bot_user_id='', observed_at=None):
        if (not msg.is_group or not msg.conv_id or not msg.user_id or not msg.message_id
                or msg.user_id==bot_user_id or not msg.text.strip() or msg.text.lstrip().startswith('/')
                or not safe_text(msg.text,2000)
                or re.search(r'(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_ -]?key|token|secret)\s*[=:])',msg.text)):
            return
        if msg.channel=='onebot':
            gid=str(msg.raw.get('group_id',''))
            if gid in config.ONEBOT_GROUP_BLACKLIST or config.ONEBOT_GROUP_WHITELIST and gid not in config.ONEBOT_GROUP_WHITELIST:
                return
        from ..campaign_state import is_dm_mode
        if is_dm_mode(msg.conv_id):
            return
        name=msg.user_name[:100]
        if not safe_text(name,100):
            name=''
        message=dict(id=msg.message_id,user_id=msg.user_id,name=name,text=msg.text[:600],
                     time=observed_at or time.time(),mentions=list(msg.mention_user_ids),at_bot=msg.is_at_bot,
                     quote_author_id=msg.quote.author_id if msg.quote else '',
                     quote_id=msg.raw.get('reply_id',''))
        if await asyncio.to_thread(self.store.append,msg.conv_id,message,bot_user_id):
            self.wake.set()

    async def process(self, group_id, *, force=False):
        now=time.time()
        if not force and now<self.retry_at.get(group_id,0):
            return False
        snapshot=await asyncio.to_thread(self.store.snapshot,group_id,prune=True)
        pending=snapshot['pending_ids']
        if not pending:
            return False
        first=next(m['time'] for m in snapshot['messages'] if m['id']==pending[0])
        if not force and len(pending)<self.batch_size and now-first<self.interval:
            return False
        try:
            facts=await self.analyzer(self.store,snapshot)
            await asyncio.to_thread(self.store.complete,group_id,snapshot,facts)
        except asyncio.CancelledError:
            raise  # Pending raw messages remain durable for the next process.
        except Exception as exc:
            self.retry_at[group_id]=time.time()+max(30,self.interval)
            log_event('memory.batch.failed',conversation_id=group_id,error_type=type(exc).__name__)
            return False
        self.retry_at.pop(group_id,None)
        log_event('memory.batch.committed',conversation_id=group_id,processed=len(pending),
                  habits=len(facts['habits']),glossary=len(facts['glossary']))
        return True

    async def run(self):
        while True:
            self.wake.clear()
            try:
                groups=await asyncio.to_thread(self.store.groups)
                for gid in groups:
                    try:
                        await self.process(gid)
                    except (OSError,ValueError,KeyError,TypeError) as exc:
                        log_event('memory.storage.failed',conversation_id=gid,error_type=type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event('memory.worker.failed',error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=5)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        if self.ingester:
            try:
                await asyncio.wait_for(self.ingress.join(),3)
            except asyncio.TimeoutError:
                log_event('memory.observe.dropped',count=self.ingress.qsize(),reason='shutdown_backlog')
            self.ingester.cancel()
            await asyncio.gather(self.ingester,return_exceptions=True)
            self.ingester=None
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker,return_exceptions=True)
            self.worker=None


async def start():
    global _service
    if config.GROUP_MEMORY_ENABLE and _service is None:
        _service=MemoryService(get_store())
        _service.start()
        log_event('memory.started',batch_size=config.GROUP_MEMORY_BATCH_SIZE,
                  interval_sec=config.GROUP_MEMORY_INTERVAL_SEC,model=config.GROUP_MEMORY_MODEL)


async def observe(msg, bot_user_id=''):
    if _service is not None and msg.is_group:
        _service.enqueue(msg,bot_user_id)


def context(group_id, user_ids, text):
    if not config.GROUP_MEMORY_ENABLE:
        return {}
    try:
        return get_store().relevant(group_id,user_ids,text)
    except (OSError,ValueError,KeyError,TypeError):
        log_event('memory.read.failed',conversation_id=group_id)
        return {}


async def close():
    global _service
    if _service:
        await _service.close()
        _service=None
