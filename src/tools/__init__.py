"""
Tool Registry — 工具管理中心

负责：
- 注册、管理所有 Provider 提供的工具
- 按 Skill 白名单过滤工具
- 统一的工具执行入口
- 生成 OpenAI Tool Calling 格式定义

架构：Provider（按类别实现工具）→ Registry（统一注册+白名单）→ LLM
"""
import json
from typing import Callable

# ============================================
# Tool Provider 基类
# ============================================
class ToolProvider:
    """工具提供者基类。每个 Provider 负责一类工具的实现和定义。"""

    name: str = ""      # 如 "search"
    description: str = ""  # 如 "搜索相关工具"

    def get_tools(self) -> list[dict]:
        """
        返回该 Provider 提供的所有工具元信息列表。
        每项格式: {"name": str, "description": str, "parameters": dict, "handler": callable}
        """
        raise NotImplementedError

    def get_tool_names(self) -> list[str]:
        """返回该 Provider 提供的所有工具名"""
        return [t["name"] for t in self.get_tools()]


# ============================================
# Tool Registry 管理中心
# ============================================
class ToolRegistry:
    """统一管理所有 Provider 的工具注册、白名单过滤、执行调度。"""

    def __init__(self):
        self._providers: list[ToolProvider] = []
        self._tool_map: dict[str, tuple[dict, Callable]] = {}  # name → (def, handler)

    def register_provider(self, provider: ToolProvider):
        """注册一个 Provider，将其所有工具加入注册表"""
        self._providers.append(provider)
        for tool_meta in provider.get_tools():
            name = tool_meta["name"]
            definition = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_meta["description"],
                    "parameters": tool_meta["parameters"],
                },
            }
            self._tool_map[name] = (definition, tool_meta["handler"])
        print(f"[ToolRegistry] 注册 Provider「{provider.name}」 -> 工具: {provider.get_tool_names()}")

    def get_definitions(self, whitelist: set[str] | None = None) -> list[dict]:
        """
        获取 OpenAI Tool Calling 格式的工具定义列表。
        whitelist: 可选白名单（工具名集合），只返回白名单内的工具。None 则返回全部。
        """
        if whitelist is not None:
            return [defn for name, (defn, _) in self._tool_map.items() if name in whitelist]
        return [defn for defn, _ in self._tool_map.values()]

    def execute(self, tool_name: str, arguments: str) -> str:
        """执行指定工具。返回执行结果字符串。"""
        entry = self._tool_map.get(tool_name)
        if entry is None:
            return f"未知工具：{tool_name}"
        _, handler = entry
        try:
            args = json.loads(arguments)
            return handler(**args)
        except Exception as e:
            return f"工具执行失败：{e}"

    def get_all_provider_names(self) -> list[str]:
        return [p.name for p in self._providers]


# ============================================
# 全局单例
# ============================================
_registry = ToolRegistry()

# 加载 Provider（新增 Provider 在这里注册）
from .search_provider import SearchProvider  # noqa: E402
from .image_provider import ImageProvider  # noqa: E402
from .bilibili_provider import BilibiliProvider  # noqa: E402
from .profile_provider import ProfileProvider  # noqa: E402

_registry.register_provider(SearchProvider())
_registry.register_provider(ImageProvider())
_registry.register_provider(BilibiliProvider())
_registry.register_provider(ProfileProvider())


# ============================================
# 对外接口（兼容旧调用方式）
# ============================================
def get_definitions(whitelist: set[str] | None = None) -> list[dict]:
    """获取所有已注册工具的 OpenAI Tool Calling 定义"""
    return _registry.get_definitions(whitelist=whitelist)


def execute(tool_name: str, arguments: str) -> str:
    """执行指定工具"""
    return _registry.execute(tool_name, arguments)
