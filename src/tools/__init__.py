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
import math
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ChatContext:
    """
    一轮会话的**显式**上下文：会话 id + 说话人 +（可选）本轮表态箱。

    为什么必须有它：注册表是全局单例，而工具 handler 需要知道"现在是哪个群、谁在说话"。
    过去这些信息放在模块级全局变量里（user_profile / campaign_state 的 _current_group_id
    等），而不同群是可以并发跑同一轮 chat 的（通道层只做同会话串行 + 全局并发上限），
    于是 A 群在 await 处让出、B 群把全局覆盖成 B 之后，A 群的工具调用就写进了 B 群的存档。
    改成随调用链显式传递后，工具层再也拿不到"别人的"会话来误写。

    outbox 只对表态类工具（reply / skip）有意义：它们改的是本轮要不要开口，
    其余工具既不读也不该读它。
    """

    conv_id: str = ""
    user_id: str = ""
    user_name: str = ""
    outbox: object | None = None
    extra: dict = field(default_factory=dict)
    # None 仅供受信的本地直接调用；模型调用必须给当次请求的快照（空集合即全拒）。
    allowed_tools: frozenset[str] | None = None

    def actor(self, source_message_id=""):
        actors = self.extra.get("actors", {})
        if source_message_id:
            if source_message_id not in actors:
                raise ValueError("source_message_id 不在本轮可信消息快照中")
            return actors[source_message_id]
        if len({uid for uid, _ in actors.values()}) > 1:
            raise ValueError("本轮有多位作者，写状态时必须指定 source_message_id")
        return self.user_id, self.user_name


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
                  "tag": str（可选，中文进度标签）, "max_calls": int（可选，默认 5）, "needs_context": bool（可选，默认 False）,
                  "hidden": bool（可选，默认 False）}

        - needs_context=True：handler 的第一个参数是**调用方每轮传入的 ChatContext**
          （会话 id + 说话人 + 表态箱）。画像、跑团工具靠它拿到"现在服务的是哪个群/谁"
          （不再读模块级全局变量 —— 那样跨群并发时会写错档）；reply / skip 靠它的 outbox
          记本轮表态。
        - hidden=True：不进默认工具列表，只由调用方按白名单显式注入（reply / skip 就是：
          它们不该出现在私聊、跑团这些不打算让 AI 表态的路径上）。
          这两件事互不包含：画像/跑团工具 needs_context=True 但 hidden=False。

        这两个开关也可以写成 Provider 的类属性（``needs_context = True``），
        整批工具口径一致时用类属性更省事，也少一处漏标的机会。
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
        self._tags: dict[str, str] = {}
        # name → (def, handler, max_calls, needs_context, hidden)
        self._tool_map: dict[str, tuple[dict, Callable, int, bool, bool]] = {}

    def register_provider(self, provider: ToolProvider):
        """注册一个 Provider，将其所有工具加入注册表"""
        self._providers.append(provider)
        for tool_meta in provider.get_tools():
            name = tool_meta["name"]
            self._tags[name] = tool_meta.get("tag", "工具")
            definition = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_meta["description"],
                    "parameters": tool_meta["parameters"],
                },
            }
            max_calls = tool_meta.get("max_calls", 5)  # 默认 5 次
            # needs_context / hidden 既可以写在单个工具的元信息里，也可以写成 Provider 的
            # 类属性（整批工具都一样时更省事，也少一处漏标的机会）
            needs_context = bool(tool_meta.get("needs_context", getattr(provider, "needs_context", False)))
            hidden = bool(tool_meta.get("hidden", getattr(provider, "hidden", False)))
            self._tool_map[name] = (
                definition,
                tool_meta["handler"],
                max_calls,
                needs_context,
                # hidden：只在调用方显式按白名单注入时才出现（reply / skip）。
                # 它和 needs_context 是两件事：画像/跑团工具同样需要会话上下文，
                # 但它们必须留在默认工具列表里，而不是被"需要上下文"顺手隐藏掉。
                hidden,
            )
        print(f"[ToolRegistry] 注册 Provider「{provider.name}」 -> 工具: {provider.get_tool_names()}")

    def get_definitions(self, whitelist: set[str] | None = None) -> list[dict]:
        """
        获取 OpenAI Tool Calling 格式的工具定义列表。
        whitelist: 可选白名单（工具名集合），只返回白名单内的工具。
                   None 则返回全部**常规工具** —— 不含 hidden 的控制类工具
                   （reply / skip）：它们只在普通群聊唤醒路径显式注入，绝不能混进
                   私聊、跑团这些不打算让 AI 表态的路径。
        """
        if whitelist is not None:
            return [
                defn for name, (defn, _, _, _, _) in self._tool_map.items() if name in whitelist
            ]
        return [
            defn for defn, _, _, _, hidden in self._tool_map.values() if not hidden
        ]

    def get_max_calls(self, tool_name: str) -> int:
        """
        获取某个工具的调用次数上限。

        未注册的工具返回 **-1**（不是 0）：调用方普遍写 ``count >= max_calls``，
        过去未注册返回 0 会让 0>=0 成立，模型收到的是"已达到本轮的调用上限（0次）"这种
        假提示，排查会被带偏。返回 -1 让调用方必须先判断"有没有这个工具"。
        """
        entry = self._tool_map.get(tool_name)
        if entry is None:
            return -1
        return entry[2]

    def has_tool(self, tool_name: str) -> bool:
        """该工具是否已注册（用于把"未知工具"和"工具已达上限"区分开）"""
        return tool_name in self._tool_map

    def get_tool_names(self) -> list[str]:
        """所有已注册工具名（含 needs_context 的控制类工具）"""
        return list(self._tool_map.keys())

    def needs_context(self, tool_name: str) -> bool:
        """该工具是否需要调用方提供会话上下文（handler 的第一个参数）"""
        entry = self._tool_map.get(tool_name)
        return bool(entry[3]) if entry else False

    @staticmethod
    def _prepare_args(definition: dict, args: dict) -> tuple[dict | None, str | None]:
        """
        按 schema 归一化模型给的参数：丢掉多余字段、把 "5" 这类数字字符串转成数字。

        两件事都是必需的：
        - 模型偶尔会多给一个幻觉字段。schema 里写了 additionalProperties: false，但注册表
          过去并不执行它，``handler(**args)`` 会直接 TypeError，这次调用白费；
        - 模型也常把数字写成字符串（"5"、radius="3000"），而 handler 里 min/max/int 之类的
          运算遇到字符串同样 TypeError。

        返回 (干净参数, 错误说明)；错误说明非空时调用方直接把它回给模型。
        """
        params = (definition.get("function") or {}).get("parameters") or {}
        properties = params.get("properties") or {}
        missing = set(params.get("required", ())) - set(args)
        if missing:
            return None, "缺少必填参数：" + ", ".join(sorted(missing))
        clean: dict = {}
        dropped: list[str] = []
        for key, value in args.items():
            if properties and key not in properties:
                dropped.append(key)
                continue
            expect = (properties.get(key) or {}).get("type")
            if isinstance(value, float) and not math.isfinite(value):
                return None, f"参数 {key} 必须是有限数字"
            if isinstance(value, bool):          # bool 是 int 的子类，别被当成数字转换
                if expect != "boolean":
                    return None, f"参数 {key} 类型错误，不接受布尔值"
            elif expect == "integer" and isinstance(value, str):
                try:
                    value = int(value.strip())
                except ValueError:
                    return None, f"参数 {key} 需要整数，收到的是字符串 {value!r}"
            elif expect == "integer" and isinstance(value, float):
                # 5.0 / 5.7 都收：向下取整。留着小数，下游 min()/max() 会算出小数
                # （page_size=5.7 之类）再被接口拒掉，报错信息还看不出是这里的问题。
                if not value.is_integer():
                    return None, f"参数 {key} 必须是整数"
                value = int(value)
            elif expect == "number" and isinstance(value, str):
                try:
                    value = float(value.strip())
                except ValueError:
                    return None, f"参数 {key} 需要数字，收到的是字符串 {value!r}"
            elif expect == "string" and isinstance(value, (int, float)):
                value = str(value)          # 模型常把 "5" 写成 5，转回字符串即可
            elif expect == "boolean" and isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "false"):
                    value = low == "true"
            elif expect == "array" and not isinstance(value, list):
                return None, f"参数 {key} 需要数组（JSON array），收到的是 {type(value).__name__}"
            elif expect == "object" and not isinstance(value, dict):
                return None, f"参数 {key} 需要对象（JSON object），收到的是 {type(value).__name__}"
            elif expect == "string" and not isinstance(value, str):
                # 类型明显不对就别让它进 handler：那边多半是 keywords.strip() 之类的调用，
                # 崩出来的 AttributeError 对模型毫无信息量（"list 没有 strip"）。
                return None, f"参数 {key} 需要字符串，收到的是 {type(value).__name__}"
            schema = properties.get(key) or {}
            if expect == "boolean" and not isinstance(value, bool):
                return None, f"参数 {key} 必须是布尔值"
            if expect in ("integer", "number"):
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    return None, f"参数 {key} 必须是有限数字"
                if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
                    return None, f"参数 {key} 超出允许范围"
            if "enum" in schema and value not in schema["enum"]:
                return None, f"参数 {key} 可选值为 {schema['enum']}"
            if expect == "array":
                item_type = schema.get("items", {}).get("type")
                if item_type == "string" and any(not isinstance(item, str) for item in value):
                    return None, f"参数 {key} 的元素必须是字符串"
            clean[key] = value
        if dropped:
            print(f"[ToolRegistry] 已忽略模型多给的参数：{dropped}")
        return clean, None

    def execute(self, tool_name: str, arguments: str, context=None) -> str:
        """
        执行指定工具。返回执行结果字符串。

        context.allowed_tools 非 None 时对所有工具执行权限校验（包含不需要上下文的工具）。
        context: 会话上下文（ChatContext），只有 needs_context 的工具会收到它（作为 handler
                 的第一个参数）；其余工具忽略。缺上下文时不执行，直接告诉模型
                 （宁可这轮不表态，也不写错地方）。
        """
        from .. import runtime
        runtime.check()
        allowed = getattr(context, "allowed_tools", None)
        if allowed is not None and tool_name not in allowed:
            return f"工具「{tool_name}」未在本次模型请求中开放，本次调用没有执行。请先加载对应技能，并等待下一次请求。"
        entry = self._tool_map.get(tool_name)
        if entry is None:
            return f"未知工具：{tool_name}"
        definition, handler, _, needs_context, _ = entry
        try:
            args = json.loads(arguments or "{}")
        except ValueError as e:
            return f"工具「{tool_name}」的参数不是合法 JSON（{e}），本次调用没有执行。"
        if not isinstance(args, dict):
            return f"工具「{tool_name}」的参数需要是 JSON 对象，本次调用没有执行。"
        args, arg_error = self._prepare_args(definition, args)
        if arg_error:
            return f"工具「{tool_name}」的参数有问题：{arg_error}，本次调用没有执行。"
        lease = runtime.current()
        if lease:
            from copy import deepcopy
            savepoint = deepcopy(lease.writes), list(lease.after_commit), lease.reset_history
        try:
            if needs_context:
                if context is None:
                    return f"工具「{tool_name}」需要会话上下文，本轮没有可用的上下文，本次调用没有执行。"
                return handler(context, **args)
            return handler(**args)
        except Exception as e:
            if lease:
                lease.writes, lease.after_commit, lease.reset_history = savepoint
            # 异常原文（可能带函数签名、调用参数、本机文件路径）只留在控制台，
            # 回喂给模型的是脱敏版本：模型能据此换个参数重试，但拿不到内部细节。
            print(f"[ToolRegistry] 工具「{tool_name}」执行异常：{type(e).__name__}: {e}")
            if isinstance(e, (ValueError, KeyError)):
                detail = str(e)[:200]
            else:
                detail = type(e).__name__
            return f"工具「{tool_name}」执行失败（{detail}），本次本地状态提交已取消。外部请求可能已经发生。"

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
from .navi_provider import NaviProvider  # noqa: E402
from .chat_provider import ChatProvider  # noqa: E402
from .skill_provider import SkillProvider  # noqa: E402

_registry.register_provider(SearchProvider())
_registry.register_provider(ImageProvider())
_registry.register_provider(VideoProvider())
_registry.register_provider(ProfileProvider())
_registry.register_provider(CampaignProvider())
_registry.register_provider(NaviProvider())
_registry.register_provider(ChatProvider())
_registry.register_provider(SkillProvider())


# ============================================
# 对外接口（兼容旧调用方式）
# ============================================
def get_definitions(whitelist: set[str] | None = None) -> list[dict]:
    """获取所有已注册工具的 OpenAI Tool Calling 定义"""
    return _registry.get_definitions(whitelist=whitelist)


def get_max_calls(tool_name: str) -> int:
    """获取某个工具的调用次数上限（未注册返回 -1）"""
    return _registry.get_max_calls(tool_name)


def has_tool(tool_name: str) -> bool:
    """该工具是否已注册"""
    return _registry.has_tool(tool_name)


def get_tool_names() -> list[str]:
    """注册工具名，用于校验技能声明；不代表本轮授权。"""
    return _registry.get_tool_names()


def execute(tool_name: str, arguments: str, context=None) -> str:
    """执行指定工具（needs_context 的工具由 context 传入本轮会话上下文）"""
    return _registry.execute(tool_name, arguments, context=context)


def needs_context(tool_name: str) -> bool:
    """该工具 handler 是否需要显式 ChatContext（与是否隐藏/授权互相独立）。"""
    return _registry.needs_context(tool_name)


def get_tag(tool_name: str) -> str:
    """Internal progress label, never added to the model tool schema."""
    return _registry._tags.get(tool_name, "工具")
