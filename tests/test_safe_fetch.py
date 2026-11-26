"""src/safe_fetch.py (SlowMist SM-AR-2026-035, SSRF) and its use by the Finch API.
DNS and HTTP are faked: no network."""
import asyncio
import json
import socket
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from src import finch, image_proxy, main, safe_fetch, uploads, usage
from src.agent import limits, orchestrator, planner

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100
DNS = {
    "img.example.com": ["93.184.216.34"],
    "cdn.example.com": ["151.101.1.1"],
    "mixed.example.com": ["93.184.216.35", "10.0.0.7"],       # one private address
    "meta.example.com": ["169.254.169.254"],
    "loop.example.com": ["127.0.0.1"],
    "v6.example.com": ["2606:4700::1111"],
}


def ok(body=JPEG, kind="image/jpeg", **headers):
    return httpx.Response(200, content=body, headers={"content-type": kind, **headers})


def redirect(location, code=302):
    return httpx.Response(code, headers={"location": location})


@pytest.fixture
def net(monkeypatch):
    """routes: {(host, path): Response | callable}; seen: every request sent."""
    state = {"routes": {}, "seen": [], "resolved": []}

    async def resolve(host):
        state["resolved"].append(host)
        if host not in DNS:
            raise socket.gaierror("no such host")
        return DNS[host]

    def handler(request: httpx.Request):
        host = request.headers["host"]
        state["seen"].append({"ip": request.url.host, "host": host, "path": request.url.path,
                              "sni": request.extensions.get("sni_hostname"),
                              "scheme": request.url.scheme})
        route = state["routes"].get((host, request.url.path))
        if route is None:
            return httpx.Response(404)
        return route(request) if callable(route) else route

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    monkeypatch.setattr(safe_fetch, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False))
    return state


def fetch(url, **kw):
    return asyncio.run(safe_fetch.fetch_image(url, **kw))


def rejected(url, reason, **kw):
    with pytest.raises(safe_fetch.ImageUrlRejected) as e:
        fetch(url, **kw)
    assert e.value.reason == reason, (e.value.reason, str(e.value))


# ── the happy paths ──────────────────────────────────────────────────────

def test_https_image_is_fetched_from_the_checked_address_with_the_real_host_name(net):
    net["routes"][("img.example.com", "/a.jpg")] = ok()
    assert fetch("https://img.example.com/a.jpg") == (JPEG, "image/jpeg")
    (req,) = net["seen"]
    # Connected to the address we checked; TLS and Host name the real host.
    assert req == {"ip": "93.184.216.34", "host": "img.example.com", "path": "/a.jpg",
                   "sni": "img.example.com", "scheme": "https"}


def test_https_to_https_redirects_are_followed_up_to_three(net):
    r = net["routes"]
    r[("img.example.com", "/1")] = redirect("https://cdn.example.com/2")
    r[("cdn.example.com", "/2")] = redirect("/3", 301)                  # relative
    r[("cdn.example.com", "/3")] = redirect("https://img.example.com/4.png", 307)
    r[("img.example.com", "/4.png")] = ok(PNG, "image/png")
    assert fetch("https://img.example.com/1") == (PNG, "image/png")
    assert [s["host"] for s in net["seen"]] == ["img.example.com", "cdn.example.com",
                                                "cdn.example.com", "img.example.com"]
    assert net["resolved"] == ["img.example.com", "cdn.example.com", "cdn.example.com",
                               "img.example.com"]                      # every hop re-checked


def test_ipv6_public_host_is_bracketed(net):
    net["routes"][("v6.example.com", "/a.jpg")] = ok()
    assert fetch("https://v6.example.com/a.jpg")[1] == "image/jpeg"
    assert net["seen"][0]["ip"] == "2606:4700::1111"


def test_content_type_aliases_are_accepted(net):
    net["routes"][("img.example.com", "/a")] = ok(JPEG, "image/jpg; charset=binary")
    assert fetch("https://img.example.com/a")[1] == "image/jpeg"


# ── refused: scheme, address, redirects ──────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://10.0.0.1/x.jpg", "https://172.16.5.4/x.jpg", "https://192.168.1.1/x.jpg",
    "https://127.0.0.1/x.jpg", "https://127.8.8.8/x.jpg", "https://[::1]/x.jpg",
    "https://169.254.169.254/latest/meta-data", "https://169.254.1.1/x.jpg",
    "https://[fe80::1]/x.jpg", "https://[fd00:ec2::254]/x.jpg", "https://[fc00::1]/x.jpg",
    "https://0.0.0.0/x.jpg", "https://[::]/x.jpg", "https://224.0.0.1/x.jpg",
    "https://[ff02::1]/x.jpg", "https://240.0.0.1/x.jpg", "https://255.255.255.255/x.jpg",
    "https://100.64.0.1/x.jpg", "https://[::ffff:127.0.0.1]/x.jpg",
    "https://[::ffff:169.254.169.254]/x.jpg",
])
def test_private_and_reserved_addresses_are_refused(net, url):
    rejected(url, "private_address")
    assert net["seen"] == []                     # nothing was sent


@pytest.mark.parametrize("host", ["mixed.example.com", "meta.example.com", "loop.example.com"])
def test_a_host_name_with_any_private_address_is_refused(net, host):
    rejected(f"https://{host}/x.jpg", "private_address")
    assert net["seen"] == []


@pytest.mark.parametrize("url,reason", [
    ("http://img.example.com/a.jpg", "not_https"),
    ("ftp://img.example.com/a.jpg", "not_https"),
    ("file:///etc/passwd", "not_https"),
    ("https://img.example.com:8443/a.jpg", "port_not_allowed"),
    ("https://img.example.com:22/a.jpg", "port_not_allowed"),
    ("https://user:pw@img.example.com/a.jpg", "bad_url"),
    ("https:///nohost.jpg", "bad_url"),
    ("https://no-such-host.example/a.jpg", "unresolvable"),
])
def test_bad_urls_are_refused(net, url, reason):
    rejected(url, reason)
    assert net["seen"] == []


def test_redirect_to_http_is_refused_no_downgrade(net):
    net["routes"][("img.example.com", "/a")] = redirect("http://img.example.com/a.jpg")
    rejected("https://img.example.com/a", "not_https")
    assert len(net["seen"]) == 1                 # the http URL was never requested


@pytest.mark.parametrize("target", ["https://127.0.0.1/a.jpg",
                                    "https://169.254.169.254/latest/meta-data/",
                                    "https://meta.example.com/x", "https://[::1]/a.jpg",
                                    "https://10.1.2.3:443/a.jpg"])
def test_redirect_to_an_internal_address_is_refused(net, target):
    net["routes"][("img.example.com", "/a")] = redirect(target)
    rejected("https://img.example.com/a", "private_address")
    assert [s["host"] for s in net["seen"]] == ["img.example.com"]


def test_more_than_three_redirects_are_refused(net):
    for i in range(4):
        net["routes"][("img.example.com", f"/{i}")] = redirect(f"/{i + 1}")
    net["routes"][("img.example.com", "/4")] = ok()
    rejected("https://img.example.com/0", "too_many_redirects")
    assert len(net["seen"]) == 4                 # 3 redirects followed, the 4th refused


def test_redirect_without_location_is_refused(net):
    net["routes"][("img.example.com", "/a")] = httpx.Response(302)
    rejected("https://img.example.com/a", "bad_redirect")


def test_dns_is_resolved_once_per_hop_and_that_address_is_used(net, monkeypatch):
    """Rebinding: a second lookup would say 127.0.0.1, but there is no second lookup."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def rebinding(host):
        return next(answers)
    monkeypatch.setattr(safe_fetch, "_resolve", rebinding)
    net["routes"][("img.example.com", "/a.jpg")] = ok()
    fetch("https://img.example.com/a.jpg")
    assert [s["ip"] for s in net["seen"]] == ["93.184.216.34"]


# ── refused: not an image, too large ─────────────────────────────────────

@pytest.mark.parametrize("body,kind", [
    (b"<html>hi</html>", "text/html"),
    (b"{}", "application/json"),
    (b"<html>not really</html>", "image/jpeg"),         # header lies: file header says no
    (WEBP, "image/webp"),                                # an image, but not JPEG/PNG
    (b"GIF89a....", "image/gif"),
    (JPEG, ""),                                          # no content type
])
def test_non_images_are_refused(net, body, kind):
    net["routes"][("img.example.com", "/a")] = ok(body, kind)
    rejected("https://img.example.com/a", "not_image")


def test_too_large_by_content_length_is_refused_before_reading(net):
    net["routes"][("img.example.com", "/big")] = httpx.Response(
        200, headers={"content-type": "image/jpeg", "content-length": str(11 * 2**20)},
        content=JPEG)
    rejected("https://img.example.com/big", "too_large")


def test_too_large_while_streaming_is_refused(net):
    big = JPEG + b"\x00" * (10 * 2**20)
    net["routes"][("img.example.com", "/big")] = httpx.Response(
        200, headers={"content-type": "image/jpeg"}, stream=httpx.ByteStream(big))
    rejected("https://img.example.com/big", "too_large")
    small = JPEG + b"\x00" * 1000
    net["routes"][("img.example.com", "/ok")] = ok(small)
    assert fetch("https://img.example.com/ok", max_bytes=2000)[0] == small
    rejected("https://img.example.com/ok", "too_large", max_bytes=500)


def test_host_4xx_is_the_callers_link_and_5xx_or_timeouts_are_upstream(net, monkeypatch):
    net["routes"][("img.example.com", "/gone")] = httpx.Response(404)
    rejected("https://img.example.com/gone", "http_status")
    net["routes"][("img.example.com", "/err")] = httpx.Response(503)
    with pytest.raises(safe_fetch.ImageUpstreamError):
        fetch("https://img.example.com/err")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    net["routes"][("img.example.com", "/slow")] = slow
    with pytest.raises(safe_fetch.ImageUpstreamError) as e:
        fetch("https://img.example.com/slow")
    assert e.value.reason == "timeout"


def test_whole_download_is_cut_at_the_total_timeout(net, monkeypatch):
    monkeypatch.setattr(safe_fetch, "TOTAL_TIMEOUT_S", 0.05)

    async def hang(host):
        await asyncio.sleep(1)
        return DNS[host]
    monkeypatch.setattr(safe_fetch, "_resolve", hang)
    with pytest.raises(safe_fetch.ImageUpstreamError) as e:
        fetch("https://img.example.com/a.jpg")
    assert e.value.reason == "timeout"


def test_limits_are_the_ones_asked_for():
    assert safe_fetch.MAX_REDIRECTS == 3
    assert safe_fetch.CONNECT_TIMEOUT_S == 5 and safe_fetch.TOTAL_TIMEOUT_S == 15
    assert safe_fetch.MAX_BYTES == 10 * 2**20
    client = safe_fetch._client()
    assert client.follow_redirects is False and client._trust_env is False
    assert client.timeout.connect == 5


# ── the other callers use it too ─────────────────────────────────────────

def test_image_proxy_and_crop_photo_go_through_the_safe_fetcher(net):
    net["routes"][("img.example.com", "/p.webp")] = ok(WEBP, "image/webp")
    # Product images for the Lookboard may be WebP …
    assert asyncio.run(image_proxy.fetch("https://img.example.com/p.webp"))[1] == "image/webp"
    # … the user's photo (crop re-search) must be JPEG / PNG.
    with pytest.raises(image_proxy.ImageFetchError):
        asyncio.run(image_proxy.fetch_photo("https://img.example.com/p.webp"))
    net["routes"][("img.example.com", "/r")] = redirect("https://169.254.169.254/")
    with pytest.raises(image_proxy.ImageUrlRejected):
        asyncio.run(planner._photo("https://img.example.com/r"))


# ── the Finch endpoint ───────────────────────────────────────────────────

DOCS = Path(__file__).parent.parent / "docs" / "finch"
ERROR_SCHEMA = json.loads((DOCS / "plan.error.schema.json").read_text())
AUTH = {"Authorization": "Bearer t0k"}
DETECTIONS = {"detections": [{"item": "coat", "item_detail": "grey coat",
                              "box": [0.2, 0.2, 0.6, 0.7],
                              "products": [{"p_key": "c1", "product_name": "Coat",
                                            "product_price": 50, "img_url": "https://i/c1.jpg",
                                            "click_url": "https://s/c1", "score": 0.9}]}]}


@pytest.fixture
def finch_env(net, monkeypatch):
    limits.reset()
    state = {"aisum_urls": [], "saved": []}

    async def search_image(url, **kw):
        state["aisum_urls"].append(url)
        return DETECTIONS

    async def save(data, max_bytes=uploads.MAX_BYTES):
        state["saved"].append((len(data), max_bytes))
        return {"url": "https://storage.googleapis.com/bucket/uploads/x.jpg?X-Goog-Signature=s"}

    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(uploads, "save", save)
    monkeypatch.setattr(usage, "log_finch", lambda *a, **kw: None)
    monkeypatch.setenv(finch.TOKEN_ENV, "t0k")
    monkeypatch.setenv("K_SERVICE", "app")                 # as on Cloud Run
    monkeypatch.setattr(finch, "_running", 0)
    yield state
    orchestrator.SESSIONS.clear()
    limits.reset()


@pytest.fixture
def api(finch_env):
    with TestClient(main.app) as c:
        yield c


def test_finch_hands_aisum_our_signed_copy_never_the_callers_url(api, net, finch_env):
    net["routes"][("img.example.com", "/look.jpg")] = ok()
    r = api.post("/api/finch/plan", json={"image_url": "https://img.example.com/look.jpg"},
                 headers=AUTH)
    assert r.status_code == 200, r.text
    assert finch_env["saved"] == [(len(JPEG), safe_fetch.MAX_BYTES)]
    assert finch_env["aisum_urls"] == [
        "https://storage.googleapis.com/bucket/uploads/x.jpg?X-Goog-Signature=s"]


def test_finch_follows_an_https_redirect(api, net):
    net["routes"][("img.example.com", "/a")] = redirect("https://cdn.example.com/b.png")
    net["routes"][("cdn.example.com", "/b.png")] = ok(PNG, "image/png")
    r = api.post("/api/finch/plan", json={"image_url": "https://img.example.com/a"}, headers=AUTH)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("url,setup,reason", [
    ("https://10.0.0.1/a.jpg", None, "private_address"),
    ("https://img.example.com/r", ("img.example.com", "/r", "http://img.example.com/a.jpg"), "not_https"),
    ("https://img.example.com/r", ("img.example.com", "/r", "https://127.0.0.1/a.jpg"), "private_address"),
    ("https://img.example.com/r", ("img.example.com", "/r", "https://169.254.169.254/latest"), "private_address"),
    ("https://img.example.com/page", None, "not_image"),
    ("https://img.example.com/huge", None, "too_large"),
    ("https://img.example.com/0", None, "too_many_redirects"),
])
def test_finch_refuses_bad_image_urls_with_422_and_calls_nothing(api, net, finch_env, url, setup, reason):
    net["routes"][("img.example.com", "/page")] = ok(b"<html></html>", "text/html")
    net["routes"][("img.example.com", "/huge")] = httpx.Response(
        200, headers={"content-type": "image/jpeg", "content-length": str(50 * 2**20)})
    for i in range(4):
        net["routes"][("img.example.com", f"/{i}")] = redirect(f"/{i + 1}")
    if setup:
        net["routes"][(setup[0], setup[1])] = redirect(setup[2])
    r = api.post("/api/finch/plan", json={"image_url": url}, headers=AUTH)
    assert r.status_code == 422, r.text
    body = r.json()
    jsonschema.validate(body, ERROR_SCHEMA)
    assert body["code"] == "image_url_rejected" and body["reason"] == reason
    assert finch_env["aisum_urls"] == [] and finch_env["saved"] == []


def test_finch_image_host_failure_is_5xx(api, net, finch_env):
    net["routes"][("img.example.com", "/down")] = httpx.Response(502)
    r = api.post("/api/finch/plan", json={"image_url": "https://img.example.com/down"}, headers=AUTH)
    assert r.status_code == 502 and r.json()["code"] == "image_fetch_failed"
    jsonschema.validate(r.json(), ERROR_SCHEMA)
    assert finch_env["aisum_urls"] == []


def test_finch_local_run_passes_the_checked_url(api, net, finch_env, monkeypatch):
    monkeypatch.delenv("K_SERVICE")
    net["routes"][("img.example.com", "/look.jpg")] = ok()
    r = api.post("/api/finch/plan", json={"image_url": "https://img.example.com/look.jpg"},
                 headers=AUTH)
    assert r.status_code == 200
    assert finch_env["saved"] == [] and finch_env["aisum_urls"] == ["https://img.example.com/look.jpg"]
