"""Recovery baseline: real routing/planning/optimization with only AIRS mocked."""
from fastapi.testclient import TestClient

from src import main
from src.agent import orchestrator, planner
from src.auth import require_user


def product(key, name, price):
    return {"p_key": key, "product_name": name, "product_price": price,
            "img_url": f"https://example.test/{key}.jpg",
            "click_url": f"https://example.test/buy/{key}?affiliate=keep", "score": 0.8}


def test_native_variants_survive_and_budget_pins_need_no_extra_calls(monkeypatch):
    native = [product("red", "Striped Cotton Shirt - red", 70),
              product("tan", "Striped Cotton Shirt - tan", 40)]
    shoes = [product("shoe-a", "Leather Shoes", 30),
             product("shoe-b", "Canvas Shoes", 20)]
    calls = []

    async def image(url, **filters):
        calls.append((url, filters))
        return {"detections": [
            {"item": "shirt", "item_detail": "tan shirt", "products": native + [native[0]]},
            {"item": "shoe", "item_detail": "black shoe", "products": shoes},
        ]}

    async def forbidden(*args, **kwargs):
        raise AssertionError("Native baseline must not review, search again, or download images")

    monkeypatch.setattr(planner, "search_image", image)
    monkeypatch.setattr(planner, "search_text", forbidden)
    monkeypatch.setattr(planner, "review_outfit", forbidden)
    monkeypatch.setattr(planner, "_photo", forbidden)
    monkeypatch.setattr(planner.matching, "product_palettes", forbidden)
    monkeypatch.setattr(planner.llm_client, "chat", forbidden)
    assert orchestrator.GENERATE_EXPLANATIONS is False
    previous_overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[require_user] = lambda: "recovery-user"
    try:
        with TestClient(main.app) as client:
            response = client.post("/api/plan", json={
                "image_url": "https://example.test/outfit.jpg", "scenario": "outfit",
                "budget": 100, "gender": "UNISEX"})
            assert response.status_code == 200
            first = response.json()
            assert first["plan"]["shirt"]["p_key"] == "red"
            assert first["total"] == 100
            # A near-identical title is a different variant, not a duplicate product.
            assert [p["product"]["p_key"] for p in first["alternatives"]["shirt"]] == ["tan"]
            assert first["plan"]["shirt"]["click_url"].endswith("?affiliate=keep")
            assert first["report"]["_matching"]["method"] == "image_rank"
            assert all(value >= 0 for value in first["timings_ms"].values())

            replan = client.post("/api/replan", json={
                "session_id": first["session_id"], "budget": 60}).json()
            assert replan["total"] == 60
            assert replan["plan"]["shirt"]["p_key"] == "tan"
            assert replan["plan"]["shoe"]["p_key"] == "shoe-b"
            assert replan["changes"] == {"shirt": "replaced", "shoe": "replaced"}

            pinned = client.post("/api/replan", json={
                "session_id": first["session_id"], "budget": 100,
                "pin": {"shirt": "tan"}}).json()
            assert pinned["plan"]["shirt"]["p_key"] == "tan"
            assert pinned["total"] <= 100
            assert len(calls) == 1  # all adjustments reuse the original candidates
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous_overrides)
        orchestrator.SESSIONS.clear()
