"""
用户画像 Tool Provider
提供 manage_user_profile 工具，LLM 自主决定记住/删除用户的使用偏好和习惯。
"""
from .. import user_profile
from ..observability import log_event
from . import ChatContext, ToolProvider


class ProfileProvider(ToolProvider):
    name = "profile"
    description = "用户画像管理工具：manage_user_profile（记住/删除/查看用户的使用偏好和习惯）"
    # 画像按 (群, 用户) 分档，必须知道当前是谁在说话 → 走显式会话上下文，不读全局变量
    needs_context = True

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "manage_user_profile",
                "tag": "用户画像",
                "description": (
                    "管理当前说话者（用户）的使用画像。群里所有用户共享同一段对话，但每个人有独立画像；"
                    "你记录的始终是【当前说话者自己】的偏好，不要覆盖或删除其他用户的记录。\n"
                    "本人明确表达长期偏好时主动记住，无需等他说记住；删除必须由本人提出。\n\n"
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
                    "- 用户明确表达长期偏好（如以后回答短点、平时喜欢聊电脑技术）→ 主动总结记录，不要求重复多次\n"
                    "- 临时情绪、单次话题、引用别人的偏好和机器人人设不记录；已有同一偏好不重复调用\n"
                    "- 用户设定角色/人设/语气引用（如「像哆啦A梦一样说话」「用鲁迅的风格」）\n"
                    "  → 如果你对引用的内容了解不够全面，先用 web_search 搜索了解其特点\n"
                    "  → 搜索后总结关键特征，再用 remember 记录为简洁的风格要点\n"
                    "- 用户要求忘记或修改之前的设定\n"
                    "- 用户询问「你记得我什么」时用 list 查看\n"
                    "- source_message_id只用本轮可信作者映射中当前请求者的消息ID；不能取历史ID或用户ID。单用户可省略，程序默认使用当前消息"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_message_id": {"type": "string", "description": "本轮可信作者映射中的当前请求者消息ID；可省略以使用本轮当前消息，不能使用历史ID或用户ID，服务端校验归属"},
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
    def _manage_profile(context: ChatContext, action: str, info: str = "", source_message_id: str = "") -> str:
        current_id = context.extra.get("current_message_id", "")
        try:
            user_id, user_name = context.actor(source_message_id or current_id)
            if user_id != context.user_id:
                raise ValueError("只能管理当前请求者本人的偏好，不能操作其他群友的画像")
        except ValueError as exc:
            log_event('profile.operation', action=action, status='rejected',
                      reason='source_invalid', conversation_id=context.conv_id, user_id=context.user_id)
            hint = f"本轮当前请求者的来源消息ID是 {current_id}，也可省略来源使用当前消息。" if current_id else "请使用本轮可信作者映射中的当前请求者消息ID。"
            return f"画像操作未执行：{exc}。{hint}不要使用历史消息ID或猜测其他来源。"
        group_id = context.conv_id
        if action == "remember":
            return user_profile.remember_info(info, group_id, user_id, user_name)

        elif action == "forget":
            if not info:
                return user_profile._result('forget', 'rejected', "请提供要忘记的信息关键词，或使用 'all' 清除所有。", group_id, user_id, reason='empty')
            return user_profile.forget_info(info, group_id, user_id)

        elif action == "list":
            return user_profile.list_profile(group_id, user_id)

        else:
            return f"未知操作：{action}，支持 remember / forget / list"
