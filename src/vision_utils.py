"""
视觉识别工具 — 通过 Qwen3-VL 模型描述图片内容
使用阿里云百炼 OpenAI 兼容接口，新用户有免费额度。
先尝试直传 URL，若外部服务器无法访问（如 QQ CDN），则下载后 base64 编码。

百炼最新视觉 API 文档: https://help.aliyun.com/zh/model-studio/vision
"""
import base64

import requests
from openai import OpenAI

from .config import (
    DASHSCOPE_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
    VISION_ENABLE_THINKING,
    VISION_HIGH_RES,
    IMAGE_GEN_MODEL,
    IMAGE_GEN_ENDPOINT,
)

# 下载限制
_MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50MB，超过不下载
_REQUEST_TIMEOUT = 30  # 下载超时

# Qwen 视觉客户端（OpenAI 兼容）
_vision_client = None


def _get_client() -> OpenAI | None:
    """延迟初始化 VLM 客户端。未配置 API Key 返回 None。"""
    global _vision_client
    if not DASHSCOPE_API_KEY:
        return None
    if _vision_client is None:
        _vision_client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url=VISION_BASE_URL,
        )
    return _vision_client


def _build_extra_body() -> dict | None:
    """构建百炼特有参数，通过 extra_body 传递"""
    extra = {}
    if VISION_ENABLE_THINKING:
        extra["enable_thinking"] = True
    if VISION_HIGH_RES:
        extra["vl_high_resolution_images"] = True
    return extra if extra else None


def _download_as_data_url(url: str) -> str | None:
    """
    下载远程文件并转为 base64 data URL。
    用于 QQ CDN 等百炼服务器无法直接访问的链接。
    下载完成后立即释放原始字节数据。
    """
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        # 检查大小
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_DOWNLOAD_SIZE:
            print(f"[VLM] 文件过大 ({int(content_length) / 1024 / 1024:.1f}MB > 50MB)，跳过下载")
            return None

        # 流式读取，防止内存溢出
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_DOWNLOAD_SIZE:
                print(f"[VLM] 文件超过 50MB 限制，停止下载")
                return None

        data = b"".join(chunks)
        del chunks  # 释放块列表
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        b64 = base64.b64encode(data).decode("ascii")
        del data  # 释放原始字节
        data_url = f"data:{content_type};base64,{b64}"
        print(f"[VLM] 下载完成，{total / 1024 / 1024:.1f}MB -> base64 {len(b64) / 1024 / 1024:.1f}MB")
        return data_url
    except Exception as e:
        print(f"[VLM] 下载失败: {e}")
        return None


def _describe_media(
    content_parts: list[dict],
    prompt: str,
    max_tokens: int = 500,
) -> str | None:
    """通用视觉识别：发送 content 数组到 VLM"""
    client = _get_client()
    if client is None:
        return None

    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": content_parts}],
            max_tokens=max_tokens,
            extra_body=_build_extra_body(),
        )
        description = response.choices[0].message.content or ""
        print(f"[VLM] 识别结果:\n{description}")
        return description
    except Exception as e:
        print(f"[VLM] 识别失败: {e}")
        return None


def describe_image(image_url: str, question: str = "") -> str | None:
    """
    使用 Qwen-VL 描述图片内容。

    Args:
        image_url: 图片 URL（支持 http/https URL 或 data: URL）
        question: 用户的附加问题（可选，为空则做通用描述）

    Returns:
        图片描述文本；API 不可用时返回 None
    """
    if question:
        prompt = (
            f"请仔细观察这张图片，结合图片内容回答以下问题：{question}\n\n"
            "如果图片中有关键文字信息，请一并提取。"
        )
    else:
        prompt = (
            "请详细描述这张图片的内容，包括：场景、人物/物体、动作、文字信息（如有）、"
            "整体氛围等。用中文回答，简洁全面。"
        )

    content = [
        {"type": "image_url", "image_url": {"url": image_url}},
        {"type": "text", "text": prompt},
    ]

    result = _describe_media(content, prompt, max_tokens=500)
    if result is not None:
        print(f"[VLM] 图片描述完成，{len(result)} 字符")
        return result

    # URL 方式失败 → 尝试下载后 base64
    if image_url.startswith(("http://", "https://")):
        print("[VLM] 直传 URL 失败，尝试下载转 base64...")
        data_url = _download_as_data_url(image_url)
        if data_url:
            content2 = [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]
            result = _describe_media(content2, prompt, max_tokens=500)
            del data_url, content2  # 释放 base64 数据
            if result is not None:
                print(f"[VLM] 图片描述完成（base64），{len(result)} 字符")
            return result

    return None


def text_to_image(
    prompt: str,
    size: str = "1024*1024",
    n: int = 1,
) -> list[str] | None:
    """
    文生图：调用 Qwen-Image 生成图片（DashScope 原生接口，非 OpenAI 兼容）。

    注意：qwen-image 系列模型不支持 OpenAI 兼容端点，必须走原生 HTTP 接口。

    Args:
        prompt: 文生图提示词（中英文均可）
        size: 输出分辨率，如 "1024*1024"（总像素需在 512*512 ~ 2048*2048 之间）
        n: 生成图片数量（1~6）

    Returns:
        图片 URL 列表；失败时返回 None
    """
    if not DASHSCOPE_API_KEY:
        return None

    n = min(max(int(n), 1), 6)
    try:
        body = {
            "model": IMAGE_GEN_MODEL,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ]
            },
            "parameters": {
                "prompt_extend": True,
                "size": size,
                "n": n,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        }
        resp = requests.post(
            IMAGE_GEN_ENDPOINT,
            headers=headers,
            json=body,
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()

        # 同步响应：output.choices[0].message.content[].image
        choices = data.get("output", {}).get("choices", [])
        urls = []
        for choice in choices:
            for item in choice.get("message", {}).get("content", []):
                if item.get("image"):
                    urls.append(item["image"])
        if not urls:
            print(f"[文生图] 未在响应中找到图片 URL: {str(data)[:500]}")
            return None

        print(f"[文生图] 成功生成 {len(urls)} 张图片")
        return urls
    except Exception as e:
        print(f"[文生图] 生成失败: {e}")
        return None
