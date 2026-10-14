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
        每项格式: {"name": str, "description": str, "parameters": dict, "handler": callable,
                  "max_calls": int（可选，默认 5）, "needs_context": bool（可选，默认 False）}

        needs_context=True 表示 handler 的第一个参数是**调用方每轮传入的会话上下文**
        （如 reply / skip 的待发消息箱）：这类工具不产生"给模型看的素材"，只改本轮状态，
        且默认不会出现在 get_definitions() 里，要由调用方按需显式注入。
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
        # name → (def, handler, max_calls, needs_context)
        self._tool_map: dict[str, tuple[dict, Callable, int, bool]] = {}

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
            max_calls = tool_meta.get("max_calls", 5)  # 默认 5 次
            self._tool_map[name] = (
                definition,
                tool_meta["handler"],
                max_calls,
                bool(tool_meta.get("needs_context", False)),
            )
        print(f"[ToolRegistry] 注册 Provider「{provider.name}」 -> 工具: {provider.get_tool_names()}")

    def get_definitions(self, whitelist: set[str] | None = None) -> list[dict]:
        """
        获取 OpenAI Tool Calling 格式的工具定义列表。
        whitelist: 可选白名单（工具名集合），只返回白名单内的工具。
                   None 则返回全部**常规工具** —— 不含 needs_context 的控制类工具
                   （reply / skip）：它们只在普通群聊唤醒路径显式注入，绝不能混进
                   私聊、跑团这些不打算让 AI 表态的路径。
        """
        if whitelist is not None:
            return [
                defn for name, (defn, _, _, _) in self._tool_map.items() if name in whitelist
            ]
        return [
            defn for defn, _, _, needs_context in self._tool_map.values() if not needs_context
        ]

    def get_max_calls(self, tool_name: str) -> int:
        """获取某个工具的调用次数上限。未注册的工具返回 0。"""
        entry = self._tool_map.get(tool_name)
        if entry is None:
            return 0
        return entry[2]

    def needs_context(self, tool_name: str) -> bool:
        """该工具是否需要调用方提供会话上下文（控制类工具）"""
        entry = self._tool_map.get(tool_name)
        return bool(entry[3]) if entry else False

    def execute(self, tool_name: str, arguments: str, context=None) -> str:
        """
        执行指定工具。返回执行结果字符串。

        context: 会话上下文，只有 needs_context 的工具会收到它（作为 handler 的第一个参数）；
                 其余工具忽略。缺上下文时不执行，直接告诉模型（宁可这轮不表态，也不写错地方）。
        """
        entry = self._tool_map.get(tool_name)
        if entry is None:
            return f"未知工具：{tool_name}"
        _, handler, _, needs_context = entry
        try:
            args = json.loads(arguments or "{}")
        except ValueError as e:
            return f"工具「{tool_name}」的参数不是合法 JSON（{e}），本次调用没有执行。"
        if not isinstance(args, dict):
            return f"工具「{tool_name}」的参数需要是 JSON 对象，本次调用没有执行。"
        try:
            if needs_context:
                if context is None:
                    return f"工具「{tool_name}」需要会话上下文，本轮没有可用的上下文，本次调用没有执行。"
                return handler(context, **args)
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
from .video_provider import VideoProvider  # noqa: E402
from .profile_provider import ProfileProvider  # noqa: E402
from .campaign_provider import CampaignProvider  # noqa: E402
from .chat_provider import ChatProvider  # noqa: E402

_registry.register_provider(SearchProvider())
_registry.register_provider(ImageProvider())
_registry.register_provider(VideoProvider())
_registry.register_provider(ProfileProvider())
_registry.register_provider(CampaignProvider())
_registry.register_provider(ChatProvider())


# ============================================
# 对外接口（兼容旧调用方式）
# ============================================
def get_definitions(whitelist: set[str] | None = None) -> list[dict]:
    """获取所有已注册工具的 OpenAI Tool Calling 定义"""
    return _registry.get_definitions(whitelist=whitelist)


def get_max_calls(tool_name: str) -> int:
    """获取某个工具的调用次数上限"""
    return _registry.get_max_calls(tool_name)


def execute(tool_name: str, arguments: str, context=None) -> str:
    """执行指定工具（needs_context 的工具由 context 传入本轮会话上下文）"""
    return _registry.execute(tool_name, arguments, context=context)


def needs_context(tool_name: str) -> bool:
    """该工具是否需要会话上下文（控制类工具：reply / skip）"""
    return _registry.needs_context(tool_name)
