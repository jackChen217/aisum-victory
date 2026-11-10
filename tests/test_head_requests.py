"""Pages answer HEAD (uptime checks, link previews, `curl -I`) the same as GET, minus the body."""
import pytest
from fastapi.testclient import TestClient

from src import main

PAGES = ["/", "/ghostcart", "/classic", "/ghostcart/batch"]


@pytest.mark.parametrize("path", PAGES)
def test_page_answers_head_like_get(path):
    client = TestClient(main.app)
    get, head = client.get(path), client.head(path)
    assert head.status_code == get.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["cache-control"] == "no-cache"


def test_api_routes_still_reject_head():
    assert TestClient(main.app).head("/api/firebase-config").status_code == 405
