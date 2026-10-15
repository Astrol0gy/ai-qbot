"""
图片理解 —— DeepSeek 官方视觉接口（模型 deepseek-flash）

与旧实现（Qwen3-VL / 百炼）的不同：

1. 一个 Key：看图用 DEEPSEEK_API_KEY，不再依赖百炼（文生图仍在本文件末尾用 DASHSCOPE）。
2. 取图三条路，按代价从低到高：直传 URL → 下载后内联 base64（≤32MiB）→ 下载后上传
   Files API 换 file_id（≤64MiB，同一张图可长期复用，请求体里只有一个短 id）。
   QQ CDN 的图服务端常常拉不到，就得走后两条。
3. 格式按**文件内容**嗅探（JPEG/PNG/GIF/WebP），不信 CDN 的 Content-Type ——
   NapCat 常回 application/octet-stream；GIF 动图也因此能直接送进去。
4. 看图默认**关**思考（`VISION_ENABLE_THINKING`）：deepseek-flash 默认开着，而推理 token
   与可见回答共用 max_tokens，实测会把稍大图片的描述挤成空回答。
5. 描述首行固定是「类型：表情包/内容图片 ｜ 意图：…」（见 `_TYPE_GUIDE`），并且**排在最前**：
   主模型对图片的全部认知就是这段文字，分不清梗图与照片就会答错（把猫头顶问号当成猫咪
   照片来夸）；而描述进接话上下文时按 `IMAGE_NOTE_MAX` 截断，写在后面会被截掉。
   「这张图在当前对话里什么意思」仍由有上下文的主模型判断（见 skills/image.md）。
6. 看图失败**不许拖住对话**：客户端读超时短（`VISION_TIMEOUT_SEC`）、不重试，失败即
   返回，由调用方留占位（`IMAGE_UNAVAILABLE`）继续答；失败还进退避
   （`VISION_FAIL_BACKOFF_SEC`），窗口内同一张图不再下载、不再调用。
   线上日志的教训：看图失败既不降级也不退避，主模型还拿到「识别失败」四个字去琢磨，
   最后整轮思考被拖过 60 秒、超时放弃，群里连着几轮都等不到回复。
7. QQ 系 CDN（`_SERVER_UNREACHABLE_HOSTS`）跳过直传直接自己取图：服务端拉不到这些
   带签名的链接，直传只会白跑一次往返。

官方限制（见文档）：图片只能放在 user 消息里（system/assistant 会 400）；内联单图
≤32MiB、URL ≤8192 字符、请求体 ≤48MiB、单请求 ≤600 张；Files API 单图 ≤64MiB，
purpose 只接受 user_data；每张图计费上限 1024 tokens。

文档：
  图像理解 https://api-docs.deepseek.com/zh-cn/guides/vision/
  Files API https://api-docs.deepseek.com/zh-cn/guides/files_api/
"""
import asyncio
import base64
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

import requests
from openai import OpenAI, Timeout

from .config import (
    DEEPSEEK_API_KEY,
    DASHSCOPE_API_KEY,
    DEEPSEEK_REASONING_EFFORT,
    VISION_BASE_URL,
    VISION_MODEL,
    VISION_DETAIL,
    VISION_ENABLE_THINKING,
    VISION_FILES_API,
    VISION_FAIL_BACKOFF_SEC,
    VISION_TIMEOUT_SEC,
    IMAGE_GEN_MODEL,
    IMAGE_GEN_ENDPOINT,
    TRIGGER_IMAGE_CACHE_SIZE,
    TRIGGER_IMAGE_RATE_PER_MIN,
)

# 官方限制（见模块文档顶部）
_INLINE_MAX_BYTES = 32 * 1024 * 1024  # 单张内联图片（URL / base64）上限
_FILE_MAX_BYTES = 64 * 1024 * 1024    # 单张 Files API（file_id）上限，同时也是下载上限
_URL_MAX_CHARS = 8192                 # 直传 URL 长度上限，超了只能自己取图
_REQUEST_TIMEOUT = 30                 # 下载超时
_UPLOAD_TIMEOUT = 120                 # 上传超时
_FILE_ID_CACHE_SIZE = 64              # 已上传图片的 file_id 缓存条数（同一张图不重复上传）

# 识别失败后的退避：同一张图在 VISION_FAIL_BACKOFF_SEC 内不再重试。
# 没有这道闸，失败的图会在每次唤醒时重新下载 + 重新调用（日志里实测过：一张图反复失败，
# 每轮都白跑一次下载和一次必失败的直传），既拖慢整轮也白烧调用。
_FAILED: "OrderedDict[str, float]" = OrderedDict()
_FAIL_CACHE_SIZE = 256  # 记住多少张失败图（防止内存无限增长）

# 识别不出来的图在上下文里的占位。必须留占位：主模型得知道「用户确实发过一张图」，
# 否则会答非所问（用户在问图里的东西，它当没这回事）；但也**不能**写「识别失败」——
# 主模型会拿推理去琢磨这句话（日志 2904 行实测：「图像识别失败了，要不要提它？」），
# 白烧思考预算，最后把一轮拖过 60 秒超时。
IMAGE_UNAVAILABLE = "[图片，未能读取]"

# 这些 CDN 的图，DeepSeek 服务端一律拉不到（QQ 图片链接带签名且只对客户端有效），
# 直传只会白跑一次往返（实测 400 Failed to download image），所以直接跳过直传自己取图。
_SERVER_UNREACHABLE_HOSTS = (
    "multimedia.nt.qq.com.cn",
    "gchat.qpic.cn",
    "c2cpicdw.qpic.cn",
    "gxh.vip.qq.com",
    "qq.com",
)

# DeepSeek 视觉客户端（OpenAI 兼容）
_vision_client = None


def vision_available() -> bool:
    """看图功能是否可用（未配置 DeepSeek Key 时静默跳过，不影响文字对话）"""
    return bool(DEEPSEEK_API_KEY)


def _get_client() -> OpenAI | None:
    """延迟初始化视觉客户端"""
    global _vision_client
    if not vision_available():
        return None
    if _vision_client is None:
        # 看图是「给对话做配菜」的活，不能拖住整轮：读超时短（默认 25s），重试关掉。
        # 默认的 600s / 重试 2 次会在服务端半死时把一轮挂到 60 秒超时才放弃；
        # 这里失败就立刻失败，交给占位与退避处理，对话照常进行。
        _vision_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=VISION_BASE_URL,
            timeout=Timeout(
                connect=5.0,
                read=VISION_TIMEOUT_SEC,
                write=VISION_TIMEOUT_SEC,
                pool=VISION_TIMEOUT_SEC,
            ),
            max_retries=0,
        )
    return _vision_client


def _detail(explicit: str | None = None) -> str:
    """细节档位：low / high / original / auto（认不出来的值交给服务端自动挑）"""
    value = (explicit or VISION_DETAIL or "auto").strip().lower()
    return value if value in ("low", "high", "original") else "auto"


def _skip_direct(url: str) -> bool:
    """这个 URL 值不值得先试直传（QQ 系 CDN 服务端拉不到，试了也是白试）"""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _SERVER_UNREACHABLE_HOSTS)


def _in_backoff(ref: str) -> bool:
    """这张图是否还在失败退避窗口内"""
    ts = _FAILED.get(ref)
    if ts is None:
        return False
    if time.monotonic() - ts > VISION_FAIL_BACKOFF_SEC:
        _FAILED.pop(ref, None)
        return False
    _FAILED.move_to_end(ref)
    return True


def _mark_failed(ref: str) -> None:
    """记下失败时刻，窗口内不再重试（成功一次就会清掉）"""
    _FAILED[ref] = time.monotonic()
    _FAILED.move_to_end(ref)
    while len(_FAILED) > _FAIL_CACHE_SIZE:
        _FAILED.popitem(last=False)


def _clear_failed(ref: str) -> None:
    _FAILED.pop(ref, None)


def _sniff_mime(data: bytes) -> str | None:
    """
    按魔数判断图片真实格式。

    DeepSeek 也是按内容判格式，所以这里不信 CDN 的 Content-Type —— NapCat 常回
    application/octet-stream，照着它填反而白丢一个正确的 mime。
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):  # 动图与静图同为 image/gif，交给模型去看
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _download_image(url: str) -> tuple[bytes, str] | None:
    """
    下载远程图片 -> (原始字节, mime)。只收 JPEG/PNG/GIF/WebP，超过 64MB 放弃。
    用于 QQ CDN 等视觉服务端拉不到的链接。
    """
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > _FILE_MAX_BYTES:
            print(f"[图片] 文件过大（{int(declared) / 1048576:.1f}MB > 64MB），跳过")
            return None

        # 流式读取，防止内存溢出
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > _FILE_MAX_BYTES:
                print("[图片] 文件超过 64MB 上限，停止下载")
                return None

        data = b"".join(chunks)
        del chunks  # 释放块列表
        mime = _sniff_mime(data)
        if mime is None:
            ctype = resp.headers.get("Content-Type", "?")
            print(f"[图片] 不是 JPEG/PNG/GIF/WebP，跳过（Content-Type={ctype}）")
            return None
        print(f"[图片] 下载完成 {total / 1048576:.2f}MB，格式 {mime}")
        return data, mime
    except Exception as e:
        print(f"[图片] 下载失败: {e}")
        return None


def _upload_to_files(data: bytes, mime: str) -> str | None:
    """
    上传到 Files API 换 file_id（POST /files，multipart）。
    图片类文件 purpose 只接受 user_data，单文件 ≤64MiB；返回 id 形如 file-api-xxx。
    """
    if not vision_available():
        return None

    filename = "image." + mime.rsplit("/", 1)[-1]  # 服务端按内容判格式，文件名只给人看
    try:
        resp = requests.post(
            f"{VISION_BASE_URL.rstrip('/')}/files",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            files={"file": (filename, data, mime)},
            data={"purpose": "user_data"},
            timeout=_UPLOAD_TIMEOUT,
        )
        resp.raise_for_status()
        file_id = str(resp.json().get("id") or "")
        if not file_id:
            print(f"[图片] Files 上传返回异常：{str(resp.text)[:200]}")
            return None
        print(f"[图片] 已上传 Files API -> {file_id}")
        return file_id
    except Exception as e:
        print(f"[图片] Files 上传失败: {e}")
        return None


# 上传过的图：url -> file_id（LRU）。命中就不必再下载，请求体里也只有一个短 id。
_FILE_IDS: "OrderedDict[str, str]" = OrderedDict()


def _remember_file_id(url: str, file_id: str) -> None:
    _FILE_IDS[url] = file_id
    _FILE_IDS.move_to_end(url)
    while len(_FILE_IDS) > max(1, _FILE_ID_CACHE_SIZE):
        _FILE_IDS.popitem(last=False)


def _cached_file_id(url: str) -> str | None:
    file_id = _FILE_IDS.get(url)
    if file_id is not None:
        _FILE_IDS.move_to_end(url)
    return file_id


def _inline_block(ref: str, level: str) -> dict:
    """内联图片块：ref 是 http(s) URL 或 data: URL"""
    return {"type": "image_url", "image_url": {"url": ref, "detail": level}}


def _downloaded_block(url: str, level: str) -> dict | None:
    """
    自己取图之后再决定怎么送给模型：

    - 之前上传过 → 直接复用 file_id
    - VISION_FILES_API=always，或图片 >32MiB（内联放不下、Files 能到 64MiB）→ 上传换 file_id
    - 其余 → 内联 base64（少一次上传往返，日常够用）
    """
    cached = _cached_file_id(url)
    if cached:
        print(f"[图片] 复用已上传的 file_id {cached}")
        return {"type": "file", "file_id": cached}

    got = _download_image(url)
    if got is None:
        return None
    data, mime = got

    want_files = VISION_FILES_API != "off" and (
        VISION_FILES_API == "always" or len(data) > _INLINE_MAX_BYTES
    )
    if want_files:
        file_id = _upload_to_files(data, mime)
        if file_id:
            del data  # 释放原始字节
            _remember_file_id(url, file_id)
            return {"type": "file", "file_id": file_id}
        if len(data) > _INLINE_MAX_BYTES:
            print("[图片] 上传失败且超过内联上限，放弃这张图")
            return None
        print("[图片] 上传失败，退回内联 base64")

    b64 = base64.b64encode(data).decode("ascii")
    del data  # 释放原始字节
    print(f"[图片] 内联 base64 {len(b64) / 1048576:.2f}MB")
    return _inline_block(f"data:{mime};base64,{b64}", level)


def _image_block(ref: str, level: str) -> dict | None:
    """
    把各种「图片引用」变成 user 消息里的内容块：

    - ``file-api-xxx``   → file 块（已上传过，或调用方直接给 id）
    - ``data:image/...`` → 内联 image_url
    - http(s) 短 URL     → 直传 image_url（服务端自己下载，最省事）
    - 超长 URL           → 只能自己取图（文档限 8192 字符）
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("file-api-"):
        return {"type": "file", "file_id": ref}
    if ref.startswith("data:"):
        return _inline_block(ref, level)
    if ref.startswith(("http://", "https://")):
        if len(ref) <= _URL_MAX_CHARS:
            cached = _cached_file_id(ref)
            if cached:
                return {"type": "file", "file_id": cached}
            return _inline_block(ref, level)
        print(f"[图片] URL 过长（{len(ref)} > {_URL_MAX_CHARS} 字符），改为自己取图")
        return _downloaded_block(ref, level)
    return None


def _dump_response(response) -> str:
    """把服务端原始响应摊平打出来（诊断用；能 dump 就 dump，dump 不了就 repr）"""
    try:
        return str(response.model_dump())[:600]
    except Exception:
        return repr(response)[:600]


def _looks_like_outage(e: Exception) -> bool:
    """粗判「服务端不可用」：连接失败 / 超时 / 5xx / 限流。

    这类失败跟「这张图有问题」不同：重试大概率还是失败，只该退避，不该在每轮唤醒里重来。
    """
    name = type(e).__name__
    if name in ("APIConnectionError", "APITimeoutError", "InternalServerError",
                "RateLimitError", "APIStatusError"):
        return True
    return any(k in str(e) for k in ("503", "502", "500", "504", "timed out", "timeout"))


def _describe_media(block: dict, prompt: str, max_tokens: int = 500) -> str | None:
    """发一次视觉请求。图片必须放在 user 消息里（system/assistant 会 400）。"""
    client = _get_client()
    if client is None:
        return None

    kwargs: dict = {"max_tokens": max_tokens}
    if VISION_ENABLE_THINKING:
        # 思考模式与可见回答共用 max_tokens（实测一次看图推理就能吃 300~1500 token），
        # 所以要给足预算，否则回答会被挤成空
        kwargs["max_tokens"] = max(max_tokens, 2048)
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
    else:
        # 必须**显式**关：deepseek-flash 默认开着思考，看图这种简单任务白烧推理 token，
        # 还会把可见回答挤空（实测 2MB 的 GIF 描述直接变成 0 字符）
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role": "user", "content": [block, {"type": "text", "text": prompt}]},
            ],
            **kwargs,
        )
        # 服务端偶尔会返回 200 但没有 choices（线上日志里出现过，原本只打印成
        # 「'NoneType' object is not subscriptable」，查不动）。这里判空并把原始响应打出来，
        # 这样下次再遇到能直接看到服务端到底回了什么。
        choices = getattr(response, "choices", None)
        if not choices:
            print(f"[VLM] 响应里没有 choices：{_dump_response(response)}")
            return None
        choice = choices[0]
        description = (choice.message.content or "").strip()
        if not description:
            # 空回答按失败处理，交给调用方走「自己取图」兜底，避免把空描述写进上下文
            print(f"[VLM] 返回空回答（finish_reason={choice.finish_reason}），视为识别失败")
            return None
        if choice.finish_reason == "length":
            print("[VLM] 回答触达 max_tokens 被截断，可调大 max_tokens 或关掉思考")
        print(f"[VLM] 识别结果:\n{description}")
        return description
    except Exception as e:
        # 带上异常类型，并把「服务端不可用」这类原因标出来：这类失败要退避，
        # 而不是在每轮唤醒里重试到把整轮思考拖过超时
        print(f"[VLM] 识别失败（{type(e).__name__}）: {str(e)[:300]}")
        if _looks_like_outage(e):
            print(
                "[VLM] 这是服务端不可用/超时，"
                f"{int(VISION_FAIL_BACKOFF_SEC) // 60} 分钟内不再重试这张图"
            )
        return None


# 贴图判读：主模型对图片的全部认知就是这段描述文本，看不出「表情包」还是「正经图片」，
# 就会把猫头顶问号当成一张猫咪照片来夸。判类型只能由看图的模型来做（它才是见过图的那个），
# 「这张图在当前对话里什么意思」则留给有上下文的主模型（见 skills/image.md）。
#
# 第一行必须是「类型 ｜ 意图」：描述进接话上下文时按 IMAGE_NOTE_MAX 截断，写哪儿排哪儿。
_TYPE_GUIDE = (
    "先判断这张图属于哪一类：\n"
    "· 表情包／梗图：带字幕或大字的拼贴图、夸张表情、反应图（例如猫头顶问号）、"
    "低清网图、截图配字。用户发它通常是为了表达情绪或态度，不是让你描述画面。\n"
    "· 内容图片：实拍照片、屏幕截图、聊天记录、表格图表、文档扫描件等，"
    "用户通常想让你看懂里面的信息。\n"
    "输出要求：\n"
    "1. 第一行固定为「类型：表情包 或 内容图片 ｜ 意图：<发送者想表达什么>」，60 字以内；\n"
    "2. 若是表情包／梗图：写清它表达的情绪或态度（困惑、无语、震惊、赞同、卖萌……）、"
    "图上的文字，再用一句话说清画面即可，**不要**逐项罗列场景、物体、氛围；\n"
    "3. 若是内容图片：详细描述场景、主体、动作、关键文字（原文照录）、数据要点；\n"
    "4. 若为动图（GIF），补一句画面里的动作变化。用中文，简洁准确。"
)


def describe_image(image_url: str, question: str = "", detail: str | None = None) -> str | None:
    """
    用 DeepSeek 视觉模型（deepseek-flash）看图。

    Args:
        image_url: 图片引用 —— http/https URL、data: URL，或已上传的 file-api-xxx id
        question: 用户的附加问题（可选，为空则做通用描述）
        detail: 细节档位 low/high/original/auto（默认取配置 VISION_DETAIL）

    Returns:
        图片描述文本，第一行为「类型：表情包/内容图片 ｜ 意图：…」（见 _TYPE_GUIDE）；
        视觉不可用或取图失败时返回 None

    失败会进退避（VISION_FAIL_BACKOFF_SEC 秒内不再试同一张图）：调用方拿到 None 后
    应该留占位（IMAGE_UNAVAILABLE）继续对话，而不是反复重试把整轮拖慢。
    """
    if question:
        prompt = f"{_TYPE_GUIDE}\n\n最后结合图片回答这个问题：{question}"
    else:
        prompt = _TYPE_GUIDE

    level = _detail(detail)
    ref = (image_url or "").strip()
    if not ref:
        return None
    if _in_backoff(ref):
        print(f"[VLM] 该图还在失败退避窗口内（{int(VISION_FAIL_BACKOFF_SEC) // 60} 分钟），"
              f"跳过重试: {ref[:80]}...")
        return None

    is_http = ref.startswith(("http://", "https://"))
    # 只有"直传 URL"这条路失败才值得自己下载（其余路径已经是我们自己取的图）；
    # QQ 系 CDN 的图服务端一律拉不到，跳过直传直接自己取，省掉一次必失败的往返
    direct = is_http and len(ref) <= _URL_MAX_CHARS and not _skip_direct(ref)

    if is_http and not direct:
        if len(ref) > _URL_MAX_CHARS:
            print(f"[图片] URL 过长（{len(ref)} > {_URL_MAX_CHARS} 字符），改为自己取图")
        else:
            print(f"[图片] 该域名服务端拉不到，跳过直传直接自己取图: {ref[:80]}...")
        block = _downloaded_block(ref, level)
    else:
        block = _image_block(ref, level)

    if block is None:
        _mark_failed(ref)
        return None

    result = _describe_media(block, prompt, max_tokens=600)
    if result is not None:
        _clear_failed(ref)
        print(f"[VLM] 图片描述完成，{len(result)} 字符")
        return result

    if direct:
        print("[图片] 直传 URL 失败，改为自己取图...")
        block2 = _downloaded_block(ref, level)
        if block2 is not None:
            result = _describe_media(block2, prompt, max_tokens=600)
            del block2  # 释放内联的 base64
            if result is not None:
                _clear_failed(ref)
                print(f"[VLM] 图片描述完成（自己取图），{len(result)} 字符")
                return result

    # 两条路都没成：记下失败，退避窗口内不再重试（否则每次唤醒都重新下载 + 重新调用）
    _mark_failed(ref)
    return None


# ============================================
# 上下文预识别（未 @ 的群消息）
#
# 机器人「看得见」群里所有人发的图，代价是每张图一次 VLM 调用。
# 这里加两层护栏：按 URL 缓存描述（同图不重复计费）+ 每会话频控（防刷图）。
# ============================================
_IMG_CACHE: "OrderedDict[str, str]" = OrderedDict()  # url -> 描述（LRU）
_IMG_RATE: dict[str, deque[float]] = {}              # conv_id -> 最近识别时间戳
_IMG_RATE_WINDOW = 60.0


def _cache_get(url: str) -> str | None:
    desc = _IMG_CACHE.get(url)
    if desc is not None:
        _IMG_CACHE.move_to_end(url)
    return desc


def _cache_put(url: str, desc: str) -> None:
    _IMG_CACHE[url] = desc
    _IMG_CACHE.move_to_end(url)
    while len(_IMG_CACHE) > max(1, TRIGGER_IMAGE_CACHE_SIZE):
        _IMG_CACHE.popitem(last=False)


def _take_rate(conv_id: str, n: int) -> bool:
    """该会话最近一分钟是否还有 n 张图的识别预算（上限 <=0 表示不限制）"""
    limit = TRIGGER_IMAGE_RATE_PER_MIN
    if limit <= 0:
        return True

    now = time.monotonic()
    stamps = _IMG_RATE.setdefault(conv_id, deque())
    while stamps and now - stamps[0] > _IMG_RATE_WINDOW:
        stamps.popleft()

    if len(stamps) + n > limit:
        return False
    stamps.extend([now] * n)

    # 窗口滑空后顺手清掉无活动的会话，避免 _IMG_RATE 无界增长
    if len(_IMG_RATE) > 200:
        for key in [k for k, v in _IMG_RATE.items() if not v]:
            _IMG_RATE.pop(key, None)
    return True


async def describe_for_context(conv_id: str, urls: list[str]) -> list[str] | None:
    """
    为「上下文」预识别一组图片（未 @ 的群消息）。

    与 describe_image 的区别：带 URL 缓存与每会话频控。**逐张尽力**——识别不出来的图
    留占位（IMAGE_UNAVAILABLE）而不是让整组返回 None：返回 None 会让调用方保持
    「未识别」状态，于是每次唤醒都重新下载、重新试一遍（线上日志实测：一张失败的图
    反复白跑，还把整轮思考拖到 60 秒超时）。占位不进缓存，退避窗口过后还能再试。

    Args:
        conv_id: 会话标识（频控按会话计）
        urls: 图片直链列表

    Returns:
        与 urls 等长的描述列表（识别不出来的位置是 IMAGE_UNAVAILABLE 占位）；
        未配置视觉 / 超频时返回 None
    """
    if not urls or not vision_available():
        return None

    # 退避中的图不占频控额度：它连调用都不会发生
    missing = [u for u in urls if _cache_get(u) is None and not _in_backoff(u)]
    if missing and not _take_rate(conv_id, len(missing)):
        print(
            f"[VLM] 已达频控上限（{TRIGGER_IMAGE_RATE_PER_MIN} 张/分钟），"
            f"本次跳过 {len(missing)} 张图片的预识别"
        )
        return None

    descs: list[str] = []
    for url in urls:
        desc = _cache_get(url)
        if desc is None:
            print(f"[VLM] 上下文预识别: {url[:80]}...")
            # describe_image 是同步阻塞调用，放线程池跑，别卡住事件循环
            desc = await asyncio.to_thread(describe_image, url)
            if desc:
                _cache_put(url, desc)
        descs.append(desc or IMAGE_UNAVAILABLE)
    return descs


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
