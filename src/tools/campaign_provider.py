"""
跑团（DM）Tool Provider

提供一个工具：
- manage_campaign：跑团状态管理（建团/进入/退出/更新/记录事实/查看/重开/换风格/设规则/
  调整反悔次数）

骰子判定全部由玩家通过 /roll 完成，DM 不代投（roll_dice 工具已移除）。

LLM 依据 dm.md 指引自主调用这些工具维护跑团状态。
"""
import json

from .. import campaign_state
from . import ChatContext, ToolProvider


class CampaignProvider(ToolProvider):
    name = "campaign"
    description = "跑团状态管理工具：manage_campaign（建团/记录/查看跑团状态）"
    # 存档按群分文件、反悔次数按玩家记：必须知道当前是哪个群、谁在说话。
    # 过去靠模块级全局变量（deepseek_client 每轮 set_current_context），
    # 但不同群能并发跑同一轮 chat，A 群的剧情会被写进 B 群的存档。
    needs_context = True

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "manage_campaign",
                "description": (
                    "管理跑团（TRPG）状态。当用户想开始跑团、或 DM 需要记录/更新/查看剧情状态时使用。\n"
                    "【抗幻觉要求】涉及已发生的剧情事实、玩家的决定，必须用 action=log 写入 facts（只增不改）；"
                    "回应玩家前应先 action=view 查看当前状态，不得凭记忆编造。\n\n"
                    "action 取值：\n"
                    "- enter：进入 DM 模式（用户说「来跑团/开团」时调用）\n"
                    "- create：创建新跑团存档（可带 content=存档名）\n"
                    "- update：更新可编辑状态（content 为 JSON 或描述文本）\n"
                    "- log：记录一条剧情事实/玩家决定（只增不改，抗幻觉）\n"
                    "- script：记录静态设定资料（剧本剧情/世界观/规则细则/NPC，只增不改）。玩家指定剧本或规则时，先用 web_search 搜到详情再写入\n"
                    "- view：查看当前跑团完整状态\n"
                    "- reset：清空当前跑团进度重来（remake）\n"
                    "- style：更换 DM 主持风格（content=风格描述）\n"
                    "- rules：设置规则系统（content=规则名，如 D&D 5e / COC / 自定义）\n"
                    "- again_init：设定每位玩家的初始反悔次数（content=次数，默认 1；玩家要求更多时调用）\n"
                    "- again_grant：奖励/扣减某位玩家的反悔次数（content=次数，默认 1；player=玩家名，留空表示全队）\n"
                    "- exit：退出 DM 模式\n\n"
                    "content 参数：视 action 而定，多数 action 需要。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_message_id": {"type": "string", "description": "行动玩家的消息 id；多人上下文写状态必须指定，服务端校验作者"},
                        "section": {"type": "string", "description": "view 分区：state/all/facts/lore/players/roll_log/rules/dm_style，默认 state"},
                        "page": {"type": "integer", "minimum": 1, "description": "view 页码，从 1 开始，每页最多 4000 字"},
                        "action": {
                            "type": "string",
                            "enum": ["enter", "create", "update", "log", "script", "view", "reset", "style", "rules", "again_init", "again_grant", "exit"],
                            "description": "操作类型",
                        },
                        "content": {
                            "type": "string",
                            "description": "操作内容（视 action 而定，可省略）",
                        },
                        "player": {
                            "type": "string",
                            "description": "仅 again_grant 需要：玩家名（如「张三」）。留空或 all 表示全队",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                "handler": self._manage_campaign,
                "max_calls": 5,
            },
        ]

    @staticmethod
    def _manage_campaign(context: ChatContext, action: str, content: str = "", player: str = "", source_message_id: str = "", section: str = "state", page: int = 1) -> str:
        # 群号与说话人一律从本轮上下文取（不读模块级全局变量，避免跨群并发写错存档）
        gid, uid = context.conv_id, context.user_id
        action = (action or "").strip().lower()
        content = (content or "").strip()
        if action != "view":
            uid, actor_name = context.actor(source_message_id)
            campaign_state.ensure_player(gid, uid, actor_name)

        if action == "enter":
            return campaign_state.enter_dm(gid, campaign_name=content)
        elif action == "create":
            if not content:
                return "请提供跑团存档名（content=存档名）。"
            return campaign_state.create_campaign(content, gid)
        elif action == "update":
            if not content:
                return "请提供要更新的状态内容。"
            return campaign_state.update_state(content, gid, uid)
        elif action == "log":
            if not content:
                return "请提供要记录的事实内容。"
            return campaign_state.append_fact(content, gid, uid)
        elif action == "script":
            if not content:
                return "请提供要记录的剧本/设定资料内容。"
            return campaign_state.append_lore(content, gid)
        elif action == "view":
            return campaign_state.view_state(gid, section, page)
        elif action == "reset":
            return campaign_state.remake(gid)
        elif action == "style":
            if not content:
                return "请提供 DM 风格描述。"
            return campaign_state.set_dm_style(content, gid)
        elif action == "rules":
            if not content:
                return "请提供规则系统名称。"
            return campaign_state.set_rules(content, gid)
        elif action == "again_init":
            # 玩家说「反悔次数再多点」时用；内容为空按默认 1 次处理
            return campaign_state.set_initial_again(content or "1", gid)
        elif action == "again_grant":
            return campaign_state.grant_again(content or "1", player, gid)
        elif action == "exit":
            return campaign_state.exit_dm(gid)
        else:
            return (
                f"未知操作：{action}，支持 enter/create/update/log/script/view/reset/"
                "style/rules/again_init/again_grant/exit"
            )


def _view_state(group_id: str, section="state", page=1) -> str:
    """查看存储内容的一页，独立于截断的提示摘要。"""
    return campaign_state.view_state(group_id, section, page)
