"""
用户画像管理器

- 按 (group_id, user_id) 维度存储用户画像为独立 .md 文件
- LLM 通过 manage_user_profile 工具自主决定记住/删除信息
- 隐私边界：明确拒绝记录个人敏感数据（手机号、身份证、邮箱、密码等）
"""
import os
import re
from datetime import datetime

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "user_profiles")

# 当前对话上下文（每次 LLM 调用前由 deepseek_client 设置）
_current_group_id: str = ""
_current_user_id: str = ""


def set_current_context(group_id: str, user_id: str):
    """设置当前对话的群和用户 ID，供 profile 工具使用"""
    global _current_group_id, _current_user_id
    _current_group_id = group_id
    _current_user_id = user_id


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
def _get_profile_path(group_id: str, user_id: str) -> str:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    safe_id = re.sub(r'[<>:"/\\|?*]', "_", user_id)
    return os.path.join(PROFILES_DIR, f"{group_id}_{safe_id}.md")


def _ensure_header(path: str):
    """确保文件有头部标题和时间"""
    if not os.path.exists(path):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 用户画像\n创建时间: {timestamp}\n\n")


# ============================================
# 对外接口
# ============================================
def remember_info(info: str, group_id: str = "", user_id: str = "") -> str:
    """
    记住用户的一条信息。
    如果 group_id/user_id 为空，使用当前上下文。
    返回操作结果描述。
    """
    gid = group_id or _current_group_id
    uid = user_id or _current_user_id
    if not gid or not uid:
        return "无法确定当前用户，请稍后重试。"

    # 隐私检查
    personal_type = _detect_personal_data(info)
    if personal_type:
        return (
            f"【隐私保护】检测到{personal_type}信息，我不会记录。\n"
            "我只记录使用偏好（如回答风格、语气、关注话题），不存储任何个人隐私数据。"
        )

    path = _get_profile_path(gid, uid)
    _ensure_header(path)

    # 去重
    stripped = info.strip()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if stripped in content:
        return f"这条信息已经记录过了。"

    # 追加
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- [{timestamp}] {stripped}\n")

    return f"已记住：{stripped}"


def forget_info(info: str, group_id: str = "", user_id: str = "") -> str:
    """
    忘记用户的一条信息（模糊关键词匹配）。
    返回操作结果。
    """
    gid = group_id or _current_group_id
    uid = user_id or _current_user_id
    if not gid or not uid:
        return "无法确定当前用户。"

    # 特殊指令：清除所有
    if info.strip().lower() in ("all", "全部", "所有", "清空", "清除"):
        return forget_all(gid, uid)

    path = _get_profile_path(gid, uid)
    if not os.path.exists(path):
        return "你还没有任何已记录的信息。"

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    removed = []
    for line in lines:
        if line.strip().startswith("- [") and info in line:
            removed.append(line.strip())
        else:
            new_lines.append(line)

    if not removed:
        return f"没有找到包含「{info}」的记录。"

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    count = len(removed)
    return f"已删除 {count} 条匹配的记录。"


def forget_all(group_id: str, user_id: str) -> str:
    """清除用户全部画像"""
    path = _get_profile_path(group_id, user_id)
    if os.path.exists(path):
        os.remove(path)
        return "已清除所有记录。"
    return "你还没有任何已记录的信息。"


def list_profile(group_id: str = "", user_id: str = "") -> str:
    """列出当前用户的全部画像记录"""
    gid = group_id or _current_group_id
    uid = user_id or _current_user_id
    if not gid or not uid:
        return "无法确定当前用户。"

    path = _get_profile_path(gid, uid)
    if not os.path.exists(path):
        return "暂无任何记录。"

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = [l.strip() for l in content.splitlines() if l.strip().startswith("- [")]
    if not lines:
        return "暂无记录。"

    return "\n".join(lines)


def get_profile_for_prompt(group_id: str = "", user_id: str = "") -> str:
    """
    获取用户画像文本，用于注入 LLM system prompt。
    如果没有记录，返回空字符串。
    """
    gid = group_id or _current_group_id
    uid = user_id or _current_user_id
    if not gid or not uid:
        return ""

    path = _get_profile_path(gid, uid)
    if not os.path.exists(path):
        return ""

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    record_lines = [l.strip() for l in content.splitlines() if l.strip().startswith("- [")]
    if not record_lines:
        return ""

    return (
        "\n\n【当前用户的画像 — 已记录的偏好和习惯】\n"
        + "\n".join(record_lines)
        + "\n请根据这些信息调整你的回答风格和内容。"
    )
