"""Default outfit path: crop re-search, unmatched items, deadline. AISUM and the
bucket are faked; the photo is a real in-memory PIL image."""
import asyncio

import pytest
from PIL import Image

from src.agent import orchestrator, outfit, planner


def prod(key, price, score):
    return {"p_key": key, "product_name": f"Item {key}", "product_price": price,
            "img_url": f"https://img/{key}.jpg", "click_url": f"https://shop/{key}?tag=keep",
            "score": score}


def det(item, detail, products, box=(0.2, 0.2, 0.6, 0.6)):
    return {"item": item, "item_detail": detail, "box": list(box), "products": products}


FIRST = {"detections": [
    det("coat", "grey coat", [prod("coat1", 60, 0.8), prod("coat2", 40, 0.7)]),
    det("shoe", "black shoe", [prod("shoe1", 30, 0.38)], (0.30, 0.90, 0.40, 0.95)),
    det("shoe", "black shoe", [prod("shoe2", 20, 0.36)], (0.55, 0.90, 0.70, 0.96)),
    det("bag", "red bag", [prod("bag1", 25, 0.3), prod("bag2", 15, 0.2),
                           prod("bag3", 10, 0.2), prod("bag4", 12, 0.1)]),
]}


@pytest.fixture
def fake(monkeypatch):
    state = {"image": [], "uploads": [], "active": 0, "peak": 0, "delay": 0.0}
    crop_results = {
        "shoe": {"detections": [det("shoe", "black loafer", [prod("shoe9", 35, 0.71),
                                                              prod("shoe8", 25, 0.6)])]},
        "bag": {"detections": [det("bag", "red bag", [prod("bag9", 20, 0.25)])]},
    }

    async def search_image(url, **kw):
        state["image"].append((url, kw))
        if url == "https://x/photo.jpg":
            return FIRST
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(state["delay"])
        finally:
            state["active"] -= 1
        return crop_results[kw["category"]]

    async def save(data):
        img = Image.open(__import__("io").BytesIO(data))
        state["uploads"].append(img.size)
        return {"url": f"https://bucket/crop{len(state['uploads'])}.jpg"}

    async def photo(url):
        return Image.new("RGB", (1000, 1500), "white")

    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(outfit.uploads, "save", save)
    monkeypatch.setattr(planner, "_photo", photo)
    return state


def test_grow_clamps():
    assert outfit.grow([0.2, 0.2, 0.6, 0.6]) == pytest.approx((0.16, 0.16, 0.64, 0.64))
    assert outfit.grow([0.0, 0.95, 0.5, 1.0]) == pytest.approx((0.0, 0.945, 0.55, 1.0))


def test_weak_items_are_cropped_and_kept_or_unmatched(fake):
    report = {}
    planned, unmatched, cands, scores, timings = asyncio.run(
        outfit.candidates("https://x/photo.jpg", 100, "F", report))
    # Shoes merged; coat strong (no crop); shoe improved by the crop; bag still weak.
    assert [s["slot"] for s in planned] == ["coat", "shoe"]
    assert [s["slot"] for s in unmatched] == ["bag"] and unmatched[0]["unmatched"]
    assert cands["shoe"][0]["p_key"] == "shoe9"
    assert cands["bag"][0]["p_key"] == "bag1"           # the crop's 0.25 was not better
    crops = [kw for url, kw in fake["image"] if url != "https://x/photo.jpg"]
    assert sorted(kw["category"] for kw in crops) == ["bag", "shoe"]
    assert all(kw["gender"] == "F" and kw["max_price"] == 100 for kw in crops)
    assert fake["image"][0][1] == {"max_price": 100, "gender": "F"}
    # The box of the better-scoring shoe (0.38 > 0.36), grown 10 % a side:
    # 0.12 x 0.06 of 1000x1500 -> 120x90, enlarged to a 320 short side.
    assert (427, 320) in fake["uploads"]
    assert report["_crop_search"]["triggered"] == 2 and report["_crop_search"]["improved"] == 1
    assert report["shoe"]["crop_search"]["used"] and not report["bag"]["crop_search"]["used"]
    assert report["bag"]["unmatched"]
    assert set(timings) == {"image_search", "crop_search"}


def test_plan_sets_unmatched_aside_with_alternatives(fake):
    d = asyncio.run(orchestrator.plan("u", "https://x/photo.jpg", "outfit", 100, gender="M"))
    assert set(d["plan"]) == {"coat", "shoe"} and "bag" not in d["plan"]
    assert d["total"] <= 100 and d["budget"] == 100
    assert {"slot": "bag", "reason": "unmatched",
            "message": "No similar product found."} in d["dropped"]
    assert [a["product"]["p_key"] for a in d["alternatives"]["bag"]] == ["bag1", "bag2", "bag3"]
    bag = next(s for s in d["slots"] if s["slot"] == "bag")
    assert bag["unmatched"] and bag["boxes"] == [[0.2, 0.2, 0.6, 0.6]]
    shoe = next(s for s in d["slots"] if s["slot"] == "shoe")
    assert len(shoe["boxes"]) == 2 and len(shoe["detections"]) == 2
    assert "No similar product found for: bag" in d["explanation"]
    # Budget change keeps the unmatched item out; pinning one of its alternatives adds it.
    re = asyncio.run(orchestrator.replan("u", d["session_id"], 80))
    assert "bag" not in re["plan"] and re["total"] <= 80
    assert any(x["reason"] == "unmatched" for x in re["dropped"])
    re = asyncio.run(orchestrator.replan("u", d["session_id"], 100, pins={"bag": "bag2"}))
    assert re["plan"]["bag"]["p_key"] == "bag2" and re["total"] <= 100
    assert not any(x["slot"] == "bag" for x in re["dropped"])
    orchestrator.SESSIONS.clear()


def test_at_most_three_crop_searches_at_once(fake, monkeypatch):
    many = {"detections": [det(f"item{i}", f"thing {i}", [prod(f"p{i}", 10, 0.1)])
                           for i in range(6)]}

    async def search_image(url, **kw):
        if url == "https://x/photo.jpg":
            return many
        fake["active"] += 1
        fake["peak"] = max(fake["peak"], fake["active"])
        await asyncio.sleep(0.02)
        fake["active"] -= 1
        return {"detections": []}

    monkeypatch.setattr(planner, "search_image", search_image)
    asyncio.run(outfit.candidates("https://x/photo.jpg", 100, "F", {}))
    assert fake["peak"] == outfit.MAX_CROP_SEARCHES


def test_deadline_cancels_slow_crop_searches(fake, monkeypatch):
    monkeypatch.setattr(outfit, "DEADLINE_S", 0.6)
    fake["delay"] = 5
    report = {}
    planned, unmatched, cands, _, _ = asyncio.run(
        outfit.candidates("https://x/photo.jpg", 100, "F", report))
    assert report["_crop_search"]["timed_out"] == 2
    assert cands["shoe"][0]["p_key"] == "shoe1"         # first results kept
    assert {s["slot"] for s in unmatched} == {"shoe", "bag"}


def test_slow_first_search_is_a_timeout(monkeypatch):
    async def slow(url, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(planner, "search_image", slow)
    monkeypatch.setattr(outfit, "DEADLINE_S", 0.2)
    with pytest.raises(outfit.PlanTimeout):
        asyncio.run(outfit.candidates("https://x/photo.jpg", 100, "F", {}))


def test_no_detections_is_a_plan_error(monkeypatch):
    async def none(url, **kw):
        return {"detections": [], "note": "Image shows a desk."}

    monkeypatch.setattr(planner, "search_image", none)
    with pytest.raises(planner.PlanError, match="desk"):
        asyncio.run(outfit.candidates("https://x/photo.jpg", 100, "F", {}))


def test_ui_format_for_plan_and_replan(fake):
    from fastapi.testclient import TestClient

    from src import main
    from src.auth import require_user

    main.app.dependency_overrides[require_user] = lambda: "ui-user"
    try:
        with TestClient(main.app) as c:
            body = {"image_url": "https://x/photo.jpg", "scenario": "outfit", "budget": 100,
                    "gender": "F"}
            plain = c.post("/api/plan", json=body).json()
            assert "plan" in plain and "matched_products" not in plain
            ui = c.post("/api/plan?format=ui", json=body).json()
            assert {"session_id", "category", "category_label", "title", "budget",
                    "total_price", "is_over_budget", "original_image", "comparison_image",
                    "matched_products", "detected_items", "unmatched_items"} <= set(ui)
            assert ui["category"] == "outfit" and ui["category_label"] == "Outfit"
            assert ui["original_image"] == "https://x/photo.jpg"
            assert ui["budget"] == 100 and not ui["is_over_budget"]
            assert {m["slot"] for m in ui["matched_products"]} == {"coat", "shoe"}
            coat = next(m for m in ui["matched_products"] if m["slot"] == "coat")
            assert coat["product_url"].endswith("?tag=keep") and coat["rank"] == 1
            assert [a["id"] for a in coat["alternatives"]] == ["coat2"]
            assert set(coat["alternatives"][0]) >= {"id", "name", "price", "image_url", "product_url"}
            shoes = [d for d in ui["detected_items"] if d["slot"] == "shoe"]
            assert len(shoes) == 2 and all(d["matched_product"]["id"] == "shoe9" for d in shoes)
            assert shoes[0]["bounding_box"]["x1"] == 0.30
            # Each detection's best product score, for the page to drop duplicate boxes.
            assert all(isinstance(d["score"], float) for d in ui["detected_items"] if d["bounding_box"])
            bag = [d for d in ui["detected_items"] if d["slot"] == "bag"]
            assert bag[0]["status"] == "unmatched" and bag[0]["matched_product"] is None
            assert ui["unmatched_items"][0]["slot"] == "bag"
            assert len(ui["unmatched_items"][0]["alternatives"]) == 3
            re = c.post("/api/replan?format=ui", json={"session_id": ui["session_id"],
                                                       "budget": 80}).json()
            assert re["budget"] == 80 and re["total_price"] <= 80
            assert re["session_id"] == ui["session_id"] and re["unmatched_items"]
    finally:
        main.app.dependency_overrides.clear()
        orchestrator.SESSIONS.clear()


def test_crop_uses_the_best_scoring_detection_not_the_largest(fake, monkeypatch):
    # Two detections share item_detail; the big one is a false detection (a face).
    sock = {"detections": [
        det("sock", "white sock", [prod("s1", 4, 0.38)], (0.43, 0.86, 0.48, 0.91)),
        det("sock", "white sock", [prod("s2", 3, 0.10)], (0.40, 0.25, 0.49, 0.33)),
    ]}
    boxes = []

    async def search_image(url, **kw):
        if url == "https://x/photo.jpg":
            return sock
        return {"detections": []}

    real_crop = outfit.crop_jpeg
    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(outfit, "crop_jpeg", lambda photo, box: boxes.append(box) or real_crop(photo, box))
    asyncio.run(outfit.candidates("https://x/photo.jpg", 100, "M", {}))
    assert boxes == [[0.43, 0.86, 0.48, 0.91]]


def test_crop_below_050_unmatched_below_045(fake, monkeypatch):
    first = {"detections": [
        det("watch", "black watch", [prod("w1", 16, 0.47)]),     # crop tried, still 0.47
        det("sock", "white sock", [prod("k1", 4, 0.40)]),        # crop tried, still 0.40
        det("coat", "grey coat", [prod("c1", 50, 0.49)]),        # crop tried, 0.49 kept
    ]}

    async def search_image(url, **kw):
        return first if url == "https://x/photo.jpg" else {"detections": []}

    monkeypatch.setattr(planner, "search_image", search_image)
    report = {}
    planned, unmatched, *_ = asyncio.run(outfit.candidates("https://x/photo.jpg", 100, "F", report))
    assert report["_crop_search"]["triggered"] == 3       # all under 0.5
    assert {s["slot"] for s in planned} == {"watch", "coat"}
    assert [s["slot"] for s in unmatched] == ["sock"]      # only under 0.45
