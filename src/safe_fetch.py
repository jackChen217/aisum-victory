"""Download an image from a URL someone else gave us, without SSRF (SlowMist
SM-AR-2026-035: Finch image_url followed redirects to http and to private addresses).

Every hop, the first one and each redirect, is checked again before anything is sent:

- https only, port 443 only, no user:password@ in the URL. A redirect to http is
  refused (no downgrade).
- The host name is resolved here, and EVERY address it resolves to must be public:
  no private (10/8, 172.16/12, 192.168/16, fc00::/7), loopback (127/8, ::1), link-local
  (169.254/16, fe80::/10, so also the 169.254.169.254 metadata server), fd00:ec2::254,
  0.0.0.0 / ::, multicast, reserved, CGNAT 100.64/10, documentation ranges, or an
  IPv4 address hidden in IPv6 (::ffff:127.0.0.1). One bad address refuses the URL.
- The connection then goes to one of those checked addresses, never to a fresh DNS
  lookup (no DNS rebinding). TLS still verifies the certificate for the host name
  (httpcore's sni_hostname), and the Host header names the host.
- Redirects are followed by hand, at most MAX_REDIRECTS. Proxies from the
  environment are ignored (trust_env=False), so nothing bypasses the address check.
- Connect timeout 5 s, 15 s for the whole download (all hops), at most max_bytes
  (default 10 MB), Content-Type must be an allowed image type and the file's first
  bytes must say so too.

Errors: ImageUrlRejected (the URL or what it points at is not acceptable: the caller
answers 422) and ImageUpstreamError (the host failed or timed out: 5xx). Both are
ImageFetchError and carry a short `reason` code.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 3
CONNECT_TIMEOUT_S = 5.0
TOTAL_TIMEOUT_S = 15.0
MAX_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = ("image/jpeg", "image/png")
# Product images for the Lookboard export may also be WebP / GIF / AVIF.
PRODUCT_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = "GhostCart/1.0 (+image fetch)"

_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}
# Named on top of what ipaddress already treats as not global, so the intent is explicit.
_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "fd00:ec2::254/128",
)]


class ImageFetchError(ValueError):
    """Base: `reason` is a short code, str(e) a sentence safe to show the caller."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class ImageUrlRejected(ImageFetchError):
    """The URL, a redirect, or the file is not acceptable (caller's input: 422)."""


class ImageUpstreamError(ImageFetchError):
    """The image host failed or timed out (5xx)."""


def blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None and blocked_ip(inner):
            return True
    return (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or ip.is_loopback or ip.is_link_local or ip.is_private
            or any(ip in net for net in _BLOCKED))


def _check_url(url: str):
    """-> SplitResult of an acceptable URL, or ImageUrlRejected."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ImageUrlRejected("bad_url", "The image URL is not a valid URL.") from None
    if parts.scheme.lower() != "https":
        raise ImageUrlRejected("not_https", "The image URL must use https.")
    if not parts.hostname or any(c.isspace() for c in url):
        raise ImageUrlRejected("bad_url", "The image URL is not a valid URL.")
    if parts.username or parts.password:
        raise ImageUrlRejected("bad_url", "The image URL must not contain a user name or password.")
    if port not in (None, 443):
        raise ImageUrlRejected("port_not_allowed", "The image URL must use the standard https port.")
    return parts


async def _resolve(host: str) -> list[str]:
    """All addresses for `host` (tests replace this)."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _checked_addresses(host: str) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addrs = [str(literal)]
    else:
        try:
            addrs = await _resolve(host)
        except (socket.gaierror, UnicodeError):
            raise ImageUrlRejected("unresolvable", "The image host name does not resolve.") from None
    if not addrs:
        raise ImageUrlRejected("unresolvable", "The image host name does not resolve.")
    ips = []
    for a in dict.fromkeys(addrs):
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if blocked_ip(ip):
            raise ImageUrlRejected("private_address",
                                   "The image URL points at a private or reserved address.")
        ips.append(ip)
    ips.sort(key=lambda ip: ip.version)          # IPv4 first (Cloud Run egress)
    return [str(ip) for ip in ips]


def _client() -> httpx.AsyncClient:
    """Tests replace this with a MockTransport client."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        follow_redirects=False, trust_env=False)


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def _pinned(parts, ip: str) -> tuple[str, dict, dict]:
    """The URL with the checked address as host, plus the Host header and TLS name."""
    host = f"[{ip}]" if ":" in ip else ip
    target = parts._replace(netloc=host).geturl()
    return target, {"Host": parts.hostname, "User-Agent": USER_AGENT,
                    "Accept": "image/*"}, {"sni_hostname": parts.hostname}


async def _one_hop(client, parts, max_bytes: int, allowed: tuple[str, ...]):
    """-> ("redirect", location) or ("image", bytes, type)."""
    addrs = await _checked_addresses(parts.hostname)
    last_error: Exception | None = None
    for ip in addrs:
        url, headers, ext = _pinned(parts, ip)
        try:
            request = client.build_request("GET", url, headers=headers, extensions=ext)
            response = await client.send(request, stream=True)
        except httpx.ConnectError as e:
            last_error = e
            continue                                   # try the next checked address
        try:
            return await _read(response, max_bytes, allowed)
        finally:
            await response.aclose()
    raise ImageUpstreamError("connect_failed",
                             f"Could not connect to the image host ({type(last_error).__name__}).")


async def _read(r: httpx.Response, max_bytes: int, allowed: tuple[str, ...]):
    if r.status_code in REDIRECT_CODES:
        location = r.headers.get("location")
        if not location:
            raise ImageUrlRejected("bad_redirect", "The image URL redirects without a location.")
        return ("redirect", location)
    if 400 <= r.status_code < 500:
        raise ImageUrlRejected("http_status",
                               f"The image host answered HTTP {r.status_code} for this URL.")
    if r.status_code != 200:
        raise ImageUpstreamError("http_status", f"The image host answered HTTP {r.status_code}.")
    kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
    kind = _ALIASES.get(kind, kind)
    if kind not in allowed:
        raise ImageUrlRejected("not_image", "The URL is not a " + " or ".join(
            t.split("/")[1].upper() for t in allowed) + f" image ({kind or 'no content type'}).")
    length = r.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise ImageUrlRejected("too_large", f"The image is larger than {max_bytes // 2**20} MB.")
    data = bytearray()
    async for chunk in r.aiter_bytes():
        data += chunk
        if len(data) > max_bytes:
            raise ImageUrlRejected("too_large", f"The image is larger than {max_bytes // 2**20} MB.")
    sniffed = _sniff(bytes(data[:16]))
    if sniffed not in allowed:
        raise ImageUrlRejected("not_image", "The file is not a valid image of an allowed type.")
    return ("image", bytes(data), sniffed)


async def fetch_image(url: str, *, max_bytes: int = MAX_BYTES,
                      allowed: tuple[str, ...] = PHOTO_TYPES) -> tuple[bytes, str]:
    """(bytes, content type from the file header) of the image at `url`."""
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            async with _client() as client:
                current = url
                for hop in range(MAX_REDIRECTS + 1):
                    result = await _one_hop(client, _check_url(current), max_bytes, allowed)
                    if result[0] == "image":
                        return result[1], result[2]
                    current = urljoin(current, result[1])
                raise ImageUrlRejected("too_many_redirects",
                                       f"The image URL redirects more than {MAX_REDIRECTS} times.")
    except TimeoutError:
        raise ImageUpstreamError("timeout", f"The image took longer than "
                                            f"{TOTAL_TIMEOUT_S:g} seconds to download.") from None
    except httpx.TimeoutException:
        raise ImageUpstreamError("timeout", "The image host timed out.") from None
    except httpx.HTTPError as e:
        raise ImageUpstreamError("fetch_failed",
                                 f"Could not download the image ({type(e).__name__}).") from None
