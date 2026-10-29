"""Opt-in real VLM check on a few copied library images; never harvests or sends."""
import argparse
import ast
import base64
import io
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import vision_utils as vision, stickers_utils as stickers
from PIL import Image


def run(root, repeats, baseline=None):
    cases = [('number_screenshot', '无奈_显示_数字_截图_43_1.png', False),
             ('reaction_hamster', '震惊_张嘴尖叫_仓鼠_低清网图_无文字_1.png', True),
             ('reaction_thumbsup', '赞同_点赞_卡通猫_手绘_无文字_1.jpg', True)]
    output = Path('data/live_checks') / ('sticker-classification-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    rows=[]
    for repeat in range(1,repeats+1):
        for name, filename, expected in cases:
            with Image.open(root/filename) as source:
                frame=source.convert('RGB')
                frame.thumbnail((768,768))
                data=io.BytesIO()
                frame.save(data,'PNG')
            block={'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(data.getvalue()).decode('ascii')}}
            description=vision._describe_media(block,vision._TYPE_GUIDE,max_tokens=650)
            actual=stickers.parse_tags(description or '') is not None
            row=dict(case=name,repeat=repeat,expected_collect=expected,actual_collect=actual,
                     passed=bool(description) and expected==actual,description=description)
            rows.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
            (output/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
    if baseline:
        tree=ast.parse(baseline.read_text(encoding='utf8'))
        prompt=ast.literal_eval(next(n.value for n in tree.body if isinstance(n,ast.Assign)
                and any(isinstance(t,ast.Name) and t.id=='_TYPE_GUIDE' for t in n.targets)))
        path=root/cases[0][1]
        block={'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode('ascii')}}
        old=vision._describe_media(block,prompt,max_tokens=650)
        print('BASELINE_NUMBER',old,flush=True)
        (output/'baseline.txt').write_text(old or '',encoding='utf8')
    print('REPORT',output,sum(r['passed'] for r in rows),'/',len(rows))
    return all(r['passed'] for r in rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--baseline',type=Path)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.root,args.repeats,args.baseline) else 1)
