"""
图片类 Tool Provider
提供 describe_image（DeepSeek 视觉图片描述）、search_images（官方搜索+页面提取图片）、
text_to_image（Qwen-Image 文生图）、list_stickers / send_sticker（本地表情包库）五个工具。

这里只做工具的定义与包装：算图逻辑在 ``vision_utils``，表情包库（采集/入库/检索）在 ``stickers_utils``。
``send_sticker`` 挑中后返回一行 ``[img]<库内路径>[/img]``，模型把它原样放进回复，
core 解析 ``[img]`` 时用 ``stickers_utils.resolve`` 认出这是库里的文件，随那句话一起发出去
（用法见 ``src/skills/image.md``）。
"""
from __future__ import annotations

from .. import sticker_rag, stickers_utils
from ..config import STICKER_ENABLE
from ..vision_utils import describe_image, text_to_image, strip_tag_line

from . import ToolProvider
from .search_provider import extract_page_images, official_search_results


class ImageProvider(ToolProvider):
    name = "image"
    description = (
        "图片相关工具：describe_image（看图）、search_images（图片搜索）、text_to_image（文生图）、"
        "list_stickers / send_sticker（本地表情包库）"
    )

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "describe_image",
                "tag": "识图",
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
                "tag": "搜图",
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
                "tag": "生图",
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
            {
                "name": "list_stickers",
                "tag": "表情检索",
                "description": (
                    "看本地表情包库里有什么：现有多少张、常用情绪标签有哪些。"
                    "send_sticker 已支持语义检索，通常不需要先调用；只有想了解库规模或标签风格时再看。"
                    "库是空的就别调了（群里有人发梗图会自动攒进去）。"
                ),
                "parameters": {"type": "object", "properties": {}},
                "handler": self._list_stickers,
                "max_calls": 2,
            },
            {
                "name": "send_sticker",
                "tag": "表情选择",
                "description": (
                    "用语义检索从本地表情包库挑图，返回按相似度排序的 top3 候选。"
                    "用图片表达情绪或玩笑（无语、震惊、好笑、赞同、委屈、卖萌、认输……）。"
                    "每条候选都带 rank、标签和一行 [img]库里的文件路径[/img]；"
                    "随后系统会把这些候选的真实图片交给你看图，核对实际画面和文字后再选；标签可能有误。"
                    "挑最贴合的一条，把它**原样**放进回复里，也可以只发图、不配文字。"
                    "有贴合候选就选一条，无需等用户索要；都不贴合就用文字，不反复检索硬凑。"
                    "认真求助、严肃问答、现实痛苦或冲突、用户要求纯文字时专注文字；轻松吐槽和打趣可以用。"
                    "一轮最多发一张，不连续重复同一张。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "想表达的情绪或场景，如 无语、震惊、点赞、求安慰；"
                                "描述得越具体，语义检索越准"
                            ),
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "handler": self._send_sticker,
                "max_calls": 2,
            },
        ]

    # --- describe_image ---
    @staticmethod
    def _describe_image(url: str, question: str = "") -> str:
        result = describe_image(url, question=question)
        if result is None:
            # 别让模型对着同一个 URL 反复重试（每试一次都要重新下载，还可能白等几十秒）；
            # 失败已进退避，重试也是同样的结果
            return (
                "这张图没能读到（视觉服务暂时不可用，或这张图取不到）。"
                "不要换 URL 反复重试，也不要猜图里的内容：按「没看到图」继续回应，"
                "需要图才能答就直接说明没看清、请对方描述一下。"
            )
        return strip_tag_line(result)

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
            if pages is None:
                # 搜索本身失败 ≠ 没有图片：说清楚，模型才知道该重试而不是换个说法再搜一遍
                return f"图片搜索失败：搜索服务这次没有返回结果（可能是网络或鉴权问题），不是「没有图片」。可以稍后重试。"
            if not pages:
                return f"未找到「{query}」的相关图片。"

            lines = []
            failed_pages = 0
            for page in pages:
                if len(lines) >= count:
                    break
                title = (page.get("title") or "图片").strip()
                page_url = page.get("url", "")
                imgs = extract_page_images(page_url, max_images=1)
                if imgs is None:
                    failed_pages += 1
                    continue  # 这一页抓取失败，试下一个
                if not imgs:
                    continue  # 这一页确实没有可用的图
                lines.append(
                    f"- {title}\n"
                    f"   图片直链: {imgs[0]}\n"
                    f"   来源页面: {page_url}"
                )

            if not lines:
                if failed_pages and failed_pages == len(pages):
                    return (f"图片搜索失败：{len(pages)} 个结果页都抓取失败（网络问题？），"
                            f"不是「没有图片」，可以稍后重试。")
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

    # --- list_stickers / send_sticker（库里那套东西在 stickers_utils） ---
    @staticmethod
    def _list_stickers() -> str:
        if not STICKER_ENABLE:
            return "表情包功能当前是关闭的，别再用这个工具了。"
        summary = stickers_utils.tag_summary(limit=20)
        if not summary:
            return "表情包库现在是空的（群里有人发梗图时会自动攒进去），暂时没有图可发。"
        return summary + "想发的时候调 send_sticker，query 用上面这些标签词最准。"

    @staticmethod
    def _format_candidates(candidates) -> str:
        lines = [
            "按语义相似度选出的候选（随后先看真实图片核对内容，不只凭标签；确认贴合后再选一条[img]）："
        ]
        for candidate in candidates:
            lines.append(f"{candidate.rank}. [img]{candidate.path}[/img]")
            tags = "、".join(candidate.tags) or "无"
            lines.append(f"   标签：{tags} ｜ 相似度：{candidate.score:.2f}")
        lines.append("有贴合候选就选一条放进最终回复，可只发图或配一句短话；都不贴合则用文字。一轮最多一张。")
        return "\n".join(lines)

    @staticmethod
    def _send_sticker(query: str) -> str:
        if not STICKER_ENABLE:
            return "表情包功能当前是关闭的，别再用这个工具了。"
        try:
            candidates = sticker_rag.search(query, top_k=3)
        except sticker_rag.RagUnavailable:
            candidates = []
        except Exception as exc:
            print(f"[表情包] 语义检索异常，回退标签匹配：{type(exc).__name__}: {exc}")
            candidates = []
        if candidates:
            print(f"[表情包] 语义检索「{query}」 -> "
                  f"{', '.join(f'{c.rank}.{c.name}({c.score:.2f})' for c in candidates)}")
            return ImageProvider._format_candidates(candidates)

        # 模型未加载、索引未就绪或没有过阈值的结果：回退到原有标签匹配。
        picked = stickers_utils.pick(query)
        if picked is None:
            # 宁可这一轮不发，也不糊一张不相关的图上去；同时掐死"换个词再试一次"的循环
            return (
                f"库里没有和「{query}」贴切的表情包，这张就不发了。"
                "用文字说就行，别为了发表情包反复换关键词硬凑。"
            )
        print(f"[表情包] 标签匹配命中 {picked.name}（{query}）")
        return (
            f"[img]{picked.path}[/img]\n"
            f"挑中「{'、'.join(picked.tags)}」（{picked.name}）。"
            "把上面那一行 [img]…[/img] 原样放进你的回复里，通道会先发文字，再把表情包单独发成一条消息"
            "（允许回复只有这一行、不配文字；表情包明确发送失败时，通道会自动尝试一次文字兜底）。"
        )
