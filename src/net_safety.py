"""
出站 URL 的安全校验：只允许访问**公网**地址。

为什么要单独有这么一个模块：工具的 url 参数来自模型，而模型的内容来自群消息 ——
任何人发一句「帮我抓一下 http://169.254.169.254/latest/meta-data/」都会经过模型变成
一次服务端请求。requests 默认直连，于是本机服务、内网地址、云元数据地址全都能打，
抓到的内容还会被回喂给模型、可能念进群里（等于群成员能借它读本机的 HTTP 服务）。

所以「能不能访问这个地址」这件事收敛到这里的 check_url / safe_get 两个函数，
web 抓取（tools/search_provider.fetch_url）与图片下载（vision_utils._download_image）共用。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
import os
import time
import urllib3
import threading


class UnsafeURLError(ValueError):
    """URL 不安全（协议不支持 / 指向非公网地址 / 重定向到非公网地址）"""


# fake-IP is allowed only behind an explicitly configured trusted proxy.
_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)


def _is_proxy_fake_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip.version == net.version and ip in net for net in _FAKE_IP_NETWORKS)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """公网地址才算安全：本机、内网、链路本地（含云元数据 169.254.x.x）一律拒绝"""
    return ip.is_global and not ip.is_multicast


def _resolve(url: str):
    """
    校验 URL 并返回去掉首尾空白的版本，不安全时抛 UnsafeURLError。

    域名与字面 IP 都要过 **DNS 解析后** 的地址判断：只看着像"域名"就放行是没用的，
    attacker.example 完全可以解析到 127.0.0.1。
    """
    text = (url or "").strip()
    if not text:
        raise UnsafeURLError("URL 为空")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"只支持 http/https，收到的是 {parsed.scheme or '（没有写协议）'}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL 里没有主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.username or parsed.password or "\\" in text or any(ord(c) < 32 for c in text):
        raise UnsafeURLError("不允许凭证、反斜杠或控制字符")
    host = host.encode("idna").decode("ascii")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"域名解析失败：{host}（{e}）") from e
    ips = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:  # 解析出的东西不是 IP（理论上不会发生）
            raise UnsafeURLError(f"{host} 解析出无法识别的地址 {info[4][0]!r}") from None
        if not _is_public_ip(ip) and not (os.getenv("TRUST_PROXY_URL", "").strip() and _is_proxy_fake_ip(ip)):
            raise UnsafeURLError(f"{host} 指向非公网地址 {ip}，拒绝访问")
        ips.append(str(ip))
    if not ips:
        raise UnsafeURLError("域名没有可用地址")
    return text, parsed, host, port, ips


def check_url(url):
    return _resolve(url)[0]


def _pool(scheme, ip, port, host):
    if scheme == "https":
        return urllib3.HTTPSConnectionPool(ip, port, assert_hostname=host,
            server_hostname=host, cert_reqs="CERT_REQUIRED", ca_certs=requests.certs.where())
    return urllib3.HTTPConnectionPool(ip, port)


def _limit_connection_lifetime(pool, deadline):
    if not isinstance(pool, urllib3.HTTPConnectionPool):
        return
    base = pool.ConnectionCls
    class DeadlineConnection(base):
        _deadline_timer = None

        def connect(self):
            super().connect()
            sock = self.sock
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._deadline_timer = threading.Timer(max(0, deadline-time.monotonic()), expire)
            self._deadline_timer.daemon = True
            self._deadline_timer.start()

        def close(self):
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
            super().close()
    pool.ConnectionCls = DeadlineConnection


def safe_get(
    session: requests.Session | None,
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 10,
    max_redirects: int = 5,
    max_bytes: int = 4 * 1024 * 1024,
    **kwargs,
) -> requests.Response:
    """
    带逐跳校验的 GET。

    不能直接用 ``requests.get(..., allow_redirects=True)``：第一次校验的是我们给的 URL，
    而 30x 的目标是**服务端**决定的，公网页面可以把你重定向到 http://127.0.0.1/，
    这一跳就绕过了所有校验。所以这里自己跟重定向，每一跳都重新 check_url。
    """
    deadline = time.monotonic() + float(timeout)
    current = url
    request_headers = dict(getattr(session, "headers", {}) or {})
    request_headers.update(headers or {})
    # Explicit opt-in: the proxy becomes responsible for DNS/routing restrictions.
    # Environment HTTP_PROXY/HTTPS_PROXY and fake-IP are never implicitly trusted.
    proxy = os.getenv("TRUST_PROXY_URL", "").strip()
    for _ in range(max_redirects + 1):
        current, parsed, host, port, ips = _resolve(current)
        authority = f"[{host}]" if ":" in host else host
        if parsed.port:
            authority += f":{port}"
        request_headers["Host"] = authority
        request_headers["Accept-Encoding"] = "identity"
        pool = (urllib3.ProxyManager(proxy, cert_reqs="CERT_REQUIRED", ca_certs=requests.certs.where())
                if proxy else _pool(parsed.scheme, ips[0], port, host))
        if not proxy:
            _limit_connection_lifetime(pool, deadline)
        target = current if proxy else (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("下载总时限已到")
            response = pool.urlopen("GET", target, headers=request_headers, redirect=False,
                retries=False, preload_content=False,
                timeout=urllib3.Timeout(connect=min(5, remaining), read=remaining))
            location = response.headers.get("Location")
            if response.status in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                request_headers.pop("Authorization", None)
                request_headers.pop("Cookie", None)
                continue
            if int(response.headers.get("Content-Length", "0")) > max_bytes:
                raise UnsafeURLError("响应体超过大小上限")
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise UnsafeURLError("拒绝压缩响应，避免解压体积绕过限制")
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("下载总时限已到")
                sock = getattr(getattr(response, "connection", None), "sock", None)
                if sock is None:
                    raw = getattr(getattr(getattr(response, "_fp", None), "fp", None), "raw", None)
                    sock = getattr(raw, "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes - total + 1), decode_content=True)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError("响应体超过大小上限")
                chunks.append(chunk)
            result = requests.Response()
            result.status_code = response.status
            result.headers.update(response.headers)
            result.url = current
            result._content = b"".join(chunks)
            result._content_consumed = True
            result.encoding = requests.utils.get_encoding_from_headers(result.headers)
            return result
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise UnsafeURLError(f"重定向次数超过 {max_redirects} 次，放弃")
