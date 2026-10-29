"""Private, bounded snapshots of delivered conversation history, including summaries."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .observability import log_event

ROOT = Path(__file__).resolve().parent.parent / 'data' / 'conversations'
TTL = 7 * 86400
MAX_FILES = 500
MAX_BYTES = 2 * 1024 * 1024


class HistoryStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)

    def path(self, conversation_id):
        return self.root / (hashlib.sha256(conversation_id.encode('utf8')).hexdigest() + '.json')

    def load_all(self):
        loaded = {}
        files = sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:MAX_FILES]:
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                    continue
                data = json.loads(path.read_text(encoding='utf8'))
                cid, messages = data['conversation_id'], data['messages']
                if (data.get('version') != 1 or not isinstance(cid, str) or self.path(cid) != path
                        or time.time() - data['updated_at'] > TTL or not isinstance(messages, list)
                        or len(messages) > 2002):
                    continue
                if any(not isinstance(m, dict) or m.get('role') not in {'user', 'assistant'}
                       or not isinstance(m.get('content'), str) for m in messages):
                    continue
                loaded[cid] = [{'role': m['role'], 'content': m['content']} for m in messages]
            except (OSError, ValueError, KeyError, TypeError):
                log_event('history.load.failed', file=path.name)
        return loaded

    def save(self, conversation_id, messages):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {'version': 1, 'conversation_id': conversation_id, 'updated_at': time.time(),
                'messages': [{'role': m['role'], 'content': m['content']} for m in messages]}
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        if len(payload.encode('utf8')) > MAX_BYTES:
            raise ValueError('conversation history exceeds snapshot limit')
        fd, temporary = tempfile.mkstemp(prefix='.history-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf8', newline='\n') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(conversation_id))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        # Bound inactive files too; expiration is independent of model activity.
        files = sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= MAX_FILES or time.time() - path.stat().st_mtime > TTL:
                path.unlink(missing_ok=True)

    def clear(self, conversation_id):
        self.path(conversation_id).unlink(missing_ok=True)
