"""The fixed file WeChat fetches for the domain appeal. No sign-in."""
from fastapi.testclient import TestClient

from src import main


def test_verification_file_is_served_exactly():
    r = TestClient(main.app).get("/102905f7b945d56af40e18fb57d9d994.txt")
    assert r.status_code == 200
    assert r.content == b"49660d71b2a5f9874ff53248f7bb5402979df318"
    assert r.headers["content-type"].startswith("text/plain")


def test_no_other_root_file_is_served():
    client = TestClient(main.app)
    assert client.get("/other.txt").status_code == 404
    assert client.get("/.env").status_code == 404
