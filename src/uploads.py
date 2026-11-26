"""Image uploads: bytes in, a URL that AISUM's servers and DeepSeek can fetch.

Stored in our own Cloud Storage bucket (UPLOAD_BUCKET) under uploads/<random>.<ext>.
The bucket is private and deletes objects after 1 day. On Cloud Run every URL handed
out is a V4 signed GET URL valid for SIGNED_MINUTES; `fresh` signs a new one when an
old one has run out (a re-plan an hour later, the Lookboard export).

Signing has no key file: it goes through the IAM signBlob API with the service
account's access token. That account needs, on the bucket, objectCreator (to store)
and objectViewer (a signed URL reads with the signer's rights), and tokenCreator on
itself (to sign).

A laptop's user credentials cannot sign, so local development gets the plain bucket
URL. That URL only opens if the bucket is readable; it is never used on Cloud Run,
where a signing failure is an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
PREFIX = "uploads/"
SIGNED_MINUTES = 60
RESIGN_MARGIN_S = 300  # a URL with less than this left counts as expired

# Trust the bytes, not the Content-Type header.
_SIGNATURES = {b"\xff\xd8\xff": ("jpg", "image/jpeg"),
               b"\x89PNG\r\n\x1a\n": ("png", "image/png")}


class UploadError(ValueError):
    pass


def bucket_name() -> str | None:
    return os.environ.get("UPLOAD_BUCKET") or None


def sniff(data: bytes) -> tuple[str, str]:
    """(extension, content type) for a JPEG or PNG; UploadError for anything else."""
    for magic, kind in _SIGNATURES.items():
        if data.startswith(magic):
            return kind
    raise UploadError("Only JPEG and PNG images can be uploaded.")


_client = None


def _bucket(name: str):
    global _client
    if _client is None:
        from google.cloud import storage  # imported lazily: only uploads need it

        from .config import FIREBASE_PROJECT_ID
        # User ADC carries no project; the bucket lives in the team's Firebase project.
        _client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT")
                                 or FIREBASE_PROJECT_ID)
    return _client.bucket(name)


def on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))  # set by Cloud Run on every instance


_creds = None
_creds_lock = threading.Lock()


def _signing_identity() -> tuple[str, str]:
    """(service account email, access token), refreshed when the token has run out."""
    global _creds
    import google.auth
    from google.auth.transport.requests import Request

    with _creds_lock:
        if _creds is None:
            _creds, _ = google.auth.default()
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.service_account_email, _creds.token


def _signed_url(name: str, path: str) -> str:
    email, token = _signing_identity()
    return _bucket(name).blob(path).generate_signed_url(
        version="v4", expiration=timedelta(minutes=SIGNED_MINUTES), method="GET",
        service_account_email=email, access_token=token)


def _url(name: str, path: str) -> str:
    if on_cloud_run():
        return _signed_url(name, path)
    return f"https://storage.googleapis.com/{name}/{path}"


def _expired_path(url: str) -> str | None:
    """The object path if `url` is one of our signed URLs and has (nearly) run out.

    Only URLs that are already signed qualify, so an unsigned address of a bucket
    object can never be turned into a working link, and the name must be exactly the
    random name `save` generates."""
    name, parts = bucket_name(), urlsplit(url)
    if not name or parts.hostname != "storage.googleapis.com":
        return None
    found = re.fullmatch(rf"/{re.escape(name)}/({PREFIX}[0-9a-f]{{32}}\.(?:jpg|png))", parts.path)
    query = parse_qs(parts.query)
    if not found or "X-Goog-Signature" not in query:
        return None
    try:
        signed = datetime.strptime(query["X-Goog-Date"][0], "%Y%m%dT%H%M%SZ")
        life = int(query["X-Goog-Expires"][0])
    except (KeyError, ValueError):
        return None
    ends = signed.replace(tzinfo=timezone.utc) + timedelta(seconds=life)
    left = (ends - datetime.now(timezone.utc)).total_seconds()
    return found[1] if left < RESIGN_MARGIN_S else None


async def fresh(url: str) -> str:
    """`url`, or a newly signed one if it is our signed URL and has run out. Any other
    URL comes back unchanged; if signing fails the old URL does too."""
    path = _expired_path(url) if on_cloud_run() else None
    if path is None:
        return url
    try:
        return await asyncio.to_thread(_signed_url, bucket_name(), path)
    except Exception as e:
        log.warning("could not re-sign an upload URL: %s", type(e).__name__)
        return url


async def save(data: bytes, max_bytes: int = MAX_BYTES) -> dict:
    """Validate and store one image. Returns {"url", "path", "content_type", "bytes"}.
    `max_bytes`: the Finch API stores photos up to 10 MB (src/safe_fetch.py)."""
    name = bucket_name()
    if not name:
        raise RuntimeError("UPLOAD_BUCKET is not set")
    if not data:
        raise UploadError("The upload is empty.")
    if len(data) > max_bytes:
        raise UploadError(f"The image is larger than {max_bytes // (1024 * 1024)} MB.")
    ext, content_type = sniff(data)
    path = f"{PREFIX}{uuid.uuid4().hex}.{ext}"

    def upload():
        blob = _bucket(name).blob(path)
        blob.cache_control = "private, max-age=3600"
        blob.upload_from_string(data, content_type=content_type)
        return _url(name, path)

    url = await asyncio.to_thread(upload)  # the client library is blocking
    return {"url": url, "path": path,
            "content_type": content_type, "bytes": len(data)}
