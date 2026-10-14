"""
图片类 Tool Provider
提供 describe_image（DeepSeek 视觉图片描述）、search_images（官方搜索+页面提取图片）、
text_to_image（Qwen-Image 文生图）三个工具。
"""
from ..vision_utils import describe_image, text_to_image

from . import ToolProvider
from .search_provider import extract_page_images, official_search_results


class ImageProvider(ToolProvider):
    name = "image"
    description = "图片相关工具：describe_image（看图）、search_images（图片搜索）、text_to_image（文生图）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "describe_image",
                "description": (
                    "用视觉模型（DeepSeek）分析图片：结果第一行会判明这是"
                    "「表情包（用户想表达情绪或态度）」还是「内容图片（照片/截图/图表，"
                    "想让你看懂里面的信息）」，再给文字提取、场景与物体描述；"
                    "支持 JPEG/PNG/GIF/WebP，GIF 动图会看到画面里的动作变化。"
                    "当用户发了图片、或询问图片相关内容时使用。"
                    "注意：群里用户刚发的图片通常已带着描述进了上下文，先看上下文，"
                    "别对同一张图反复调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "图片的完整 URL 地址（http/https 直链）",
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
                    "搜索互联网上的图片（官方搜索+从来源页提取图片直链）。"
                    "返回图片标题、图片直链和来源页面链接。"
                    "用于用户想看某个事物的图片、找素材、找参考图等场景。"
                    "将图片直链用 [img]URL[/img] 格式集中放在回复最前面（最多3张），"
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
                            "description": "返回结果数量，默认 3，最多 6",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "handler": self._search_images,
                "max_calls": 3,
            },
            {
                "name": "text_to_image",
                "description": (
                    "根据文字描述生成图片（文生图）。"
                    "当用户想让你「画一张图」「生成一张图」「画个 XX」等需要 AI 创作图片时使用。"
                    "生成后把图片直链用 [img]URL[/img] 格式放在回复最前面，"
                    "系统会自动发送图片到群聊，文字部分简要说明即可。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "图片描述提示词（中英文均可），尽量具体描述画面内容、风格、氛围",
                        },
                        "size": {
                            "type": "string",
                            "description": "输出分辨率，如 1024*1024、1280*720、720*1280（可选，默认 1024*1024）",
                        },
                        "n": {
                            "type": "integer",
                            "description": "生成图片数量，1~6，默认 1",
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                "handler": self._text_to_image,
                "max_calls": 2,
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
    def _search_images(query: str, count: int = 3) -> str:
        """
        官方 web search 找相关页面 → 抓取页面提取图片直链。
        """
        count = min(max(count, 1), 6)
        try:
            # 附加"图片"让官方搜索优先命中图片类页面；多取一些页面提高提取成功率
            pages = official_search_results(f"{query} 图片", max_results=count * 2)
            if not pages:
                return f"未找到「{query}」的相关图片。"

            lines = []
            for page in pages:
                if len(lines) >= count:
                    break
                title = (page.get("title") or "图片").strip()
                page_url = page.get("url", "")
                imgs = extract_page_images(page_url, max_images=1)
                if not imgs:
                    continue  # 该页提取不到图就跳过，试下一个
                lines.append(
                    f"- {title}\n"
                    f"   图片直链: {imgs[0]}\n"
                    f"   来源页面: {page_url}"
                )

            if not lines:
                return f"未找到「{query}」的相关图片。"
            return "\n\n".join(lines)
        except Exception as e:
            return f"图片搜索失败：{e}"

    # --- text_to_image ---
    @staticmethod
    def _text_to_image(prompt: str, size: str = "1024*1024", n: int = 1) -> str:
        urls = text_to_image(prompt, size=size, n=n)
        if not urls:
            return "文生图服务未配置或生成失败，请稍后重试。"
        lines = [f"[img]{u}[/img]" for u in urls]
        lines.append(f"已生成 {len(urls)} 张图片。")
        return "\n".join(lines)
