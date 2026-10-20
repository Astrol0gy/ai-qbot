"""Bounded FIFO intake shared by both adapters; enrichment happens after intake."""
import asyncio
from collections import deque
from ..observability import log_event, message_fields


class ConversationQueue:
    def __init__(self, per_conversation=128, total=2048, collect_sec=1.2, max_collect_sec=4):
        self.queues = {}
        self.per_conversation = per_conversation
        self.total = total
        self.size = 0
        self.owners = set()
        self.changed = {}
        self.inflight_extra = {}
        self.collect_sec = collect_sec
        self.max_collect_sec = max_collect_sec

    def pending(self, key):
        return tuple(record[0] for record in self.queues.get(key, ()))

    async def submit(self, key, item, handler, *, batch_handler=None):
        fields = message_fields(item[0]) if isinstance(item, tuple) and hasattr(item[0], 'trace_id') else {'conversation_id': key}
        queue = self.queues.get(key)
        if self.size + len(self.owners) + sum(self.inflight_extra.values()) >= self.total or (queue is not None and len(queue) >= self.per_conversation):
            log_event('message.finish', **fields, status='rejected', stage='queue', reason='queue_full')
            raise asyncio.QueueFull(f"会话 {key} 队列已满，拒绝本次入站消息")
        if queue is not None:
            queue.append((item, handler, batch_handler))
            self.size += 1
            self.changed[key].set()
            log_event('queue.enqueued', **fields, pending=len(queue))
            return
        queue = self.queues[key] = deque([(item, handler, batch_handler)])
        changed = self.changed[key] = asyncio.Event()
        self.size += 1
        owner = asyncio.current_task()
        self.owners.add(owner)
        try:
            while queue:
                _, _, batch = queue[0]
                if batch is not None and self.collect_sec:
                    deadline = asyncio.get_running_loop().time() + self.max_collect_sec
                    while all(record[2] == batch for record in queue):
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        changed.clear()
                        try:
                            await asyncio.wait_for(changed.wait(), min(self.collect_sec, remaining))
                        except asyncio.TimeoutError:
                            break
                current, current_handler, _ = queue.popleft()
                items = [current]
                if batch is not None:
                    while queue and queue[0][2] == batch and len(items) < 32:
                        items.append(queue.popleft()[0])
                self.size -= len(items)
                self.inflight_extra[key] = len(items) - 1
                fields = message_fields(current[0]) if isinstance(current, tuple) and hasattr(current[0], 'trace_id') else {'conversation_id': key}
                log_event('queue.dispatch', **fields, pending=len(queue))
                try:
                    if batch is not None:
                        log_event('queue.batch', **fields, count=len(items))
                        await batch(items)
                    else:
                        await current_handler(current)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[消息处理失败] 会话={key} {type(exc).__name__}: {exc}")
                finally:
                    self.inflight_extra.pop(key, None)
        finally:
            self.size -= len(queue)
            if queue:
                for pending, _, _ in queue:
                    pending_fields = message_fields(pending[0]) if isinstance(pending, tuple) and hasattr(pending[0], 'trace_id') else {'conversation_id': key}
                    log_event('message.finish', **pending_fields, status='cancelled', stage='queue', reason='channel_shutdown')
                print(f"[通道停止] 会话={key} 尚有 {len(queue)} 条未处理消息")
            self.queues.pop(key, None)
            self.changed.pop(key, None)
            self.owners.discard(owner)

    async def close(self):
        owners = list(self.owners - {asyncio.current_task()})
        for task in owners:
            task.cancel()
        if owners:
            await asyncio.gather(*owners, return_exceptions=True)
