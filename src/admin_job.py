"""Detached Linux control worker: survives the bot screen exiting during reload/stop."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def run(action, job_id):
    import fcntl
    if action not in ('reload', 'stop') or not re.fullmatch(r'[0-9a-f]{12}', job_id):
        raise ValueError('invalid control job')
    folder = ROOT / 'data' / 'admin' / 'jobs'
    path = folder / (job_id + '.json')
    def record(status, **extra):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(dict(id=job_id, action=action, status=status, **extra)), encoding='utf-8')
        temp.chmod(0o600)
        os.replace(temp, path)
    with (folder / '.control.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record('busy')
            return
        record('running')
        try:
            env = dict(os.environ, PYTHON_CMD=sys.executable)
            result = subprocess.run(['bash', str(ROOT / 'start.sh'), action], cwd=ROOT,
                                    env=env, timeout=180)
            record('ok' if result.returncode == 0 else 'failed', exit_code=result.returncode)
        except Exception as exc:
            record('failed', error_type=type(exc).__name__)


if __name__ == '__main__':
    run(*sys.argv[1:])
