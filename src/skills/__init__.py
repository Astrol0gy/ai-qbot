"""
Skill 模块

设计遵循 Claude Agent Skills 三层渐进式加载：
  Level 1: skill 元数据（name + description）→ 始终注入 system prompt
  Level 2: SKILL.md body → LLM 判定 skill 相关后完整加载
  Level 3: 附加文件（未来: references/, scripts/）→ LLM 按需打开

Skill 文件格式（.md）：
  ---
  name: weather
  description: 查询天气
  tools: web_search, fetch_url   # 可选: 该 skill 可用的工具白名单
  ---

  # 天气查询指引
  ...具体操作流程...
"""
import os
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent

# 已加载的 skill
_SKILLS: list[dict] = []


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    解析 Markdown 文件的 YAML frontmatter。
    返回 (元数据字典, body 内容)。
    简单实现，不依赖 pyyaml。
    """
    meta: dict = {}
    body = content

    # 检查是否以 --- 开头
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return meta, content

    # 找到第一个 --- 和第二个 --- 之间的内容
    match = re.match(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", stripped, re.DOTALL)
    if not match:
        return meta, content

    frontmatter_str = match.group(1)
    body = match.group(2).strip()

    # 解析 key: value
    for line in frontmatter_str.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # 解析 tools 字段为 list
            if key == "tools" and value:
                value = [t.strip() for t in value.split(",") if t.strip()]
            meta[key] = value

    return meta, body


def _load_skills():
    """扫描 skills/ 目录，加载所有 .md 文件"""
    global _SKILLS
    _SKILLS = []

    for filepath in SKILLS_DIR.glob("*.md"):
        content = filepath.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)

        name = meta.get("name", filepath.stem)
        description = meta.get("description", "")
        tool_whitelist = meta.get("tools", None)  # list of tool names or None

        _SKILLS.append({
            "name": name,
            "description": description,
            "tools": tool_whitelist,
            "body": body,
            "file": filepath.name,
        })
        tool_info = f" [tools: {tool_whitelist}]" if tool_whitelist else ""
        print(f"[Skill] 加载 {filepath.name} -> 「{name}」{tool_info}")


def get_metadata_section() -> str:
    """
    Level 1 输出：所有 skill 的元数据摘要（仅 name + description）。
    注入 system prompt，让 LLM 知道有哪些 skill 可用。
    """
    if not _SKILLS:
        return ""

    lines = []
    for s in _SKILLS:
        lines.append(f"- **{s['name']}**：{s['description']}")
    descriptions = "\n".join(lines)

    return (
        "\n\n---\n"
        "## 可用技能\n"
        f"{descriptions}\n\n"
        "当用户请求匹配某个技能时，严格按照该技能的指引逐步操作，不要跳过任何步骤。\n"
        "---"
    )


def get_full_prompt_for_skill(skill_name: str) -> str:
    """
    获取指定 skill 的完整指引内容（body）。
    目前所有 skill body 都预先注入 system prompt，此方法预留用于按需加载（Level 2）。
    """
    for s in _SKILLS:
        if s["name"] == skill_name:
            return s["body"]
    return ""


def get_all_skill_bodies() -> str:
    """
    将所有 skill 的完整 body 拼接为 system prompt 段落。
    当前策略：全部预注入（简单直接，skill 数量少时无性能问题）。
    未来 skill 多时可改为按需加载（Level 2）。
    """
    if not _SKILLS:
        return ""

    parts = []
    for s in _SKILLS:
        tools_note = ""
        if s["tools"]:
            tools_note = f"\n**该技能可用工具：** {', '.join(s['tools'])}。只能使用列出的工具。"
        parts.append(f"### {s['name']}\n{s['body']}{tools_note}")

    return "\n\n" + "\n\n".join(parts)


def get_skill(name: str) -> dict | None:
    """按名称查找 skill"""
    for s in _SKILLS:
        if s["name"] == name:
            return s
    return None


def get_tool_whitelist_for_prompt(user_message: str) -> set[str] | None:
    """
    根据用户消息匹配 skill，返回该 skill 的工具白名单。
    目前简化实现：遍历所有 skill，如果用户消息包含 skill 名称关键词则返回其白名单。
    未来可改为让 LLM 先选择 skill 再应用白名单。
    """
    for s in _SKILLS:
        if s["tools"] and s["name"] in user_message:
            return set(s["tools"])

    # 没有匹配到 skill 或 skill 没有限制工具 → 返回 None（允许全部工具）
    for s in _SKILLS:
        if s["tools"]:
            return None  # 有 skill 但没匹配到 → 先不做限制

    return None


# ============================================
# 启动时自动加载所有 .md skill
# ============================================
_load_skills()
