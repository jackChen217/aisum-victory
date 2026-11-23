"""Pages carry ?v=<hash of the static scripts and styles>, and still ask browsers to revalidate."""
import re

import pytest
from fastapi.testclient import TestClient

from src import main

PAGES = ["/", "/ghostcart", "/classic", "/ghostcart/batch"]
ASSET = re.compile(r'(?:src|href)="(/static/[^"]+\.(?:js|css)[^"]*)"')


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app)


@pytest.mark.parametrize("path", PAGES)
def test_every_local_asset_carries_the_current_version(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assets = ASSET.findall(r.text)
    assert assets, "the page should load at least one /static script or stylesheet"
    for url in assets:
        assert url.endswith(f"?v={main.ASSET_VERSION}"), url


def test_ghostcart_loads_its_own_files_versioned(client):
    html = client.get("/ghostcart").text
    v = main.ASSET_VERSION
    for name in ("styles.css", "ghostcart.js", "auth.js", "inapp.js"):
        assert f"/static/{name}?v={v}" in html
    # No hand-set version left over (?v=2", ?v=3"). Match the whole value: the content
    # hash itself can start with a 2 or a 3.
    assert not re.search(r'\?v=\d{1,3}"', html)


def test_static_files_still_revalidate(client):
    r = client.get(f"/static/ghostcart.js?v={main.ASSET_VERSION}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_version_follows_file_contents(tmp_path):
    (tmp_path / "a.js").write_text("one")
    (tmp_path / "b.css").write_text("body{}")
    (tmp_path / "notes.txt").write_text("ignored")
    first = main.asset_version(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{10}", first)
    assert main.asset_version(tmp_path) == first
    (tmp_path / "notes.txt").write_text("still ignored")
    assert main.asset_version(tmp_path) == first
    (tmp_path / "a.js").write_text("two")
    assert main.asset_version(tmp_path) != first


def test_off_site_scripts_are_left_alone(client):
    html = client.get("/ghostcart").text
    assert "https://fonts.googleapis.com/css2?family=" in html
    assert re.search(r'https?://[^"]+\?v=' + main.ASSET_VERSION, html) is None
