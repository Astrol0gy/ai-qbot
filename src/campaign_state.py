"""
跑团（DM）状态管理器

职责：
- 按群维度持久化跑团状态（data/campaigns/<group_id>.json）
- 维护 DM 模式开关（dm_mode，按群隔离）
- 管理多存档（进行中/已完结剧情）
- 抗幻觉：facts（剧情事实/玩家决定，只增不改）与 state（角色卡/HP/场景等可编辑）分离
- /again 反悔：操作历史栈 + 各玩家剩余撤回次数
- 生成注入 system prompt 的纯文本状态摘要

仿 user_profile.py 的文件持久化范式。
"""
import json
import os
import re
from datetime import datetime
from . import runtime

CAMPAIGNS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "campaigns")

# 注入 prompt 时的长度上限（避免超长剧情撑爆 token）
_PROMPT_MAX_CHARS = 6000

# 这里曾经有 _current_group_id / _current_user_id 两个模块级全局变量，由 deepseek_client
# 在每轮对话前用 set_current_context() 覆盖。不同群可以并发跑同一轮 chat（通道层只保证
# 同会话串行 + 一个全局并发上限），于是"A 群设全局 → await → 工具读全局"这段窗口里，
# B 群会把全局改成 B，A 群的工具调用就写进了 B 群的存档。现在所有接口都从调用链上显式
# 接收 group_id / user_id（工具层从 ChatContext 取），不再有全局状态。

# 默认每位玩家反悔次数
DEFAULT_AGAIN = 1



# ============================================
# 文件读写
# ============================================
def _safe_component(group_id: str) -> str:
    """
    把群号洗成安全文件名片段。

    用**转义**而不是"统一替换成一个下划线"：替换会让 a:b 与 a/b 撞成同一个文件，
    两个群的存档就会互相覆盖。转义保证不同输入仍是不同文件名。
    """
    # Hex is injective even on case-insensitive Windows filesystems.
    return "id-" + str(group_id).encode("utf-8").hex()


def _get_file_path(group_id: str) -> str:
    with runtime.lock_for(group_id):
        os.makedirs(CAMPAIGNS_DIR, exist_ok=True)
        path = os.path.join(CAMPAIGNS_DIR, f"{_safe_component(group_id)}.json")
        # Existing ordinary QQ IDs have an unambiguous legacy path. Preserve the
        # original as a backup; ambiguous escaped IDs require explicit migration.
        legacy_safe = re.fullmatch(r"[A-Za-z0-9_-]+", str(group_id))
        if os.name == "nt":
            legacy_safe = re.fullmatch(r"(?:onebot_(?:group|private)_)?[0-9]+", str(group_id))
        if not runtime.exists(path) and legacy_safe:
            legacy = os.path.join(CAMPAIGNS_DIR, f"{group_id}.json")
            if os.path.isfile(legacy) and not re.fullmatch(r"id-[0-9a-f]+", str(group_id)):
                old = runtime.read_text(legacy)
                try:
                    identity = json.loads(old).get("_group_id")
                except (ValueError, AttributeError):
                    identity = None
                if identity is None or identity == group_id:
                    runtime.write_text(path, old)
        return path


def _load(group_id: str) -> dict:
    """
    加载某群的状态，无文件时返回空结构。

    解析失败时**先把这个损坏档改名备份**再返回空结构：否则下一次 _save 会把损坏档
    原地覆盖成空档，玩家进度就无声丢了，而且没有回去看的余地。
    """
    path = _get_file_path(group_id)
    content = runtime.read_text(path)
    if content is None:
        return _empty_group_state()
    try:
        data = json.loads(content)
        data = _validate(data, group_id)
    except (ValueError, TypeError) as e:
        runtime.check()
        backup = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        try:
            os.replace(path, backup)
            print(f"[⚠️ 跑团状态加载失败] {e}；损坏档已备份为 {os.path.basename(backup)}")
        except OSError as move_error:
            raise OSError(f"损坏档备份失败，拒绝覆盖：{move_error}") from e
        return _empty_group_state()
    return data


def _validate(data, group_id):
    if not isinstance(data, dict):
        raise ValueError(f"顶层不是对象，而是 {type(data).__name__}")
    if data.get("_group_id", group_id) != group_id:
        raise OSError("存档身份不匹配，拒绝读取或覆盖")
    if not isinstance(data.get("campaigns"), dict):
        raise ValueError("campaigns 必须是对象")
    if not isinstance(data.get("dm_mode"), bool) or not isinstance(data.get("active_campaign"), str):
        raise ValueError("dm_mode / active_campaign 类型错误")
    for campaign in data["campaigns"].values():
        if not isinstance(campaign, dict):
            raise ValueError("存档必须是对象")
        for field, kind in (("state", dict), ("players", dict), ("facts", list), ("lore", list), ("history_stack", list), ("roll_log", list)):
            if field not in campaign:
                campaign[field] = kind()
            if not isinstance(campaign[field], kind):
                raise ValueError(f"存档字段 {field} 类型错误")
        for field in ("facts", "lore"):
            if any(not isinstance(item, str) for item in campaign[field]):
                raise ValueError(f"{field} 只能包含文本")
        for field in ("rules", "dm_style", "name"):
            if not isinstance(campaign.get(field, ""), str):
                raise ValueError(f"{field} 必须是文本")
        for player in campaign["players"].values():
            if not isinstance(player, dict) or not isinstance(player.get("name", ""), str):
                raise ValueError("玩家记录类型错误")
            count = player.get("again_remaining", DEFAULT_AGAIN)
            if type(count) is not int or not 0 <= count <= 10:
                raise ValueError("反悔次数必须是 0..10 的整数")
        if any(not isinstance(item, dict) for item in campaign["roll_log"] + campaign["history_stack"]):
            raise ValueError("掷骰或撤回记录类型错误")
        if "fact_ids" in campaign and (not isinstance(campaign["fact_ids"], list)
                or len(campaign["fact_ids"]) != len(campaign["facts"])
                or any(not isinstance(item, str) for item in campaign["fact_ids"])):
            raise ValueError("事实标识与事实列表不匹配")
    return data


def _save(group_id: str, data: dict):
    """
    保存某群状态到磁盘：先写同目录的临时文件，再原子替换。

    直接 open(path, "w") 是先截断再写，写一半崩掉（断电/被杀进程）就只剩半个 JSON；
    而加载端读不出来时只能当空档处理。os.replace 在同一目录内是原子的，读者要么看到
    旧档、要么看到新档，不会看到半截。临时文件名带上线程号，避免两个线程同时写同一群时
    互相踩到对方的临时文件（工具现在跑在线程池里）。
    """
    data["_group_id"] = group_id
    runtime.write_text(_get_file_path(group_id), json.dumps(data, ensure_ascii=False, indent=2))


def _empty_group_state() -> dict:
    return {
        "dm_mode": False,
        "active_campaign": "",
        "campaigns": {},
    }


def _empty_campaign(name: str, dm_style: str = "") -> dict:
    return {
        "name": name,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "finished": False,
        "dm_style": dm_style,
        "rules": "",          # 规则系统（D&D/COC/自定义等）
        "lore": [],           # 静态设定资料（剧本剧情/世界观/规则细则/NPC等，只增不改）
        "facts": [],          # 只增不改的剧情事实/玩家决定
        "state": {},          # 可编辑状态（角色卡/HP/场景等）
        "players": {},        # player_key -> {"name": str, "again_remaining": int}
        "history_stack": [],  # 操作快照，供 /again 回退单步
        "roll_log": [],       # 玩家 /roll 的真实骰点（只记最近若干条，注入 prompt 用）
    }


def _get_active(group_id: str) -> tuple[dict, dict | None]:
    """返回 (群状态, 激活存档或 None)"""
    data = _load(group_id)
    name = data.get("active_campaign", "")
    campaign = data.get("campaigns", {}).get(name) if name else None
    return data, campaign


# ============================================
# DM 模式开关
# ============================================
def _drop_chat_context(gid: str) -> None:
    """
    切换模式时丢掉普通群聊攒下的上下文与互动窗口。

    跑团与普通 chat 互斥：留着这些片段，退出跑团后会被当成「刚刚发生的事」
    喂给 AI；反过来普通 chat 的历史也不该带进跑团。
    """
    runtime.reset_context(gid)


def is_dm_mode(group_id: str = "") -> bool:
    """当前群是否处于 DM 模式"""
    gid = group_id
    if not gid:
        return False
    return bool(_load(gid).get("dm_mode", False))


def enter_dm(group_id: str = "", campaign_name: str = "") -> str:
    """进入 DM 模式。若已有激活存档则直接激活，否则新建。"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    data["dm_mode"] = True

    if campaign_name and campaign_name not in data.get("campaigns", {}):
        data["campaigns"][campaign_name] = _empty_campaign(campaign_name)
    if campaign_name:
        data["active_campaign"] = campaign_name
    if not data.get("active_campaign") and data.get("campaigns"):
        data["active_campaign"] = next(iter(data["campaigns"]))

    _save(gid, data)
    _drop_chat_context(gid)
    active = data.get("active_campaign", "")
    return f"已进入 DM 模式。当前跑团：{active or '（尚未创建，请告诉 DM 世界观和规则）'}"


def exit_dm(group_id: str = "") -> str:
    """退出 DM 模式，存档保留"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    data["dm_mode"] = False
    _save(gid, data)
    _drop_chat_context(gid)
    return "已退出 DM 模式，回到普通群 bot 模式。跑团存档已保留，可随时 /load 或再说「来跑团」继续。"


# ============================================
# 存档管理
# ============================================
def list_campaigns(group_id: str = "") -> str:
    """列出所有存档（进行中/已完结），纯文本"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    campaigns = data.get("campaigns", {})
    if not campaigns:
        return "当前还没有任何跑团存档。"

    lines = []
    for name, c in campaigns.items():
        status = "进行中" if not c.get("finished") else "已完结"
        marker = " [当前]" if name == data.get("active_campaign") else ""
        players = len(c.get("players", {}))
        lines.append(f"· {name}（{status}，玩家 {players} 人）{marker}")
    return "跑团存档列表：\n" + "\n".join(lines)


def load_campaign(name: str, group_id: str = "") -> str:
    """加载指定存档为激活存档"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    campaigns = data.get("campaigns", {})
    if name not in campaigns:
        return f"未找到存档「{name}」。可用 /campaigns 查看所有存档。"
    data["active_campaign"] = name
    data["dm_mode"] = True
    _save(gid, data)
    _drop_chat_context(gid)
    return f"已加载跑团「{name}」，进入 DM 模式继续冒险。"


def create_campaign(name: str, group_id: str = "", dm_style: str = "") -> str:
    """创建新存档并激活"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    if name in data.get("campaigns", {}):
        return f"存档「{name}」已存在，可用 /load {name} 加载。"
    data["campaigns"][name] = _empty_campaign(name, dm_style)
    data["active_campaign"] = name
    data["dm_mode"] = True
    _save(gid, data)
    _drop_chat_context(gid)
    return f"已创建跑团「{name}」并进入 DM 模式。"


def remake(group_id: str = "") -> str:
    """清空当前激活存档的进度重来（保留存档框架，清空 facts/state/history，重置反悔次数）"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    name = data.get("active_campaign", "")
    if not name or name not in data.get("campaigns", {}):
        return "当前没有激活的跑团。"
    c = data["campaigns"][name]
    dm_style = c.get("dm_style", "")
    rules = c.get("rules", "")
    lore = c.get("lore", [])
    # 保留规则系统、DM 风格和设定资料（lore），清空进度
    data["campaigns"][name] = _empty_campaign(name, dm_style)
    data["campaigns"][name]["rules"] = rules
    data["campaigns"][name]["lore"] = lore
    _save(gid, data)
    _drop_chat_context(gid)
    return f"跑团「{name}」进度已清空，从头重新开始。"


# ============================================
# 状态更新与事实日志
# ============================================
def update_state(content: str, group_id: str, user_id: str = "") -> str:
    """
    更新可编辑状态（state 字段）。
    content 为 JSON 字符串（部分字段合并）或自然语言追加到 notes。
    """
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    # 改动前的 state 快照（JSON 往返复制，状态本来就是从 JSON 读出来的）
    before = json.loads(json.dumps(campaign.get("state") or {}, ensure_ascii=False))
    # 尝试解析为 JSON 合并，否则存为 notes
    try:
        updates = json.loads(content)
        if isinstance(updates, dict):
            campaign["state"].update(updates)
        else:
            campaign["state"]["notes"] = campaign["state"].get("notes", "") + "\n" + str(updates)
    except Exception:
        campaign["state"]["notes"] = campaign["state"].get("notes", "") + "\n" + content
    changes = {k: {"existed": k in before, "before": before.get(k), "after": v}
               for k, v in campaign["state"].items() if k not in before or before[k] != v}
    _push_undo(campaign, gid, user_id, f"状态更新：{content[:80]}", {"fields": changes})
    _save(gid, data)
    return "跑团状态已更新。"


def append_fact(fact: str, group_id: str, user_id: str = "") -> str:
    """
    追加一条剧情事实（只增不改，抗幻觉核心）。

    每次追加都在操作栈里留一个回退点（记住追加前的条数），玩家才可能用 /again
    撤回"上一步决定"—— 过去没有任何地方往栈里压东西，/again 永远说"没有可撤回的操作"。
    """
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    timestamp = datetime.now().strftime("%m-%d %H:%M")
    import uuid
    ids = campaign.setdefault("fact_ids", [uuid.uuid4().hex for _ in campaign["facts"]])
    fact_id = uuid.uuid4().hex
    ids.append(fact_id)
    _push_undo(campaign, gid, user_id, fact, {"fact_id": fact_id})
    campaign["facts"].append(f"[{timestamp}] {fact}")
    _save(gid, data)
    return f"已记录事实：{fact}"


def append_lore(lore: str, group_id: str = "") -> str:
    """
    追加一条静态设定资料（剧本剧情/世界观/规则细则/NPC 等）。
    与 facts（已发生的事实）语义分离：lore 是建团时搜到的背景资料，只增不改。
    """
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    timestamp = datetime.now().strftime("%m-%d %H:%M")
    campaign["lore"].append(f"[{timestamp}] {lore}")
    _save(gid, data)
    return f"已记录设定资料：{lore}"


def set_dm_style(style: str, group_id: str = "") -> str:
    """更换 DM 主持风格"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    campaign["dm_style"] = style
    _save(gid, data)
    return f"DM 风格已更换为：{style}"


def set_rules(rules: str, group_id: str = "") -> str:
    """设置规则系统"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    campaign["rules"] = rules
    _save(gid, data)
    return f"规则系统已设为：{rules}"


# ============================================
# 掷骰记录
# ============================================
#: 存档里保留、并注入 prompt 的掷骰条数
_MAX_ROLL_LOG = 20


def record_roll(expression: str, result: str, group_id: str, user_id: str, user_name: str = "") -> None:
    """
    把一次 /roll 的结果记进当前存档（只保留最近 _MAX_ROLL_LOG 条）。

    为什么非记不可：dm.md 要求 DM「严格依据 /roll 返回的结果判定，严禁编造骰点」，
    而 /roll 是**本地指令** —— 掷出来的数字只发到了群里，从没进过模型上下文
    （commands.py 里指令处理完就 return，不写对话历史）。于是 DM 下一轮根本不知道
    掷出了几点，只能等玩家复述，或者干脆自己编一个。记进存档后由 get_state_for_prompt
    注入，判定才有依据。

    非 DM 模式（或没有激活存档）静默跳过：骰子照掷，不因为没人接手就掷不出来。
    """
    if not group_id:
        return
    data = _load(group_id)
    if not data.get("dm_mode"):
        return
    campaign = data.get("campaigns", {}).get(data.get("active_campaign", "")) or None
    if campaign is None:
        return
    _register_player(campaign, group_id, user_id, user_name)
    log = campaign.setdefault("roll_log", [])
    log.append({
        "at": datetime.now().strftime("%H:%M"),
        "player": (user_name or "").strip() or f"用户{str(user_id)[-4:]}",
        "expr": expression or "1d6",
        "result": result,
    })
    del log[:-_MAX_ROLL_LOG]
    _save(group_id, data)


# ============================================
# 玩家与反悔次数
# ============================================
def _player_key(group_id: str, user_id: str) -> str:
    return f"{group_id}:{user_id}"


def ensure_player(group_id: str, user_id: str, user_name: str = "") -> str:
    """确保玩家已注册，返回 player_key。

    新玩家按存档的 ``default_again`` 起步（由 set_initial_again 写入，DM 建团时可调），
    没有该字段才回落到常量 DEFAULT_AGAIN —— 过去这里直接写死常量，于是 set_initial_again
    存下的 default_again 永远没人读，注释里那句"供后续新玩家使用"是假的。
    """
    data = _load(group_id)
    name = data.get("active_campaign", "")
    if not name or name not in data.get("campaigns", {}):
        return ""
    c = data["campaigns"][name]
    key = _player_key(group_id, user_id)
    _register_player(c, group_id, user_id, user_name)
    _save(group_id, data)
    return key


def _register_player(campaign, gid, uid, name=""):
    if not uid:
        return
    key = _player_key(gid, uid)
    entry = campaign.setdefault("players", {}).setdefault(key, {
        "name": name or f"用户{uid[-4:]}",
        "again_remaining": campaign.get("default_again", DEFAULT_AGAIN),
    })
    if name:
        entry["name"] = name


def set_initial_again(count: int, group_id: str) -> str:
    """
    设定每位玩家的初始反悔次数（manage_campaign 的 again_init，DM 建团时用）。

    dm.md 明确写了"告知玩家每位初始反悔次数（默认 1 次），如玩家要求更多可调整"，
    所以这个能力要真的挂到工具上，不能只是躺在模块里没人调。
    """
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    try:
        count = max(0, min(int(count), 10))
    except (TypeError, ValueError):
        return f"反悔次数需要是数字，收到的是 {count!r}。"
    for p in campaign["players"].values():
        p["again_remaining"] = count
    # 记下默认值：之后新加入的玩家也按这个数起步（ensure_player 会读它）
    campaign["default_again"] = count
    _save(gid, data)
    return f"每位玩家的初始反悔次数已设为 {count} 次。"


def grant_again(amount: int, player: str = "", group_id: str = "") -> str:
    """
    奖励（amount 为负则是扣减）反悔次数，manage_campaign 的 again_grant 用。

    ``player`` 留空或 all = 全队；否则按玩家名（或 player_key）匹配 —— DM 在剧情里
    看到的只有名字，拿不到 "群号:openid" 这种内部 key，所以必须支持按名找人。
    """
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    players = campaign["players"]
    if not players:
        return "当前跑团还没有玩家记录（玩家第一次行动后才会登记）。"
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return f"反悔次数需要是数字，收到的是 {amount!r}。"

    target = (player or "").strip()
    if not target or target.lower() in ("all", "所有人", "全队"):
        picked = list(players.keys())
    else:
        picked = [
            k for k, p in players.items()
            if k == target or (p.get("name") or "").strip() == target
        ]
        if not picked:
            # 模型可能只记得名字的一部分（「张三」而不是「张三丰」）
            picked = [k for k, p in players.items() if target in (p.get("name") or "")]
        if not picked:
            known = "、".join(p.get("name") or k for k, p in players.items())
            return f"没有找到玩家「{target}」。当前玩家：{known}"
        if len(picked) != 1:
            return "玩家名称不唯一，请使用完整 player_key。"

    for k in picked:
        players[k]["again_remaining"] = max(0, min(10, players[k].get("again_remaining", 0) + amount))
    _save(gid, data)
    verb = "奖励" if amount >= 0 else "扣减"
    who = "、".join(players[k].get("name") or k for k in picked)
    detail = "、".join(f"{players[k].get('name') or k} 剩 {players[k]['again_remaining']} 次" for k in picked)
    return f"已为 {who} {verb} {abs(amount)} 次反悔机会（{detail}）。"


# ============================================
# 操作历史栈与 /again 回退
# ============================================
#: 栈深上限（每条是一次可回退的状态改动，防止无限增长）
_MAX_HISTORY = 200


def _push_undo(campaign: dict, gid: str, user_id: str, snapshot: str, restore: dict) -> None:
    """
    把一个可回退点压进操作栈（**不落盘**，由调用方 _save，省一次写文件）。

    条目带两样东西：
    - snapshot：给人看的"撤回内容"，就是那条事实本身；
    - restore：给机器用的还原数据 —— 真正的回退靠它。过去只有 snapshot（纯文本），
      于是 undo_last 除了弹栈什么也没改，等于"口头撤回"。

    压在**状态改动之前**调用，restore 里记的是改动前的样子。facts / state 是仅有的两处
    "变更会被反悔"的地方：lore（剧本设定）是只增的背景资料，纠错靠再补一条，不占反悔次数。
    """
    stack = campaign.setdefault("history_stack", [])
    _register_player(campaign, gid, user_id)
    lease = runtime.current()
    transaction = lease.id if lease else ""
    # log + update in one model decision form one undo operation per actor.
    previous = next((entry for entry in reversed(stack) if transaction and entry.get("transaction") == transaction and entry.get("player") == _player_key(gid, user_id)), None)
    if previous is not None:
        previous.setdefault("restores", [previous["restore"]]).append(restore)
        previous["snapshot"] += "；" + snapshot
        return
    stack.append({
        "transaction": transaction,
        "player": _player_key(gid, user_id) if user_id else "",
        "snapshot": snapshot,
        "restore": restore,
        "at": datetime.now().strftime("%m-%d %H:%M"),
    })
    del stack[:-_MAX_HISTORY]  # 只保留最近 _MAX_HISTORY 条


def undo_last(group_id: str, user_id: str) -> str:
    """撤回当前玩家最近一次状态改动（单步），并扣减其反悔次数"""
    gid = group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"

    key = _player_key(gid, user_id)
    player = campaign["players"].get(key)
    remaining = player.get("again_remaining", 0) if player else 0

    stack = campaign["history_stack"]
    if not stack:
        return f"没有可撤回的操作（剩余反悔次数：{remaining} 次）。"

    # 找到当前玩家最近的一次操作（栈里夹着别人记的事实，不能直接弹栈顶）
    idx = None
    for i in range(len(stack) - 1, -1, -1):
        if stack[i].get("player") == key:
            idx = i
            break
    if idx is None:
        return f"你还没有任何可撤回的操作（剩余反悔次数：{remaining} 次）。"

    if remaining <= 0:
        return "你的反悔次数已用完，无法撤回。"

    removed = stack[idx]
    # Simulate all reversals before accepting; overlapping later edits are a conflict.
    candidate = json.loads(json.dumps(campaign, ensure_ascii=False))
    restores = removed.get("restores", [removed.get("restore") or {}])
    for restore in reversed(restores):
        if "fields" in restore:
            for field, change in restore["fields"].items():
                if field not in candidate["state"] or candidate["state"][field] != change["after"]:
                    return "无法撤回：该字段已有后续修改，请先处理冲突；未扣减次数。"
                if change["existed"]:
                    candidate["state"][field] = change["before"]
                else:
                    candidate["state"].pop(field, None)
        elif "fact_id" in restore:
            ids = candidate.get("fact_ids", [])
            if restore["fact_id"] not in ids:
                return "无法撤回：事实记录已变化；未扣减次数。"
            fact_index = ids.index(restore["fact_id"])
            ids.pop(fact_index)
            candidate["facts"].pop(fact_index)
        elif idx != len(stack) - 1:
            return "旧版快照只能撤回全局最后一步，以免覆盖其他玩家进度。"
        else:
            if isinstance(restore.get("facts_len"), int):
                del candidate["facts"][restore["facts_len"]:]
                if "fact_ids" in candidate:
                    del candidate["fact_ids"][restore["facts_len"]:]
            if isinstance(restore.get("state"), dict):
                candidate["state"] = restore["state"]
    campaign["state"] = candidate["state"]
    campaign["facts"] = candidate["facts"]
    if "fact_ids" in candidate:
        campaign["fact_ids"] = candidate["fact_ids"]
    stack.pop(idx)

    if player:
        player["again_remaining"] = remaining - 1
    _save(gid, data)
    _drop_chat_context(gid)

    snap = removed.get("snapshot", "")
    return f"已撤回上一步操作。\n撤回内容：{snap}\n剩余反悔次数：{remaining - 1} 次。"


# ============================================
# 生成注入 prompt
# ============================================
def get_state_for_prompt(group_id: str = "") -> str:
    """
    生成注入 system prompt 的纯文本状态摘要。
    仅在 DM 模式且存在激活存档时返回非空。
    """
    gid = group_id
    if not gid:
        return ""
    data = _load(gid)
    if not data.get("dm_mode"):
        return ""
    name = data.get("active_campaign", "")
    c = data.get("campaigns", {}).get(name)
    if not c:
        return ""

    parts = [f"当前跑团：{name}"]
    if c.get("state"):
        parts.append("当前状态（可编辑）：\n" + json.dumps(c["state"], ensure_ascii=False, indent=1)[:2000])
    if c.get("dm_style"):
        parts.append(f"DM 风格：{c['dm_style'][:300]}")
    if c.get("rules"):
        parts.append(f"规则系统：{c['rules'][:500]}")

    # 静态设定资料（剧本剧情/世界观/规则细则等）
    # Lore is appended last, after current state, players, dice and recent facts.

    # 玩家列表（含反悔次数）
    if c.get("players"):
        player_lines = []
        for k, p in c["players"].items():
            p_name = p.get("name") or k.split(":", 1)[-1][-4:]
            player_lines.append(f"· {p_name}（剩余反悔 {p.get('again_remaining', 0)} 次）")
        parts.append("玩家：\n" + "\n".join(player_lines)[:500])

    # 掷骰记录：/roll 的结果只发给群里，不在这里呈现的话 DM 根本看不到骰点，
    # 而 dm.md 要求它严格按骰点判定 —— 看不到就只能编
    if c.get("roll_log"):
        rolls = c["roll_log"][-10:]
        parts.append(
            "最近掷骰记录（玩家自己掷的，判定必须以此为准，不得改写）：\n"
            + "\n".join(
                f"· [{r.get('at', '')}] {r.get('player', '')} {r.get('expr', '')} → {r.get('result', '')}"[:120]
                for r in rolls
            )
        )

    # 事实日志（只增，最近的放前面便于快速回顾）
    if c.get("facts"):
        facts = c["facts"][-50:]  # 最多最近 50 条
        parts.append("已发生的事实：\n" + "\n".join(facts)[-1000:])

    # 可编辑状态
    if c.get("lore"):
        parts.append("剧本与设定资料（静态，只增）：\n" + "\n".join(c["lore"][-30:])[-1000:])

    text = "\n\n".join(parts)
    if len(text) > _PROMPT_MAX_CHARS:
        text = text[:_PROMPT_MAX_CHARS] + "\n……（状态过长，已截断，可用 view 查看完整内容）"
    return "\n\n【跑团状态 — 续团前务必先了解，以此为准，不得凭记忆编造】\n" + text


def view_state(group_id, section="state", page=1):
    """Bounded pages expose every stored field without truncating the only view."""
    _, campaign = _get_active(group_id)
    if campaign is None:
        return "当前没有激活的跑团。"
    if section == "all":
        value = campaign
    elif section in campaign:
        value = {section: campaign[section]}
    else:
        return "可用 section：all/state/facts/lore/players/roll_log/rules/dm_style"
    text = json.dumps(value, ensure_ascii=False, indent=2)
    pages = max(1, (len(text) + 3999) // 4000)
    page = int(page)
    if not 1 <= page <= pages:
        return f"页码范围 1..{pages}"
    return f"{section} 第 {page}/{pages} 页\n" + text[(page-1)*4000:page*4000]


for _operation in ("enter_dm", "exit_dm", "load_campaign", "create_campaign", "remake", "update_state", "append_fact", "append_lore", "set_dm_style", "set_rules", "record_roll", "ensure_player", "set_initial_again", "grant_again", "undo_last"):
    globals()[_operation] = runtime.serialized(globals()[_operation])
