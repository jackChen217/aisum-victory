"""Finch Direct API (src/finch.py): token, input checks, response schema, timeout,
log line, and the limiter tier that puts Finch behind website users. AISUM is faked."""
import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src import finch, main, usage
from src.agent import limits, orchestrator, outfit, planner

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs" / "finch"
REQUEST_SCHEMA = json.loads((DOCS / "plan.request.schema.json").read_text())
RESPONSE_SCHEMA = json.loads((DOCS / "plan.response.schema.json").read_text())
ERROR_SCHEMA = json.loads((DOCS / "plan.error.schema.json").read_text())
EXAMPLE = json.loads((DOCS / "example-request.json").read_text())
TOKEN = "test-finch-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def prod(key, price, score=0.8):
    return {"p_key": key, "product_name": f"Item {key}", "brand": "", "product_price": price,
            "img_url": f"https://img/{key}.jpg",
            "click_url": f"https://shop/{key}?aff=keep&x=1", "score": score}


DETECTIONS = {"detections": [
    {"item": "coat", "item_detail": "brown wool coat", "box": [0.2, 0.2, 0.6, 0.7],
     "products": [prod("coat1", 150), prod("coat2", 60, 0.7)]},
    {"item": "shoe", "item_detail": "black boot", "box": [0.3, 0.9, 0.4, 0.95],
     "products": [prod("shoe1", 45, 0.75)]},
    {"item": "shoe", "item_detail": "black boot", "box": [0.5, 0.9, 0.6, 0.95],
     "products": [prod("shoe2", 50, 0.7)]},
    {"item": "hat", "item_detail": "red beret", "box": [0.4, 0.0, 0.5, 0.1],
     "products": [prod("hat1", 20, 0.3)]},
]}


@pytest.fixture
def env(monkeypatch):
    """Fake AISUM through the real limiter, the token set, log lines captured."""
    limits.reset()
    state = {"calls": [], "delay": 0.0, "logs": []}

    async def search_image(url, **kw):
        async with limits.slot("search_products_by_image"):
            state["calls"].append(kw)
            await asyncio.sleep(state["delay"])
            return DETECTIONS if "category" not in kw else {"detections": []}

    async def save(data):
        return {"url": "https://bucket/crop.jpg"}

    async def photo(url):
        return Image.new("RGB", (800, 1200), "white")

    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(outfit.uploads, "save", save)
    monkeypatch.setattr(planner, "_photo", photo)
    monkeypatch.setattr(usage, "log_finch",
                        lambda *a, **kw: state["logs"].append((a, kw)))
    monkeypatch.setenv(finch.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(finch, "_running", 0)
    yield state
    orchestrator.SESSIONS.clear()
    limits.reset()


@pytest.fixture
def client(env):
    with TestClient(main.app) as c:
        yield c


# ── health ───────────────────────────────────────────────────────────────

def test_health_needs_no_auth(client):
    r = client.get("/api/finch/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["configured"] is True


# ── token ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN}, {"Authorization": "Bearer "},
    {"Authorization": f"Bearer {TOKEN}x"},
])
def test_missing_or_wrong_token_is_401(client, env, headers):
    r = client.post("/api/finch/plan", json=EXAMPLE, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    jsonschema.validate(r.json(), ERROR_SCHEMA)
    assert env["calls"] == []            # no AISUM call


def test_auth_comes_before_input_checks(client):
    assert client.post("/api/finch/plan", json={"nope": 1}).status_code == 401


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_token_refuses_everything(client, env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(finch.TOKEN_ENV)
    else:
        monkeypatch.setenv(finch.TOKEN_ENV, value)
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer x"}):
        assert client.post("/api/finch/plan", json=EXAMPLE, headers=headers).status_code == 503
    assert env["calls"] == []


def test_token_rotates_with_the_env_var(client, monkeypatch):
    monkeypatch.setenv(finch.TOKEN_ENV, "new-token")
    assert client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH).status_code == 401
    r = client.post("/api/finch/plan", json=EXAMPLE,
                    headers={"Authorization": "Bearer new-token"})
    assert r.status_code == 200


# ── input ────────────────────────────────────────────────────────────────

BAD_INPUTS = {
    "budget too low": {**EXAMPLE, "budget_usd": 9.99},
    "budget too high": {**EXAMPLE, "budget_usd": 2000.01},
    "budget as string": {**EXAMPLE, "budget_usd": "200"},
    "budget as bool": {**EXAMPLE, "budget_usd": True},
    "budget null": {**EXAMPLE, "budget_usd": None},
    "bad gender": {**EXAMPLE, "gender": "X"},
    "unisex not offered": {**EXAMPLE, "gender": "UNISEX"},
    "null gender": {**EXAMPLE, "gender": None},
    "extra field": {**EXAMPLE, "notes": "hi"},
    "not a URL": {**EXAMPLE, "image_url": "not a url"},
    "http URL": {**EXAMPLE, "image_url": "http://images.unsplash.com/x.jpg"},
    "space in URL": {**EXAMPLE, "image_url": "https://images.unsplash.com/a b.jpg"},
    "image_url missing": {"budget_usd": 100},
}


# The schema has no regex (Finch doesn't support "pattern"), so it can't say "https only"
# or "no spaces"; the server still refuses those.
SERVER_ONLY = {"http URL", "space in URL", "not a URL"}


@pytest.mark.parametrize("name", BAD_INPUTS)
def test_invalid_input_is_422_and_the_schema_agrees(client, env, name):
    body = BAD_INPUTS[name]
    r = client.post("/api/finch/plan", json=body, headers=AUTH)
    assert r.status_code == 422, r.text
    jsonschema.validate(r.json(), ERROR_SCHEMA)
    assert env["calls"] == []
    if name in SERVER_ONLY:
        return
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(body, REQUEST_SCHEMA, format_checker=jsonschema.FormatChecker())


def test_only_image_url_uses_the_default_budget_of_200(client, env):
    body = {"image_url": EXAMPLE["image_url"]}
    jsonschema.validate(body, REQUEST_SCHEMA, format_checker=jsonschema.FormatChecker())
    r = client.post("/api/finch/plan", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    jsonschema.validate(out, RESPONSE_SCHEMA, format_checker=jsonschema.FormatChecker())
    assert out["budget_usd"] == 200 and out["total_usd"] <= 200
    assert env["calls"][0]["max_price"] == 200 and env["calls"][0].get("gender") is None
    assert REQUEST_SCHEMA["properties"]["budget_usd"]["default"] == finch.DEFAULT_BUDGET == 200


def test_request_schema_has_no_pattern_and_only_image_url_is_required():
    assert REQUEST_SCHEMA["required"] == ["image_url"]
    assert "pattern" not in json.dumps(REQUEST_SCHEMA)
    assert "Optional, defaults to 200" in REQUEST_SCHEMA["properties"]["budget_usd"]["description"]


def test_body_that_is_not_json_is_422(client):
    r = client.post("/api/finch/plan", content=b"{oops", headers=AUTH)
    assert r.status_code == 422


def test_example_request_follows_the_schema_and_uses_an_unsplash_photo():
    jsonschema.Draft202012Validator.check_schema(REQUEST_SCHEMA)
    jsonschema.validate(EXAMPLE, REQUEST_SCHEMA, format_checker=jsonschema.FormatChecker())
    assert EXAMPLE["image_url"].startswith("https://images.unsplash.com/photo-")
    assert EXAMPLE["image_url"].endswith("?w=1080&q=85&fm=jpg")


def test_every_schema_object_forbids_extra_properties():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for schema in (REQUEST_SCHEMA, RESPONSE_SCHEMA, ERROR_SCHEMA):
        jsonschema.Draft202012Validator.check_schema(schema)
        walk(schema)


# ── a normal request ─────────────────────────────────────────────────────

def test_plan_returns_items_within_budget_that_follow_the_schema(client, env):
    r = client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    jsonschema.validate(body, RESPONSE_SCHEMA, format_checker=jsonschema.FormatChecker())
    assert body["budget_usd"] == 200 and body["total_usd"] <= 200
    assert body["open_url"] == "https://app-103901851610.us-central1.run.app/ghostcart?src=finch"
    by_cat = {i["category"]: i for i in body["items"]}
    assert set(by_cat) == {"coat", "shoe"}           # left/right boot merged into one item
    assert by_cat["coat"]["click_url"] == "https://shop/coat1?aff=keep&x=1"   # untouched
    assert by_cat["coat"]["similarity_score"] == 0.8
    assert by_cat["coat"]["description"] == "brown wool coat"
    assert body["total_usd"] == round(sum(i["price_usd"] for i in body["items"]), 2)
    assert [u["category"] for u in body["unmatched"]] == ["hat"]   # 0.3 even after the crop
    first, crop = env["calls"]
    assert first == {"max_price": 200, "gender": "F"}
    assert crop["category"] == "hat"
    assert not [k for k in orchestrator.SESSIONS if k[0] == finch.FINCH_UID]   # no growth
    (status, outcome, _ms, items, unmatched, gender), _ = env["logs"][0]
    assert (status, outcome, items, unmatched, gender) == (200, "ok", 2, 1, "F")


def test_tight_budget_keeps_the_whole_outfit_under_it(client):
    r = client.post("/api/finch/plan", json={**EXAMPLE, "budget_usd": 120}, headers=AUTH)
    body = r.json()
    jsonschema.validate(body, RESPONSE_SCHEMA)
    assert body["total_usd"] <= 120 and body["items"]


def test_no_gender_means_no_gender_filter(client, env):
    body = {k: v for k, v in EXAMPLE.items() if k != "gender"}
    assert client.post("/api/finch/plan", json=body, headers=AUTH).status_code == 200
    assert env["calls"][0].get("gender") is None   # planner.search_image drops None args


def test_photo_without_fashion_items_is_422(client, monkeypatch):
    async def empty(url, **kw):
        return {"detections": [], "note": "No fashion items found."}
    monkeypatch.setattr(planner, "search_image", empty)
    r = client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH)
    assert r.status_code == 422 and r.json()["code"] == "NO_ITEMS"


def test_slow_plan_returns_504_not_a_hang(client, env, monkeypatch):
    env["delay"] = 5
    monkeypatch.setattr(finch, "TOTAL_TIMEOUT_S", 0.2)
    r = client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH)
    assert r.status_code == 504 and r.json()["code"] == "TIMEOUT"
    assert env["logs"][-1][0][1] == "timeout"


def test_finch_gets_a_longer_outfit_deadline_than_the_website(client, monkeypatch):
    seen = {}
    real = outfit.candidates

    async def spy(*a, deadline_s=None, **kw):
        seen["deadline_s"] = deadline_s
        return await real(*a, deadline_s=deadline_s, **kw)
    monkeypatch.setattr(outfit, "candidates", spy)
    client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH)
    assert seen["deadline_s"] == finch.OUTFIT_DEADLINE_S > outfit.DEADLINE_S
    assert finch.OUTFIT_DEADLINE_S < finch.TOTAL_TIMEOUT_S < 120


def test_too_many_at_once_is_429(client, monkeypatch):
    monkeypatch.setattr(finch, "_running", finch.MAX_CONCURRENT)
    r = client.post("/api/finch/plan", json=EXAMPLE, headers=AUTH)
    assert r.status_code == 429 and r.headers["retry-after"]


def test_finch_log_line_is_its_own_event_without_email(capsys):
    usage.log_finch(200, "ok", 1234.5, 3, 1, "F")
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == usage.FINCH_EVENT == "ghostcart_finch"
    assert line["event"] not in (usage.PLAN_EVENT, usage.GALLERY_EVENT)
    assert line["trigger"] == "finch"
    assert "email" not in json.dumps(line) and "uid" not in line


# ── limiter: Finch waits, but behind website users ───────────────────────

def run(coro):
    return asyncio.run(coro)


def test_freed_slot_goes_to_a_waiting_user_before_an_earlier_finch_call():
    async def go():
        g = limits.Gate(1)
        await g.take()                       # one search running
        order = []

        async def waiter(name, tier):
            await g.take(tier)
            order.append(name)
            await asyncio.sleep(0)
            g.give()

        f1 = asyncio.create_task(waiter("finch1", limits.FINCH))
        await asyncio.sleep(0)
        f2 = asyncio.create_task(waiter("finch2", limits.FINCH))
        await asyncio.sleep(0)
        u1 = asyncio.create_task(waiter("user1", limits.USER))   # arrives last
        await asyncio.sleep(0)
        u2 = asyncio.create_task(waiter("user2", limits.USER))
        await asyncio.sleep(0)
        assert g.try_take() is False         # background never jumps either line
        g.give()
        await asyncio.gather(f1, f2, u1, u2)
        assert order == ["user1", "user2", "finch1", "finch2"]
        assert g.free == 1
    run(go())


def test_finch_does_not_take_a_free_slot_while_a_user_waits():
    async def go():
        g = limits.Gate(2)
        await g.take()
        await g.take()
        user = asyncio.create_task(g.take(limits.USER))
        await asyncio.sleep(0)
        g.free += 1                          # a slot appears with the user still queued
        fin = asyncio.create_task(g.take(limits.FINCH))
        await asyncio.sleep(0)
        assert not fin.done()                # Finch queued behind the waiting user
        g.free -= 1
        g.give()
        await asyncio.sleep(0)
        assert user.done() and not fin.done()
        g.give()
        await asyncio.sleep(0)
        assert fin.done()
    run(go())


def test_user_does_not_queue_behind_finch_and_cancelled_finch_leaves_the_line():
    async def go():
        g = limits.Gate(1)
        await g.take()
        fin = asyncio.create_task(g.take(limits.FINCH))
        await asyncio.sleep(0)
        assert g.waiting(limits.FINCH) == 1
        fin.cancel()
        await asyncio.gather(fin, return_exceptions=True)
        assert g.waiting(limits.FINCH) == 0
        g.give()
        assert g.free == 1
    run(go())


def test_searches_inside_finch_context_wait_instead_of_raising_busy():
    async def go():
        limits.reset()
        g = limits.gate("search_products_by_image")
        for _ in range(g.size):
            await g.take()
        order = []

        async def search(name, as_finch):
            if as_finch:
                with limits.finch():
                    assert limits.is_finch() and not limits.is_background()
                    async with limits.slot("search_products_by_image"):
                        order.append(name)
            else:
                async with limits.slot("search_products_by_image"):
                    order.append(name)

        fin = asyncio.create_task(search("finch", True))
        await asyncio.sleep(0)
        usr = asyncio.create_task(search("user", False))
        await asyncio.sleep(0)
        assert g.waiting(limits.FINCH) == 1 and g.waiting(limits.USER) == 1
        g.give()
        await asyncio.gather(fin, usr)
        assert order == ["user", "finch"]
        assert not limits.is_finch()
        limits.reset()
    run(go())
