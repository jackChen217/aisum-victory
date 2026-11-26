"""Fetch a public image server-side, so the browser can draw it on a canvas.

Product images (m.media-amazon.com and friends) send no CORS headers, so a canvas
that draws them is "tainted" and cannot be exported as PNG. The page fetches them
through this server instead.

Not an open proxy: /api/lookboard-image only accepts a session and slot, and the
server looks the URL up in that user's own session. Even so the URL started as user
input (the photo) or as a catalog URL, so every download goes through
src/safe_fetch.py: https only, every redirect re-checked (at most 3), only public
addresses (checked, then connected to directly: no DNS rebinding), size and type
limits.
"""
from __future__ import annotations

from .safe_fetch import (PHOTO_TYPES, PRODUCT_IMAGE_TYPES, ImageFetchError,  # noqa: F401
                         ImageUpstreamError, ImageUrlRejected, fetch_image)

MAX_BYTES = 8 * 1024 * 1024


async def fetch(url: str, allowed: tuple[str, ...] = PRODUCT_IMAGE_TYPES) -> tuple[bytes, str]:
    """(bytes, content type) of a public image, or ImageFetchError."""
    return await fetch_image(url, max_bytes=MAX_BYTES, allowed=allowed)


async def fetch_photo(url: str) -> tuple[bytes, str]:
    """A user's photo: JPEG or PNG only (what AISUM's image search accepts)."""
    return await fetch_image(url, max_bytes=MAX_BYTES, allowed=PHOTO_TYPES)
