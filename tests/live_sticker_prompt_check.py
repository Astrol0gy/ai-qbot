"""Opt-in real-model A/B check; reads the existing RAG index, never sends QQ messages.

Export trusted local prompt variants, then run the bundle against a deployment's
existing config/gallery. No .env, chat logs or credentials are exported. Group
participation is held at REPLY to isolate expression choice from intent routing.
"""
import argparse
import ast
import asyncio
import contextlib
import contextvars
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def extract(source, name, *, parent=None, value=False):
    tree = ast.parse(source)
    nodes = next(n for n in tree.body if getattr(n, 'name', None) == parent).body if parent else tree.body
    if value:
        node = next(n for n in nodes if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
        if name == 'CHAT_POLICY':
            # This trusted project literal has one configured-budget f-string.
            return eval(compile(ast.Expression(node.value), '<trusted-policy>', 'eval'),
                        {'CHAT_BRIEF_MAX_CHARS': 80, '__builtins__': {}})
        return ast.literal_eval(node.value)
    node = next(n for n in nodes if getattr(n, 'name', None) == name)
    node.decorator_list = []
    return ast.unparse(node)


def export_bundle(root, baseline, output):
    def variant(ref):
        def read(path):
            return ((root / path).read_text(encoding='utf-8') if ref is None else
                    subprocess.check_output(['git', 'show', f'{ref}:{path}'], cwd=root).decode('utf-8'))
        image_source = read('src/tools/image_provider.py')
        # Extract only literal tool descriptions; do not instantiate a provider.
        tree = ast.parse(image_source)
        tool_dict = next(n for n in ast.walk(tree) if isinstance(n, ast.Dict)
                         and any(isinstance(k, ast.Constant) and k.value == 'name'
                                 and isinstance(v, ast.Constant) and v.value == 'send_sticker'
                                 for k, v in zip(n.keys, n.values)))
        description = next(ast.literal_eval(v) for k, v in zip(tool_dict.keys, tool_dict.values)
                           if isinstance(k, ast.Constant) and k.value == 'description')
        return dict(round_source=extract(read('src/deepseek_client.py'), '_chat_round'),
                    chat_policy=extract(read('src/tools/chat_provider.py'), 'CHAT_POLICY', value=True),
                    answer_policy=extract(read('src/chat_intent.py'), 'ANSWER_POLICY', value=True),
                    image_skill=read('src/skills/image.md'), description=description,
                    formatter=extract(image_source, '_format_candidates', parent='ImageProvider'))
    bundle = dict(baseline_ref=baseline, baseline=variant(baseline), candidate=variant(None))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')


CASES = [
    ('group_tease', '你这吃白饭的大肥鱼', True, 'casual'),
    ('group_sandbox_banter', '傻孩子，我还没给你接sandbox', True, 'casual'),
    ('private_game', '刚打游戏又输了，气死我了', False, 'casual'),
    ('private_cute', '你这条大肥鱼也太可爱了吧哈哈哈', False, 'casual'),
    ('group_technical', '代码报错10061是什么意思？简单说', True, 'text'),
    ('private_text_only', '只用文字回复，别发表情：你这吃白饭的大肥鱼', False, 'text'),
    ('group_distress', '我家人正在住院，我很害怕，想找你认真聊聊', True, 'text'),
    ('private_conflict', '我和家人吵得很严重，现在真的很难受，请认真听我说', False, 'text'),
    ('group_explicit_sticker', '只发一张无语的表情包，不要配字', True, 'sticker_only'),
    ('private_explicit_sticker', '发张卖萌的表情包吧', False, 'sticker'),
]


async def run(args):
    sys.path.insert(0, str(args.root.resolve()))
    from src import deepseek_client as dc, chat_intent, skills, runtime, tools
    from src import campaign_state, user_profile, sticker_rag, stickers_utils
    from src.tools.image_provider import ImageProvider
    from src.tools.chat_provider import ReplyOutbox, reply_text_length
    from unittest.mock import patch

    bundle = json.loads(args.bundle.read_text(encoding='utf-8'))
    args.out.mkdir(parents=True, exist_ok=True)
    user_profile.PROFILES_DIR = str(args.out / 'profiles')
    campaign_state.CAMPAIGNS_DIR = str(args.out / 'campaigns')
    frozen_gallery = stickers_utils.snapshot()
    if not frozen_gallery or not sticker_rag.model_available():
        raise RuntimeError('Real gallery and installed CLIP model are required for this check')
    sticker_rag.default_snapshot = lambda: frozen_gallery
    # Background-mode search only reads cached vectors; do not start an index worker.
    rag = sticker_rag.get_rag()
    if not rag._available_entries(frozen_gallery):
        raise RuntimeError('Existing RAG cache is required; this check never builds it')
    baseline_read = skills._read_text
    current_variant = None
    def read_skill(path, limit):
        if path.name == 'image.md':
            return current_variant['image_skill']
        return baseline_read(path, limit)
    record = contextvars.ContextVar('sticker_prompt_case')
    actual_execute = dc.execute_tool
    actual_create = dc.client.chat.completions.create
    def execute(name, arguments, context=None):
        row = record.get()
        if name not in {'load_skill', 'read_skill_resource', 'list_stickers', 'send_sticker'}:
            raise RuntimeError('Unexpected tool in read-only experiment: ' + name)
        result = actual_execute(name, arguments, context=context)
        row['tools'].append(dict(name=name, arguments=json.loads(arguments), result=result))
        return result
    async def create(**kwargs):
        response = await actual_create(**kwargs)
        row = record.get()
        row['requests'] += 1
        row['usage'].append(response.usage.model_dump() if response.usage else {})
        # Visible final/tool content only; never record hidden reasoning.
        return response

    report = dict(model=dc.DEEPSEEK_MODEL, baseline_ref=bundle['baseline_ref'],
                  bundle_sha256=hashlib.sha256(args.bundle.read_bytes()).hexdigest(),
                  gallery_size=len(frozen_gallery), cases=[])
    def save():
        (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def apply(arm):
        nonlocal current_variant
        current_variant = bundle[arm]
        exec(compile(current_variant['round_source'], '<trusted-prompt-variant>', 'exec'), dc.__dict__)
        dc.CHAT_POLICY = current_variant['chat_policy'].replace('最多80字', f'最多{dc.CHAT_BRIEF_MAX_CHARS}字')
        chat_intent.ANSWER_POLICY = current_variant['answer_policy']
        meta, _ = skills._parse_frontmatter(current_variant['image_skill'])
        skills.get_skill(meta['name'])['description'] = meta['description']
        tools._registry._tool_map['send_sticker'][0]['function']['description'] = current_variant['description']
        namespace = {}
        exec(compile(current_variant['formatter'], '<trusted-candidate-prompt>', 'exec'), namespace)
        ImageProvider._format_candidates = staticmethod(namespace['_format_candidates'])

    semaphore = asyncio.Semaphore(args.concurrency)
    async def case(arm, repeat, item):
        async with semaphore:
            name, text, group, kind = item
            row = dict(arm=arm, repeat=repeat, name=name, input=text, group=group,
                       kind=kind, tools=[], requests=0, usage=[])
            token = record.set(row)
            key = f'prompt_check_{arm}_{repeat}_{name}'
            lease = runtime.RoundLease(key, None)
            lease.focus_text = text
            lease.reply_only = group
            lease.reply_to = '1'
            lease.intent_decision = chat_intent.IntentDecision(
                action='REPLY', recipient='bot', expression='complete',
                target_message_ids=('1',), context_message_ids=(),
                evidence_message_ids=('1',), reason='实验固定参与，仅比较回答阶段的表情选择')
            outbox = ReplyOutbox(known_ids={'1'}) if group else None
            started = time.monotonic()
            try:
                with runtime.use(lease):
                    result = await dc.chat(key, 'test_user', text, user_name='测试用户', outbox=outbox)
                row['output'] = result
                markers = re.findall(r'\[img\](.*?)\[/img\]', result, re.S)
                row.update(images=len(markers), text_chars=reply_text_length(result),
                           selected_real_image=bool(markers) and all(stickers_utils.resolve(p) for p in markers),
                           queried=any(t['name'] == 'send_sticker' for t in row['tools']))
                row['valid'] = bool(result) and len(markers) <= 1 and row['text_chars'] <= dc.CHAT_BRIEF_MAX_CHARS
                if markers:
                    row['valid'] = row['valid'] and row['selected_real_image'] and row['queried']
                if kind == 'text':
                    row['valid'] = row['valid'] and not markers and not row['queried']
                if kind.startswith('sticker'):
                    row['valid'] = row['valid'] and bool(markers)
                if kind == 'sticker_only':
                    row['valid'] = row['valid'] and not row['text_chars']
            except Exception as exc:
                row.update(valid=False, error=f'{type(exc).__name__}: {exc}')
            finally:
                lease.revoke()
                record.reset(token)
            row['seconds'] = round(time.monotonic() - started, 2)
            report['cases'].append(row)
            save()
            print(json.dumps({k:v for k,v in row.items() if k not in {'tools','usage'}}, ensure_ascii=False), flush=True)

    try:
        with patch.object(skills, '_read_text', read_skill), patch.object(dc, 'execute_tool', execute), \
             patch.object(dc.client.chat.completions, 'create', create), \
             (args.out / 'runtime.log').open('w', encoding='utf-8') as log:
            for repeat in range(1, args.repeats + 1):
                for arm in (('baseline','candidate') if repeat % 2 else ('candidate','baseline')):
                    apply(arm)
                    with contextlib.redirect_stdout(log):
                        await asyncio.gather(*(case(arm, repeat, item) for item in CASES))
                    print(f'Completed {arm} repeat {repeat}: {len(report["cases"])} rounds', flush=True)
    finally:
        report['summary'] = {
            arm: {kind: dict(
                rounds=len(rows), queried=sum(r.get('queried', False) for r in rows),
                images=sum(r.get('images', 0) > 0 for r in rows),
                valid=sum(r['valid'] for r in rows))
                for kind in ('casual', 'text', 'sticker_only', 'sticker')
                if (rows := [r for r in report['cases'] if r['arm'] == arm and r['kind'] == kind])}
            for arm in ('baseline', 'candidate')}
        save()
        await dc.client.close()
    print('REPORT', args.out / 'report.json', flush=True)
    return len(report['cases']) == len(CASES) * args.repeats * 2 and all(r['valid'] for r in report['cases'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--export', type=Path)
    parser.add_argument('--baseline', default='HEAD')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--concurrency', type=int, default=2)
    options = parser.parse_args()
    if options.export:
        export_bundle(options.root, options.baseline, options.export)
    else:
        if not options.bundle or not options.out:
            parser.error('--bundle and --out are required to run real-model checks')
        raise SystemExit(0 if asyncio.run(run(options)) else 1)
