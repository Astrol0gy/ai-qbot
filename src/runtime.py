"""A round owns tentative writes until it is still valid and ready to deliver.

Context variables propagate into asyncio.to_thread. Cancelling the waiter therefore
revokes the worker's permission to commit, even when the worker cannot be stopped.
"""
import contextvars
import functools
import inspect
import os
import tempfile
import threading
import time
import uuid
import weakref
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


class RoundExpired(RuntimeError):
    pass


_current = contextvars.ContextVar("qbot_round", default=None)
_rounds = weakref.WeakSet()
_guard = threading.RLock()
_locks = [threading.RLock() for _ in range(128)]
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qbot-io")
_worker_slots = threading.BoundedSemaphore(8)
_workers = set()


async def run_blocking(function, *args):
    check()
    if not _worker_slots.acquire(blocking=False):
        raise RuntimeError("后台 I/O 已达并发上限，请稍后重试")
    context = contextvars.copy_context()
    try:
        future = _executor.submit(context.run, function, *args)
    except BaseException:
        _worker_slots.release()
        raise
    with _guard:
        _workers.add(future)
    def finished(future):
        with _guard:
            _workers.discard(future)
        _worker_slots.release()
    future.add_done_callback(finished)
    return await asyncio.wrap_future(future)


async def shutdown():
    with _guard:
        for lease in list(_rounds):
            lease.revoke()
        futures = list(_workers)
    if futures:
        done, pending = await asyncio.wait([asyncio.wrap_future(f) for f in futures], timeout=5)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for future in pending:
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    _executor.shutdown(wait=False, cancel_futures=True)


def lock_for(key):
    return _locks[hash(str(key)) % len(_locks)]


def current():
    return _current.get()


def check():
    lease = current()
    if lease:
        lease.check()


def remaining(limit):
    check()
    lease = current()
    return min(limit, max(0.001, lease.deadline - time.monotonic())) if lease else limit


@contextmanager
def phase(kind, tag=""):
    """Share the active phase with the channel without exposing tool arguments."""
    lease = current()
    if lease:
        lease.progress = (kind, tag, time.monotonic(), object())
    try:
        yield
    finally:
        if lease:
            lease.progress = ("model", "", time.monotonic(), object())


def service_timeout(connect=5):
    """API response waiting has no read deadline; connection failures still surface."""
    check()
    return (connect, None)


def _disk(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def read_text(path):
    check()
    lease = current()
    path = os.path.abspath(path)
    if lease and path in lease.writes:
        return lease.writes[path][1]
    return _disk(path)


def exists(path):
    check()
    lease = current()
    path = os.path.abspath(path)
    if lease and path in lease.writes:
        return lease.writes[path][1] is not None
    return os.path.exists(path)


def _atomic(path, text):
    if text is None:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".qbot-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_text(path, text):
    path = os.path.abspath(path)
    with _guard:
        check()
        lease = current()
        if lease:
            original = lease.writes[path][0] if path in lease.writes else _disk(path)
            lease.writes[path] = (original, text)
        else:
            _atomic(path, text)


class RoundLease:
    def __init__(self, conv_id, timeout):
        self.conv_id = conv_id
        self.id = uuid.uuid4().hex
        self.deadline = float("inf") if timeout is None else time.monotonic() + timeout
        self.valid = True
        self.progress = ("model", "", time.monotonic(), object())
        self.business_tools_used = False
        self.chat_segments_allowed = True
        self.brief_chat = False
        self.focus_text = None
        self.reply_only = False
        self.intent_decision = None
        self.reply_to = None
        self.current_message_id = ""
        self.writes = {}
        self.after_commit = []
        self.history = None
        self.reset_history = False
        with _guard:
            _rounds.add(self)

    def check(self):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None and task.cancelling():
            self.revoke()
        if not self.valid or time.monotonic() >= self.deadline:
            raise RoundExpired("这一轮已过期，取消状态与历史提交")

    def revoke(self):
        with _guard:
            self.valid = False
            self.writes.clear()
            self.history = None

    def commit(self):
        # Compare before replacing: concurrent local commands must never be lost.
        with _guard:
            self.check()
            for path, (before, _) in self.writes.items():
                if _disk(path) != before:
                    raise RuntimeError("状态已被其他操作修改，请重新处理本轮")
            applied = []
            try:
                for path, (before, after) in self.writes.items():
                    self.check()
                    _atomic(path, after)
                    applied.append((path, before))
            except BaseException:
                for path, before in reversed(applied):
                    _atomic(path, before)
                raise
            self.writes.clear()
            callbacks, self.after_commit = self.after_commit, []
            for callback in callbacks:
                callback()


@contextmanager
def use(lease):
    token = _current.set(lease)
    try:
        yield lease
    finally:
        _current.reset(token)


def invalidate(conv_id):
    with _guard:
        for lease in list(_rounds):
            if lease.conv_id == conv_id and lease is not current():
                lease.revoke()


def reset_context(conv_id):
    def reset():
        from . import deepseek_client
        from .trigger import trigger
        invalidate(conv_id)
        deepseek_client._conversations.pop(conv_id, None)
        trigger.buffer.clear(conv_id)
        trigger.clear_waiting(conv_id)
        trigger.leave_session(conv_id)
    lease = current()
    if lease:
        lease.check()
        lease.reset_history = True
        lease.after_commit.append(reset)
    else:
        reset()


def serialized(function):
    """Lock the entire read/modify/write operation, not just os.replace."""
    signature = inspect.signature(function)
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        key = bound.arguments.get("group_id", "")
        with lock_for(key):
            check()
            try:
                return function(*args, **kwargs)
            except OSError as exc:
                if current():
                    raise
                return f"保存失败，原数据未更改：{exc}"
    return wrapped
