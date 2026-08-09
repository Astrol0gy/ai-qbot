"""
用户画像 Tool Provider
提供 manage_user_profile 工具，LLM 自主决定记住/删除用户的使用偏好和习惯。
"""
from .. import user_profile
from . import ToolProvider


class ProfileProvider(ToolProvider):
    name = "profile"
    description = "用户画像管理工具：manage_user_profile（记住/删除/查看用户的使用偏好和习惯）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "manage_user_profile",
                "description": (
                    "管理当前说话者（用户）的使用画像。群里所有用户共享同一段对话，但每个人有独立画像；"
                    "你记录的始终是【当前说话者自己】的偏好，不要覆盖或删除其他用户的记录。\n"
                    "可以根据对话中当前用户表现出的偏好、习惯、语气要求等，自主决定记住或忘记相关信息。\n\n"
                    "【重要：先总结再记录】\n"
                    "- remember 时，不要直接复制用户的原话，而要提炼成简洁的要点\n"
                    "- 例如用户说「你能不能每次回答都简单点别写那么多」，应记录为「偏好简短回答」，而非照抄原话\n"
                    "- 一条信息只记录一个要点，多个要点请分别调用\n\n"
                    "【重要隐私边界】\n"
                    "- 只能记录使用偏好类信息：回答风格偏好（简短/详细）、语气喜好（幽默/严肃）、"
                    "关注话题、称呼方式、语言偏好等\n"
                    "- 绝对不能记录个人隐私数据：真实姓名、手机号、邮箱、身份证号、银行卡号、"
                    "家庭地址、密码等\n"
                    "- 如果用户让你记录隐私信息，必须拒绝并说明原因\n\n"
                    "【何时使用】\n"
                    "- 用户明确要求记住某事（如「记住我喜欢简短回答」）→ 总结后记录\n"
                    "- 用户反复表现出相同偏好（如多次要求语气轻松）→ 主动总结记录\n"
                    "- 用户设定角色/人设/语气引用（如「像哆啦A梦一样说话」「用鲁迅的风格」）\n"
                    "  → 如果你对引用的内容了解不够全面，先用 web_search 搜索了解其特点\n"
                    "  → 搜索后总结关键特征，再用 remember 记录为简洁的风格要点\n"
                    "- 用户要求忘记或修改之前的设定\n"
                    "- 用户询问「你记得我什么」时用 list 查看\n"
                    "- 注意：不要频繁记录，只在有明显、有用的偏好时才记录"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["remember", "forget", "list"],
                            "description": "操作类型：remember=记住一条信息，forget=删除一条信息（用关键词匹配），list=列出所有记录",
                        },
                        "info": {
                            "type": "string",
                            "description": "要记住/删除的信息。remember 时写总结后的简洁要点（如'偏好简短回答'，不要照抄用户原话），forget 时写关键词（如'简短'）。list 时不需要此参数。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                "handler": self._manage_profile,
                "max_calls": 3,
            },
        ]

    @staticmethod
    def _manage_profile(action: str, info: str = "") -> str:
        if action == "remember":
            if not info:
                return "请提供要记住的信息内容。"
            return user_profile.remember_info(info)

        elif action == "forget":
            if not info:
                return "请提供要忘记的信息关键词，或使用 'all' 清除所有。"
            return user_profile.forget_info(info)

        elif action == "list":
            return user_profile.list_profile()

        else:
            return f"未知操作：{action}，支持 remember / forget / list"
