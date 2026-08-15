"""
跑团（DM）Tool Provider

提供一个工具：
- manage_campaign：跑团状态管理（建团/进入/退出/更新/记录事实/查看/重开/换风格/设规则）

骰子判定全部由玩家通过 /roll 完成，DM 不代投（roll_dice 工具已移除）。

LLM 依据 dm.md 指引自主调用这些工具维护跑团状态。
"""
import json

from .. import campaign_state
from . import ToolProvider


class CampaignProvider(ToolProvider):
    name = "campaign"
    description = "跑团状态管理工具：manage_campaign（建团/记录/查看跑团状态）"

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
                    "- exit：退出 DM 模式\n\n"
                    "content 参数：视 action 而定，多数 action 需要。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["enter", "create", "update", "log", "script", "view", "reset", "style", "rules", "exit"],
                            "description": "操作类型",
                        },
                        "content": {
                            "type": "string",
                            "description": "操作内容（视 action 而定，可省略）",
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
    def _manage_campaign(action: str, content: str = "") -> str:
        action = (action or "").strip().lower()
        content = (content or "").strip()

        if action == "enter":
            return campaign_state.enter_dm(campaign_name=content)
        elif action == "create":
            if not content:
                return "请提供跑团存档名（content=存档名）。"
            return campaign_state.create_campaign(content)
        elif action == "update":
            if not content:
                return "请提供要更新的状态内容。"
            return campaign_state.update_state(content)
        elif action == "log":
            if not content:
                return "请提供要记录的事实内容。"
            return campaign_state.append_fact(content)
        elif action == "script":
            if not content:
                return "请提供要记录的剧本/设定资料内容。"
            return campaign_state.append_lore(content)
        elif action == "view":
            return _view_state()
        elif action == "reset":
            return campaign_state.remake()
        elif action == "style":
            if not content:
                return "请提供 DM 风格描述。"
            return campaign_state.set_dm_style(content)
        elif action == "rules":
            if not content:
                return "请提供规则系统名称。"
            return campaign_state.set_rules(content)
        elif action == "exit":
            return campaign_state.exit_dm()
        else:
            return f"未知操作：{action}，支持 enter/create/update/log/script/view/reset/style/rules/exit"


def _view_state() -> str:
    """查看当前跑团完整状态（供 LLM 读取，抗幻觉）"""
    text = campaign_state.get_state_for_prompt()
    if not text:
        return "当前没有激活的跑团（不在 DM 模式或尚未创建存档）。"
    return text
