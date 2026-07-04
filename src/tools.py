"""
Tool Calling 模块
管理所有工具的注册、定义生成、执行调度。
新增工具只需在 _REGISTRY 中注册即可，无需改动其他模块。
"""
import json

import requests
from bs4 import BeautifulSoup

# ============================================
# Tool 注册表
# key = 工具名称, value = (API定义, 执行函数)
# ============================================
_RegistryEntry = tuple[dict, callable]
_REGISTRY: dict[str, _RegistryEntry] = {}


def _register(name: str, description: str, parameters: dict, handler: callable):
    """注册一个工具"""
    _REGISTRY[name] = (
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        },
        handler,
    )


# ============================================
# 工具实现
# ============================================
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


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


def _fetch_url(url: str) -> str:
    """抓取网页并提取文字内容"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 去掉 script/style/nav/footer/header 等无关标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # 去掉多余空行
        lines = [line for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        if len(text) > 3000:
            text = text[:3000] + "\n...(内容过长，已截断)"

        return text if text else "页面无有效文字内容。"
    except Exception as e:
        return f"抓取失败：{e}"


# ============================================
# 注册所有工具（新增工具在这里加一行即可）
# ============================================
_register(
    name="web_search",
    description="搜索互联网获取信息。只返回标题、摘要和链接，不包含完整内容。若摘要信息不足，请用 fetch_url 打开具体链接获取全文。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（使用更具体的关键词以获得精确结果，如'成都 2026年7月4日 天气'而非'成都天气'）",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_web_search,
)

_register(
    name="fetch_url",
    description="抓取指定网页的完整文字内容。当 web_search 返回的摘要信息不够详细时，用此工具打开具体页面获取完整数据（如天气详情、新闻全文等）。",
    parameters={
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
    handler=_fetch_url,
)

# ============================================
# 对外接口
# ============================================
def get_definitions() -> list[dict]:
    """获取所有已注册工具的 API 定义列表"""
    return [definition for definition, _ in _REGISTRY.values()]


def execute(tool_name: str, arguments: str) -> str:
    """
    执行指定工具。
    tool_name: 模型返回的工具名称
    arguments: 模型返回的 JSON 参数字符串
    """
    entry = _REGISTRY.get(tool_name)
    if entry is None:
        return f"未知工具：{tool_name}"

    _, handler = entry
    args = json.loads(arguments)
    return handler(**args)
