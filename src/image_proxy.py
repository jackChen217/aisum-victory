"""Fetch a public image server-side, so the browser can draw it on a canvas.

Product images (m.media-amazon.com and friends) send no CORS headers, so a canvas
that draws them is "tainted" and cannot be exported as PNG. The page fetches them
through this server instead.

Not an open proxy: /api/lookboard-image only accepts a session and slot, and the
server looks the URL up in that user's own session. Even so the URL started as user
input (the photo) or as a catalog URL, so it must be https, resolve only to public
addresses (no metadata server, no localhost), and must not redirect.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT = 15.0


class ImageFetchError(ValueError):
    pass


async def _public_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ImageFetchError("only https image URLs are allowed")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname,
                                        parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ImageFetchError(f"cannot resolve {parts.hostname}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ImageFetchError("the image host is not a public address")


async def fetch(url: str) -> tuple[bytes, str]:
    """(bytes, content type) of a public image, or ImageFetchError."""
    await _public_host(url)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        async with client.stream("GET", url, headers={"User-Agent": "GhostCart/1.0"}) as r:
            if r.status_code != 200:
                raise ImageFetchError(f"the image host answered HTTP {r.status_code}")
            kind = r.headers.get("content-type", "").split(";")[0].strip()
            if not kind.startswith("image/"):
                raise ImageFetchError(f"not an image ({kind or 'no content type'})")
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ImageFetchError("the image is too large")
    return bytes(data), kind
