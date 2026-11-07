"""Offline tests: the LLM reply parser, result cleaning, and find_candidates with the
AISUM adapter replaced. Nothing here calls the network."""
import asyncio

import pytest

from src.agent import planner
from src.mcp_client import McpError


def p(key, name, price=10):
    return {"p_key": key, "product_name": name, "product_price": price}


# ── _parse_slots ─────────────────────────────────────────────────────────

def test_parse_slots_normalises_fields():
    reply = '''```json
    [{"slot": "Gaming Mouse", "required": true, "weight": 9, "query": "black RGB mouse",
      "domain": "electronics"},
     {"slot": "lamp", "required": "yes", "weight": "2", "query": "desk lamp",
      "domain": ["furniture"]},
     {"slot": "gaming mouse", "query": "duplicate"},
     {"slot": "no_query"}]
    ```'''
    slots = planner._parse_slots(reply, ["office"])
    assert slots == [
        {"slot": "gaming_mouse", "required": True, "weight": 5, "query": "black RGB mouse",
         "domain": ["electronics"]},
        # "yes" is not true; unknown domain falls back to the scenario default.
        {"slot": "lamp", "required": False, "weight": 2, "query": "desk lamp",
         "domain": ["office"]},
    ]


def test_parse_slots_accepts_wrapped_object_and_caps_count():
    items = ",".join(f'{{"slot": "s{i}", "query": "q{i}"}}' for i in range(12))
    slots = planner._parse_slots(f'Here you go: {{"slots": [{items}]}}', ["office"])
    assert len(slots) == planner.MAX_SLOTS


@pytest.mark.parametrize("bad", ["", "no json here", "[1, 2]", '{"a": 1}', "[{"])
def test_parse_slots_rejects_unusable(bad):
    with pytest.raises(ValueError):
        planner._parse_slots(bad, ["office"])


def test_llm_slots_retries_once(monkeypatch):
    replies = iter(["sorry, not JSON", '[{"slot": "desk", "query": "black desk"}]'])
    calls = []

    async def fake_chat(messages, tools=None):
        calls.append(messages)
        return {"message": {"role": "assistant", "content": next(replies)},
                "finish_reason": "stop"}

    monkeypatch.setattr(planner.llm_client, "chat", fake_chat)
    slots = asyncio.run(planner.plan_slots("https://x/img.jpg", "desk"))
    assert [s["slot"] for s in slots] == ["desk"]
    assert len(calls) == 2
    image_part = calls[0][1]["content"][1]
    assert image_part == {"type": "image_url", "image_url": {"url": "https://x/img.jpg"}}


# ── clean ────────────────────────────────────────────────────────────────

def test_clean():
    products = [
        p("a", "Corsair Vengeance RGB Pro 16GB DDR4 3200 Black"),
        p("a", "same key again"),
        p("b", "Corsair Vengeance RGB Pro 16GB DDR4 3200 - Black"),  # near-duplicate
        p("c", 'ASUS ROG Strix G15 Gaming Laptop 15â€ 16:10'),        # mojibake
        p("d", "Razer BlackWidow V3 Mechanical Keyboard"),
    ]
    assert [x["p_key"] for x in planner.clean(products)] == ["a", "d"]


# ── find_candidates ──────────────────────────────────────────────────────

def test_find_candidates(monkeypatch):
    seen = []

    async def fake_search(query, *, domain=None, min_price=None, max_price=None, top_k=10):
        seen.append((query, domain, max_price, top_k))
        if query == "boom":
            raise McpError("busy", code="RATE_LIMITED")
        return {"results": [p(query, f"{query} one"), p(query + "2", f"{query} two zz")],
                "ignored_filters": ["domain"] if query == "lamp" else []}

    monkeypatch.setattr(planner, "search_text", fake_search)
    slots = [
        {"slot": "desk", "query": "desk", "domain": ["office"]},
        {"slot": "lamp", "query": "lamp", "domain": ["home_living"]},
        {"slot": "coat", "query": "beige coat", "candidates": [p("k", "Trench coat")]},
        {"slot": "fail", "query": "boom"},
    ]
    report = {}
    out = asyncio.run(planner.find_candidates(slots, 350, report=report))
    assert [x["p_key"] for x in out["desk"]] == ["desk", "desk2"]
    assert [x["p_key"] for x in out["coat"]] == ["k"]  # not searched again
    assert out["fail"] == []
    assert report == {"lamp": {"ignored_filters": ["domain"]},
                      "fail": {"error": "RATE_LIMITED: busy"}}
    assert ("desk", ["office"], 350, 15) in seen
    assert all(q != "beige coat" for q, *_ in seen)


# ── outfit: merging detections, max_price ───────────────────────────────

def det(item, detail, *keys, box=(0, 0, 1, 1), score=0.8):
    return {"item": item, "item_detail": detail, "box": list(box),
            "products": [dict(p(k, f"{detail} product {k}"), score=score) for k in keys]}


def test_outfit_merges_identical_detections_and_passes_budget(monkeypatch):
    seen = {}

    async def fake_image(image_url, *, min_price=None, max_price=None, top_k=15):
        seen["max_price"] = max_price
        return {"detections": [
            det("shoe", "beige shoe", "s1", "s2", box=(0.3, 0.8, 0.4, 0.9)),
            det("pants", "blue pants", "p1"),
            det("shoe", "beige shoe", "s3", box=(0.5, 0.8, 0.6, 0.9)),  # other foot
            det("shoe", "black shoe", "b1"),                            # a different shoe
        ], "ignored_filters": []}

    monkeypatch.setattr(planner, "search_image", fake_image)
    slots = asyncio.run(planner.plan_slots("https://x/o.jpg", "outfit", budget=150))
    assert seen["max_price"] == 150
    assert [(s["slot"], s["query"]) for s in slots] == [
        ("shoe", "beige shoe"), ("pants", "blue pants"), ("shoe_2", "black shoe")]
    shoe = slots[0]
    assert [x["p_key"] for x in shoe["candidates"]] == ["s1", "s2", "s3"]
    assert shoe["boxes"] == [[0.3, 0.8, 0.4, 0.9], [0.5, 0.8, 0.6, 0.9]]
    assert shoe["source"] == "image_search" and shoe["image_url"] == "https://x/o.jpg"
    assert all(s["required"] and s["weight"] == 3 for s in slots)


def test_outfit_without_detections_raises_with_note(monkeypatch):
    async def fake_image(image_url, **kw):
        return {"detections": [], "note": "Image shows furniture."}

    monkeypatch.setattr(planner, "search_image", fake_image)
    with pytest.raises(planner.PlanError, match="furniture"):
        asyncio.run(planner.plan_slots("https://x/desk.jpg", "outfit"))


# ── self-correction ──────────────────────────────────────────────────────

def outfit_slot(name, n, score):
    return {"slot": name, "query": f"{name} detail", "item": name, "required": True,
            "weight": 3, "source": "image_search", "image_url": "https://x/o.jpg",
            "boxes": [[0.1, 0.2, 0.3, 0.4]], "domain": ["fashion"],
            "candidates": [dict(p(f"{name}{i}", f"{STYLES[i]} {name}"), score=score)
                           for i in range(n)]}


# Distinct enough that clean() keeps them all.
STYLES = ["Oversized wool", "Cropped denim", "Quilted puffer", "Linen relaxed",
          "Faux leather biker", "Knitted chunky", "Corduroy vintage", "Satin evening"]


@pytest.fixture
def refine_env(monkeypatch):
    calls = {"llm": [], "text": []}

    async def fake_chat(messages, tools=None):
        calls["llm"].append(messages)
        return {"message": {"content": '"cream houndstooth wool blanket scarf"\n'},
                "finish_reason": "stop"}

    async def fake_text(query, *, domain=None, min_price=None, max_price=None, top_k=10):
        calls["text"].append({"query": query, "domain": domain, "max_price": max_price})
        return {"results": [p("t1", "Text result one"), p("t2", "Another text hit")]}

    monkeypatch.setattr(planner.llm_client, "chat", fake_chat)
    monkeypatch.setattr(planner, "search_text", fake_text)
    return calls


def test_strong_slot_is_not_refined(refine_env):
    report = {}
    out = asyncio.run(planner.find_candidates([outfit_slot("coat", 6, 0.7)], 150, report))
    assert len(out["coat"]) == 6
    assert report == {} and refine_env["llm"] == [] and refine_env["text"] == []


def test_few_candidates_appends_text_results(refine_env):
    report = {}
    out = asyncio.run(planner.find_candidates([outfit_slot("shoe", 2, 0.7)], 150, report))
    assert [x["p_key"] for x in out["shoe"]] == ["shoe0", "shoe1", "t1", "t2"]
    assert report["shoe"]["refined"] == {"reasons": ["few_candidates"],
                                         "query": "cream houndstooth wool blanket scarf",
                                         "added": 2}
    assert refine_env["text"] == [{"query": "cream houndstooth wool blanket scarf",
                                   "domain": ["fashion"], "max_price": 150}]
    # The model saw the original photo and where the item is.
    content = refine_env["llm"][0][0]["content"]
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://x/o.jpg"}}
    assert "[0.1, 0.2, 0.3, 0.4]" in content[0]["text"]


def test_low_score_puts_text_results_first(refine_env):
    report = {}
    out = asyncio.run(planner.find_candidates([outfit_slot("scarf", 8, 0.40)], 150, report))
    assert [x["p_key"] for x in out["scarf"]][:3] == ["t1", "t2", "scarf0"]
    assert report["scarf"]["refined"]["reasons"] == ["low_score"]


def test_threshold_is_per_tool(refine_env):
    # A text-search slot with a low score is not refined: no threshold for that tool,
    # and no photo to look at.
    slot = {"slot": "lamp", "query": "desk lamp", "domain": ["home_living"]}
    report = {}
    asyncio.run(planner.find_candidates([slot], 150, report))
    assert refine_env["llm"] == [] and "refined" not in report.get("lamp", {})


def test_refine_failure_keeps_candidates(monkeypatch, refine_env):
    async def broken_chat(messages, tools=None):
        raise planner.LlmError("gateway down")

    monkeypatch.setattr(planner.llm_client, "chat", broken_chat)
    report = {}
    out = asyncio.run(planner.find_candidates([outfit_slot("shoe", 2, 0.3)], 150, report))
    assert [x["p_key"] for x in out["shoe"]] == ["shoe0", "shoe1"]
    assert report["shoe"]["refine_error"] == "LlmError: gateway down"
