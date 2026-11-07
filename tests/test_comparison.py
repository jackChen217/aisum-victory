"""Exercise real comparison routes, enhancement and DP with only upstream I/O mocked."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from src import main
from src.agent import comparison, planner
from src.auth import require_user


def product(key, name, price):
    return {"p_key": key, "product_name": name, "product_price": price,
            "img_url": f"https://example.test/{key}.jpg",
            "click_url": f"https://example.test/{key}?affiliate=keep"}


@pytest.fixture
def client():
    previous = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[require_user] = lambda: "comparison-user"
    comparison.SESSIONS.clear()
    with TestClient(main.app) as value:
        yield value
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(previous)
    comparison.SESSIONS.clear()


def test_three_stages_keep_native_top1_reuse_pool_and_optimize(client, monkeypatch):
    calls = []
    coat = [product("coat-1", "Grey wool coat", 40), product("coat-2", "Charcoal trench coat", 25)]
    shoes = [product("shoe-1", "Brown leather shoes", 30), product("shoe-2", "Black canvas sneakers", 20)]
    budget_response = {"detections": [
        {"item": "coat", "item_detail": "grey coat", "products": coat},
        {"item": "shoe", "item_detail": "brown shoe", "products": shoes},
        {"item": "shoe", "item_detail": "brown shoe", "products": shoes},
    ]}

    async def image(url, **filters):
        calls.append(filters)
        result = copy.deepcopy(budget_response)
        if "max_price" not in filters:
            result["detections"][0]["products"].insert(0, product("premium", "Grey premium coat", 180))
            result["detections"].append({"item": "glasses", "products": [product("unknown", "Sunglasses", None)]})
        return result

    async def chat(*args, **kwargs):
        return {"message": {"content": json.dumps({"keep": [0, 1, 2], "correct": [], "drop": [], "missing": []})}}

    async def text(*args, **kwargs):
        return {"results": []}

    async def no_photo(*args, **kwargs):
        return None

    async def no_palettes(*args, **kwargs):
        return {}

    monkeypatch.setattr(planner, "search_image", image)
    monkeypatch.setattr(planner, "search_text", text)
    monkeypatch.setattr(planner.llm_client, "chat", chat)
    monkeypatch.setattr(planner, "_photo", no_photo)
    monkeypatch.setattr(planner.matching, "product_palettes", no_palettes)
    request = {"image_url": "https://example.test/photo.jpg", "budget": 60}
    raw = client.post("/api/compare/outfit", json={**request, "mode": "raw", "gender": "F"}).json()
    assert raw["plan"]["1_coat"]["p_key"] == "premium"
    assert raw["unknown_prices"] == 1
    assert raw["total"] == 240 and raw["over_budget"]
    # Baseline retains both shoes, does not pretend an unknown price is zero.
    assert len(raw["plan"]) == 4 and len(raw["slots"]) == 4
    assert "gender" not in raw["request_args"]
    request["comparison_id"] = raw["comparison_id"]
    budget = client.post("/api/compare/outfit", json={**request, "mode": "budget"}).json()
    assert budget["total"] == 100 and budget["over_budget"]
    assert not budget["total_budget_enforced"]
    enhanced_response = client.post("/api/compare/outfit", json={**request, "mode": "enhanced"})
    assert enhanced_response.status_code == 200, enhanced_response.text
    enhanced = enhanced_response.json()
    assert len(enhanced["plan"]) == 2  # merged shoe pair
    assert enhanced["total"] <= 60 and not enhanced["infeasible"]
    assert enhanced["total_budget_enforced"]
    assert enhanced["report"]["_matching"]["enhanced"]
    assert enhanced["report"]["_comparison"]["reused_budget_search"]
    assert calls == [{}, {"max_price": 60}]
    assert enhanced["timings_ms"]["including_reused_search"] >= enhanced["timings_ms"]["total"]
    assert all(p["click_url"].endswith("?affiliate=keep") for p in enhanced["plan"].values())
    saved = comparison.SESSIONS[("comparison-user", raw["comparison_id"])]
    assert saved["variants"]["budget"]["raw_response"] == budget_response
    # Proxy only resolves products from the authenticated owner's saved comparison.
    assert comparison.image_url_for("comparison-user", raw["comparison_id"], "raw", "1_coat").endswith("premium.jpg")
    main.app.dependency_overrides[require_user] = lambda: "another-user"
    assert client.get("/api/comparison-image", params={"comparison_id": raw["comparison_id"], "variant": "raw", "slot": "1_coat"}).status_code == 404


def test_enhancement_failure_does_not_erase_baselines(client, monkeypatch):
    async def image(*args, **kwargs):
        return {"detections": [{"item": "coat", "products": [product("a", "Coat", 20)]}]}

    async def failed(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(planner, "search_image", image)
    request = {"image_url": "https://example.test/photo.jpg", "budget": 50}
    raw = client.post("/api/compare/outfit", json={**request, "mode": "raw"}).json()
    request["comparison_id"] = raw["comparison_id"]
    assert client.post("/api/compare/outfit", json={**request, "mode": "budget"}).status_code == 200
    monkeypatch.setattr(planner, "outfit_candidates", failed)
    assert client.post("/api/compare/outfit", json={**request, "mode": "enhanced"}).status_code == 504
    assert set(comparison.SESSIONS[("comparison-user", raw["comparison_id"])]["variants"]) == {"raw", "budget"}
    assert client.post("/api/compare/outfit", json={**request, "mode": "budget", "budget": 80}).status_code == 422


def test_gender_only_changes_enhanced_search_and_empty_results_are_explicit(client, monkeypatch):
    calls = []

    async def image(url, **filters):
        calls.append(filters)
        return {"detections": [], "note": "No fashion items found."}

    monkeypatch.setattr(planner, "search_image", image)
    request = {"image_url": "https://example.test/photo.jpg", "budget": 50}
    raw = client.post("/api/compare/outfit", json={**request, "mode": "raw"}).json()
    request["comparison_id"] = raw["comparison_id"]
    client.post("/api/compare/outfit", json={**request, "mode": "budget"})
    result = client.post("/api/compare/outfit", json={**request, "mode": "enhanced", "gender": "M"}).json()
    assert calls == [{}, {"max_price": 50}, {"max_price": 50, "gender": "M"}]
    assert not result["report"]["_comparison"]["reused_budget_search"]
    assert result["plan"] == {} and "No fashion" in result["report"]["note"]
