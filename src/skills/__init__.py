"""渐进式技能加载：目录元数据 → 按需正文 → 按需附加文本。

启动只索引 Markdown frontmatter，不缓存正文。每轮创建独立 SkillSession，
load_skill 成功后才开放该技能声明的工具；多个已加载技能取工具并集。
平铺 foo.md 的资源放在 foo/references 或 foo/scripts；目录形式使用 foo/SKILL.md。
附加脚本只允许读文本，不执行。
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

SKILLS_DIR = Path(__file__).parent
LOAD_SKILL = "load_skill"
READ_RESOURCE = "read_skill_resource"
SKILL_TOOL_NAMES = frozenset({LOAD_SKILL, READ_RESOURCE})
MAX_SKILL_BYTES = 128 * 1024
MAX_RESOURCE_BYTES = 64 * 1024
_SKILLS: list[dict] = []


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    match = re.match(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        raise ValueError("技能必须包含完整的 --- frontmatter")
    meta = {}
    for line in match.group(1).splitlines():
        if line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("\"'")
        meta[key.strip()] = (
            [t.strip() for t in value.split(",") if t.strip()]
            if key.strip() == "tools" else value
        )
    return meta, match.group(2).strip()


def _read_metadata(path: Path) -> dict:
    """只读 frontmatter；遇到结束分隔符立即停止，不读取正文。"""
    with path.open("rb") as stream:
        first = stream.readline(8193).decode("utf-8-sig")
        if first.strip() != "---":
            raise ValueError(f"技能 {path.name} 缺少 frontmatter")
        header = first
        while len(header) <= 8192:
            raw = stream.readline(8193)
            if not raw:
                break
            line = raw.decode("utf-8")
            header += line
            if line.strip() == "---":
                return _parse_frontmatter(header)[0]
    raise ValueError(f"技能 {path.name} 的 frontmatter 未闭合或过长")


def _load_skills() -> None:
    """原子替换目录索引；缺 tools 表示不授予业务工具，不表示全部开放。"""
    catalog = []
    names = set()
    root = SKILLS_DIR.resolve()
    for path in sorted([*SKILLS_DIR.glob("*.md"), *SKILLS_DIR.glob("*/SKILL.md")]):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("技能文件不能指向技能目录之外")
        meta = _read_metadata(resolved)
        name = meta.get("name", "").strip()
        description = meta.get("description", "").strip()
        if not name or not description or name in names:
            raise ValueError(f"技能 {path.name} 缺少名称/简介，或名称重复")
        names.add(name)
        catalog.append({
            "name": name,
            "description": description,
            "tools": tuple(meta.get("tools", ())),
            "path": resolved,
            "resource_dir": resolved.parent if path.name == "SKILL.md" else resolved.with_suffix(""),
        })
    _SKILLS[:] = catalog


def get_skill(name: str) -> dict | None:
    return next((s for s in _SKILLS if s["name"] == name), None)


def get_metadata_section() -> str:
    """Level 1：只暴露名称和简介，正文由模型按需请求。"""
    catalog = "\n".join(f"· {s['name']}：{s['description']}" for s in _SKILLS)
    return (
        "\n\n【技能目录：仅名称与简介】\n" + catalog + "\n"
        "涉及某个技能的任务，先用 load_skill(name) 加载该技能，再按返回的正文执行。"
        "不需要业务技能的日常聊天，群聊仍须使用本轮提供的 reply/skip 决定参与；私聊可直接回答。需要额外能力时继续加载对应技能，"
        "不能仅凭旧对话或目录简介假定技能已加载。\n"
        "业务工具只开放已加载技能声明且配置允许的范围；多个已加载技能取并集，"
        "不是互相排斥的限制。load_skill/read_skill_resource 及适用的 reply/skip 是系统控制工具。\n"
        "正文提到附加文件时，按需用 read_skill_resource(name, path) 读取；不要猜文件内容。\n"
    )


def _read_text(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"文件超过 {limit} 字节读取上限")
    return data.decode("utf-8-sig")


class SkillSession:
    """每轮独占：已加载技能及其工具范围，不跨群、跨轮共享权限。"""

    def __init__(self, enabled_tools: set[str], registered_tools: set[str]):
        self.enabled_tools = frozenset(enabled_tools)
        self.registered_tools = frozenset(registered_tools)
        self.loaded: dict[str, dict] = {}

    def allowed_tools(self, controls: set[str]) -> set[str]:
        names = set(controls)
        for skill in self.loaded.values():
            names.update(skill["tools"])
        return names & self.enabled_tools

    def load(self, name: str) -> str:
        if name in self.loaded:
            return f"技能「{name}」本轮已经加载，继续使用已有指引。"
        skill = get_skill(name)
        if skill is None:
            return f"没有技能「{name}」，请使用目录中的完整名称。本次没有开放任何工具。"
        unknown = set(skill["tools"]) - self.registered_tools
        if unknown:
            return f"技能「{name}」声明了未注册工具：{', '.join(sorted(unknown))}；加载失败，未开放工具。"
        meta, body = _parse_frontmatter(_read_text(skill["path"], MAX_SKILL_BYTES))
        if meta.get("name") != name or tuple(meta.get("tools", ())) != skill["tools"]:
            raise ValueError("技能元数据已变化，请重启以刷新技能目录")
        if not body:
            raise ValueError("技能正文为空，未开放工具")
        enabled = set(skill["tools"]) & self.enabled_tools
        disabled = set(skill["tools"]) - self.enabled_tools
        # 正文完整读出、校验通过才更新本轮权限，失败不会残留授权。
        self.loaded[name] = skill
        note = f"本技能当前开放工具：{', '.join(sorted(enabled)) or '无'}。"
        if disabled:
            note += f" 配置或当前通道未开放：{', '.join(sorted(disabled))}，不要调用或编造其结果。"
        return f"【已加载技能：{name}】\n{note}\n多个已加载技能的工具可组合使用；新增工具从下一次模型请求起生效。\n\n{body}"

    def read_resource(self, name: str, path: str) -> str:
        skill = self.loaded.get(name)
        if skill is None:
            return f"请先 load_skill 加载「{name}」，再读取其附加文件。"
        relative = PurePosixPath(path.replace("\\", "/"))
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or relative.parts[0] not in {"references", "scripts"}):
            return "只允许读取本技能 references/ 或 scripts/ 内的相对路径，禁止路径越界。"
        root = skill["resource_dir"].resolve()
        target = (root / relative).resolve()
        if not root.is_relative_to(SKILLS_DIR.resolve()) or not target.is_relative_to(root):
            return "附加文件路径越界，拒绝读取。"
        content = _read_text(target, MAX_RESOURCE_BYTES)
        return f"【技能附加文本：{name}/{relative}】\n以下只作为文本读取，脚本不会执行。\n\n{content}"


_load_skills()
