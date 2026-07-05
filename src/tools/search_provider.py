"""
搜索类 Tool Provider
提供 web_search（Bing 搜索）和 fetch_url（网页抓取）两个工具。
"""
import requests
from bs4 import BeautifulSoup

from . import ToolProvider

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class SearchProvider(ToolProvider):
    name = "search"
    description = "搜索相关工具：web_search（Bing 搜索）、fetch_url（网页抓取）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "web_search",
                "description": (
                    "搜索互联网获取信息。只返回标题、摘要和链接，不包含完整内容。"
                    "若摘要信息不足，请用 fetch_url 打开具体链接获取全文。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词（使用更具体的关键词以获得精确结果，如'成都 2026年7月5日 天气'而非'成都天气'）",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "handler": self._web_search,
            },
            {
                "name": "fetch_url",
                "description": (
                    "抓取指定网页的完整文字内容。"
                    "当 web_search 返回的摘要信息不够详细时，用此工具打开具体页面获取完整数据（如天气详情、新闻全文等）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要抓取的网页完整URL",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "handler": self._fetch_url,
            },
        ]

    @staticmethod
    def _web_search(query: str) -> str:
        """使用 Bing 搜索"""
        try:
            resp = requests.get(
                "https://www.bing.com/search",
                params={"q": query, "count": 5},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            results = []
            for item in soup.select("li.b_algo")[:5]:
                title_el = item.select_one("h2 a")
                snippet_el = item.select_one(".b_caption p")
                title = title_el.get_text(strip=True) if title_el else ""
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                href = title_el.get("href", "") if title_el else ""
                if title:
                    results.append(f"{len(results) + 1}. {title}\n   {snippet[:200]}\n   {href}")

            if not results:
                return "未找到相关搜索结果。"
            return "\n\n".join(results)
        except Exception as e:
            return f"搜索失败：{e}"

    @staticmethod
    def _fetch_url(url: str) -> str:
        """抓取网页并提取文字内容"""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")

            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)
            lines = [line for line in text.splitlines() if line.strip()]
            text = "\n".join(lines)

            if len(text) > 3000:
                text = text[:3000] + "\n...(内容过长，已截断)"

            return text if text else "页面无有效文字内容。"
        except Exception as e:
            return f"抓取失败：{e}"
