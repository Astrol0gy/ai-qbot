"""Single-line runtime events, correlated across async tasks and tool threads."""
import asyncio
import contextvars
import functools
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

_context = contextvars.ContextVar('qbot_log_context', default=None)
_sensitive = re.compile(r'(?:^|[_-])(?:token|secret|password|authorization|(?:api[_-]?)?key)(?:$|[_-])', re.I)
_ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def redact(value):
    if isinstance(value, dict):
        return {str(k): '<redacted>' if _sensitive.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = _ansi.sub('', value)
    for key, secret in os.environ.items():
        if _sensitive.search(key) and len(secret) >= 6:
            value = value.replace(secret, '<redacted>')
    value = re.sub(r'(?i)(Bearer\s+)\S+', r'\1<redacted>', value)
    value = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|token|password|secret|key)["']?\s*[=:]\s*["']?)[^"'\s&,;}]+''', r'\1<redacted>', value)
    def clean_url(match):
        try:
            p = urlsplit(match.group())
            host = p.netloc.rsplit('@', 1)[-1]
            return urlunsplit((p.scheme, host, p.path, '<redacted>' if p.query else '', ''))
        except ValueError:
            return '<invalid-url>'
    return re.sub(r'(?:https?|wss?)://[^\s\"\'<>]+', clean_url, value)


def encode_event(event, **fields):
    state = _context.get() or {}
    common = {k: state[k] for k in ('trace_id', 'channel', 'conversation_id', 'message_id', 'user_id', 'round_id') if k in state}
    record = dict(time=datetime.now(timezone.utc).isoformat(timespec='milliseconds'), event=event)
    record.update(common)
    record.update(fields)
    return json.dumps(redact(record), ensure_ascii=False, default=str)


def log_event(event, **fields):
    sys.stdout.write(encode_event(event, **fields) + '\n')
    sys.stdout.flush()


def current_fields():
    state = _context.get() or {}
    return {k: state[k] for k in ('trace_id', 'channel', 'conversation_id', 'message_id', 'user_id', 'round_id') if k in state}


def active_stage():
    return (_context.get() or {}).get('stage')


def message_fields(msg):
    return dict(trace_id=msg.trace_id, channel=msg.channel, conversation_id=msg.conv_id,
        message_id=msg.message_id, user_id=msg.user_id)


def outcome(status, **fields):
    state = _context.get()
    if state is not None:
        state.update(status=status, **fields)


def set_round(round_id):
    state = _context.get()
    if state is not None:
        state['round_id'] = round_id


def tool_arguments(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {'invalid': True}
    except (TypeError, ValueError):
        return {'invalid': True}


@contextmanager
def stage(name, **fields):
    state = _context.get()
    previous = state.get('stage') if state else None
    if state is not None:
        state['stage'] = name
    start = time.monotonic()
    status = 'ok'
    log_event(name + '.start', **fields)
    try:
        yield
    except BaseException as exc:
        status = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
        raise
    finally:
        log_event(name + '.finish', status=status, elapsed_ms=round((time.monotonic()-start)*1000, 1), **fields)
        if state is not None and status == 'ok' and state.get('stage') == name and state.get('status') != 'timeout':
            state['stage'] = previous


def traced_message(function):
    """One lifetime per message, including enrichment and nested core handling."""
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        msg = next((v if hasattr(v, 'conv_id') else v[0] for v in args
            if hasattr(v, 'conv_id') or isinstance(v, tuple) and v and hasattr(v[0], 'conv_id')), None)
        if msg is None:
            return await function(*args, **kwargs)
        current = _context.get()
        if current and current['trace_id'] == msg.trace_id:
            return await function(*args, **kwargs)
        state = dict(**message_fields(msg), status='completed', stage='dispatch')
        token = _context.set(state)
        start = time.monotonic()
        log_event('message.start', queue_wait_ms=round((start-msg.received_at)*1000, 1), at_bot=msg.is_at_bot)
        try:
            return await function(*args, **kwargs)
        except asyncio.CancelledError:
            state['status'] = 'cancelled'
            raise
        except Exception as exc:
            state['status'] = 'error'
            log_event('message.error', error_type=type(exc).__name__, detail=str(exc)[:500])
            raise
        finally:
            log_event('message.finish', status=state['status'], stage=state.get('stage'),
                elapsed_ms=round((time.monotonic()-start)*1000, 1),
                total_ms=round((time.monotonic()-msg.received_at)*1000, 1))
            _context.reset(token)
    return wrapped
