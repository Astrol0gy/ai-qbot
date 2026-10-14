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

CAMPAIGNS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "campaigns")

# 注入 prompt 时的长度上限（避免超长剧情撑爆 token）
_PROMPT_MAX_CHARS = 6000

# 当前对话上下文（每次 LLM 调用前由 deepseek_client 设置）
_current_group_id = ""
_current_user_id = ""

# 默认每位玩家反悔次数
DEFAULT_AGAIN = 1


def set_current_context(group_id: str, user_id: str = ""):
    """设置当前群的 group_id 和 user_id，供工具 handler 使用"""
    global _current_group_id, _current_user_id
    _current_group_id = group_id
    _current_user_id = user_id or ""


# ============================================
# 文件读写
# ============================================
def _get_file_path(group_id: str) -> str:
    os.makedirs(CAMPAIGNS_DIR, exist_ok=True)
    safe_id = re.sub(r'[<>:"/\\|?*]', "_", group_id)
    return os.path.join(CAMPAIGNS_DIR, f"{safe_id}.json")


def _load(group_id: str) -> dict:
    """加载某群的状态，无文件时返回空结构"""
    path = _get_file_path(group_id)
    if not os.path.exists(path):
        return _empty_group_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[⚠️ 跑团状态加载失败] {e}")
        return _empty_group_state()
    return data


def _save(group_id: str, data: dict):
    """保存某群状态到磁盘"""
    path = _get_file_path(group_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[⚠️ 跑团状态写入失败] {e}")


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
    from .trigger import trigger  # 延迟导入，避免与触发层循环依赖

    trigger.buffer.clear(gid)
    trigger.leave_session(gid)


def is_dm_mode(group_id: str = "") -> bool:
    """当前群是否处于 DM 模式"""
    gid = group_id or _current_group_id
    if not gid:
        return False
    return bool(_load(gid).get("dm_mode", False))


def enter_dm(group_id: str = "", campaign_name: str = "") -> str:
    """进入 DM 模式。若已有激活存档则直接激活，否则新建。"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    _drop_chat_context(gid)
    data = _load(gid)
    data["dm_mode"] = True

    if campaign_name and campaign_name not in data.get("campaigns", {}):
        data["campaigns"][campaign_name] = _empty_campaign(campaign_name)
    if not data.get("active_campaign") and data.get("campaigns"):
        data["active_campaign"] = next(iter(data["campaigns"]))

    _save(gid, data)
    active = data.get("active_campaign", "")
    return f"已进入 DM 模式。当前跑团：{active or '（尚未创建，请告诉 DM 世界观和规则）'}"


def exit_dm(group_id: str = "") -> str:
    """退出 DM 模式，存档保留"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    _drop_chat_context(gid)
    data = _load(gid)
    data["dm_mode"] = False
    _save(gid, data)
    return "已退出 DM 模式，回到普通群 bot 模式。跑团存档已保留，可随时 /load 或再说「来跑团」继续。"


# ============================================
# 存档管理
# ============================================
def list_campaigns(group_id: str = "") -> str:
    """列出所有存档（进行中/已完结），纯文本"""
    gid = group_id or _current_group_id
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
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    campaigns = data.get("campaigns", {})
    if name not in campaigns:
        return f"未找到存档「{name}」。可用 /campaigns 查看所有存档。"
    data["active_campaign"] = name
    data["dm_mode"] = True
    _save(gid, data)
    return f"已加载跑团「{name}」，进入 DM 模式继续冒险。"


def create_campaign(name: str, group_id: str = "", dm_style: str = "") -> str:
    """创建新存档并激活"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data = _load(gid)
    if name in data.get("campaigns", {}):
        return f"存档「{name}」已存在，可用 /load {name} 加载。"
    data["campaigns"][name] = _empty_campaign(name, dm_style)
    data["active_campaign"] = name
    data["dm_mode"] = True
    _save(gid, data)
    return f"已创建跑团「{name}」并进入 DM 模式。"


def remake(group_id: str = "") -> str:
    """清空当前激活存档的进度重来（保留存档框架，清空 facts/state/history，重置反悔次数）"""
    gid = group_id or _current_group_id
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
    return f"跑团「{name}」进度已清空，从头重新开始。"


# ============================================
# 状态更新与事实日志
# ============================================
def update_state(content: str, group_id: str = "") -> str:
    """
    更新可编辑状态（state 字段）。
    content 为 JSON 字符串（部分字段合并）或自然语言描述由调用方处理。
    这里做简单处理：把 content 存为 state 的追加文本/合并。
    """
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    # 尝试解析为 JSON 合并，否则存为 notes
    try:
        updates = json.loads(content)
        if isinstance(updates, dict):
            campaign["state"].update(updates)
        else:
            campaign["state"]["notes"] = campaign["state"].get("notes", "") + "\n" + str(updates)
    except Exception:
        campaign["state"]["notes"] = campaign["state"].get("notes", "") + "\n" + content
    _save(gid, data)
    return "跑团状态已更新。"


def append_fact(fact: str, group_id: str = "") -> str:
    """追加一条剧情事实（只增不改，抗幻觉核心）"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    timestamp = datetime.now().strftime("%m-%d %H:%M")
    campaign["facts"].append(f"[{timestamp}] {fact}")
    _save(gid, data)
    return f"已记录事实：{fact}"


def append_lore(lore: str, group_id: str = "") -> str:
    """
    追加一条静态设定资料（剧本剧情/世界观/规则细则/NPC 等）。
    与 facts（已发生的事实）语义分离：lore 是建团时搜到的背景资料，只增不改。
    """
    gid = group_id or _current_group_id
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
    gid = group_id or _current_group_id
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
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    campaign["rules"] = rules
    _save(gid, data)
    return f"规则系统已设为：{rules}"


# ============================================
# 玩家与反悔次数
# ============================================
def _player_key(group_id: str, user_id: str) -> str:
    return f"{group_id}:{user_id}"


def ensure_player(group_id: str, user_id: str, user_name: str = "") -> str:
    """确保玩家已注册，返回 player_key"""
    data = _load(group_id)
    name = data.get("active_campaign", "")
    if not name or name not in data.get("campaigns", {}):
        return ""
    c = data["campaigns"][name]
    key = _player_key(group_id, user_id)
    if key not in c["players"]:
        c["players"][key] = {"name": user_name or f"用户{user_id[-4:]}", "again_remaining": DEFAULT_AGAIN}
        _save(group_id, data)
    elif user_name and not c["players"][key].get("name"):
        c["players"][key]["name"] = user_name
        _save(group_id, data)
    return key


def set_initial_again(count: int, group_id: str = "") -> str:
    """DM 建团时设定每位玩家的初始反悔次数"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    count = max(0, min(int(count), 10))
    for p in campaign["players"].values():
        p["again_remaining"] = count
    # 记录默认值，供后续新玩家使用
    campaign["default_again"] = count
    _save(gid, data)
    return f"每位玩家的初始反悔次数已设为 {count} 次。"


def grant_again(user_key: str = "", delta: int = 1, group_id: str = "") -> str:
    """奖励/扣除某玩家反悔次数"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    players = campaign["players"]
    if user_key and user_key in players:
        players[user_key]["again_remaining"] = max(0, players[user_key].get("again_remaining", 0) + delta)
    else:
        # 未指定则给所有人加
        for p in players.values():
            p["again_remaining"] = max(0, p.get("again_remaining", 0) + delta)
    _save(gid, data)
    return "反悔次数已更新。"


def get_again_remaining(group_id: str, user_id: str) -> int:
    """获取某玩家剩余反悔次数"""
    data = _load(group_id)
    name = data.get("active_campaign", "")
    c = data.get("campaigns", {}).get(name)
    if not c:
        return 0
    key = _player_key(group_id, user_id)
    return c["players"].get(key, {}).get("again_remaining", 0)


# ============================================
# 操作历史栈与 /again 回退
# ============================================
def record_action(snapshot: str, group_id: str = "", user_id: str = "") -> str:
    """
    记录一次操作（决定/骰子），入栈保存回退快照。
    snapshot 为可回退的文本描述（供 /again 时恢复）。
    """
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    key = _player_key(gid, user_id) if user_id else ""
    campaign["history_stack"].append({
        "player": key,
        "snapshot": snapshot,
        "at": datetime.now().strftime("%m-%d %H:%M"),
    })
    # 限制栈深度，防止无限增长
    if len(campaign["history_stack"]) > 200:
        campaign["history_stack"] = campaign["history_stack"][-200:]
    _save(gid, data)
    return "已记录操作。"


def undo_last(group_id: str = "", user_id: str = "") -> str:
    """撤回上一次决定/骰子（单步），扣减当前玩家反悔次数"""
    gid = group_id or _current_group_id
    if not gid:
        return "无法确定当前群。"
    data, campaign = _get_active(gid)
    if campaign is None:
        return "当前没有激活的跑团。"
    stack = campaign["history_stack"]
    if not stack:
        return "没有可撤回的操作。"

    key = _player_key(gid, user_id)
    # 找到当前玩家最近的一次操作
    idx = None
    for i in range(len(stack) - 1, -1, -1):
        if stack[i].get("player") == key:
            idx = i
            break
    if idx is None:
        return "你还没有任何可撤回的操作。"

    # 检查反悔次数
    player = campaign["players"].get(key)
    remaining = player.get("again_remaining", 0) if player else 0
    if remaining <= 0:
        return "你的反悔次数已用完，无法撤回。"

    removed = stack.pop(idx)
    if player:
        player["again_remaining"] = remaining - 1
    _save(gid, data)

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
    gid = group_id or _current_group_id
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
    if c.get("dm_style"):
        parts.append(f"DM 风格：{c['dm_style']}")
    if c.get("rules"):
        parts.append(f"规则系统：{c['rules']}")

    # 静态设定资料（剧本剧情/世界观/规则细则等）
    if c.get("lore"):
        lore = c["lore"][-30:]  # 最多最近 30 条
        parts.append("剧本与设定资料（静态，只增）：\n" + "\n".join(lore))

    # 玩家列表（含反悔次数）
    if c.get("players"):
        player_lines = []
        for k, p in c["players"].items():
            name = p.get("name") or k.split(":", 1)[-1][-4:]
            player_lines.append(f"· {name}（剩余反悔 {p.get('again_remaining', 0)} 次）")
        parts.append("玩家：\n" + "\n".join(player_lines))

    # 事实日志（只增，最近的放前面便于快速回顾）
    if c.get("facts"):
        facts = c["facts"][-50:]  # 最多最近 50 条
        parts.append("已发生的事实：\n" + "\n".join(facts))

    # 可编辑状态
    if c.get("state"):
        parts.append("当前状态（可编辑）：\n" + json.dumps(c["state"], ensure_ascii=False, indent=1))

    text = "\n\n".join(parts)
    if len(text) > _PROMPT_MAX_CHARS:
        text = text[:_PROMPT_MAX_CHARS] + "\n……（状态过长，已截断，可用 view 查看完整内容）"
    return "\n\n【跑团状态 — 续团前务必先了解，以此为准，不得凭记忆编造】\n" + text
