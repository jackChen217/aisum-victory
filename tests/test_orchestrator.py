"""Orchestrator and the two routes, offline: planner, search and LLM are faked."""
import pytest
from fastapi.testclient import TestClient

from src import main
from src.agent import orchestrator
from src.auth import require_user


def p(key, price):
    return {"p_key": key, "product_name": f"Product {key}", "product_price": price,
            "img_url": f"https://img/{key}.jpg", "click_url": f"https://shop/{key}"}


SLOTS = [
    {"slot": "monitor", "required": True, "weight": 3, "query": "monitor", "domain": ["electronics"]},
    {"slot": "mouse", "required": False, "weight": 1, "query": "mouse", "domain": ["electronics"]},
]
CANDS = {"monitor": [p("M1", 300), p("M2", 150)], "mouse": [p("m1", 50), p("m2", 20)]}


@pytest.fixture
def fakes(monkeypatch):
    calls = {"plan_slots": [], "find": [], "llm": [], "categories": 0}

    async def plan_slots(image_url, scenario, notes="", budget=None, gender=None):
        calls["plan_slots"].append((image_url, scenario, notes, budget))
        calls["gender"] = gender
        return [dict(s, image_url=image_url, boxes=[[0, 0, 1, 1]]) for s in SLOTS]

    async def find_candidates(slots, budget, report=None):
        calls["find"].append(budget)
        report["mouse"] = {"ignored_filters": ["domain"]}
        return CANDS

    async def check_categories(slots, candidates, report=None):
        calls["categories"] += 1
        return candidates

    async def chat(messages, tools=None):
        calls["llm"].append(messages[0]["content"])
        return {"message": {"content": "Here is why."}, "finish_reason": "stop"}

    monkeypatch.setattr(orchestrator.planner, "plan_slots", plan_slots)
    monkeypatch.setattr(orchestrator.planner, "find_candidates", find_candidates)
    monkeypatch.setattr(orchestrator.planner, "check_categories", check_categories)
    monkeypatch.setattr(orchestrator.llm_client, "chat", chat)
    orchestrator.SESSIONS.clear()
    return calls


@pytest.fixture
def client(fakes):
    main.app.dependency_overrides[require_user] = lambda: "user-a"
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_plan_then_replan(client, fakes):
    r = client.post("/api/plan", json={"image_url": "https://x/desk.jpg", "scenario": "desk",
                                       "budget": 350, "notes": "black theme"})
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"session_id", "slots", "plan", "dropped", "total", "explanation"}
    assert {s: x["p_key"] for s, x in data["plan"].items()} == {"monitor": "M1", "mouse": "m1"}
    assert data["total"] == 350
    assert data["explanation"] == "Here is why."
    assert data["report"] == {"mouse": {"ignored_filters": ["domain"]}}
    # Internal slot fields are not sent to the browser.
    assert all(set(s) <= {"slot", "required", "weight", "query", "domain"} for s in data["slots"])
    assert fakes["plan_slots"] == [("https://x/desk.jpg", "desk", "black theme", 350)]

    r = client.post("/api/replan", json={"session_id": data["session_id"], "budget": 200})
    assert r.status_code == 200
    re = r.json()
    assert {s: x["p_key"] for s, x in re["plan"].items()} == {"monitor": "M2", "mouse": "m1"}
    assert re["changes"] == {"monitor": "replaced", "mouse": "unchanged"}
    assert "note" not in re
    assert fakes["find"] == [350] and fakes["categories"] == 1  # no new search or check
    assert len(fakes["llm"]) == 2 and '"new_budget": 200' in fakes["llm"][1]

    # The next diff is against the re-planned version, not the first one.
    re2 = client.post("/api/replan", json={"session_id": data["session_id"], "budget": 200,
                                           "owned": ["mouse"]}).json()
    assert re2["changes"] == {"monitor": "unchanged", "mouse": "removed"}
    assert {"slot": "mouse", "reason": "owned"} in re2["dropped"]


def test_replan_above_search_budget_says_so(client):
    sid = client.post("/api/plan", json={"image_url": "https://x/d.jpg", "scenario": "desk",
                                         "budget": 200}).json()["session_id"]
    re = client.post("/api/replan", json={"session_id": sid, "budget": 500}).json()
    assert "searched up to $200" in re["note"]


def test_sessions_are_per_user(client):
    sid = client.post("/api/plan", json={"image_url": "https://x/d.jpg", "scenario": "desk",
                                         "budget": 200}).json()["session_id"]
    main.app.dependency_overrides[require_user] = lambda: "user-b"
    r = client.post("/api/replan", json={"session_id": sid, "budget": 150})
    assert r.status_code == 404


def test_bad_input_is_rejected(client):
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "kitchen",
                                       "budget": 100})
    assert r.status_code == 422
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "desk",
                                       "budget": 0})
    assert r.status_code == 422


def test_plan_error_is_422_with_message(client, monkeypatch):
    async def no_items(*a, **kw):
        raise orchestrator.planner.PlanError("Image shows furniture.")

    monkeypatch.setattr(orchestrator.planner, "plan_slots", no_items)
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "outfit",
                                       "budget": 100})
    assert r.status_code == 422 and r.json()["error"] == "Image shows furniture."


def test_routes_need_sign_in(fakes):
    with TestClient(main.app) as c:
        assert c.post("/api/plan", json={}).status_code == 401
        assert c.post("/api/replan", json={}).status_code == 401


def test_explanation_failure_keeps_plan(client, monkeypatch):
    async def down(messages, tools=None):
        raise orchestrator.LlmError("gateway down")

    monkeypatch.setattr(orchestrator.llm_client, "chat", down)
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "desk",
                                       "budget": 350})
    assert r.status_code == 200
    assert r.json()["plan"]
    assert r.json()["explanation"] == "2 items picked for $350.00 of your $350 budget."


def test_old_sessions_are_evicted(fakes):
    import asyncio
    for _ in range(orchestrator.MAX_SESSIONS_PER_USER + 3):
        asyncio.run(orchestrator.plan("u", "https://x", "desk", 350))
    assert sum(1 for k in orchestrator.SESSIONS if k[0] == "u") == orchestrator.MAX_SESSIONS_PER_USER


# ── stricter explanations ────────────────────────────────────────────────

def test_invented_attributes():
    data = '{"picked": {"shoe": {"name": "Women Suede Slip-On Loafers"}}}'
    assert orchestrator.invented_attributes("The suede loafers fit.", data) == set()
    assert orchestrator.invented_attributes("The beige suede flats.", data) == {"beige"}


def test_explanation_with_invented_colour_is_retried(client, monkeypatch):
    replies = iter(["You get beige flats.", "You get the shoes for the budget."])
    seen = []

    async def chat(messages, tools=None):
        seen.append(messages)
        return {"message": {"role": "assistant", "content": next(replies)},
                "finish_reason": "stop"}

    monkeypatch.setattr(orchestrator.llm_client, "chat", chat)
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "desk",
                                       "budget": 350}).json()
    assert r["explanation"] == "You get the shoes for the budget."
    assert "beige" in seen[1][-1]["content"]


def test_explanation_still_inventing_falls_back(client, monkeypatch):
    async def chat(messages, tools=None):
        return {"message": {"role": "assistant", "content": "A sleek black leather monitor."},
                "finish_reason": "stop"}

    monkeypatch.setattr(orchestrator.llm_client, "chat", chat)
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "desk",
                                       "budget": 350}).json()
    assert r["explanation"] == "2 items picked for $350.00 of your $350 budget."


def test_gender_reaches_outfit_planning_only(client, fakes):
    client.post("/api/plan", json={"image_url": "https://x", "scenario": "outfit",
                                   "budget": 150, "gender": "M"})
    assert fakes["gender"] == "M"
    client.post("/api/plan", json={"image_url": "https://x", "scenario": "desk",
                                   "budget": 150, "gender": "M"})
    assert fakes["gender"] is None  # ignored outside outfit
    r = client.post("/api/plan", json={"image_url": "https://x", "scenario": "outfit",
                                       "budget": 150, "gender": "X"})
    assert r.status_code == 422


# ── Lookboard image proxy ────────────────────────────────────────────────

def test_lookboard_image_uses_session_urls_only(client, monkeypatch):
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG...", "image/png"

    monkeypatch.setattr(main.image_proxy, "fetch", fake_fetch)
    sid = client.post("/api/plan", json={"image_url": "https://x/photo.jpg", "scenario": "desk",
                                         "budget": 350}).json()["session_id"]
    r = client.get("/api/lookboard-image", params={"session_id": sid, "slot": "monitor"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r = client.get("/api/lookboard-image", params={"session_id": sid, "slot": "_photo"})
    assert r.status_code == 200
    assert fetched == ["https://img/M1.jpg", "https://x/photo.jpg"]
    assert client.get("/api/lookboard-image",
                      params={"session_id": sid, "slot": "chair"}).status_code == 404
    main.app.dependency_overrides[require_user] = lambda: "user-b"
    assert client.get("/api/lookboard-image",
                      params={"session_id": sid, "slot": "monitor"}).status_code == 404


@pytest.mark.parametrize("url", ["http://example.com/a.jpg", "https://127.0.0.1/a.jpg",
                                 "https://169.254.169.254/latest", "https://localhost/a.png",
                                 "https://10.0.0.5/x.jpg"])
def test_proxy_refuses_non_public_urls(url):
    import asyncio
    with pytest.raises(main.image_proxy.ImageFetchError):
        asyncio.run(main.image_proxy.fetch(url))
