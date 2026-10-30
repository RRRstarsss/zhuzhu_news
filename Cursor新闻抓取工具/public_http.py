"""HTTP(S) connections restricted to addresses verified at connection time.

By default the opener ignores environment/system proxies. Each direct
connection resolves its hostname once, rejects the entire result if any address
is not public, and connects to a validated numeric address. The original
hostname stays on the HTTP connection for Host, TLS SNI, and certificate checks.

Explicit system_proxy=True trusts the machine's configured HTTP(S) proxies.
Proxied targets are checked before forwarding; synthetic DNS answers in
198.18.0.0/15 are allowed only for a hostname routed through its configured
proxy. Localhost, private literal targets, and other nonpublic answers remain
blocked. This mode cannot bind or verify the proxy's own target DNS resolution.
Missing or bypassed proxies use the strict direct connection path.

Timeouts retain the standard socket semantics: they are not a wall-clock limit
for DNS resolution, multiple address attempts, redirects, or a complete body.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

_SYNTHETIC_DNS = ipaddress.ip_network("198.18.0.0/15")


class PublicAddressError(OSError):
    """A target cannot be established as a public network address."""


def _is_public(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is None or _is_public(mapped)


def _checked_addresses(host, port, allow_synthetic=False):
    answers = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM,
                                 socket.IPPROTO_TCP)
    if not answers:
        raise PublicAddressError("no_public_address: empty DNS result")
    checked = []
    # Check every answer before opening a socket, including fallback addresses.
    for family, kind, protocol, _canonical, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise PublicAddressError("unsupported_address_family")
        try:
            if "%" in sockaddr[0]:
                raise ValueError("scoped address")
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise PublicAddressError("invalid_resolved_address") from exc
        synthetic = allow_synthetic and address.version == 4 and address in _SYNTHETIC_DNS
        if not _is_public(address) and not synthetic:
            raise PublicAddressError("non_public_address: " + str(address))
        if family == socket.AF_INET:
            if address.version != 4:
                raise PublicAddressError("address_family_mismatch")
            numeric = (str(address), sockaddr[1])
        else:
            if address.version != 6 or sockaddr[3]:
                raise PublicAddressError("invalid_or_scoped_ipv6_address")
            numeric = (str(address), sockaddr[1], sockaddr[2], sockaddr[3])
        checked.append((family, kind, protocol, numeric))
    return checked


def _target_parts(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only_http_https_allowed")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid_url_authority")
    if parsed.port not in (None, 80, 443):
        raise ValueError("only_target_ports_80_443_allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise PublicAddressError("localhost_target_not_allowed")
    if any(c.isspace() or ord(c) < 32 for c in host) or "%" in host or "\\" in host:
        raise ValueError("invalid_url_hostname")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _is_public(literal):
        # The synthetic exception is for a proxy's DNS mapping, not IP URLs.
        raise PublicAddressError("non_public_literal_target")
    return parsed


def _system_proxies():
    """Take proxy endpoints only from trusted machine settings, never a URL row."""
    result = {}
    for scheme, value in urllib.request.getproxies().items():
        if scheme.lower() not in {"http", "https"} or not isinstance(value, str):
            continue
        value = value.strip()
        if "://" not in value:
            value = "http://" + value
        try:
            parsed = urllib.parse.urlsplit(value)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or parsed.port == 0 or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                continue
        except ValueError:
            continue
        result[scheme.lower()] = value
    return result


def _will_proxy(parsed, proxies):
    return bool(proxies.get(parsed.scheme.lower())) and not urllib.request.proxy_bypass(parsed.netloc)


def _validate_target(url, proxies):
    parsed = _target_parts(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    _checked_addresses(parsed.hostname, port, allow_synthetic=_will_proxy(parsed, proxies))
    return url


def validate_target(url, system_proxy=False):
    """Check an absolute HTTP(S) target on port 80/443; return the input URL.

    This preflight does not replace the connection-time direct-address check.
    In proxy mode, only matching, effective system proxy settings permit a
    synthetic DNS answer. It does not resolve or pin the proxy's target DNS.
    """
    return _validate_target(url, _system_proxies() if system_proxy else {})


def _public_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None):
    """Like socket.create_connection, without resolving the target twice."""
    host, port = address
    candidates = _checked_addresses(host, port)
    last_error = None
    for family, kind, protocol, numeric in candidates:
        sock = None
        try:
            sock = socket.socket(family, kind, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            # Numeric sockaddr only: never pass the hostname to connect().
            sock.connect(numeric)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_create_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        # Keep the standard HTTPS context, hostname checking, and SNI behavior.
        super().__init__(*args, **kwargs)
        self._create_connection = _public_create_connection


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class _PublicURLGuard(urllib.request.BaseHandler):
    def __init__(self, proxies=None, token=None):
        self.proxies = proxies or {}
        self.token = token

    def default_open(self, req):
        try:
            parsed = _target_parts(req.full_url)
            internal_proxy_request = (self.token is not None
                                      and getattr(req, "_public_proxy_token", None) is self.token)
            if (req.has_proxy() or getattr(req, "_tunnel_host", None)) and not internal_proxy_request:
                raise ValueError("proxies_not_allowed")
            if _will_proxy(parsed, self.proxies):
                _validate_target(req.full_url, self.proxies)
        except (ValueError, OSError) as exc:
            raise urllib.error.URLError(str(exc)) from exc
        return None


class _TrustedSystemProxyHandler(urllib.request.ProxyHandler):
    def __init__(self, proxies, token):
        super().__init__(proxies)
        self.token = token

    def proxy_open(self, req, proxy, scheme):
        # The URL guard has already checked this target. Mark only requests
        # routed by this opener's trusted ProxyHandler, not user-set proxies.
        req._public_proxy_token = self.token
        return super().proxy_open(req, proxy, scheme)


def _uses_trusted_proxy(req, token):
    return (getattr(req, "_public_proxy_token", None) is token
            and bool(req.has_proxy() or getattr(req, "_tunnel_host", None)))


class _ProxyAwareHTTPHandler(_PublicHTTPHandler):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def http_open(self, req):
        connection = http.client.HTTPConnection if _uses_trusted_proxy(req, self.token) else PublicHTTPConnection
        return self.do_open(connection, req)


class _ProxyAwareHTTPSHandler(_PublicHTTPSHandler):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def https_open(self, req):
        connection = http.client.HTTPSConnection if _uses_trusted_proxy(req, self.token) else PublicHTTPSConnection
        return self.do_open(connection, req, context=self._context)


def public_opener(*handlers, system_proxy=False):
    """Build a public HTTP(S) opener with optional redirect/response handlers.

    Transport/proxy replacements are rejected so these options cannot silently
    re-enable an unvalidated connection path. Caller-provided handlers are
    otherwise trusted Python code, just as with urllib.request.build_opener.
    """
    protected = (urllib.request.ProxyHandler, urllib.request.HTTPHandler,
                 urllib.request.HTTPSHandler)
    for handler in handlers:
        kind = handler if isinstance(handler, type) else type(handler)
        if issubclass(kind, protected):
            raise ValueError("transport_and_proxy_handlers_cannot_be_overridden")
    proxies = _system_proxies() if system_proxy else {}
    if proxies:
        token = object()
        return urllib.request.build_opener(
            _TrustedSystemProxyHandler(proxies, token), _PublicURLGuard(proxies, token),
            _ProxyAwareHTTPHandler(token), _ProxyAwareHTTPSHandler(token), *handlers)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicURLGuard(),
        _PublicHTTPHandler(), _PublicHTTPSHandler(), *handlers)
