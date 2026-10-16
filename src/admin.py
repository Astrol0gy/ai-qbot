"""Private OneBot management, outside chat history, model tools and message queues."""
import ast
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from dotenv import dotenv_values, set_key
from . import config, stickers_utils
from .observability import log_event, redact

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / '.env'
DATA_DIR = ROOT / 'data' / 'admin'
_lock = asyncio.Lock()
_sensitive = re.compile(r'(?:KEY|TOKEN|SECRET|PASSWORD)', re.I)
_aliases = {'管理员账号': 'ADMIN_QQ', '管理员QQ': 'ADMIN_QQ', '温度': 'DEEPSEEK_TEMPERATURE',
            '模型': 'DEEPSEEK_MODEL', '主模型': 'DEEPSEEK_MODEL', '生图模型': 'IMAGE_GEN_MODEL',
            '长考提示时间': 'DEEPSEEK_THINKING_NOTICE_SEC', '工具提示时间': 'TOOL_CALL_NOTICE_SEC',
            '思考模式': 'DEEPSEEK_ENABLE_THINKING', '联网搜索': 'DEEPSEEK_ENABLE_SEARCH',
            '表情包开关': 'STICKER_ENABLE', '代理地址': 'TRUST_PROXY_URL'}

HELP = """管理员控制台（仅当前配置的管理员私聊）
/help — 普通帮助与管理员命令
/admin — 仅管理员帮助
/config list [前缀] — 查看配置，敏感值脱敏
/config get 配置名 — 查看文件值与当前运行值
/config set 配置名 值 — 校验并保存，reload 后生效
/sticker list [关键词] — 列出表情包文件名
/sticker rename 旧文件名 新文件名 — 改名并同步索引
/sticker delete 文件名 — 删除并移入回收目录
/bot log [行数] — 最近日志，默认 30 行，最多 100 行
/bot reload — 重载 Python，保留 QQ 登录
/bot stop — 执行 start.sh stop，停止机器人和 NapCat
不提供私聊启动；stop 后需 SSH 启动。
ADMIN_QQ 只能在部署端修改，私聊无法更改。
自然语言示例：查看生图模型；把工具提示时间改为45；看最近20行日志；重载机器人；把表情包 old.gif 改名为 new.gif。"""


@dataclass(frozen=True)
class Request:
    action: str
    args: tuple[str, ...] = ()


def parse(text: str) -> Request | None:
    """Recognize explicit, bounded commands. No model sees management text or keys."""
    text = text.strip()
    if re.match(r'^/(?:admin|config|sticker|bot)(?:\s|$)', text):
        try:
            parts = shlex.split(text, posix=True)
        except ValueError:
            return Request('help')
        if parts[0] == '/admin':
            return Request('help')
        if len(parts) < 2:
            return Request('help')
        prefix = {'/config': 'config', '/sticker': 'sticker', '/bot': 'bot'}[parts[0]]
        action = prefix + '.' + parts[1].lower()
        if action == 'config.set' and len(parts) >= 4:
            return Request(action, (parts[2], ' '.join(parts[3:])))
        return Request(action, tuple(parts[2:]))
    text = re.sub(r'^(?:(?:请你?|麻烦你?|帮我|能不能|可以|我想|我希望你?|给我)\s*)+', '', text).strip()
    text = re.sub(r'^(?:把|将)(?:env|\.env)(?:里面|里|中)(?:的)?\s*', '把', text, flags=re.I)
    assignment = re.fullmatch(r'([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)', text)
    if assignment:
        return Request('config.set', (assignment[1], assignment[2]))
    setting = re.fullmatch(r'(?:把|将|设置|修改)?\s*(?:配置(?:项)?\s*)?([A-Za-z_][A-Za-z_0-9]*|'+ '|'.join(map(re.escape, _aliases)) +r')\s*(?:设置为|修改为|改为|改成|设为|调成|调到|调整为|换成|=|为)\s*(.+)', text)
    if setting:
        value = setting[2].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        return Request('config.set', (setting[1], value))
    text = text.rstrip('。！!？?').strip()
    if text in ('管理帮助', '管理员帮助', '控制台帮助'):
        return Request('help')
    m = re.fullmatch(r'(?:请)?(?:帮我)?(?:重载|重新加载|reload)(?:一下)?(?:机器人|bot|程序)?', text, re.I)
    if m:
        return Request('bot.reload')
    if re.fullmatch(r'(?:请)?(?:帮我)?(?:停止|关闭|stop)(?:一下)?(?:机器人|bot|程序)', text, re.I):
        return Request('bot.stop')
    if re.fullmatch(r'(?:请)?(?:启动|开启|start)(?:机器人|bot|程序)', text, re.I):
        return Request('bot.start')
    m = re.fullmatch(r'(?:请)?(?:查看|看看|看|显示)(?:一下)?(?:机器人)?(?:最近)?\s*(\d+)?\s*(?:行)?日志', text)
    if m:
        return Request('bot.log', (m[1],) if m[1] else ())
    if re.fullmatch(r'(?:请)?(?:查看|看看|显示|列出)(?:一下)?(?:env|\.env|环境变量|配置)(?:中|里)?(?:的)?(?:全部|所有)?(?:配置|配置项)?', text, re.I):
        return Request('config.list')
    m = re.fullmatch(r'(?:请)?(?:查看|看看|显示)\s*(?:配置(?:项)?\s*)?([A-Za-z_][A-Za-z_0-9]*|'+ '|'.join(map(re.escape, _aliases)) +r')', text)
    if m:
        return Request('config.get', (m[1],))
    if text in ('管理员是谁', '管理员账号是多少'):
        return Request('config.get', ('ADMIN_QQ',))
    m = re.fullmatch(r'(?:请)?(?:查看|看看|列出|显示)(?:一下)?表情包(?:列表|库)?(?:\s+(.+))?', text)
    if m:
        return Request('sticker.list', (m[1],) if m[1] else ())
    m = re.fullmatch(r'(?:请)?(?:删除|删掉|移除)表情包\s*(.+)', text)
    if m:
        return Request('sticker.delete', (m[1].strip().strip('"\''),))
    m = re.fullmatch(r'(?:请)?(?:把|将)表情包\s*(.+?)\s*(?:改名为|重命名为|改名成)\s*(.+)', text)
    if m:
        return Request('sticker.rename', tuple(x.strip().strip('"\'') for x in m.groups()))
    # Do not send unrecognized admin commands (possibly containing credentials) to a model.
    if re.match(r'^(?:请)?(?:修改|设置|把|将).*(?:配置|env|API.?KEY|TOKEN|SECRET|ADMIN_QQ)', text, re.I):
        return Request('help')
    return None


def authorized(msg) -> bool:
    return bool(config.ADMIN_QQ and msg.channel == 'onebot' and not msg.is_group
                and msg.raw.get('message_type') == 'private'
                and str(msg.raw.get('user_id')) == config.ADMIN_QQ
                and msg.user_id == 'onebot_' + config.ADMIN_QQ)


def config_keys():
    tree = ast.parse((ROOT / 'src' / 'config.py').read_text(encoding='utf-8'))
    keys = {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'getenv' and node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)}
    keys.update(dotenv_values(ROOT / '.env.example', interpolate=False))
    return keys


def fresh_environment():
    # Inherited dotenv values would override the new file in a reloaded process.
    return {k: v for k, v in os.environ.items() if k not in config_keys()}


def key_name(raw):
    name = _aliases.get(raw, raw.upper())
    if name not in config_keys():
        raise ValueError('未知配置项，请使用 /config list 查看可用项')
    return name


def private_dir(path):
    if path != DATA_DIR:
        private_dir(DATA_DIR)
    if path.is_symlink():
        raise ValueError('管理目录不可用')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def job_status(job_id, status):
    path = DATA_DIR / 'jobs' / (job_id + '.json')
    record = json.loads(path.read_text(encoding='utf-8'))
    record['status'] = status
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record), encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


def scrub(text):
    for source in (dotenv_values(ENV_FILE, interpolate=False), os.environ):
        for key, value in source.items():
            if _sensitive.search(key) and value:
                text = text.replace(str(value), '<redacted>')
    return redact(text)


def display_value(key, value):
    if value is None:
        return '未在文件中设置'
    if _sensitive.search(key):
        return '***（已配置）' if value else '空（未配置）'
    return scrub(str(value))


def show_config(raw=None, prefix=''):
    values = dotenv_values(ENV_FILE, interpolate=False)
    if raw:
        key = key_name(raw)
        effective = getattr(config, key, '按运行时配置读取')
        if isinstance(effective, set):
            effective = ','.join(sorted(effective))
        return (f'{key}\n文件值：{display_value(key, values.get(key))}\n'
                f'当前运行值：{display_value(key, effective)}'
                + ('\n此项只能在部署端修改。' if key in config.PRIVATE_READONLY_CONFIG_KEYS else ''))
    keys = sorted(k for k in config_keys() if k.startswith(prefix.upper()))
    return '文件配置（修改后 reload 生效；敏感值脱敏）：\n' + '\n'.join(
        f'{k}={display_value(k, values.get(k))}' for k in keys)


def update_config(raw, value):
    key = key_name(raw)
    if key in config.PRIVATE_READONLY_CONFIG_KEYS:
        raise ValueError(f'{key} 只能在部署端修改，禁止通过私聊修改')
    if isinstance(getattr(config, key, None), (bool, int, float)):
        value = value.rstrip('。！!？?').strip()
    if '\n' in value or '\r' in value or '\x00' in value or len(value) > 8192:
        raise ValueError('配置值必须是单行文本，最长 8192 字符')
    if value in ('开启', '打开', '启用'):
        value = 'true'
    elif value in ('关闭', '禁用'):
        value = 'false'
    before = ENV_FILE.read_bytes()
    fd, name = tempfile.mkstemp(prefix='.admin-env-', dir=ENV_FILE.parent)
    candidate = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(before)
        set_key(candidate, key, value, quote_mode='always')
        # Validate in a fresh interpreter, without changing this process or live .env.
        code = ('import sys; from dotenv import load_dotenv; '
                'load_dotenv(sys.argv[1], override=True); '
                'from src.config import validate; validate()')
        result = subprocess.run([sys.executable, '-c', code, str(candidate)], cwd=ROOT,
                                env=fresh_environment(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            raise ValueError('配置校验失败，未保存；请检查取值、范围和通道必填项')
        if ENV_FILE.read_bytes() != before:
            raise ValueError('配置文件已被其他操作修改，本次未保存，请重试')
        folder = private_dir(DATA_DIR / 'env-backups')
        backup = folder / (uuid.uuid4().hex + '.env')
        with backup.open('xb') as stream:
            stream.write(before)
        backup.chmod(0o600)
        candidate.chmod(0o600)
        os.replace(candidate, ENV_FILE)
    finally:
        candidate.unlink(missing_ok=True)
    return f'已保存 {key}={display_value(key, value)}。执行 /bot reload 后生效。'


def recent_logs(lines=30):
    if not 1 <= lines <= 100:
        raise ValueError('日志行数必须在 1..100 之间')
    result = []
    jobs = DATA_DIR / 'jobs'
    if jobs.exists():
        for path in sorted(jobs.glob('*.json'), key=lambda p: p.stat().st_mtime)[-3:]:
            try:
                record = json.loads(path.read_text())
                result.append(f"控制任务 {record['id']} {record['action']}：{record['status']}")
            except (ValueError, KeyError, OSError):
                continue
    path = ROOT / 'logs' / 'qbot.log'
    if path.is_file():
        with path.open(encoding='utf-8', errors='replace') as stream:
            body = scrub(''.join(deque(stream, maxlen=lines)))
        if len(body) > 11000:
            body = '日志较长，仅显示尾部 11000 字符：\n' + body[-11000:]
        result.append(body)
    else:
        result.append('暂无运行日志。')
    return scrub('\n'.join(result))


def prepare_control(action):
    if action not in ('reload', 'stop'):
        raise ValueError('仅支持 reload、log、stop，不提供私聊启动')
    if sys.platform != 'linux' or not (ROOT / 'start.sh').is_file():
        raise ValueError('程序控制仅支持服务器 Linux 部署')
    folder = private_dir(DATA_DIR / 'jobs')
    job_id = uuid.uuid4().hex[:12]
    path = folder / (job_id + '.json')
    path.write_text(json.dumps(dict(id=job_id, action=action, status='prepared')), encoding='utf-8')
    path.chmod(0o600)
    return job_id


def launch_control(action, job_id):
    # argv is fixed and never passed through a user-built shell command.
    folder = DATA_DIR / 'jobs'
    with (folder / (job_id + '.log')).open('ab') as output:
        os.chmod(output.name, 0o600)
        subprocess.Popen([sys.executable, '-m', 'src.admin_job', action, job_id], cwd=ROOT,
                         env=fresh_environment(), stdin=subprocess.DEVNULL, stdout=output,
                         stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)


def execute(request, msg):
    if not authorized(msg):
        raise PermissionError('仅配置的管理员账号可在 OneBot 私聊中管理')
    action, args = request.action, request.args
    if action == 'help' and not args:
        return HELP
    if action == 'help.all' and not args:
        from .channels.commands import HELP_TEXT
        return HELP_TEXT + '\n\n' + HELP
    if action == 'config.get' and len(args) == 1:
        return show_config(args[0])
    if action == 'config.list' and len(args) <= 1:
        return show_config(prefix=args[0] if args else '')
    if action == 'config.set' and len(args) == 2:
        return update_config(*args)
    if action == 'sticker.list' and len(args) <= 1:
        names = stickers_utils.admin_list(args[0] if args else '')
        return f'匹配 {len(names)} 张（最多显示 100 张，可加关键词筛选）：\n' + '\n'.join(names[:100])
    if action == 'sticker.delete' and len(args) == 1:
        stickers_utils.admin_edit(args[0])
        return '已删除表情包，图片移入回收目录，索引已同步。'
    if action == 'sticker.rename' and len(args) == 2:
        stickers_utils.admin_edit(*args)
        return '已重命名表情包，索引已同步；标签与使用记录保留。'
    if action == 'bot.log' and len(args) <= 1:
        if args and (not args[0].isascii() or not args[0].isdigit()):
            raise ValueError('日志行数必须在 1..100 之间')
        return recent_logs(int(args[0]) if args else 30)
    if action == 'bot.start':
        return '不提供私聊启动；停止后请通过 SSH 执行 bash start.sh。'
    raise ValueError('无法识别管理命令或参数不完整。\n' + HELP)


async def send_text(sender, text):
    from .channels.base import SendResult
    for start in range(0, len(text), 3000):
        receipt = await asyncio.wait_for(sender.send(text[start:start+3000]), timeout=20)
        if not isinstance(receipt, SendResult) or receipt.status != 'ok':
            return False
    return True


async def handle(msg, sender) -> bool:
    request = (Request('help.all') if (msg.text or '').strip() == '/help' and authorized(msg)
               else parse(msg.text or ''))
    if request is None:
        return False
    # This entry runs before ordinary message logging, quoting, enrichment or the model queue.
    if not authorized(msg):
        log_event('admin.denied', user_id=msg.user_id, action=request.action)
        if not msg.is_group:
            await send_text(sender, '无管理权限。')
        return True
    async with _lock:
        try:
            if request.action in ('bot.reload', 'bot.stop') and not request.args:
                job_id = await asyncio.to_thread(prepare_control, request.action.split('.')[1])
                suffix = ('QQ 登录保留，可稍后查看 /bot log。' if request.action == 'bot.reload'
                          else '机器人及 NapCat 将停止，后续私聊无法响应，需 SSH 启动。')
                if not await send_text(sender, f'已受理 {request.action}，任务 {job_id}。{suffix}'):
                    await asyncio.to_thread(job_status, job_id, 'ack_failed')
                    log_event('admin.control.not_started', job_id=job_id, reason='ack_failed')
                    return True
                try:
                    await asyncio.to_thread(launch_control, request.action.split('.')[1], job_id)
                except Exception:
                    await asyncio.to_thread(job_status, job_id, 'launch_failed')
                    raise
                log_event('admin.control.queued', user_id=msg.user_id, action=request.action, job_id=job_id)
                return True
            else:
                text = await asyncio.to_thread(execute, request, msg)
                await send_text(sender, text)
            log_event('admin.completed', user_id=msg.user_id, action=request.action)
        except (ValueError, PermissionError) as exc:
            # Validation messages contain neither configuration values nor raw model input.
            await send_text(sender, str(exc))
            log_event('admin.rejected', action=request.action)
        except Exception as exc:
            log_event('admin.error', action=request.action, error_type=type(exc).__name__)
            await send_text(sender, '管理操作未完成，请检查服务器日志；未确认成功的操作请先核查状态。')
    return True
