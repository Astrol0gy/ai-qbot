"""
Bilibili 视频搜索 Tool Provider
提供 search_bilibili_videos：在B站搜索视频并返回列表（标题、UP主、链接等）
"""
import re

import requests

from . import ToolProvider

_BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

# B站搜索用的持久 Session（带 cookie，绕过 412 反爬）
_search_session: requests.Session | None = None


def _get_search_session() -> requests.Session:
    """获取带 cookie 的 session，首次访问 bilibili.com 获取初始 cookie"""
    global _search_session
    if _search_session is None:
        _search_session = requests.Session()
        _search_session.headers.update(_BILIBILI_HEADERS)
        try:
            _search_session.get("https://www.bilibili.com", timeout=10)
        except Exception:
            pass
    return _search_session


class BilibiliProvider(ToolProvider):
    name = "bilibili"
    description = "B站视频搜索工具：search_bilibili_videos（搜索视频）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "search_bilibili_videos",
                "description": (
                    "在 Bilibili（B站）搜索视频。根据用户需求的关键词搜索相关视频，"
                    "返回视频标题、UP主、时长、播放量、链接等信息。"
                    "适用场景：用户想看某类视频、找教程、找相关内容时，用此工具搜索然后返回结果给用户选择。"
                    "搜索结果默认返回前5条，用户可以指定数量。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词，如'Python教程'、'鬼畜'、'前端入门'",
                        },
                        "count": {
                            "type": "integer",
                            "description": "返回结果数量，默认5条，最大20条",
                        },
                    },
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                "handler": self._search_bilibili_videos,
            },
        ]

    @staticmethod
    def _search_bilibili_videos(keyword: str, count: int = 5) -> str:
        """搜索 B 站视频，返回格式化结果列表"""
        count = min(max(count, 1), 20)
        try:
            session = _get_search_session()
            resp = session.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": keyword, "page": 1},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                return f"B站搜索暂时不可用（code={data.get('code')}），请稍后重试。"

            results = data.get("data", {}).get("result", [])
            if not results:
                return f"未找到与「{keyword}」相关的视频，试试换个关键词？"

            total = data["data"].get("numResults", len(results))
            lines = [f"在B站搜索「{keyword}」的结果（共 {total} 个结果）："]

            for i, r in enumerate(results[:count], 1):
                # 去掉搜索高亮标签 <em class="keyword">
                title = re.sub(r"<[^>]+>", "", r.get("title", ""))
                bvid = r.get("bvid", "")
                duration = r.get("duration", "").lstrip("0:")
                play = r.get("play", 0)
                author = r.get("author", "")
                url = f"https://www.bilibili.com/video/{bvid}"

                # 播放量格式化
                if play >= 10000:
                    play_str = f"{play / 10000:.1f}万"
                else:
                    play_str = str(play)

                lines.append(
                    f"{i}. 【{title}】\n"
                    f"   UP主：{author} | 时长：{duration} | 播放：{play_str}\n"
                    f"   链接：{url}"
                )

            return "\n\n".join(lines)
        except Exception as e:
            print(f"[B站] 搜索失败: {e}")
            return f"B站搜索失败：{e}"

