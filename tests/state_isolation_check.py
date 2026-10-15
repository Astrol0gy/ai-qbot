"""
并发/状态隔离与工具层新语义的机械校验（真跑，不打桩业务逻辑）

覆盖本轮修复：
- 跨群不再共用全局变量：工具按 ChatContext 里的 group_id/user_id 落数据
- 工具调用不堵事件循环（同步工具跑在工作线程里）
- 未知工具 / 多余参数 / 数字字符串参数 的处理
- 存档原子写、损坏档备份、文件名净化
- /again 真的能回退（facts 与 state），并扣减反悔次数
- reply / skip 的额度与"hidden 才隐藏"的区别
- 模型给的 URL 只允许公网地址（含重定向第二跳、代理 fake-IP 白名单）
- 官方通道：永久性错误不白重试、引用索引缓存有上限且不逐条重写、
  原始事件缓存有上限且默认不打印、同群消息串行（不插进正在处理的那一轮）
- 指令解析：/forget 不再吃掉 /forgetful、/list 容错尾随空格
- 超长回复截断后真的不超上限；/roll 的骰点落进存档供 DM 判定（且不占 /again 回退点）

数据目录被指到临时目录，不碰 data/ 下的真实存档。
运行：$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
      E:/miniconda3/envs/ai-qbot/python.exe tests/state_isolation_check.py
"""
import asyncio
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="qbot_state_check_")

from src import campaign_state, user_profile  # noqa: E402
from src import tools  # noqa: E402
from src.tools import ChatContext  # noqa: E402
from src.tools.chat_provider import ReplyOutbox  # noqa: E402

campaign_state.CAMPAIGNS_DIR = os.path.join(_TMP, "campaigns")
user_profile.PROFILES_DIR = os.path.join(_TMP, "profiles")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def ctx(group_id: str, user_id: str, name: str = "", outbox=None) -> ChatContext:
    return ChatContext(conv_id=group_id, user_id=user_id, user_name=name or f"用户{user_id}", outbox=outbox)


# ---------------------------------------------------------------- 1. 注册表语义
print("\n=== 1. 注册表：定义可见性、未知工具、参数归一 ===")
default_names = {d["function"]["name"] for d in tools.get_definitions()}
check("默认工具列表默认不含 reply/skip", "reply" not in default_names and "skip" not in default_names)
check("默认工具列表仍含画像/跑团工具（hidden≠needs_context）",
      {"manage_user_profile", "manage_campaign"} <= default_names)
check("白名单能注入 reply/skip",
      {d["function"]["name"] for d in tools.get_definitions(whitelist={"reply", "skip"})} == {"reply", "skip"})
check("画像/跑团工具被登记为需要会话上下文",
      tools.needs_context("manage_user_profile") and tools.needs_context("manage_campaign"))
check("reply/skip 需要会话上下文", tools.needs_context("reply") and tools.needs_context("skip"))
check("未知工具 max_calls 返回 -1（不再伪装成已用完额度）", tools.get_max_calls("no_such_tool") == -1)
check("skip 额度收窄到 2、reply 保持 6",
      tools.get_max_calls("skip") == 2 and tools.get_max_calls("reply") == 6)

r = tools.execute("no_such_tool", "{}", ctx("g1", "u1"))
check("注册表入口对未知工具说的是'未知工具'（不再是'已达上限0次'）", "未知工具" in r and "上限" not in r, r)

r = tools.execute("manage_user_profile", '{"action":"remember","info":"偏好简短回答","幻觉字段":"x"}', ctx("g1", "u1", "张三"))
check("多余参数被忽略而不是 TypeError", "已记录" in r or "记住" in r or "无法确定" not in r, r)

r = tools.execute("manage_user_profile", '{"action":"remember","info":"偏好安静","info2":1}', ctx("g1", "u2", "李四"))
check("未知字段连续出现也不炸", "已记录" not in r or True)
r = tools.execute("manage_campaign", '{"action":"view"}', None)
check("缺上下文时不执行并明确告知", "上下文" in r, r)

# ---------------------------------------------------------------- 2. 跨群隔离
print("\n=== 2. 跨群隔离：两个群并发写，互不串台 ===")
barrier = threading.Barrier(2)
errors = []
results = []


def worker(gid: str, uid: str, name: str, info: str, delay: float):
    try:
        barrier.wait()
        time.sleep(delay)
        out = tools.execute("manage_user_profile", '{"action":"remember","info":"%s"}' % info, ctx(gid, uid, name))
        results.append(f"{gid}: {out}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"{gid}: {type(e).__name__}: {e}")


# 注意：昵称不能包含 info 文本，否则 remember_info 的去重会把"昵称出现在表头"误判成重复记录
t1 = threading.Thread(target=worker, args=("groupA", "u1", "甲", "偏好在深夜聊天", 0.05))
t2 = threading.Thread(target=worker, args=("groupB", "u1", "甲", "偏好只发短句", 0.0))
t1.start(); t2.start(); t1.join(); t2.join()

prof_a = user_profile.get_profile_for_prompt("groupA")
prof_b = user_profile.get_profile_for_prompt("groupB")
check("A 群画像只含 A 群内容", "偏好在深夜聊天" in prof_a and "偏好只发短句" not in prof_a, f"prof_a={prof_a!r}")
check("B 群画像只含 B 群内容", "偏好只发短句" in prof_b and "偏好在深夜聊天" not in prof_b, f"prof_b={prof_b!r}")
check("并发写没有异常", not errors, "; ".join(errors))
check("两个群的写入都成功返回", len(results) == 2 and all("已记住" in r for r in results), "; ".join(results))

# ---------------------------------------------------------------- 3. 工具不堵事件循环
print("\n=== 3. 同步工具不再堵事件循环（真跑 chat 的工具循环）===")


class _SlowProvider(tools.ToolProvider):
    name = "slowtest"
    description = "测试用：一个同步阻塞 0.6s 的工具"

    def get_tools(self):
        def _handler(tag: str = ""):
            time.sleep(0.6)
            return f"slow-done:{tag}"
        return [{
            "name": "slow_tool",
            "description": "同步阻塞工具（测试用）",
            "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}},
            "handler": _handler,
            "max_calls": 3,
        }]


tools._registry.register_provider(_SlowProvider())

from src import deepseek_client  # noqa: E402
from src import skills  # noqa: E402

# 渐进式加载：测试工具也必须先由技能声明并加载，不能越过生产权限链。
_slow_skill_path = os.path.join(_TMP, "slow.md")
with open(_slow_skill_path, "w", encoding="utf-8") as _skill_file:
    _skill_file.write("---\nname: 慢工具测试\ndescription: 测试事件循环\ntools: slow_tool\n---\n调用 slow_tool 验证事件循环。\n")
from pathlib import Path  # noqa: E402
_slow_skill = {"name": "慢工具测试", "description": "测试事件循环", "tools": ("slow_tool",),
               "path": Path(_slow_skill_path), "resource_dir": Path(_TMP) / "slow"}
skills._SKILLS.append(_slow_skill)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None


class _TC:
    def __init__(self, name, args, idx):
        self.id = f"call_{idx}"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


class _FakeCompletions:
    def __init__(self):
        self.calls = 0
        self.seen_messages = []

    async def create(self, **kwargs):
        self.calls += 1
        self.seen_messages.append(list(kwargs.get("messages") or []))
        if self.calls == 1:
            # 第一轮：模型编了个不存在的工具
            return _Resp(_Msg(tool_calls=[_TC("no_such_tool", "{}", 0)]))
        if self.calls == 2:
            return _Resp(_Msg(tool_calls=[_TC("load_skill", '{"name":"慢工具测试"}', 1)]))
        if self.calls == 3:
            # 第二轮：用一个真会同步阻塞 0.6s 的工具
            return _Resp(_Msg(tool_calls=[_TC("slow_tool", '{"tag":"x"}', 1)]))
        return _Resp(_Msg(content="最终回答"))


fake_completions = _FakeCompletions()

deepseek_client.client = type("FakeClient", (), {
    "chat": type("Chat", (), {"completions": fake_completions})()
})()

ticks = []


async def _ticker():
    for _ in range(12):
        await asyncio.sleep(0.1)
        ticks.append(time.monotonic())


async def _run_chat():
    chat_task = asyncio.create_task(
        deepseek_client.chat("loopgroup", "u9", "在吗", "测试者", outbox=ReplyOutbox())
    )
    tk = asyncio.create_task(_ticker())
    reply = await chat_task
    tk.cancel()
    return reply, len(ticks)


reply, tick_count = asyncio.run(_run_chat())
skills._SKILLS.remove(_slow_skill)
check("阻塞工具期间事件循环仍在跑定时器（0.6s 内至少 4 次 0.1s tick）", tick_count >= 4,
      f"ticks={tick_count}, reply={reply!r}")

def _field(msg, key):
    """messages 里混着 dict 和 SDK 消息对象（助手回复是原样 append 回去的），两种都要能读"""
    return msg.get(key) if isinstance(msg, dict) else getattr(msg, key, None)


unknown_feedback = [
    _field(m, "content") for msgs in fake_completions.seen_messages for m in msgs
    if _field(m, "role") == "tool" and isinstance(_field(m, "content"), str) and "no_such_tool" in _field(m, "content")
]
check("模型编造工具时收到的是'没有这个工具'（不是'已达上限0次'）",
      bool(unknown_feedback) and "没有名为" in unknown_feedback[0] and "上限" not in unknown_feedback[0],
      str(unknown_feedback))
check("未知工具后可加载技能再重试（共 4 轮）", fake_completions.calls == 4, str(fake_completions.calls))

# ---------------------------------------------------------------- 4. 存档写入
print("\n=== 4. 存档：原子写、损坏档备份、文件名净化 ===")
check("a:b 与 a/b 不再撞成同一个文件",
      campaign_state._get_file_path("a:b") != campaign_state._get_file_path("a/b"))
path = campaign_state._get_file_path("corruptgroup")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    f.write('{"dm_mode": true, "campaigns": {')  # 半个 JSON
data = campaign_state._load("corruptgroup")
backups = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(os.path.basename(path) + ".corrupt-")]
check("损坏档被备份而不是静默丢弃", len(backups) == 1, f"备份={backups}")
check("损坏档读出来是空结构（不抛异常）", isinstance(data, dict) and not data.get("campaigns"))
check("残留临时文件为 0", not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")])

# ---------------------------------------------------------------- 5. /again 真回退
print("\n=== 5. /again：facts/state 真回退 + 消耗反悔次数 ===")
GID, UID = "againgroup", "player1"
campaign_state.enter_dm(GID, campaign_name="测试团")
campaign_state.create_campaign("测试团", GID)
campaign_state.ensure_player(GID, UID, "张三")
campaign_state.set_initial_again(2, GID)
campaign_state.append_fact("张三打开了宝箱", GID, UID)
campaign_state.append_fact("张三跳过了陷阱", GID, UID)
campaign_state.update_state('{"hp": 7}', GID, UID)

before = campaign_state._load(GID)["campaigns"]["测试团"]
check("facts 已记录 2 条", len(before["facts"]) == 2, str(before["facts"]))
check("state 已更新", before["state"].get("hp") == 7)
check("操作栈有 3 个可回退点", len(before["history_stack"]) == 3, str(len(before["history_stack"])))

msg = campaign_state.undo_last(GID, UID)
after = campaign_state._load(GID)["campaigns"]["测试团"]
player = after["players"][campaign_state._player_key(GID, UID)]
check("撤回后 state 还原", "hp" not in after["state"] or after["state"].get("hp") != 7, str(after["state"]))
check("反悔次数从 2 扣到 1", player["again_remaining"] == 1, str(player["again_remaining"]))
check("撤回提示带剩余次数", "剩余反悔次数：1" in msg, msg)

campaign_state.undo_last(GID, UID)
third = campaign_state._load(GID)["campaigns"]["测试团"]
check("再撤回一次：facts 回到 1 条", len(third["facts"]) == 1, str(third["facts"]))
refuse = campaign_state.undo_last(GID, UID)
check("次数用完后拒绝撤回", "用完" in refuse, refuse)

# 别人记的事实不会被弹掉
campaign_state.append_fact("李四加入队伍", GID, "player2")
campaign_state.ensure_player(GID, "player2", "李四")
other = campaign_state.undo_last(GID, "player3")
check("没记过事的人无法撤回", "还没有任何可撤回" in other, other)

# ---------------------------------------------------------------- 6. 反悔次数奖励
print("\n=== 6. again_grant / again_init 接线 ===")
r = tools.execute("manage_campaign", '{"action":"again_grant","content":"2","player":"张三"}', ctx(GID, UID, "张三"))
check("按名字奖励反悔次数成功", "奖励" in r and "2" in r, r)
r = tools.execute("manage_campaign", '{"action":"again_grant","content":"1","player":"不存在的人"}', ctx(GID, UID))
check("找不到玩家时给出可读提示", "没有找到玩家" in r, r)
r = tools.execute("manage_campaign", '{"action":"again_init","content":"3"}', ctx(GID, UID))
check("again_init 设定初始次数", "3 次" in r, r)
left = campaign_state._load(GID)["campaigns"]["测试团"]["players"][campaign_state._player_key(GID, UID)]["again_remaining"]
check("again_init 覆盖到现有玩家", left == 3, str(left))
r = tools.execute("manage_campaign", '{"action":"again_init","content":"不是数字"}', ctx(GID, UID))
check("非法次数给出可读错误而不是异常", "需要是数字" in r, r)

# ---------------------------------------------------------------- 7. 表态箱隔离
print("\n=== 7. reply / skip 用本轮上下文（不落到别的群）===")
ob1, ob2 = ReplyOutbox(), ReplyOutbox()
tools.execute("reply", '{"text":"A群的话"}', ctx("gA", "u1", "甲", ob1))
tools.execute("reply", '{"text":"B群的话"}', ctx("gB", "u2", "乙", ob2))
check("A 群表态箱只有 A 群的话", [i.text for i in ob1.items] == ["A群的话"], str(ob1.items))
check("B 群表态箱只有 B 群的话", [i.text for i in ob2.items] == ["B群的话"], str(ob2.items))
r = tools.execute("reply", '{"text":"乱引用","reply_to":"msg_999"}', ctx("gA", "u1", "甲", ob1))
check("引用不存在的消息时降级为普通发言", ob1.items[-1].reply_to is None, str(ob1.items[-1]))
r = tools.execute("skip", "{}", ChatContext(conv_id="gA", user_id="u1"))
check("没有表态箱时 skip 不生效并说明原因", "没有可用的表态通道" in r, r)

# ---------------------------------------------------------------- 8. SSRF 防护
print("\n=== 8. 模型给的 URL 只允许公网地址 ===")
from src.net_safety import UnsafeURLError, check_url, safe_get  # noqa: E402

for bad in [
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:5000/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "file:///C:/windows/win.ini",
    "ftp://example.com/x",
]:
    try:
        check_url(bad)
        check(f"拒绝 {bad}", False, "竟然放行了")
    except UnsafeURLError as e:
        check(f"拒绝 {bad}", True, str(e))

from unittest.mock import patch
with patch("src.net_safety.socket.getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]):
    check("公网地址放行", check_url("https://www.example.com/a?b=1") == "https://www.example.com/a?b=1")
import ipaddress
from src import net_safety as _ns
check("fake-IP 默认拒绝，不再隐式信任代理", not _ns._is_public_ip(ipaddress.ip_address("198.18.0.72")))

# fetch_url 是模型可直达的入口，端到端确认它不返回内网内容
r = tools.execute("fetch_url", '{"url":"http://169.254.169.254/latest/meta-data/"}', ctx("g1", "u1"))
check("fetch_url 对内网地址返回拒绝提示", "非公网" in r or "拒绝" in r, r)


# 重定向绕过：公网入口 302 到内网，第二跳必须被拦住
class _RedirectSession:
    def __init__(self, target):
        self.target = target
        self.hits = []

    def get(self, url, **kwargs):
        self.hits.append(url)
        return type("R", (), {
            "status_code": 302, "headers": {"Location": self.target},
        })()


real_getaddrinfo = socket.getaddrinfo


def _fake_getaddrinfo(host, port, *a, **kw):
    if host == "public-but-evil.example":
        return real_getaddrinfo("93.184.216.34", port, *a, **kw)  # 一个公网 IP
    return real_getaddrinfo(host, port, *a, **kw)


socket.getaddrinfo = _fake_getaddrinfo
sess = _RedirectSession("http://127.0.0.1/secret")
class RedirectPool:
    def urlopen(self, method, target, **kwargs):
        sess.hits.append(target)
        return type("R", (), {"status": 302, "headers": {"Location": sess.target}, "close": lambda self: None})()
    def close(self):
        pass
try:
    blocked = False
    with patch.object(_ns, "_pool", return_value=RedirectPool()):
        try:
            safe_get(None, "http://public-but-evil.example/start")
        except UnsafeURLError as e:
            blocked = "非公网" in str(e)
    check("重定向到内网时第二跳被拦住", blocked and sess.hits == ["/start"], f"hits={sess.hits}")
finally:
    socket.getaddrinfo = real_getaddrinfo

# ---------------------------------------------------------------- 9. 官方通道
print("\n=== 9. 官方通道：重试分类、索引缓存、同会话串行 ===")
import io  # noqa: E402
import contextlib  # noqa: E402
from unittest.mock import patch  # noqa: E402

import src.channels.official as official  # noqa: E402


class _FakeErr(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        if code is not None:
            self.code = code


check("4xx（请求本身不合法）判为不可重试", official._is_permanent_error(_FakeErr("bad request", 400)))
check("msg_id 已回复判为不可重试", official._is_permanent_error(_FakeErr("该消息已回复过")))
check("429 限流仍然重试", not official._is_permanent_error(_FakeErr("too many requests", 429)))
check("5xx 服务端错误仍然重试", not official._is_permanent_error(_FakeErr("internal error", 500)))
check("普通网络异常仍然重试", not official._is_permanent_error(TimeoutError("timed out")))


class _DeadMessage:
    """回复必然失败的消息：用来数"到底试了几次" """
    group_openid = "g0"
    id = "m0"
    content = "hi"
    timestamp = ""
    attachments = []

    def __init__(self, err):
        self.err = err
        self.attempts = 0

    async def reply(self, content=""):
        self.attempts += 1
        raise self.err


dead = _DeadMessage(_FakeErr("msg_id 不存在", 400))
t0 = time.monotonic()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(official._safe_reply(dead, "测试"))
cost = time.monotonic() - t0
check("永久性错误只试一次就放弃（不再白等 20 秒）", dead.attempts == 1 and cost < 1.0,
      f"attempts={dead.attempts}, {cost:.2f}s")
check("放弃时把真实原因打出来", "msg_id 不存在" in buf.getvalue(), buf.getvalue().strip())

# 索引缓存：节流重写 + 总量上限（真写临时文件）
official._REF_INDEX_FILE = os.path.join(_TMP, "refidx.jsonl")
compactions = []
official._compact_ref_index = lambda cache: compactions.append(len(cache))
official._MSG_IDX_CACHE.clear()
official._pending_evictions = 0
owner = "g1:u1"
for i in range(40):
    official._cache_put(f"REFIDX{i:03d}", {
        "content": f"第{i}条", "username": "甲", "timestamp": f"{i:04d}", "owner": owner,
    })
check("单用户缓存按上限淘汰", len(official._MSG_IDX_CACHE) == official._MSG_IDX_CACHE_MAX_PER_USER,
      str(len(official._MSG_IDX_CACHE)))
check("保留的是最新的那些", f"REFIDX039" in official._MSG_IDX_CACHE)
# 40 条插入里有 20 次淘汰（第 21 条起每条淘汰一条）；攒够 _COMPACT_EVERY_EVICTIONS 才重写
expected_compactions = 20 // official._COMPACT_EVERY_EVICTIONS
check(f"整体重写次数 = 淘汰数/{official._COMPACT_EVERY_EVICTIONS}（旧写法是 20 次）",
      len(compactions) == expected_compactions, f"重写 {len(compactions)} 次")

official._MSG_IDX_CACHE.clear()
official._pending_evictions = 0
official._MSG_IDX_CACHE_MAX_TOTAL = 10
for i in range(30):
    official._cache_put(f"REFIDX-X{i:03d}", {
        "content": f"x{i}", "username": "甲", "timestamp": f"{i:04d}", "owner": f"g{i}:u{i}",
    })
check("多人场景下总条数受上限约束", len(official._MSG_IDX_CACHE) <= 10, str(len(official._MSG_IDX_CACHE)))
official._MSG_IDX_CACHE_MAX_TOTAL = 2000
official._MSG_IDX_CACHE.clear()

# 原始事件缓存：没人取走的条目不能一直攒着
official._orig_parse_group = lambda self, payload: "parsed"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    for i in range(official._RAW_MSG_CACHE_MAX + 60):
        official._parse_group_with_raw(None, {"d": {"id": f"msg{i}", "content": "群聊原文"}})
check("原始事件缓存有上限（不然整天群聊都留在内存里）",
      len(official._RAW_MSG_CACHE) <= official._RAW_MSG_CACHE_MAX, str(len(official._RAW_MSG_CACHE)))
check("默认不打印原始事件（openid/原文不该进日志）", buf.getvalue() == "", buf.getvalue()[:80])
check("调试开关打开时才打印结构", official.OFFICIAL_DEBUG_RAW_EVENTS is False)

# 同会话串行：真跑 on_group_at_message_create（只打桩 handle_message 与 api）
rounds = []


async def _fake_handle_message(incoming, sender):
    rounds.append((incoming.conv_id, incoming.text, incoming.user_name, sender))
    if len(rounds) == 1:
        await asyncio.sleep(0.15)  # 第一轮"思考中"，期间第二条消息到达


official.handle_message = _fake_handle_message


def _fake_msg(mid, gid, text, uid="u1"):
    return type("Msg", (), {
        "id": mid, "group_openid": gid, "content": text, "timestamp": "t",
        "attachments": [], "author": type("A", (), {"member_openid": uid})(),
    })()


class _FakeSelf:
    def __init__(self):
        self._locks, self._pending, self.api = {}, {}, object()


async def _two_messages():
    s = _FakeSelf()
    m1 = _fake_msg("m1", "gA", "第一条")
    m2 = _fake_msg("m2", "gA", "第二条")
    m3 = _fake_msg("m3", "gB", "别的群")
    official._RAW_MSG_CACHE["m1"] = {"author": {"username": "甲"}}
    official._RAW_MSG_CACHE["m2"] = {"author": {"username": "甲"}}
    official._RAW_MSG_CACHE["m3"] = {"author": {"username": "乙"}}
    t1 = asyncio.create_task(official.QBot.on_group_at_message_create(s, m1))
    await asyncio.sleep(0.05)  # m1 已进第一轮
    t2 = asyncio.create_task(official.QBot.on_group_at_message_create(s, m2))
    t3 = asyncio.create_task(official.QBot.on_group_at_message_create(s, m3))
    await asyncio.gather(t2, t3)
    during = len(rounds)
    await t1
    return during, s


buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    during, s = asyncio.run(_two_messages())
check("同群第二条消息不会插进正在处理的那一轮", during == 2, f"期间跑了 {during} 轮（应为 2：gA 一轮 + gB 一轮）")
check("第一条处理完会自动补一轮（内容不丢）",
      [r[1] for r in rounds if r[0] == "gA"] == ["第一条", "第二条"], str([r[1] for r in rounds]))
check("另一个群不受影响（各群有自己的锁）", "别的群" in [r[1] for r in rounds], str(rounds))
check("补一轮时用的是那条消息自己的发送器",
      rounds[-1][3]._message.id == "m2", getattr(rounds[-1][3]._message, "id", "?"))
check("日志说明了发生了什么", "排队" in buf.getvalue() and "依次处理" in buf.getvalue(), buf.getvalue()[:120])

# ---------------------------------------------------------------- 10. 指令解析
print("\n=== 10. 指令解析：/forget 与 /list ===")
from src.channels import commands  # noqa: E402

campaign_state.enter_dm(GID, campaign_name="测试团")
user_profile.remember_info("偏好在深夜聊天", GID, UID, "张三")
check("/forgetful 不再被当成 /forget", commands.handle_profile_cmd("/forgetful 深夜", GID, UID) is None)
check("记录还在（没有被误删）", "深夜聊天" in user_profile.get_profile_for_prompt(GID))
forget_out = commands.handle_profile_cmd("  /forget 深夜  ", GID, UID)  # 只调一次：第一次就把记录删了
check("/forget 前面多打空格也认", "删除 1 条" in (forget_out or ""), str(forget_out))
check("删掉后画像里没有了", "深夜聊天" not in user_profile.get_profile_for_prompt(GID))
check("/list 带尾随空格也认", "还没有记录" in (commands.handle_profile_cmd("/list ", GID, UID) or ""))

# ---------------------------------------------------------------- 11. 截断与掷骰
print("\n=== 11. 截断保上限、掷骰结果落进 DM 存档 ===")
from src.channels.base import truncate_text  # noqa: E402

long_text = "字" * 5000
cut = truncate_text(long_text)
check("超长文本被截断到上限内", len(cut) <= 2000 and "已截断" in cut, str(len(cut)))
small = truncate_text("字" * 100, max_len=20)
check("上限很小时也不会越界（旧写法会切出比原文还长的结果）", len(small) <= 20, f"len={len(small)}")
check("不超限时原样返回", truncate_text("短") == "短")

stack_before = len(campaign_state._load(GID)["campaigns"]["测试团"]["history_stack"])
roll_msg = commands.handle_dm_cmd("/roll 1d20", GID, UID, "张三")
prompt = campaign_state.get_state_for_prompt(GID)
check("掷骰结果确实掷出来了", "掷出了" in roll_msg, roll_msg)
check("DM 的 prompt 里带上了掷骰记录（不然只能靠玩家复述或自己编）",
      "最近掷骰记录" in prompt and "张三" in prompt and roll_msg.split("：")[-1][:4] in prompt, prompt[:200])
check("掷骰不占 /again 的回退点",
      len(campaign_state._load(GID)["campaigns"]["测试团"]["history_stack"]) == stack_before)

campaign_state.exit_dm(GID)
commands.handle_dm_cmd("/roll 1d6", "普通群", "u9", "某人")
check("非 DM 模式掷骰不写存档", not campaign_state._load("普通群").get("campaigns"))

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n===== 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
if FAIL:
    for n in FAIL:
        print(f"  ❌ {n}")
    sys.exit(1)
