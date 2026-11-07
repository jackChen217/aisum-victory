"""Image uploads: bytes in, a public URL that AISUM's servers and DeepSeek can fetch.

Stored in our own Cloud Storage bucket (UPLOAD_BUCKET) under uploads/<random>.<ext>.
The bucket is public-read (allUsers: objectViewer) and deletes objects after 1 day,
so a URL works long enough for a plan and its re-plans and then disappears.

Credentials are Application Default Credentials: `gcloud auth application-default
login` on a laptop, the service account on Cloud Run (it needs objectCreator on the
bucket). No key file.
"""
from __future__ import annotations

import asyncio
import os
import uuid

MAX_BYTES = 8 * 1024 * 1024
PREFIX = "uploads/"

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


async def save(data: bytes) -> dict:
    """Validate and store one image. Returns {"url", "path", "content_type", "bytes"}."""
    name = bucket_name()
    if not name:
        raise RuntimeError("UPLOAD_BUCKET is not set")
    if not data:
        raise UploadError("The upload is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    ext, content_type = sniff(data)
    path = f"{PREFIX}{uuid.uuid4().hex}.{ext}"

    def upload():
        blob = _bucket(name).blob(path)
        blob.cache_control = "public, max-age=3600"
        blob.upload_from_string(data, content_type=content_type)

    await asyncio.to_thread(upload)  # the client library is blocking
    return {"url": f"https://storage.googleapis.com/{name}/{path}", "path": path,
            "content_type": content_type, "bytes": len(data)}
