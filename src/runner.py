"""Console + bounded rotating logs for start.sh (same process, same signals)."""
import asyncio
import io
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LoggedStream(io.TextIOBase):
    def __init__(self, console, logger):
        self.console, self.logger, self.pending = console, logger, ""

    @property
    def encoding(self):
        return "utf-8"

    def reconfigure(self, **kwargs):
        if hasattr(self.console, "reconfigure"):
            self.console.reconfigure(**kwargs)

    def write(self, text):
        self.console.write(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.logger.info(line)
        return len(text)

    def flush(self):
        self.console.flush()


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
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    run()
