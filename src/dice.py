"""
骰子解析与掷骰模块

支持语法：XdY、XdY+Z、XdY-Z（如 1d20、3d6、2d10+3、1d6-2）
默认表达式：1d6（无参数时）

供两处复用：
- /roll 指令（玩家主动掷骰，main.py 本地确定性执行）
- roll_dice 工具（DM 主动判定，campaign_provider.py）
"""
import random
import re

# 上限保护：防止 9999d9999 之类的表达式撑爆内存
MAX_DICE_COUNT = 20
MAX_DICE_SIDES = 1000

# 骰子表达式正则：XdY[+|-]Z
_DICE_RE = re.compile(r"^(\d*)d(\d+)(?:\s*([+-])\s*(\d+))?$", re.IGNORECASE)


def parse_dice(expression: str) -> tuple[int, int, int] | str:
    """
    解析骰子表达式，返回 (骰子数量, 面数, 修正值) 或错误提示字符串。

    规则：
    - "d20" 等价于 "1d20"（数量省略时为 1）
    - 修正值可正可负，如 "2d10+3"、"1d6-2"
    - 默认（空字符串）视为 "1d6"
    """
    expr = (expression or "").strip()
    if not expr:
        expr = "1d6"

    m = _DICE_RE.match(expr)
    if not m:
        return f"无法识别的骰子表达式「{expression}」。请使用类似 1d20、3d6、2d10+3 的格式。"

    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    modifier = 0
    if m.group(3) and m.group(4):
        sign = 1 if m.group(3) == "+" else -1
        modifier = sign * int(m.group(4))

    if count < 1 or count > MAX_DICE_COUNT:
        return f"骰子数量需在 1~{MAX_DICE_COUNT} 之间，收到 {count} 个。"
    if sides < 1 or sides > MAX_DICE_SIDES:
        return f"骰子面数需在 1~{MAX_DICE_SIDES} 之间，收到 {sides} 面。"

    return (count, sides, modifier)


def roll_dice(expression: str) -> str:
    """
    掷骰并返回纯文本结果（各骰值 + 合计）。
    出错时返回友好提示。
    """
    parsed = parse_dice(expression)
    if isinstance(parsed, str):
        return parsed

    count, sides, modifier = parsed
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier

    # 组装纯文本结果
    dice_part = f"{count}d{sides}"
    rolls_text = " + ".join(str(r) for r in rolls)

    if count == 1 and modifier == 0:
        result = f"掷出了 {rolls[0]} 点（1d{sides}）"
    elif modifier > 0:
        result = f"{dice_part}+{modifier} = {rolls_text} + {modifier} = {total} 点"
    elif modifier < 0:
        result = f"{dice_part}{modifier} = {rolls_text} - {abs(modifier)} = {total} 点"
    else:
        result = f"{dice_part} = {rolls_text} = {total} 点"

    return result
