"""
用户画像管理器

- 按 (group_id, user_id) 维度存储用户画像为独立 .md 文件
- LLM 通过 manage_user_profile 工具自主决定记住/删除信息
- 隐私边界：明确拒绝记录个人敏感数据（手机号、身份证、邮箱、密码等）
"""
import os
import re
from datetime import datetime
from . import runtime
from .observability import log_event

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "user_profiles")

# 这里曾经有 _current_group_id / _current_user_id / _current_user_name 三个模块级全局变量，
# 由 deepseek_client 在每轮对话前用 set_current_context() 覆盖。不同群是可以并发跑同一轮
# chat 的，于是"设置全局 → await → 工具读全局"之间会被别的群插进来改写，画像/记忆就写错人。
# 现在改为所有接口都从调用链上显式接收 (group_id, user_id, user_name)，不再有全局状态。


# ============================================
# 敏感数据检测
# ============================================
_PERSONAL_PATTERNS = [
    (r"\b1[3-9]\d{9}\b", "手机号"),
    (r"\b\d{17}[\dXx]\b", "身份证号"),
    (r"\b\d{16,19}\b", "银行卡号"),
    (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "邮箱"),
    (r"密码|password|passwd", "密码"),
    (r"家庭住址|详细地址|身份证|手机号|电话号码|银行卡|真实姓名", "个人隐私"),
]


def _detect_personal_data(info: str) -> str | None:
    """检测是否包含个人敏感数据，返回隐私类型描述或 None"""
    lower = info.lower()
    for pattern, desc in _PERSONAL_PATTERNS:
        if re.search(pattern, lower):
            return desc
    return None


# ============================================
# 文件读写
# ============================================
def _safe_component(raw_id: str) -> str:
    return "id-" + str(raw_id).encode("utf-8").hex()


def _get_profile_path(group_id: str, user_id: str) -> str:
    return os.path.join(PROFILES_DIR, _safe_component(group_id), _safe_component(user_id) + ".md")


def _header(gid, uid, name):
    import json
    name = str(name).replace("\r", " ").replace("\n", " ")[:100]
    return ("# 用户画像\n标识: " + json.dumps([gid, uid], ensure_ascii=False)
            + f"\n用户名: {name}\n创建时间: {datetime.now():%Y-%m-%d %H:%M}\n\n")


def _result(action, status, message, group_id, user_id, *, reason=""):
    # Keep preference text and private values out of operation telemetry.
    log_event('profile.operation', action=action, status=status, reason=reason,
              conversation_id=group_id, user_id=user_id)
    return message


def _change(action, path, content, group_id, user_id):
    try:
        runtime.write_text(path, content)
    except Exception as exc:
        log_event('profile.operation', action=action, status='failed',
                  error_type=type(exc).__name__, conversation_id=group_id, user_id=user_id)
        raise
    lease = runtime.current()
    log_event('profile.operation', action=action, status='staged' if lease else 'committed',
              conversation_id=group_id, user_id=user_id)
    def persisted():
        log_event('profile.persist', action=action, status='committed',
                  conversation_id=group_id, user_id=user_id)
    if lease:
        lease.after_commit.append(persisted)
    else:
        persisted()


@runtime.serialized
def remember_info(info: str, group_id: str, user_id: str, user_name: str = "") -> str:
    if not group_id or not user_id:
        return _result('remember', 'rejected', "无法确定当前用户，请稍后重试。", group_id, user_id, reason='identity_missing')
    personal_type = _detect_personal_data(info)
    if personal_type:
        return _result('remember', 'rejected', f"【隐私保护】检测到{personal_type}信息，我不会记录。只记录使用偏好。", group_id, user_id, reason='privacy')
    info = info.strip().replace("\n", " ")
    if not info:
        return _result('remember', 'rejected', "请提供要记住的信息。", group_id, user_id, reason='empty')
    if len(info) > 1000:
        return _result('remember', 'rejected', "单条偏好最多 1000 字。", group_id, user_id, reason='too_long')
    path = _get_profile_path(group_id, user_id)
    content = runtime.read_text(path) or _header(group_id, user_id, user_name)
    records = [line for line in content.splitlines() if line.startswith("- [")]
    if any(line.split("] ", 1)[-1] == info for line in records):
        return _result('remember', 'duplicate', "这条信息已经记录过了。", group_id, user_id)
    if len(records) >= 100:
        return _result('remember', 'rejected', "已达 100 条偏好上限，请先删除旧记录。", group_id, user_id, reason='record_limit')
    _change('remember', path, content + f"- [{datetime.now():%Y-%m-%d %H:%M}] {info}\n", group_id, user_id)
    return f"已记住：{info}"


@runtime.serialized
def forget_info(info: str, group_id: str, user_id: str) -> str:
    if info.strip().lower() in ("all", "全部", "所有", "清空", "清除"):
        return forget_all(group_id, user_id)
    path = _get_profile_path(group_id, user_id)
    content = runtime.read_text(path)
    if content is None:
        return _result('forget', 'not_found', "你还没有任何已记录的信息。", group_id, user_id)
    lines = content.splitlines(keepends=True)
    kept = [line for line in lines if not (line.startswith("- [") and info in line)]
    count = len(lines) - len(kept)
    if not count:
        return _result('forget', 'not_found', f"没有找到包含「{info}」的记录。", group_id, user_id)
    _change('forget', path, "".join(kept), group_id, user_id)
    return f"已删除 {count} 条匹配的记录。"


@runtime.serialized
def forget_all(group_id: str, user_id: str) -> str:
    path = _get_profile_path(group_id, user_id)
    if runtime.read_text(path) is None:
        return _result('forget', 'not_found', "你还没有任何已记录的信息。", group_id, user_id)
    _change('forget', path, None, group_id, user_id)
    return "已清除所有记录。"


def list_profile(group_id: str, user_id: str) -> str:
    content = runtime.read_text(_get_profile_path(group_id, user_id)) or ""
    return "\n".join(line.strip() for line in content.splitlines() if line.startswith("- [")) or "暂无记录。"


def get_profile_for_prompt(group_id: str) -> str:
    import json
    directory = os.path.join(PROFILES_DIR, _safe_component(group_id))
    if not os.path.isdir(directory):
        return ""
    sections, remaining = [], 6000
    for filename in sorted(os.listdir(directory))[:100]:
        if not filename.endswith(".md"):
            continue
        content = runtime.read_text(os.path.join(directory, filename)) or ""
        lines = content.splitlines()
        identity = next((line[4:] for line in lines if line.startswith("标识: ")), "")
        try:
            gid, uid = json.loads(identity)
        except (ValueError, TypeError):
            continue
        if gid != group_id:
            continue
        records = [line for line in lines if line.startswith("- [")]
        if not records:
            continue
        name = next((line[5:] for line in lines if line.startswith("用户名: ")), "") or f"用户{uid[-4:]}"
        section = f"【{name} 用户id={uid} 的偏好】\n" + "\n".join(records)
        sections.append(section[:remaining])
        remaining -= len(section)
        if remaining <= 0:
            break
    return "\n\n【群内用户的画像】\n" + "\n\n".join(sections) if sections else ""
