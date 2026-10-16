"""Fake-IP downloads via an explicit local test proxy; no external APIs."""
import os
import io
from pathlib import Path
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import net_safety as ns


TARGET = "http://cdn.test/image.png"
PNG = b"\x89PNG\r\n\x1a\n"


class ProxyCases(unittest.TestCase):
    def setUp(self):
        original = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host == "cdn.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                         "", ("198.18.0.127", port))]
            return original(host, port, *args, **kwargs)

        dns = patch.object(ns.socket, "getaddrinfo", side_effect=resolve)
        dns.start()
        self.addCleanup(dns.stop)

    def test_fake_ip_rejected_with_actionable_hint(self):
        with patch.dict(os.environ, {"TRUST_PROXY_URL": ""}):
            with self.assertRaisesRegex(ns.UnsafeURLError, "fake-IP.*TRUST_PROXY_URL"):
                ns.safe_get(None, TARGET, timeout=2)

    def test_real_proxy_download_and_private_redirect_rejection(self):
        seen = []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Host")))
                if self.path.endswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1/private")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(PNG)))
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(PNG)

        server = HTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"TRUST_PROXY_URL": f"http://127.0.0.1:{server.server_port}"}):
                response = ns.safe_get(None, TARGET, timeout=2)
                self.assertEqual(response.content, PNG)
                self.assertEqual(seen, [(TARGET, "cdn.test")])
                original = socket.getaddrinfo
                def remote_dns(host, port, *args, **kwargs):
                    if host == "cdn.test":
                        raise socket.gaierror("local DNS unavailable")
                    return original(host, port, *args, **kwargs)
                with patch.object(ns.socket, "getaddrinfo", side_effect=remote_dns):
                    self.assertEqual(ns.safe_get(None, TARGET, timeout=2).content, PNG)
                with self.assertRaises(ns.UnsafeURLError):
                    ns.safe_get(None, TARGET, timeout=2, max_bytes=4)
                with self.assertRaises(ns.UnsafeURLError):
                    ns.safe_get(None, "http://cdn.test/redirect", timeout=2)
                before = len(seen)
                with self.assertRaises(ns.UnsafeURLError):
                    ns.safe_get(None, "http://127.0.0.1/private", timeout=2)
                self.assertEqual(len(seen), before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_proxy_failure_preserves_original_error_and_clears_manager(self):
        with patch.dict(os.environ, {"TRUST_PROXY_URL": "http://trusted-proxy:8080"}):
            with patch.object(ns.urllib3, "ProxyManager", autospec=True) as manager:
                manager.return_value.urlopen.side_effect = TimeoutError("proxy timeout")
                with self.assertRaisesRegex(TimeoutError, "proxy timeout"):
                    ns.safe_get(None, TARGET, timeout=2)
                manager.return_value.clear.assert_called_once()

    @staticmethod
    def stream():
        body = io.BytesIO(PNG)
        return NS(status=200, headers={"Content-Length": str(len(PNG))},
                  connection=None, read1=lambda size, **kwargs: body.read1(size), close=body.close)

    def test_no_proxy_downloads_public_target_directly(self):
        dns = [(2, 1, 6, "", ("93.184.216.34", 80))]
        with patch.dict(os.environ, {"TRUST_PROXY_URL": ""}), \
             patch.object(ns.socket, "getaddrinfo", return_value=dns), \
             patch.object(ns, "_pool") as direct, \
             patch.object(ns.urllib3, "ProxyManager") as proxy:
            direct.return_value.urlopen.return_value = self.stream()
            self.assertEqual(ns.safe_get(None, TARGET).content, PNG)
            direct.assert_called_once_with("http", "93.184.216.34", 80, "cdn.test")
            proxy.assert_not_called()

    def test_stopped_proxy_falls_back_then_next_request_retries_proxy(self):
        fake = [(2, 1, 6, "", ("198.18.0.127", 80))]
        real = [(2, 1, 6, "", ("93.184.216.34", 80))]
        failure = ns.urllib3.exceptions.ProxyError("offline",
            ns.urllib3.exceptions.NewConnectionError(None, "connection refused"))
        with patch.dict(os.environ, {"TRUST_PROXY_URL": "http://127.0.0.1:7890"}), \
             patch.object(ns.socket, "getaddrinfo", side_effect=[fake, real, fake]) as dns, \
             patch.object(ns, "_pool") as direct, \
             patch.object(ns.urllib3, "ProxyManager", autospec=True) as proxy:
            proxy.return_value.urlopen.side_effect = [failure, self.stream()]
            direct.return_value.urlopen.return_value = self.stream()
            self.assertEqual(ns.safe_get(None, TARGET, max_redirects=0).content, PNG)
            self.assertEqual(ns.safe_get(None, TARGET).content, PNG)
            self.assertEqual(dns.call_count, 3)
            direct.assert_called_once_with("http", "93.184.216.34", 80, "cdn.test")
            self.assertEqual(proxy.call_count, 2)
            self.assertEqual(os.environ["TRUST_PROXY_URL"], "http://127.0.0.1:7890")

    def test_fallback_revalidates_dns_and_keeps_timeout_errors(self):
        failure = ns.urllib3.exceptions.ProxyError("offline",
            ns.urllib3.exceptions.NewConnectionError(None, "connection refused"))
        with patch.dict(os.environ, {"TRUST_PROXY_URL": "http://127.0.0.1:7890"}), \
             patch.object(ns.urllib3, "ProxyManager", autospec=True) as proxy, \
             patch.object(ns, "_pool") as direct:
            proxy.return_value.urlopen.side_effect = failure
            with self.assertRaises(ns.UnsafeURLError):
                ns.safe_get(None, TARGET)
            direct.assert_not_called()
            proxy.return_value.urlopen.side_effect = ns.urllib3.exceptions.ReadTimeoutError(None, TARGET, "read timed out")
            with self.assertRaises(ns.urllib3.exceptions.ReadTimeoutError):
                ns.safe_get(None, TARGET)
            direct.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
