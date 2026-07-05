"""
图片类 Tool Provider
提供 describe_image（VLM 图片描述）工具。
"""
from ..vision_utils import describe_image

from . import ToolProvider


class ImageProvider(ToolProvider):
    name = "image"
    description = "图片相关工具：describe_image（通过视觉模型描述图片内容）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "describe_image",
                "description": (
                    "通过视觉模型（Qwen-VL）分析并描述图片内容。"
                    "支持场景识别、物体检测、文字提取、图表理解等。"
                    "当用户发送图片或询问图片相关内容时使用此工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "图片的完整 URL 地址",
                        },
                        "question": {
                            "type": "string",
                            "description": "关于图片的具体问题（可选，为空则做通用描述）",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "handler": self._describe_image,
            },
        ]

    @staticmethod
    def _describe_image(url: str, question: str = "") -> str:
        result = describe_image(url, question=question)
        if result is None:
            return "视觉识别服务未配置或暂时不可用，无法分析图片。"
        return result
