from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import zlib
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


class FetchError(RuntimeError):
    pass


# These infrastructure endpoints must not become reachable merely because an
# operator enables checks of private application addresses. Link-local ranges
# are rejected separately; the explicit set also covers metadata services in
# otherwise usable private/CGNAT address space.
BLOCKED_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
        # Azure WireServer is a host-local platform endpoint reachable on port
        # 80 even though it is represented by a nominally public address.
        ipaddress.ip_address("168.63.129.16"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    hostname: str
    port: int
    target_ip: str
    request_target: str


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


def _permitted_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address,
                       allow_private: bool) -> bool:
    # IPv4-mapped IPv6 literals must be evaluated as IPv4 or they can bypass
    # both exact metadata checks and IPv4 address-classification rules.
    canonical = (
        address.ipv4_mapped
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped
        else address
    )
    if canonical in BLOCKED_METADATA_ADDRESSES:
        return False
    if allow_private:
        # Private and loopback ranges can be intentionally monitored, but
        # link-local metadata endpoints and non-unicast addresses stay blocked.
        return not (
            canonical.is_unspecified
            or canonical.is_multicast
            or canonical.is_link_local
            or (canonical.is_reserved and not canonical.is_loopback)
        )
    return canonical.is_global


def validate_outbound_url(
    raw_url: str,
    *,
    allow_private: bool,
    allowed_ports: frozenset[int],
) -> ValidatedURL:
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2048:
        raise UnsafeURLError("URL is required and must be at most 2048 characters")
    raw_url = raw_url.strip()
    if any(ord(character) < 32 for character in raw_url):
        raise UnsafeURLError("URL contains control characters")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError("only absolute HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URL credentials are not allowed")
    if parsed.fragment:
        raise UnsafeURLError("URL fragments are not allowed")
    if port not in allowed_ports:
        raise UnsafeURLError(f"outbound port {port} is not allowed")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise UnsafeURLError("hostname is invalid") from exc
    if not hostname or hostname.endswith(".localhost") or hostname == "localhost":
        if not allow_private:
            raise UnsafeURLError("private and local destinations are disabled")

    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
        addresses = [literal]
    except ValueError:
        try:
            answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURLError("hostname could not be resolved") from exc
        addresses = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer[4][0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise UnsafeURLError("hostname did not resolve to an IP address")
    if any(not _permitted_address(address, allow_private) for address in addresses):
        raise UnsafeURLError("destination resolves to a blocked IP range")

    host_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = host_for_netloc if port == default_port else f"{host_for_netloc}:{port}"
    path = quote(parsed.path or "/", safe="/%:@-._~!$&'()*+,;=")
    query = quote(parsed.query, safe="=&?/:@-._~!$'()*+,;%")
    request_target = path + (f"?{query}" if query else "")
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    return ValidatedURL(
        url=normalized,
        scheme=scheme,
        hostname=hostname,
        port=port,
        target_ip=str(addresses[0]),
        request_target=request_target,
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, target_ip: str, port: int, timeout: int):
        super().__init__(hostname, port=port, timeout=timeout)
        self._target_ip = target_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._target_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, target_ip: str, port: int, timeout: int):
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._target_ip = target_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._target_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _read_limited(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    if max_bytes == 0:
        # A single byte is enough to make servers start sending the response;
        # closing the connection immediately keeps availability checks cheap.
        response.read(1)
        return b""
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise FetchError(f"response exceeds {max_bytes} bytes")
    encoding = response.getheader("Content-Encoding", "").lower()
    if not encoding or encoding == "identity":
        return raw
    if encoding == "gzip":
        try:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = decompressor.decompress(raw, max_bytes + 1)
            if decompressor.unconsumed_tail or len(body) > max_bytes:
                raise FetchError(f"decompressed response exceeds {max_bytes} bytes")
            remaining = max_bytes + 1 - len(body)
            body += decompressor.flush(remaining)
        except zlib.error as exc:
            raise FetchError("invalid gzip response") from exc
        if not decompressor.eof:
            raise FetchError("truncated gzip response")
        if len(body) > max_bytes:
            raise FetchError(f"decompressed response exceeds {max_bytes} bytes")
        return body
    raise FetchError(f"unsupported content encoding: {encoding}")


def fetch_url(
    raw_url: str,
    *,
    allow_private: bool,
    allowed_ports: frozenset[int],
    timeout: int,
    max_bytes: int,
    accept: str = "*/*",
    max_redirects: int = 3,
) -> FetchResult:
    current_url = raw_url
    for redirect_count in range(max_redirects + 1):
        target = validate_outbound_url(
            current_url,
            allow_private=allow_private,
            allowed_ports=allowed_ports,
        )
        connection_type = (
            _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(
            target.hostname, target.target_ip, target.port, timeout
        )
        try:
            connection.request(
                "GET",
                target.request_target,
                headers={
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "ffknd-portal/0.1 (+status-and-feed-reader)",
                },
            )
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(4096)
                if not location:
                    raise FetchError("redirect response has no Location header")
                if redirect_count >= max_redirects:
                    raise FetchError("too many redirects")
                current_url = urljoin(target.url, location)
                continue
            body = _read_limited(response, max_bytes)
            return FetchResult(target.url, response.status, headers, body)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise FetchError(str(exc) or exc.__class__.__name__) from exc
        finally:
            connection.close()
    raise FetchError("too many redirects")


def validate_redirect_target(raw_url: str) -> str:
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2048:
        raise ValueError("target_url is required and must be at most 2048 characters")
    raw_url = raw_url.strip()
    try:
        parsed = urlsplit(raw_url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("target_url is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target_url cannot contain credentials")
    if any(ord(character) < 32 for character in raw_url):
        raise ValueError("target_url contains control characters")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise ValueError("target_url hostname is invalid") from exc
    host_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_netloc
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    path = quote(parsed.path or "/", safe="/%:@-._~!$&'()*+,;=")
    query = quote(parsed.query, safe="=&?/:@-._~!$'()*+,;%")
    fragment = quote(parsed.fragment, safe="?/:@-._~!$&'()*+,;=%")
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, fragment))
