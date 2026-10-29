"""Group-scoped, bounded observation memory. Model output never chooses a path."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from ..observability import log_event
from ..user_profile import _detect_personal_data

RAW_LIMIT = 96
RAW_TTL = 7 * 86400
FACT_TTL = 90 * 86400
CATEGORIES = {'vocabulary', 'sentence', 'punctuation', 'expression', 'tone'}


def safe_text(value, limit):
    return (isinstance(value, str) and 0 < len(value.strip()) <= limit
            and not _detect_personal_data(value))


class MemoryStore:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def path(self, group_id):
        return self.root / (hashlib.sha256(group_id.encode('utf-8')).hexdigest() + '.json')

    def _read(self, group_id):
        path = self.path(group_id)
        if not path.exists():
            return dict(version=1, group_id=group_id, messages=[], pending_ids=[], seen_ids=[],
                        habits=[], glossary=[], processed_at=0, bot_user_id='')
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('群记忆文件不可用')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('group_id') != group_id:
            raise ValueError('群记忆文件归属或版本不匹配')
        return data

    def _write(self, group_id, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.memory-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path(group_id))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def append(self, group_id, message, bot_user_id=''):
        with self.lock:
            data = self._read(group_id)
            if message['id'] in data['seen_ids']:
                return False
            now = time.time()
            data['messages'] = [m for m in data['messages'] if now - m['time'] < RAW_TTL]
            data['messages'] = (data['messages'] + [message])[-RAW_LIMIT:]
            valid = {m['id'] for m in data['messages']}
            pending = data['pending_ids'] + [message['id']]
            data['pending_ids'] = [mid for mid in pending if mid in valid]
            if len(pending) != len(data['pending_ids']):
                log_event('memory.queue.trimmed', conversation_id=group_id,
                          dropped=len(pending)-len(data['pending_ids']))
            data['seen_ids'] = (data['seen_ids'] + [message['id']])[-1024:]
            if bot_user_id:
                data['bot_user_id'] = bot_user_id
            for key in ('habits', 'glossary'):
                data[key] = [r for r in data[key] if now-r['updated_at'] < FACT_TTL]
            self._write(group_id, data)
            return True

    def groups(self):
        if not self.root.is_dir():
            return []
        groups = []
        for path in self.root.glob('*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > 32*1024*1024:
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data.get('group_id'), str) and self.path(data['group_id']) == path:
                    groups.append(data['group_id'])
            except (OSError, ValueError):
                log_event('memory.storage.invalid', file=path.name)
        return groups

    def snapshot(self, group_id, *, prune=False):
        with self.lock:
            data = self._read(group_id)
            before = (len(data['messages']),len(data['pending_ids']),len(data['habits']),len(data['glossary']))
            now = time.time()
            data['messages'] = [m for m in data['messages'] if now-m['time'] < RAW_TTL]
            valid = {m['id'] for m in data['messages']}
            data['pending_ids'] = [mid for mid in data['pending_ids'] if mid in valid]
            for key in ('habits','glossary'):
                data[key]=[r for r in data[key] if now-r['updated_at']<FACT_TTL]
            after = (len(data['messages']),len(data['pending_ids']),len(data['habits']),len(data['glossary']))
            if prune and before != after:
                self._write(group_id,data)
            return data

    def existing(self, data):
        users = {m['user_id'] for m in data['messages']}
        text = '\n'.join(m['text'] for m in data['messages']).casefold()
        now = time.time()
        return dict(habits=[r for r in data['habits'] if r['user_id'] in users and now-r['updated_at'] < FACT_TTL][-32:],
                    glossary=[r for r in data['glossary'] if r['term'].casefold() in text and now-r['updated_at'] < FACT_TTL][-24:])

    def validate(self, data, output):
        if not isinstance(output, dict) or set(output) != {'habits','glossary'}:
            raise ValueError('群记忆结果必须只含habits和glossary')
        pool = {m['id']:m for m in data['messages']}
        existing = self.existing(data)
        records = {r['id']:r for rows in existing.values() for r in rows}
        for r in records.values():
            pool.update({m['id']:m for m in r['evidence'] if m['id'] not in pool})
        valid, rejected = {'habits':[], 'glossary':[]}, 0
        reasons = {}
        for kind in valid:
            items = output[kind]
            if not isinstance(items, list) or len(items)>16:
                raise ValueError('群记忆列表格式或长度错误')
            for item in items:
                try:
                    if not isinstance(item, dict):
                        raise ValueError('record')
                    ids = item.get('evidence_ids')
                    if (not isinstance(ids, list) or not ids or len(ids)>8
                            or any(not isinstance(mid,str) or mid not in pool for mid in ids)):
                        raise ValueError('evidence')
                    ids = list(dict.fromkeys(ids))
                    evidence = [pool[mid] for mid in ids]
                    if not set(ids).intersection(data['pending_ids']):
                        raise ValueError('no_new_evidence')
                    confidence = item.get('confidence')
                    if (isinstance(confidence,bool) or not isinstance(confidence,(int,float))
                            or not math.isfinite(confidence) or not 0<=confidence<=1
                            or not isinstance(item.get('uncertain'),bool)):
                        raise ValueError('confidence')
                    old_id = item.get('record_id', '')
                    old = records.get(old_id) if old_id else None
                    if old_id and (old is None or old not in existing[kind]):
                        raise ValueError('unknown_record')
                    brief_evidence=[{k:m[k] for k in ('id','user_id','name','time','mentions','at_bot')}
                                    | {'text':m['text'][:160]} for m in evidence]
                    common = dict(id=old_id or uuid.uuid4().hex, confidence=confidence,
                                  uncertain=item['uncertain'], evidence=brief_evidence, updated_at=time.time())
                    if kind=='habits':
                        uid = item.get('user_id')
                        if (len(ids)<3 or not isinstance(uid,str) or any(m['user_id']!=uid for m in evidence)
                                or item.get('category') not in CATEGORIES or not safe_text(item.get('description'),160)
                                or old and old['user_id']!=uid):
                            raise ValueError('habit_owner_or_samples')
                        common.update(user_id=uid, user_name=evidence[-1]['name'], category=item['category'],
                                      description=item['description'].strip())
                    else:
                        target = item.get('referent_user_id','')
                        target_type = item.get('referent_type')
                        known = {m['user_id'] for m in evidence} | {uid for m in evidence for uid in m['mentions']}
                        if (len(ids)<2 or not safe_text(item.get('term'),32) or not safe_text(item.get('meaning'),180)
                                or not any(item['term'].strip().casefold() in m['text'].casefold() for m in evidence)
                                or not isinstance(item.get('scope'),str) or len(item['scope'])>80
                                or _detect_personal_data(item['scope'])
                                or target_type not in {'bot','user','topic','unknown'}
                                or not isinstance(target,str)
                                or target_type=='user' and target not in known
                                or target_type!='user' and target
                                or target_type=='bot' and not any(m['at_bot'] for m in evidence)
                                or old and old['term']!=item['term']):
                            raise ValueError('glossary_target_or_samples')
                        common.update(term=item['term'].strip(), meaning=item['meaning'].strip(),
                                      scope=item['scope'].strip(), referent_type=target_type, referent_user_id=target)
                    valid[kind].append(common)
                except (ValueError, TypeError, KeyError) as exc:
                    rejected += 1
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    reasons[reason] = reasons.get(reason, 0) + 1
        if rejected:
            log_event('memory.validation.rejected', conversation_id=data['group_id'],
                      rejected=rejected, reasons=reasons)
        return valid, rejected

    def complete(self, group_id, snapshot, facts):
        with self.lock:
            data = self._read(group_id)  # New arrivals during inference must survive.
            for kind, rows in facts.items():
                for row in rows:
                    old = next((r for r in data[kind] if r['id']==row['id']), None)
                    if not old:
                        keys = ('user_id','category','description') if kind=='habits' else ('term','meaning','scope','referent_type','referent_user_id')
                        old = next((r for r in data[kind] if all(r[k]==row[k] for k in keys)), None)
                    if old:
                        row = {**row,'id':old['id']}
                        data[kind].remove(old)
                    data[kind].append(row)
                if kind=='habits':
                    kept=[]
                    for uid in dict.fromkeys(r['user_id'] for r in data[kind]):
                        kept.extend([r for r in data[kind] if r['user_id']==uid][-8:])
                    data[kind]=kept[-8000:]
                else:
                    data[kind]=data[kind][-128:]
            completed=set(snapshot['pending_ids'])
            data['pending_ids']=[mid for mid in data['pending_ids'] if mid not in completed]
            data['processed_at']=time.time()
            self._write(group_id,data)

    def relevant(self, group_id, user_ids, text, budget=3500):
        data = self.snapshot(group_id)
        now = time.time()
        confirmed=lambda r: not r['uncertain'] and r['confidence']>=0.8 and now-r['updated_at']<FACT_TTL
        result={'users':[], 'glossary':[]}
        for uid in list(dict.fromkeys(user_ids))[-8:]:
            rows=[r for r in data['habits'] if r['user_id']==uid and confirmed(r)]
            if rows:
                result['users'].append(dict(user_id=uid,name=rows[-1]['user_name'],
                    habits=[dict(category=r['category'],description=r['description'],confidence=r['confidence']) for r in rows[-3:]]))
        for r in sorted(data['glossary'], key=lambda r:len(r['term']), reverse=True):
            if confirmed(r) and r['term'].casefold() in text.casefold():
                result['glossary'].append({k:r[k] for k in ('term','meaning','scope','referent_type','referent_user_id','confidence')})
                if len(result['glossary'])>=8:
                    break
        while len(json.dumps(result,ensure_ascii=False))>budget:
            if result['users']:
                result['users'].pop(0)
            elif result['glossary']:
                result['glossary'].pop()
            else:
                break
        return result if any(result.values()) else {}
