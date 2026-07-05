"""
图片类 Tool Provider
提供 describe_image（VLM 图片描述）、search_images（Bing 图片搜索）两个工具。
"""
import json
import re

import requests
from bs4 import BeautifulSoup

from ..vision_utils import describe_image

from . import ToolProvider

# --- search_images 相关 ---
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TITLE_CLEAN = re.compile(r"\s+的?(图像|图片|照片)结果$")


class ImageProvider(ToolProvider):
    name = "image"
    description = "图片相关工具：describe_image（VLM 图片识别）、search_images（Bing 图片搜索）"

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
                "max_calls": 3,
            },
            {
                "name": "search_images",
                "description": (
                    "搜索互联网上的图片。返回图片标题、图片直链(murl)和来源页面链接(purl)。"
                    "用于用户想看某个事物的图片、找素材、找参考图等场景。"
                    "将图片 murl 用 [img]URL[/img] 格式集中放在回复最前面（最多3张），"
                    "系统会自动先发送图片再到群聊。文字部分简明介绍即可，不需要逐张编号。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "图片搜索关键词（中英文均可，如 '暹罗猫'、'sunset over mountain'）",
                        },
                        "count": {
                            "type": "integer",
                            "description": "返回结果数量，默认 5，最多 10",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "handler": self._search_images,
                "max_calls": 3,
            },
        ]

    # --- describe_image ---
    @staticmethod
    def _describe_image(url: str, question: str = "") -> str:
        result = describe_image(url, question=question)
        if result is None:
            return "视觉识别服务未配置或暂时不可用，无法分析图片。"
        return result

    # --- search_images ---
    @staticmethod
    def _search_images(query: str, count: int = 5) -> str:
        """使用 Bing 图片搜索"""
        count = min(max(count, 1), 10)
        try:
            resp = requests.get(
                "https://www.bing.com/images/search",
                params={"q": query, "count": count},
                headers=_IMG_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            results = []
            for a_tag in soup.select("a.iusc")[:count]:
                m_data = a_tag.get("m", "")
                try:
                    info = json.loads(m_data)
                except json.JSONDecodeError:
                    continue

                murl = info.get("murl", "")
                purl = info.get("purl", "")
                if not murl:
                    continue

                title = a_tag.get("aria-label", "")
                title = _TITLE_CLEAN.sub("", title).strip()

                idx = len(results) + 1
                lines = [f"{idx}. {title}" if title else f"{idx}. 图片"]
                lines.append(f"   图片链接: {murl}")
                if purl:
                    lines.append(f"   来源页面: {purl}")
                results.append("\n".join(lines))

            if not results:
                return f"未找到「{query}」的相关图片。"
            return "\n\n".join(results)
        except Exception as e:
            return f"图片搜索失败：{e}"
