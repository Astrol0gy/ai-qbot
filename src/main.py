"""
QQ 群聊机器人 - 入口文件

启动方式（在项目根目录执行）：
    python -m src.main

支持两条接入通道，至少启用一条：

  官方通道（OFFICIAL_ENABLE）
    QQ 开放平台机器人，只能收到 @机器人 的消息。
    channels/official.py 在 import 时会对 botpy 打 monkeypatch，因此这里延迟导入，
    纯 OneBot 部署无需安装/配置官方凭据。

  OneBot 通道（ONEBOT_ENABLE）
    对接 NapCatQQ 等协议端，能收到群内全部消息，无需 @。
    是否响应由 src/trigger.py 的触发策略决定。

两条通道共用同一套业务核心 channels/core.py，
指令、工具、跑团、用户画像等能力完全一致。

前提：已正确填写项目根目录的 .env 文件中的所有配置项。
"""
import asyncio
import sys

# Windows 控制台/重定向到文件时默认可能是 GBK，而本项目日志含 emoji，
# 打印会抛 UnicodeEncodeError 直接中断进程（重连循环里尤其致命）。
# 这里统一把标准输出改为 UTF-8，无法编码的字符用 ? 代替而不是崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .config import OFFICIAL_ENABLE, ONEBOT_ENABLE, validate


async def _run_official_channel() -> None:
    from .channels.official import run_official

    await run_official()


async def _run_onebot_channel() -> None:
    from .channels.onebot import run_onebot

    await run_onebot()


async def main() -> None:
    validate()  # 启动前检查配置

    channel_names = []
    tasks = []
    if OFFICIAL_ENABLE:
        channel_names.append("官方 botpy")
        tasks.append(_run_official_channel())
    if ONEBOT_ENABLE:
        channel_names.append("OneBot")
        tasks.append(_run_onebot_channel())

    print(f"🚀 启动通道：{' + '.join(channel_names)}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 已手动停止。")
