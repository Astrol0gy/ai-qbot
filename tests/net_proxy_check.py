"""Fake-IP downloads via an explicit local test proxy; no external APIs."""
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
