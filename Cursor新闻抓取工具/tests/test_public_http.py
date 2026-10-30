"""Offline transport tests: no DNS query or real socket is permitted."""
from contextlib import ExitStack
import io
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_http


def answer(ip, port=80):
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                "", (ip, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            "", (ip, port))


class PublicHTTPTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.resolve = stack.enter_context(mock.patch(
            "public_http.socket.getaddrinfo",
            side_effect=AssertionError("unexpected DNS query")))
        self.sockets = stack.enter_context(mock.patch(
            "public_http.socket.socket",
            side_effect=AssertionError("unexpected socket creation")))
        self.standard_connection = stack.enter_context(mock.patch(
            "public_http.socket.create_connection",
            side_effect=AssertionError("must not resolve target again")))

    def allow(self, addresses):
        self.resolve.side_effect = None
        self.resolve.return_value = addresses
        self.sockets.side_effect = None
        self.sockets.return_value = mock.MagicMock(name="tcp_socket")
        return self.sockets.return_value

    def test_rejects_nonpublic_addresses_before_creating_socket(self):
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
                   "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                   "240.0.0.1", "::1", "fc00::1", "fe80::1", "ff02::1",
                   "::ffff:127.0.0.1"):
            with self.subTest(ip=ip):
                self.resolve.side_effect = None
                self.resolve.return_value = [answer(ip)]
                with self.assertRaises(public_http.PublicAddressError):
                    public_http._public_create_connection(("news.example", 80), 3)
        self.sockets.assert_not_called()

    def test_rejects_mixed_public_private_dns_before_any_connection(self):
        self.allow([answer("8.8.8.8"), answer("127.0.0.1")])
        with self.assertRaises(public_http.PublicAddressError):
            public_http._public_create_connection(("news.example", 80), 3)
        self.sockets.assert_not_called()

    def test_connects_validated_ip_with_one_dns_lookup(self):
        sock = self.allow([answer("8.8.8.8")])
        result = public_http._public_create_connection(("news.example", 80), 3)
        self.assertIs(result, sock)
        self.resolve.assert_called_once_with("news.example", 80, 0,
                                             socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.connect.assert_called_once_with(("8.8.8.8", 80))
        sock.settimeout.assert_called_once_with(3)
        self.standard_connection.assert_not_called()

    def test_connects_numeric_ipv6(self):
        sock = self.allow([answer("2606:4700:4700:0:0:0:0:1111", 443)])
        public_http._public_create_connection(("news.example", 443), 4)
        sock.connect.assert_called_once_with(("2606:4700:4700::1111", 443, 0, 0))
        self.resolve.assert_called_once()

    def test_fallback_uses_only_checked_addresses_and_closes_failed_socket(self):
        first = self.allow([answer("8.8.8.8"), answer("1.1.1.1")])
        second = mock.MagicMock(name="second_socket")
        first.connect.side_effect = OSError("first failed")
        self.sockets.side_effect = [first, second]
        result = public_http._public_create_connection(("news.example", 80), 2)
        self.assertIs(result, second)
        first.close.assert_called_once()
        second.connect.assert_called_once_with(("1.1.1.1", 80))
        self.resolve.assert_called_once()

    def test_failed_connection_closes_socket_and_preserves_error(self):
        sock = self.allow([answer("8.8.8.8")])
        failure = OSError("connection refused")
        sock.connect.side_effect = failure
        with self.assertRaises(OSError) as raised:
            public_http._public_create_connection(("news.example", 80), 2)
        self.assertIs(raised.exception, failure)
        sock.close.assert_called_once()

    def test_empty_dns_result_is_rejected(self):
        self.allow([])
        with self.assertRaises(public_http.PublicAddressError):
            public_http._public_create_connection(("news.example", 80))
        self.sockets.assert_not_called()

    def test_http_host_header_remains_original_hostname(self):
        sock = self.allow([answer("8.8.8.8")])
        connection = public_http.PublicHTTPConnection("news.example", timeout=3)
        connection.request("GET", "/article")
        wire = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        self.assertIn(b"Host: news.example\r\n", wire)
        sock.connect.assert_called_once_with(("8.8.8.8", 80))

    def test_https_default_context_checks_certificate_and_hostname(self):
        connection = public_http.PublicHTTPSConnection("news.example")
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)

    def test_https_uses_original_hostname_for_sni_after_numeric_connection(self):
        sock = self.allow([answer("8.8.8.8", 443)])
        connection = public_http.PublicHTTPSConnection("news.example", timeout=3)
        with mock.patch.object(connection._context, "wrap_socket") as wrap:
            connection.connect()
        wrap.assert_called_once_with(sock, server_hostname="news.example")
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)

    def test_opener_never_loads_environment_or_system_proxy_settings(self):
        with mock.patch("urllib.request.getproxies",
                        side_effect=AssertionError("proxy settings must be ignored")):
            opener = public_http.public_opener()
        transports = [type(h) for h in opener.handlers
                      if isinstance(h, (urllib.request.HTTPHandler,
                                        urllib.request.HTTPSHandler))]
        self.assertEqual(set(transports), {public_http._PublicHTTPHandler,
                                          public_http._PublicHTTPSHandler})

    def test_opener_rejects_non_http_schemes_and_url_credentials(self):
        opener = public_http.public_opener()
        for url in ("file:///private/tmp/example", "ftp://news.example/article",
                    "data:text/plain,example", "http://user:pass@news.example/article"):
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                opener.open(url, timeout=2)
        self.resolve.assert_not_called()
        self.sockets.assert_not_called()

    def test_explicit_proxy_request_is_rejected(self):
        request = urllib.request.Request("https://news.example/article")
        request.set_proxy("proxy.example:8080", "http")
        with self.assertRaises(urllib.error.URLError):
            public_http.public_opener().open(request, timeout=2)
        self.resolve.assert_not_called()

    def test_redirect_target_is_rechecked_at_connection_time(self):
        sock = self.allow([answer("8.8.8.8")])
        sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/article\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        self.resolve.side_effect = [[answer("8.8.8.8")], [answer("127.0.0.1")]]
        with self.assertRaises(urllib.error.URLError) as raised:
            public_http.public_opener().open("http://news.example/article", timeout=2)
        self.assertIsInstance(raised.exception.reason, public_http.PublicAddressError)
        self.assertEqual(self.resolve.call_count, 2)
        self.sockets.assert_called_once()

    def test_custom_redirect_handler_is_supported(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        handler = NoRedirect()
        opener = public_http.public_opener(handler)
        self.assertIn(handler, opener.handlers)
        redirects = [h for h in opener.handlers
                     if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(redirects, [handler])

    def test_transport_or_proxy_overrides_are_rejected(self):
        for handler in (urllib.request.ProxyHandler({}), urllib.request.ProxyHandler,
                        urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()):
            with self.subTest(handler=handler), self.assertRaises(ValueError):
                public_http.public_opener(handler)


if __name__ == "__main__":
    unittest.main()
