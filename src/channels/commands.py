"""
纯函数式指令处理器（与通道无关）。

这些函数只做本地状态操作，不涉及任何平台 API，
因此可以被任意通道直接复用。
"""
from .. import user_profile
from .. import campaign_state
from ..dice import roll_dice

HELP_TEXT = """【小Q 功能指南】

聊天空谈、查资料、搜图片视频，@我就能帮你～

注意：群聊中所有指令都需要 @我 才会生效。

命令：
  /help  - 查看本帮助
  /reset - 清除对话记忆
  /look <信息> - 让我记住你的偏好（如 /look 我喜欢简短回答）
  /forget <关键词> - 忘记已记录的偏好（/forget all 清除全部）
  /list - 查看已记录的偏好

能力：
  · 联网搜索 - 查天气、新闻、实时信息
  · 图片识别 - 发图让我帮你分析图片内容
  · 图片搜索 - 告诉我你想看什么图
  · 视频搜索 - B站搜视频直达链接
  · 用户画像 - 记住你的偏好，越聊越懂你
  · 跑团主持 - 说「来跑团」让我当你的 DM（地下城主）

跑团命令：
  /roll [骰子]  - 掷骰（默认 1d6，如 /roll 1d20、/roll 3d6），结果会记进存档供 DM 判定
  /campaigns   - 列出所有跑团存档
  /load <名>   - 加载某个跑团存档继续
  /remake      - 清空当前跑团进度重来
  /again       - 撤回上一步决定（DM 记的那条事实/状态改动），消耗反悔次数
  /dmstyle <风格> - 更换 DM 主持风格
  /exit        - 退出跑团模式，回到普通模式"""


def handle_profile_cmd(user_msg: str, group_id: str, user_id: str) -> str | None:
    """
    处理用户画像指令：/forget, /list
    /look 由 LLM 处理器负责（总结提炼后调用 manage_user_profile 工具）
    返回操作结果字符串，非指令时返回 None。
    """
    stripped = user_msg.strip()

    # 用「整词匹配」而不是 startswith：后者会把 /forgetful 也当成 /forget，
    # 再用固定下标切剩下的字符，关键词就变成了 "ul ..." 之类的垃圾，删错记录
    if stripped == "/forget" or stripped.startswith("/forget "):
        info = stripped[len("/forget"):].strip()
        if not info:
            return "请提供要忘记的信息关键词，例如：/forget 简短\n或使用 /forget all 清除所有记录。"
        return user_profile.forget_info(info, group_id, user_id)

    # 同样先去掉首尾空白：别处指令都 strip 过了，只有这里要求严格等于 "/list"，
    # 手滑多打一个空格就毫无反应
    if stripped == "/list":
        result = user_profile.list_profile(group_id, user_id)
        if result == "暂无任何记录。" or result == "暂无记录。":
            return "还没有记录任何关于你的使用偏好。\n你可以用 /look <信息> 来记录，或者我会在对话中自己记住你的偏好。"
        return f"已记录的偏好和习惯：\n{result}"

    return None


def handle_dm_cmd(user_msg: str, group_id: str, user_id: str, user_name: str = "") -> str | None:
    """
    处理跑团（DM）指令：/roll /campaigns /load /remake /again /dmstyle /exit
    返回操作结果字符串，非跑团指令时返回 None。
    """
    stripped = user_msg.strip()

    # /roll [表达式]
    if stripped == "/roll" or stripped.startswith("/roll "):
        expr = stripped[len("/roll"):].strip()
        result = roll_dice(expr)
        # 掷骰结果必须落进存档（DM 模式下）：指令在本地就返回了，不写任何上下文，
        # 而 dm.md 要求 DM 严格按骰点判定 —— 不记下来它根本看不到掷了几点
        saved = campaign_state.record_roll(expr, result, group_id, user_id, user_name)
        return result + (f"\n{saved}" if saved else "")

    # /exit 退出 DM 模式
    if stripped == "/exit":
        return campaign_state.exit_dm(group_id)

    # /campaigns 列出存档
    if stripped == "/campaigns":
        return campaign_state.list_campaigns(group_id)

    # /load <存档名>
    if stripped.startswith("/load "):
        name = stripped[len("/load"):].strip()
        if not name:
            return "请指定要加载的存档名，例如：/load 我的冒险"
        return campaign_state.load_campaign(name, group_id)

    # /remake 清空进度重来
    if stripped == "/remake":
        return campaign_state.remake(group_id)

    # /again 撤回上一步
    if stripped == "/again":
        campaign_state.ensure_player(group_id, user_id, user_name)
        return campaign_state.undo_last(group_id, user_id)

    # /dmstyle <风格>
    if stripped.startswith("/dmstyle "):
        style = stripped[len("/dmstyle"):].strip()
        if not style:
            return "请指定 DM 风格，例如：/dmstyle 悬疑恐怖"
        return campaign_state.set_dm_style(style, group_id)

    return None
