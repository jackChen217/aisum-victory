"""/api/upload offline: the Cloud Storage bucket is faked."""
import pytest
from fastapi.testclient import TestClient

from src import main, uploads
from src.auth import require_user

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class FakeBlob:
    def __init__(self, store, path):
        self.store, self.path, self.cache_control = store, path, None

    def upload_from_string(self, data, content_type):
        self.store[self.path] = (data, content_type, self.cache_control)


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
