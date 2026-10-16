"""Console + bounded rotating logs for start.sh (same process, same signals)."""
import asyncio
import io
import json
import logging
import os
import signal
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .observability import current_fields, encode_event, log_event, redact


class LoggedStream(io.TextIOBase):
    def __init__(self, console, logger):
        self.console, self.logger = console, logger
        self.pending = {}
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return "utf-8"

    def reconfigure(self, **kwargs):
        if hasattr(self.console, "reconfigure"):
            self.console.reconfigure(**kwargs)

    def write(self, text):
        fields = current_fields()
        key = (threading.get_ident(), fields.get('trace_id'))
        with self.lock:
            buffered = self.pending.pop(key, '') + text
            while '\n' in buffered:
                line, buffered = buffered.split('\n', 1)
                self._emit(line, fields)
            if buffered:
                self.pending[key] = buffered
        return len(text)

    def _emit(self, line, fields):
        if not line.strip():
            return
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not {'time', 'event'} <= event.keys():
                raise ValueError('legacy output')
            formatted = json.dumps(redact(event), ensure_ascii=False, default=str)
        except (ValueError, TypeError):
            formatted = encode_event('legacy', **fields, text=line)
        self.console.write(formatted + '\n')
        self.logger.info(formatted)

    def flush(self):
        self.console.flush()

    def drain(self):
        with self.lock:
            for (_, trace_id), text in list(self.pending.items()):
                self._emit(text, {'trace_id': trace_id} if trace_id else {})
            self.pending.clear()
        self.flush()


async def _run():
    from .main import main
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    def stop(*_):
        loop.call_soon_threadsafe(task.cancel)
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    try:
        await main()
    except asyncio.CancelledError:
        pass


def run():
    path = Path(os.getenv("QBOT_LOG", "logs/qbot.log"))
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.getLogger("qbot.console")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = LoggedStream(stdout, logger), LoggedStream(stderr, logger)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("机器人已停止。")
    except Exception as exc:
        log_event('process.error', error_type=type(exc).__name__, detail=str(exc)[:500],
            traceback=traceback.format_exc())
        raise SystemExit(1) from None
    finally:
        sys.stdout.drain()
        sys.stderr.drain()
        sys.stdout, sys.stderr = stdout, stderr
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    run()
