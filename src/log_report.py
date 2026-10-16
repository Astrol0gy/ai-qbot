"""Read current and rotated JSONL logs without importing the bot or its secrets."""
import argparse
import json
from collections import Counter
from pathlib import Path


def read_events(path):
    paths = [Path(str(path) + f'.{i}') for i in (3, 2, 1)] + [Path(path)]
    for file in paths:
        if not file.is_file():
            continue
        with file.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and 'event' in event:
                    yield event


def report(events):
    counts, outcomes = Counter(), Counter()
    opened, finished, timeouts = {}, set(), []
    for event in events:
        name = event['event']
        counts[name] += 1
        trace = event.get('trace_id')
        if trace and name in ('message.received', 'message.start'):
            opened[trace] = event
        if trace and name == 'message.finish':
            finished.add(trace)
            outcomes[event.get('status', 'unknown')] += 1
            if event.get('status') == 'timeout':
                timeouts.append({k: event.get(k) for k in ('time', 'trace_id', 'conversation_id', 'message_id', 'stage', 'elapsed_ms')})
    return dict(events=dict(counts), outcomes=dict(outcomes), timeouts=timeouts,
        unfinished=[{k: event.get(k) for k in ('trace_id', 'conversation_id', 'message_id')}
            for trace, event in opened.items() if trace not in finished])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', default='logs/qbot.log')
    parser.add_argument('--group')
    parser.add_argument('--trace')
    args = parser.parse_args()
    path = Path(args.path)
    if not any(p.is_file() for p in [path, *[Path(str(path)+f'.{i}') for i in (1, 2, 3)]]):
        parser.error(f'log file does not exist: {path}')
    events = read_events(path)
    if args.group:
        events = (e for e in events if e.get('conversation_id') in (args.group, f'onebot_group_{args.group}'))
    if args.trace:
        for event in events:
            if event.get('trace_id') == args.trace:
                print(json.dumps(event, ensure_ascii=False))
    else:
        print(json.dumps(report(events), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
