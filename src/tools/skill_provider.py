"""按需读取技能正文与附加文本；权限状态属于当前 ChatContext。"""
from ..skills import LOAD_SKILL, READ_RESOURCE, SkillSession
from . import ChatContext, ToolProvider


class SkillProvider(ToolProvider):
    name = "skill"
    description = "渐进式技能加载"
    needs_context = True

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": LOAD_SKILL,
                "tag": "技能加载",
                "description": "按目录中的完整名称加载一个技能正文；成功后才开放其声明且配置允许的工具。每轮按需加载，可组合多个技能。",
                "parameters": {
                    "type": "object", "properties": {"name": {"type": "string", "description": "技能目录中的完整名称"}},
                    "required": ["name"], "additionalProperties": False,
                },
                "handler": self._load_skill,
                "max_calls": 10,
            },
            {
                "name": READ_RESOURCE,
                "tag": "资料读取",
                "description": "读取已加载技能明确提到的 references/ 或 scripts/ 附加文本，不执行脚本；不能读取其他技能或本机任意文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "已经加载的技能名"},
                        "path": {"type": "string", "description": "包内相对路径，如 references/rules.md"},
                    },
                    "required": ["name", "path"], "additionalProperties": False,
                },
                "handler": self._read_resource,
                "max_calls": 6,
            },
        ]

    @staticmethod
    def _session(context: ChatContext) -> SkillSession:
        session = context.extra.get("skills")
        if not isinstance(session, SkillSession):
            raise ValueError("本轮没有技能加载会话")
        return session

    @classmethod
    def _load_skill(cls, context: ChatContext, name: str) -> str:
        return cls._session(context).load(name)

    @classmethod
    def _read_resource(cls, context: ChatContext, name: str, path: str) -> str:
        return cls._session(context).read_resource(name, path)
