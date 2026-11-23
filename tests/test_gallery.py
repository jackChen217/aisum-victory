"""Gallery v2: cached looks, background refresh, the AISUM limiter, the tour record.
AISUM is faked; the store is a temporary local folder."""
import asyncio
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from src import gallery, usage
from src.agent import limits, orchestrator, outfit, planner

ROOT = Path(__file__).parent.parent
# Any outfit look in the catalogue; the tests don't depend on which photos are chosen.
LOOK = next(x["id"] for x in json.loads((ROOT / "static" / "gallery.json").read_text())["looks"]
            if x["category"] == "outfit")


def prod(key, price, score=0.8, name=None):
    return {"p_key": key, "product_name": name or f"Item {key}", "product_price": price,
            "img_url": f"https://img/{key}.jpg", "click_url": f"https://shop/{key}?tag=keep",
            "score": score}


DETECTIONS = {"detections": [
    {"item": "coat", "item_detail": "grey coat", "box": [0.2, 0.2, 0.6, 0.7],
     "products": [prod("coat1", 60), prod("coat2", 40, 0.7)]},
    {"item": "shoe", "item_detail": "black shoe", "box": [0.3, 0.9, 0.4, 0.95],
     "products": [prod("shoe1", 30, 0.75)]},
]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake AISUM (through the real limiter), a temp store, captured log lines."""
    limits.reset()
    gallery.reset()
    gallery._store = gallery.LocalStore(tmp_path)
    state = {"calls": 0, "delay": 0.0, "logs": []}

    async def search_image(url, **kw):
        async with limits.slot("search_products_by_image"):
            state["calls"] += 1
            await asyncio.sleep(state["delay"])
            return DETECTIONS if "category" not in kw else {"detections": []}

    async def save(data):
        return {"url": "https://bucket/crop.jpg"}

    async def photo(url):
        return Image.new("RGB", (800, 1200), "white")

    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(outfit.uploads, "save", save)
    monkeypatch.setattr(planner, "_photo", photo)
    monkeypatch.setattr(usage, "log_gallery",
                        lambda uid, look_id, scenario, trigger, outcome, claims=None:
                        state["logs"].append((uid, look_id, trigger, outcome)))
    yield state
    orchestrator.SESSIONS.clear()
    gallery.reset()
    limits.reset()


def run(coro):
    return asyncio.run(coro)


# ── catalog ──────────────────────────────────────────────────────────────

def test_catalog_has_three_categories_budgets_and_free_photos():
    doc = json.loads((ROOT / "static" / "gallery.json").read_text())
    assert doc["budgets"] == {"outfit": 200, "desk": 400, "room": 400}
    ids = [x["id"] for x in doc["looks"]]
    assert len(ids) == len(set(ids))
    for cat in ("outfit", "desk", "room"):
        looks = [x for x in doc["looks"] if x["category"] == cat]
        assert 1 <= len(looks) <= 12, cat          # the grid shows at most 12
    for x in doc["looks"]:
        if x.get("source") == "pexels":
            # Pexels License: a numeric photo id, credited to the photographer's page.
            assert re.fullmatch(r"\d+", x["photo"]), x["id"]
            assert x["credit"]["name"] and x["credit"]["url"].startswith("https://www.pexels.com/@")
            assert x["credit"]["page"].startswith("https://www.pexels.com/photo/")
            continue
        assert x.get("source") in (None, "unsplash"), x["id"]
        # Free Unsplash photos only: Unsplash+ ids start with premium_photo.
        assert re.fullmatch(r"photo-[0-9a-f-]+", x["photo"]), x["id"]
        assert x["credit"]["url"].startswith("https://unsplash.com")
        if x["category"] != "outfit":
            assert x["credit"]["page"].startswith("https://unsplash.com/photos/")
            assert x["credit"]["name"] and x["credit"]["url"].startswith("https://unsplash.com/@")
    for x in doc["looks"]:
        if x["category"] == "outfit":
            assert x["gender"] in ("F", "M", "UNISEX")


# ── opening a look ───────────────────────────────────────────────────────

def test_first_open_plans_live_then_later_opens_come_from_the_store(env):
    async def go():
        first = await gallery.open_look("u1", LOOK)
        assert first["gallery"]["cached"] is False
        calls_after_first = env["calls"]
        second = await gallery.open_look("u2", LOOK)
        assert second["gallery"]["cached"] is True
        assert set(second["plan"]) == set(first["plan"])
        # The cached open itself made no search; only its background refresh does.
        assert env["calls"] == calls_after_first
        await asyncio.wait_for(gallery._tasks["u2"], 5) if "u2" in gallery._tasks else None
        return first, second
    first, second = run(go())
    assert first["budget"] == 200                     # outfit budget from gallery.json
    triggers = [(t, o) for _, _, t, o in env["logs"]]
    assert ("gallery_live", "ok") in triggers
    assert ("gallery_view", "ok") in triggers
    assert ("gallery_refresh", "ok") in triggers


def test_a_cached_look_is_a_normal_session_the_slider_can_replan(env):
    async def go():
        await gallery.open_look("u1", LOOK)
        data = await gallery.open_look("u1", LOOK)
        return await orchestrator.replan("u1", data["session_id"], 50)
    re_ = run(go())
    assert re_["total"] <= 50


def test_refresh_is_not_repeated_for_the_same_look_within_ten_minutes(env):
    async def go():
        await gallery.open_look("u1", LOOK)          # live build, no refresh
        await gallery.open_look("u2", LOOK)          # refresh starts
        await gallery.open_look("u3", LOOK)          # too soon
        for t in list(gallery._tasks.values()):
            await t
    run(go())
    refresh = [o for _, _, t, o in env["logs"] if t == "gallery_refresh"]
    assert refresh.count("ok") == 1 and "skipped_recent" in refresh


def test_users_own_search_cancels_their_refresh(env):
    async def go():
        await gallery.open_look("u1", LOOK)
        env["delay"] = 0.5                                   # the refresh's search is slow
        await gallery.open_look("u1", LOOK)
        await asyncio.sleep(0.05)
        assert gallery.cancel_for("u1") is True
        await asyncio.sleep(0.05)
    run(go())
    assert ("u1", LOOK, "gallery_refresh", "cancelled") in env["logs"]


def test_refresh_steps_aside_when_users_hold_every_image_slot(env):
    async def go():
        await gallery.open_look("u1", LOOK)
        stored = await gallery.store().latest(LOOK)
        g = limits.gate("search_products_by_image")
        for _ in range(g.size):
            await g.take()                                   # three users searching
        await gallery.open_look("u2", LOOK)
        await gallery._tasks["u2"] if "u2" in gallery._tasks else None
        for _ in range(g.size):
            g.give()
        return stored, await gallery.store().latest(LOOK)
    before, after = run(go())
    assert ("u2", LOOK, "gallery_refresh", "skipped_busy") in env["logs"]
    assert after["built_at"] == before["built_at"]           # nothing half-done stored


def test_desk_refresh_redoes_text_searches_only_and_keeps_checked_products(env, monkeypatch):
    old_session = {
        "scenario": "desk", "image_url": "https://img/desk.jpg", "notes": "", "gender": None,
        "search_budget": 400, "budget": 400, "owned": [], "pins": {}, "unmatched": [],
        "desk_width_in": None,
        "slots": [{"slot": "monitor", "required": True, "weight": 3, "query": "monitor",
                   "domain": ["electronics"]}],
        "candidates": {"monitor": [prod("m1", 200, name="27 inch monitor"),
                                   prod("m2", 150, name="24 inch monitor")]},
        "scores": {"monitor": [{"quality": 0.5}, {"quality": 0.3}]},
        "plan": {},
    }
    seen = {}

    async def find_candidates(slots, budget, report=None):
        seen["budget"] = budget
        # m2 has a new price; m9 is new (never category-checked) and must not be added.
        return {"monitor": [prod("m9", 90, name="monitor arm"), prod("m2", 140, name="24 inch monitor")]}

    async def no_llm(*a, **k):
        raise AssertionError("a refresh must not call the LLM")

    monkeypatch.setattr(planner, "find_candidates", find_candidates)
    monkeypatch.setattr(planner, "plan_slots", no_llm)
    monkeypatch.setattr(planner, "check_categories", no_llm)
    item = gallery.look("glow-desk")
    doc = run(gallery.build_refresh(item, {"session": old_session}))
    keys = [p["p_key"] for p in doc["session"]["candidates"]["monitor"]]
    assert keys == ["m2"] and doc["session"]["candidates"]["monitor"][0]["product_price"] == 140
    assert seen["budget"] == 400 and doc["how"] == "refresh"


def test_unknown_look_is_refused(env):
    with pytest.raises(gallery.UnknownLook):
        run(gallery.open_look("u1", "nope"))


# ── limiter ──────────────────────────────────────────────────────────────

def test_users_queue_in_order_and_background_never_jumps_the_queue():
    async def go():
        g = limits.Gate(1)
        await g.take()                       # one search running
        order = []

        async def user(n):
            await g.take()
            order.append(n)
            g.give()

        waiting = [asyncio.create_task(user(i)) for i in range(2)]
        await asyncio.sleep(0)
        assert g.try_take() is False         # background: someone is waiting
        g.give()
        await asyncio.gather(*waiting)
        assert order == [0, 1]
        assert g.try_take() is True          # free and nobody waiting
        g.give()
        assert g.free == 1
    run(go())


def test_background_search_without_a_free_slot_raises_busy_and_is_recorded():
    async def go():
        limits.reset()
        g = limits.gate("search_products_by_text")
        for _ in range(g.size):
            await g.take()
        with limits.background() as state:
            with pytest.raises(limits.Busy):
                async with limits.slot("search_products_by_text"):
                    pass
        assert state["refused"] is True
        for _ in range(g.size):
            g.give()
        limits.reset()
    run(go())


def test_planner_search_goes_through_the_limiter(monkeypatch):
    async def go():
        limits.reset()
        peak = {"now": 0, "max": 0}

        class FakeMcp:
            async def call_waiting_once(self, name, args):
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.02)
                peak["now"] -= 1
                return {"detections": []}

        async def client():
            return FakeMcp()

        monkeypatch.setattr(planner, "_client", client)
        await asyncio.gather(*(planner.search_image(f"https://x/{i}.jpg") for i in range(7)))
        limits.reset()
        return peak["max"]
    assert run(go()) == limits.AT_ONCE["search_products_by_image"]


# ── endpoints: tour record, reset is local-only, log lines ───────────────

@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient

    from src import main
    from src.auth import require_user

    main.app.dependency_overrides[require_user] = lambda: "tour-user"
    monkeypatch.delenv("K_SERVICE", raising=False)
    try:
        yield main.app, TestClient
    finally:
        main.app.dependency_overrides.clear()


def test_tour_is_recorded_per_account(client):
    app, TestClient = client
    c = TestClient(app)
    assert c.get("/api/me/onboarding").json() == {"seen": False}
    assert c.post("/api/me/onboarding").json() == {"seen": True}
    assert c.get("/api/me/onboarding").json() == {"seen": True}


def test_tour_reset_only_from_this_machine_and_never_on_cloud_run(client, monkeypatch):
    app, TestClient = client
    TestClient(app).post("/api/me/onboarding")
    assert TestClient(app).delete("/api/me/onboarding").status_code == 404   # client "testclient"
    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.delete("/api/me/onboarding").json() == {"seen": False}
    monkeypatch.setenv("K_SERVICE", "app")
    assert local.delete("/api/me/onboarding").status_code == 404


def test_gallery_open_endpoint_returns_the_ui_shape(client):
    app, TestClient = client
    with TestClient(app) as c:
        r = c.post(f"/api/gallery/{LOOK}/open?format=ui")
        assert r.status_code == 200
        body = r.json()
        assert body["category"] == "outfit" and body["budget"] == 200
        assert body["gallery"]["look_id"] == LOOK and body["matched_products"]
        assert c.post("/api/gallery/nope/open?format=ui").status_code == 404


def test_plan_log_line_says_user_and_gallery_lines_are_a_separate_event(capsys):
    usage.log_plan("u", "outfit", None, 200)
    plan = json.loads(capsys.readouterr().out)
    assert plan["event"] == usage.PLAN_EVENT and plan["trigger"] == "user"
    usage.log_gallery("u", LOOK, "outfit", "gallery_refresh", "ok")
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == usage.GALLERY_EVENT != usage.PLAN_EVENT
    assert "email" not in json.dumps(line)


def test_vendored_libraries_carry_their_licences():
    vendor = ROOT / "static" / "vendor"
    text = (vendor / "LICENSES.txt").read_text()
    assert "Kamran Ahmed" in text and "MIT License" in text
    for f in ("driver-1.14.0.mjs", "driver-1.14.0.css"):
        assert (vendor / f).is_file()
    html = (ROOT / "static" / "ghostcart.html").read_text()
    assert "/static/vendor/driver-1.14.0.css" in html


@pytest.mark.parametrize("name", ["ghostcart.js", "resultmarks.js", "authmsg.js", "auth.js"])
def test_page_modules_parse_as_es_modules(name, tmp_path):
    """A duplicate top-level name or a typo stops the whole page module from running."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    copy = tmp_path / (Path(name).stem + ".mjs")     # .mjs: checked as an ES module
    copy.write_text((ROOT / "static" / name).read_text())
    done = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_restoring_a_look_does_not_start_a_refresh(env):
    async def go():
        await gallery.open_look("u1", LOOK)                   # live build
        await gallery.open_look("u2", LOOK, refresh=False)    # Back / reload
        return dict(gallery._tasks)
    assert run(go()) == {}
    assert not [o for _, _, t, o in env["logs"] if t == "gallery_refresh"]


def test_photo_url_follows_the_source():
    assert gallery.photo_url({"photo": "photo-1-a"}, 700) == \
        "https://images.unsplash.com/photo-1-a?w=700&q=85&fm=jpg"
    assert gallery.photo_url({"photo": "123", "source": "pexels"}, 700) == \
        "https://images.pexels.com/photos/123/pexels-photo-123.jpeg?auto=compress&cs=tinysrgb&w=700"
