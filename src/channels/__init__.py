"""
通道适配层。

对外只暴露三样东西：
- IncomingMessage：通道无关的入站消息契约
- Sender：通道无关的发送接口
- handle_message：共享业务核心

注意：官方通道（channels/official.py）带有 import 期的 monkeypatch，
**不要**在这里导入它，应由 main.py 在启用官方通道时延迟导入。
"""
from .base import IncomingMessage, Sender, truncate_text
from .core import handle_message

__all__ = ["IncomingMessage", "Sender", "truncate_text", "handle_message"]
