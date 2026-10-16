"""
搜索类 Tool Provider
提供 web_search（DeepSeek 官方内置搜索）和 fetch_url（网页抓取）两个工具。
"""
import re

import anthropic
from httpx import Timeout
import requests
from bs4 import BeautifulSoup

from ..config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_ANTHROPIC_BASE_URL,
)
# url 来自模型：抓取前先过公网地址校验（内网/本机/云元数据地址一律拒掉）
from ..net_safety import safe_get

from . import ToolProvider

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 官方 web search 客户端（Anthropic 兼容端点）
_search_client: anthropic.Anthropic | None = None


def _get_search_client() -> anthropic.Anthropic:
    """懒加载官方搜索客户端"""
    global _search_client
    if _search_client is None:
        _search_client = anthropic.Anthropic(
            base_url=DEEPSEEK_ANTHROPIC_BASE_URL,
            api_key=DEEPSEEK_API_KEY,
            timeout=Timeout(connect=5, read=None, write=30, pool=10),
            max_retries=0,
        )
    return _search_client


# 过滤小图标/装饰图（logo、icon、头像等不适合当搜索结果图）
_IMG_SKIP_KEYWORDS = re.compile(
    r"(logo|icon|avatar|favicon|emoji|sprite|banner|qr|spinner|loading|placeholder|"
    r"hover|btn|button|arrow|shadow|bkgr?|header|footer|menu|slide|thumb|default|"
    r"search|cover-\d|icon-|logo-|\.min\.|_small)",
    re.IGNORECASE,
)
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", re.IGNORECASE)
# 水印/标记/占位/应用图标等无效图特征
_IMG_JUNK_RE = re.compile(
    r"(mark|mark2|_sm_|nx_mark|\.svg$|white\.svg|watermark|unsupport|hd_app|\.app\.|notfound|ewm|ercode|qrcode)",
    re.IGNORECASE,
)


def official_search_results(query: str, max_results: int = 5) -> list[dict]:
    """
    调用 DeepSeek 官方 web search，返回原始结果列表 [{title, url}, ...]。

    官方端点返回的内容是加密的，只暴露 title 和 url，
    但这对搜图/找来源页已经足够（后续可 fetch_url 抓取页面提取图片）。
    """
    try:
        resp = _get_search_client().messages.create(
            model=DEEPSEEK_MODEL,
            max_tokens=1024,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"请搜索以下内容并返回结果列表：{query}",
                },
            ],
        )

        results: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", "") != "web_search_tool_result":
                continue
            for item in getattr(block, "content", []) or []:
                if getattr(item, "type", "") != "web_search_result":
                    continue
                url = getattr(item, "url", "") or ""
                title = getattr(item, "title", "") or ""
                if url:
                    results.append({"title": title, "url": url})
        return results[:max_results]
    except Exception as e:
        # 返回 None（而不是空列表）表示"这次搜索本身失败了"。过去一律返回 []，
        # 调用方只能把它说成"未找到相关图片"，网络/鉴权问题被伪装成"没有结果"，
        # 排查时完全看不出该重试还是该换关键词。
        print(f"[official_search_results] 官方搜索失败: {e}")
        return None


def extract_page_images(page_url: str, max_images: int = 3) -> list[str] | None:
    """
    抓取网页并提取可用的图片直链（og:image 优先，其次 img 标签兜底）。
    用于官方搜索只返回来源页 URL、需要从页面里拿图片的场景。

    返回绝对 URL 列表（去重）；页面抓取失败返回 **None**（和"页面里没有图"返回 [] 区分开），
    否则网络问题会一路伪装成"未找到相关图片"。
    """
    import urllib.parse

    try:
        resp = safe_get(None, page_url, headers=_HEADERS, timeout=8)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        candidates: list[str] = []
        # 占位图/空白图/错误图也跳过
        _PLACEHOLDER = re.compile(
            r"(empty|placeholder|blank|\.gif|\.webp|transparent|404\.png)", re.IGNORECASE
        )

        def _acceptable(src: str) -> bool:
            if not src or src.startswith("data:"):
                return False
            if _IMG_SKIP_KEYWORDS.search(src):
                return False
            if _IMG_JUNK_RE.search(src):
                return False
            if _PLACEHOLDER.search(src):
                return False
            return True

        # 1) og:image 优先
        og = soup.find("meta", property="og:image")
        if og and og.get("content") and _acceptable(og["content"]):
            candidates.append(og["content"])

        # 2) srcset 取最大图（高分页常见）
        for img in soup.select("img[srcset]"):
            if len(candidates) >= max_images:
                break
            srcset = img.get("srcset", "")
            entries = [
                e.split()[0]
                for e in srcset.split(",")
                if e.split() and _acceptable(e.split()[0])
            ]
            if entries:
                candidates.append(entries[-1])  # srcset 通常按从小到大排序，取最后（最大）

        # 3) 普通 img 兜底（跳过 logo/icon/小尺寸/占位图）
        _ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-lazyload")
        for img in soup.select("img"):
            if len(candidates) >= max_images:
                break
            src = ""
            for a in _ATTRS:
                src = img.get(a) or ""
                if src:
                    break
            if not _acceptable(src):
                continue
            # 尺寸过滤：太小的图跳过
            w = img.get("width") or img.get("data-width") or ""
            try:
                if w and int(w) < 200:
                    continue
            except ValueError:
                pass
            candidates.append(src)

        # 转为绝对 URL 并去重、过滤非图片扩展名
        seen: set[str] = set()
        urls: list[str] = []
        for src in candidates:
            abs_url = urllib.parse.urljoin(page_url, src)
            if abs_url in seen or abs_url.startswith("data:"):
                continue
            if _IMG_SKIP_KEYWORDS.search(abs_url):
                continue
            # 必须看起来像图片（og:image 也可能不带扩展名，放行；img 兜底要求扩展名）
            if not og and not _IMG_EXT_RE.search(abs_url):
                continue
            seen.add(abs_url)
            urls.append(abs_url)
            if len(urls) >= max_images:
                break
        return urls
    except Exception as e:
        # 抓不到就是抓不到：返回 None 让调用方能说"抓取失败"，而不是"没有图片"
        print(f"[extract_page_images] 抓取失败 {page_url}: {e}")
        return None


class SearchProvider(ToolProvider):
    name = "search"
    description = "搜索相关工具：web_search（DeepSeek 官方搜索）、fetch_url（网页抓取）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "web_search",
                "tag": "搜索",
                "description": (
                    "搜索互联网获取实时信息（DeepSeek 官方搜索）。"
                    "返回带来源的搜索摘要，若需要完整内容可用 fetch_url 打开来源链接。"
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
                "max_calls": 5,
            },
            {
                "name": "fetch_url",
                "tag": "网页读取",
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
                "max_calls": 5,
            },
        ]

    @staticmethod
    def _web_search(query: str) -> str:
        """使用 DeepSeek 官方内置 web search（Anthropic 兼容端点）"""
        try:
            resp = _get_search_client().messages.create(
                model=DEEPSEEK_MODEL,
                max_tokens=1024,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                    },
                ],
                messages=[
                    {
                        "role": "user",
                        "content": f"请搜索以下内容并整理结果：{query}",
                    },
                ],
            )

            # 提取文本块（官方搜索直接返回格式化摘要 + 来源列表）
            parts = [
                block.text.strip()
                for block in resp.content
                if getattr(block, "type", "") == "text" and block.text.strip()
            ]
            if not parts:
                # 兜底：若无文本，取 tool 结果
                parts = [
                    str(block).strip()
                    for block in resp.content
                    if getattr(block, "type", "") in ("web_search_tool_result", "server_tool_use")
                ]
            return "\n\n".join(parts) or "官方搜索未返回有效结果。"
        except Exception as e:
            print(f"[web_search] 官方搜索失败: {e}")
            return f"搜索失败：{e}"

    @staticmethod
    def _fetch_url(url: str) -> str:
        """抓取网页并提取文字内容（url 来自模型 → 必须先过公网地址校验）"""
        try:
            resp = safe_get(None, url, headers=_HEADERS, timeout=10)
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
