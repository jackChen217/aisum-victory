"""/api/upload offline: the Cloud Storage bucket is faked."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import main, uploads
from src.auth import require_user

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


SIGNED = []


def fresh(url):
    return asyncio.run(uploads.fresh(url))


NAME = "uploads/" + "a" * 32 + ".jpg"


def signed_link(path=NAME, signature="old", age_s=0, life=3600):
    at = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y%m%dT%H%M%SZ")
    return (f"https://storage.googleapis.com/test-bucket/{path}?X-Goog-Algorithm=GOOG4-RSA-SHA256"
            f"&X-Goog-Date={at}&X-Goog-Expires={life}&X-Goog-Signature={signature}")


class FakeBlob:
    def __init__(self, store, path):
        self.store, self.path, self.cache_control = store, path, None

    def upload_from_string(self, data, content_type):
        self.store[self.path] = (data, content_type, self.cache_control)

    def generate_signed_url(self, **kwargs):
        SIGNED.append(kwargs)
        return signed_link(self.path, "new")


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, path):
        return FakeBlob(self.store, path)


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setenv("UPLOAD_BUCKET", "test-bucket")
    monkeypatch.setattr(uploads, "_bucket", lambda name: b)
    return b


@pytest.fixture
def client(bucket):
    main.app.dependency_overrides[require_user] = lambda: "user-a"
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize("data,ctype,ext", [(JPEG, "image/jpeg", "jpg"), (PNG, "image/png", "png")])
def test_upload_stores_under_random_name(client, bucket, data, ctype, ext):
    r = client.post("/api/upload", content=data, headers={"Content-Type": ctype})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"https://storage.googleapis.com/test-bucket/{body['path']}"
    assert body["path"].startswith("uploads/") and body["path"].endswith(f".{ext}")
    assert len(body["path"]) == len("uploads/") + 32 + 1 + len(ext)
    assert bucket.store[body["path"]][:2] == (data, ctype)


def test_type_comes_from_bytes_not_header(client, bucket):
    r = client.post("/api/upload", content=b"GIF89a....", headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 415
    assert bucket.store == {}
    # A PNG labelled as JPEG is stored as PNG.
    r = client.post("/api/upload", content=PNG, headers={"Content-Type": "image/jpeg"})
    assert r.json()["content_type"] == "image/png"


def test_too_large_and_empty(client, bucket):
    big = JPEG + b"\x00" * uploads.MAX_BYTES
    assert client.post("/api/upload", content=big).status_code == 413
    assert client.post("/api/upload", content=b"").status_code == 400
    assert bucket.store == {}


def test_needs_sign_in(bucket):
    with TestClient(main.app) as c:
        assert c.post("/api/upload", content=JPEG).status_code == 401


def test_not_configured(client, monkeypatch):
    monkeypatch.delenv("UPLOAD_BUCKET")
    assert client.post("/api/upload", content=JPEG).status_code == 503


def test_storage_failure_is_502(client, monkeypatch):
    def broken(name):
        raise PermissionError("403 caller does not have storage.objects.create")

    monkeypatch.setattr(uploads, "_bucket", broken)
    r = client.post("/api/upload", content=JPEG)
    assert r.status_code == 502 and "storage.objects.create" in r.json()["error"]


@pytest.fixture
def cloud_run(monkeypatch, bucket):
    SIGNED.clear()
    monkeypatch.setenv("K_SERVICE", "app")
    monkeypatch.setattr(uploads, "_signing_identity", lambda: ("sa@test", "token-1"))


def test_cloud_run_returns_a_60_minute_signed_get_url(client, cloud_run, bucket):
    body = client.post("/api/upload", content=JPEG).json()
    assert "X-Goog-Signature=new" in body["url"] and body["path"] in body["url"]
    assert SIGNED == [{"version": "v4", "expiration": timedelta(minutes=60), "method": "GET",
                       "service_account_email": "sa@test", "access_token": "token-1"}]
    assert bucket.store[body["path"]][2].startswith("private")


def test_cloud_run_never_falls_back_to_a_plain_url(client, cloud_run, monkeypatch):
    def broken():
        raise PermissionError("iam.serviceAccounts.signBlob denied")

    monkeypatch.setattr(uploads, "_signing_identity", broken)
    r = client.post("/api/upload", content=JPEG)
    assert r.status_code == 502 and "signBlob" in r.json()["error"]


def test_crop_uploads_are_signed_too(cloud_run):
    assert "X-Goog-Signature=new" in asyncio.run(uploads.save(JPEG))["url"]


def test_fresh_resigns_only_our_expired_signed_urls(cloud_run):
    expired = signed_link(age_s=3600)
    assert "Signature=new" in fresh(expired)
    assert "Signature=new" in fresh(signed_link(age_s=3400))  # almost out
    unchanged = [
        signed_link(age_s=60),                                   # still good
        f"https://storage.googleapis.com/test-bucket/{NAME}",    # never signed
        signed_link(path="other/" + "a" * 32 + ".jpg", age_s=7200),  # not an upload
        signed_link(path="uploads/../secret.jpg", age_s=7200),
        expired.replace("test-bucket", "another-bucket"),
        expired.replace("storage.googleapis.com", "example.com"),
        "https://images.unsplash.com/photo.jpg",
    ]
    for url in unchanged:
        assert fresh(url) == url
    assert len(SIGNED) == 2


def test_fresh_keeps_the_url_when_signing_fails(cloud_run, monkeypatch):
    def broken():
        raise PermissionError("denied")

    monkeypatch.setattr(uploads, "_signing_identity", broken)
    expired = signed_link(age_s=3600)
    assert fresh(expired) == expired


def test_local_development_does_not_sign(bucket, monkeypatch):
    SIGNED.clear()
    monkeypatch.delenv("K_SERVICE", raising=False)
    expired = signed_link(age_s=3600)
    assert fresh(expired) == expired
    assert SIGNED == []


def test_replan_and_lookboard_renew_an_expired_photo_link(client, cloud_run, monkeypatch):
    from src.agent import orchestrator

    expired = signed_link(age_s=3600)
    orchestrator.SESSIONS[("user-a", "s1")] = {
        "scenario": "outfit", "image_url": expired, "notes": "", "gender": "F",
        "search_budget": 100, "budget": 100, "owned": [], "pins": {}, "slots": [],
        "unmatched": [], "desk_width_in": None, "candidates": {}, "scores": {}, "plan": {}}
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return JPEG, "image/jpeg"

    monkeypatch.setattr(main.image_proxy, "fetch", fetch)
    assert client.get("/api/lookboard-image",
                      params={"session_id": "s1", "slot": "_photo"}).status_code == 200
    assert "Signature=new" in fetched[0]

    r = client.post("/api/replan?format=ui", json={"session_id": "s1", "budget": 80})
    assert r.status_code == 200 and "Signature=new" in r.json()["original_image"]
    orchestrator.SESSIONS.pop(("user-a", "s1"))
