"""Offline tests: the LLM reply parser, result cleaning, and find_candidates with the
AISUM adapter replaced. Nothing here calls the network."""
import asyncio
import json

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
        p("c", 'ASUS ROG Strix G15 Gaming Laptop 15â€ 16:10'),        # mojibake: repaired
        p("e", "Broken \ufffd name"),                                 # unrepairable: dropped
        p("d", "Razer BlackWidow V3 Mechanical Keyboard"),
    ]
    out = planner.clean(products)
    assert [x["p_key"] for x in out] == ["a", "c", "d"]
    assert out[1]["product_name"] == "ASUS ROG Strix G15 Gaming Laptop 15” 16:10"


# ── find_candidates ──────────────────────────────────────────────────────

def test_find_candidates(monkeypatch):
    seen = []

    async def fake_search(query, *, domain=None, min_price=None, max_price=None, gender=None, top_k=10):
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

    async def fake_image(image_url, *, min_price=None, max_price=None, gender=None, top_k=15):
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


# ── check_categories ─────────────────────────────────────────────────────

CAT_SLOTS = [{"slot": "desk", "query": "black gaming desk"},
             {"slot": "speaker", "query": "desktop speaker"},
             {"slot": "mouse", "query": "gaming mouse"}]


def cat_cands():
    return {"desk": [p("d1", "L-Shaped Gaming Desk"), p("d2", "Gaming Mouse Pad XL"),
                     p("d3", "Standing Desk 55 inch")],
            "speaker": [p("s1", "Computer Speakers Pair"), p("s2", "2Pcs Internal Speaker Replacement")],
            "mouse": []}


def test_check_categories_one_call_per_slot(monkeypatch):
    prompts = []
    replies = {"desk": '```json\n{"reject": [1]}\n```', "speaker": '{"reject": [1, 9, "x"]}'}

    async def chat(messages, tools=None):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        slot = "desk" if '"slot": "desk"' in prompt else "speaker"
        return {"message": {"content": replies[slot]}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    report = {}
    out = asyncio.run(planner.check_categories(CAT_SLOTS, cat_cands(), report))
    assert len(prompts) == 2  # one per slot with candidates; empty "mouse" skipped
    assert [x["p_key"] for x in out["desk"]] == ["d1", "d3"]
    assert [x["p_key"] for x in out["speaker"]] == ["s1"]  # out-of-range / non-int ids ignored
    assert out["mouse"] == []
    assert report["desk"]["category_rejected"] == ["Gaming Mouse Pad XL"]
    assert report["_category_check"] == {"rejected": 2, "failed": []}


def test_check_categories_runs_in_parallel_capped(monkeypatch):
    running, peak = 0, 0

    async def chat(messages, tools=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"message": {"content": '{"reject": []}'}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    slots = [{"slot": f"s{i}", "query": "x"} for i in range(9)]
    cands = {s["slot"]: [p(s["slot"], "Thing")] for s in slots}
    asyncio.run(planner.check_categories(slots, cands, {}))
    assert peak == planner.MAX_CONCURRENT_CHECKS


def test_check_categories_one_failure_keeps_that_slot(monkeypatch):
    async def chat(messages, tools=None):
        if '"slot": "desk"' in messages[0]["content"]:
            raise planner.LlmError("down")
        return {"message": {"content": '{"reject": [1]}'}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    report = {}
    cands = cat_cands()
    out = asyncio.run(planner.check_categories(CAT_SLOTS, cands, report))
    assert out["desk"] == cands["desk"]  # failed: unchanged
    assert [x["p_key"] for x in out["speaker"]] == ["s1"]  # the other slot still checked
    assert report["desk"]["category_error"] == "LlmError: down"
    assert report["_category_check"] == {"rejected": 1, "failed": ["desk"]}


def test_check_categories_bad_reply_keeps_slot(monkeypatch):
    async def chat(messages, tools=None):
        return {"message": {"content": "sure, here you go"}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    report = {}
    cands = cat_cands()
    assert asyncio.run(planner.check_categories(CAT_SLOTS, cands, report)) == cands
    assert sorted(report["_category_check"]["failed"]) == ["desk", "speaker"]


# ── gender and mojibake ─────────────────────────────────────────────────

def test_gender_must_be_a_tool_value():
    with pytest.raises(ValueError):
        asyncio.run(planner.plan_slots("https://x", "outfit", gender="Women"))


@pytest.mark.parametrize("raw,fixed", [
    ("Gaming Laptop 15â€ 16:10 FHD", "Gaming Laptop 15” 16:10 FHD"),
    ("AMD Ryzenâ„¢ 7", "AMD Ryzen™ 7"),
    ("CafÃ© Table", "Café Table"),
    ("Itâ€™s 5Â°C ready", "It’s 5°C ready"),
    ("1PC Crossbody Bag, Multi\u00e2\u0080\u0091Pocket Purse", "1PC Crossbody Bag, Multi\u2011Pocket Purse"),
    ("Mixed: CafÃ© and Itâ€™s and 15â€ 16:10", "Mixed: Café and It’s and 15” 16:10"),
    ("Plain name", "Plain name"),
    ("broken \ufffd name", None),
])
def test_fix_mojibake(raw, fixed):
    assert planner.fix_mojibake(raw) == fixed


def test_clean_repairs_without_touching_original():
    original = p("a", "Laptop 15â€ 16:10")
    out = planner.clean([original, p("b", "bad \ufffd")])
    assert [x["product_name"] for x in out] == ["Laptop 15” 16:10"]
    assert original["product_name"] == "Laptop 15â€ 16:10"


# ── auto scenario ────────────────────────────────────────────────────────

def test_auto_uses_outfit_when_clothes_are_detected(monkeypatch):
    async def fake_image(image_url, **kw):
        return {"detections": [det("coat", "red coat", "c1")]}

    monkeypatch.setattr(planner, "search_image", fake_image)
    slots = asyncio.run(planner.plan_slots("https://x", "auto", budget=100))
    assert planner.scenario_of(slots) == "outfit" and slots[0]["slot"] == "coat"


def test_auto_falls_back_to_desk_planning(monkeypatch):
    async def fake_image(image_url, **kw):
        return {"detections": [], "note": "Image shows furniture and electronics."}

    async def fake_chat(messages, tools=None):
        assert "desk or workstation" in messages[1]["content"][0]["text"]
        return {"message": {"content": '[{"slot": "monitor", "query": "27 inch monitor"}]'}}

    monkeypatch.setattr(planner, "search_image", fake_image)
    monkeypatch.setattr(planner.llm_client, "chat", fake_chat)
    slots = asyncio.run(planner.plan_slots("https://x", "auto", budget=100))
    assert planner.scenario_of(slots) == "desk" and slots[0]["slot"] == "monitor"


# ── review_outfit ────────────────────────────────────────────────────────

def outfit_slots(gender="M"):
    res = {"detections": [
        det("jacket", "brown jacket", "j1", box=(0.3, 0.2, 0.7, 0.6)),
        det("shorts", "black shorts", box=(0.3, 0.55, 0.7, 0.75)),        # no products
        det("shoe", "black shoe", "s1", box=(0.3, 0.9, 0.4, 1.0)),
        det("shoe", "black shoe", "s2", box=(0.6, 0.9, 0.7, 1.0)),        # merged with #2
    ]}
    return planner._outfit_slots(res, "https://x/o.jpg", gender)


def review_reply(payload):
    async def chat(messages, tools=None):
        chat.calls.append(messages)
        return {"message": {"content": payload if isinstance(payload, str) else json.dumps(payload)}}
    chat.calls = []
    return chat


def test_review_corrects_adds_and_records(monkeypatch):
    chat = review_reply({"keep": [2, 3], "correct": [
        {"id": 0, "description": "rust red wool duffle coat with toggle buttons"},
        {"id": 1, "description": "black leather mini skirt"}],
        "missing": [{"slot": "Socks", "layer": "socks", "description": "white crew socks"},
                    {"slot": "shoes", "layer": "footwear", "description": "dupe of shoe"},
                    {"slot": "cap", "layer": "headwear", "description": "red baseball cap"},
                    {"slot": "belt", "layer": "weird", "description": "black leather belt"},
                    {"slot": "scarf", "layer": "accessory", "description": "over the limit"}]})
    monkeypatch.setattr(planner.llm_client, "chat", chat)
    report = {}
    slots = asyncio.run(planner.review_outfit("https://x/o.jpg", outfit_slots(), report))

    assert len(chat.calls) == 1  # one call for the whole outfit
    sent = chat.calls[0][0]["content"]
    assert sent[1] == {"type": "image_url", "image_url": {"url": "https://x/o.jpg"}}
    assert '"id": 3' in sent[0]["text"] and "[0.6, 0.9, 0.7, 1.0]" in sent[0]["text"]

    by = {s["slot"]: s for s in slots}
    assert by["jacket"]["refine_query"] == "rust red wool duffle coat with toggle buttons"
    assert by["jacket"]["detected_as"] == "brown jacket"
    assert "refine_query" not in by["shoe"]
    # "shoes" duplicates "shoe"; only the first 3 missing items are considered.
    assert [s["slot"] for s in slots if s["source"] == "review"] == ["socks", "cap"]
    assert by["socks"]["required"] is False and by["socks"]["gender"] == "M"
    assert "candidates" not in by["socks"]
    assert report["_review"]["corrected"] == [
        {"slot": "jacket", "from": "brown jacket", "to": "rust red wool duffle coat with toggle buttons"},
        {"slot": "shorts", "from": "black shorts", "to": "black leather mini skirt"}]
    assert [a["slot"] for a in report["_review"]["added"]] == ["socks", "cap"]


def test_review_unconfirmed_detection_is_kept_and_noted(monkeypatch):
    monkeypatch.setattr(planner.llm_client, "chat", review_reply({"keep": [0, 2], "correct": [], "missing": []}))
    report = {}
    slots = asyncio.run(planner.review_outfit("https://x", outfit_slots(), report))
    assert [s["slot"] for s in slots] == ["jacket", "shorts", "shoe"]
    assert report["_review"] == {"corrected": [], "added": [], "removed": [], "unconfirmed": ["shorts"]}


@pytest.mark.parametrize("reply", ["no json here", '{"keep": "all"', planner.LlmError("down")])
def test_review_failure_keeps_original_flow(monkeypatch, reply):
    async def chat(messages, tools=None):
        if isinstance(reply, Exception):
            raise reply
        return {"message": {"content": reply}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    report, original = {}, outfit_slots()
    assert asyncio.run(planner.review_outfit("https://x", original, report)) == original
    assert "error" in report["_review"]


def test_corrected_and_added_slots_are_searched(monkeypatch):
    monkeypatch.setattr(planner.llm_client, "chat", review_reply({"keep": [2, 3], "correct": [
        {"id": 1, "description": "black leather mini skirt"}],
        "missing": [{"slot": "socks", "layer": "socks", "description": "white crew socks"}]}))
    searched = []

    async def fake_text(query, *, domain=None, min_price=None, max_price=None, gender=None, top_k=10):
        searched.append((query, domain, gender, max_price))
        return {"results": [p(f"t-{query[:5]}", f"Text hit for {query}")]}

    monkeypatch.setattr(planner, "search_text", fake_text)
    slots = asyncio.run(planner.review_outfit("https://x", outfit_slots(gender="F")))
    report = {}
    out = asyncio.run(planner.find_candidates(slots, 150, report))
    assert sorted(searched) == [("black leather mini skirt", ["fashion"], "F", 150),
                                ("white crew socks", ["fashion"], "F", 150)]
    assert [x["p_key"] for x in out["shorts"]] == ["t-black"]          # had no image products
    assert [x["p_key"] for x in out["jacket"]] == ["j1"]               # kept: not searched again
    assert [x["p_key"] for x in out["socks"]] == ["t-white"]
    assert report["shorts"]["corrected_search"] == {"query": "black leather mini skirt", "added": 1}


def test_corrected_results_go_before_image_results(monkeypatch):
    slot = dict(outfit_slots()[0], refine_query="rust duffle coat")

    async def fake_text(query, **kw):
        return {"results": [p("t1", "Duffle coat with toggles")]}

    monkeypatch.setattr(planner, "search_text", fake_text)
    out = asyncio.run(planner.find_candidates([slot], 150, {}))
    assert [x["p_key"] for x in out["jacket"]] == ["t1", "j1"]



def test_review_drops_background_items(monkeypatch):
    monkeypatch.setattr(planner.llm_client, "chat", review_reply(
        {"keep": [1, 2, 3], "correct": [], "drop": [0], "missing": []}))
    report = {}
    slots = asyncio.run(planner.review_outfit("https://x", outfit_slots(), report))
    assert [s["slot"] for s in slots] == ["shorts", "shoe"]
    assert report["_review"]["removed"] == [{"slot": "jacket", "was": "brown jacket"}]


def test_review_prompt_limits_to_the_person():
    assert "WEARING or HOLDING" in planner._REVIEW_PROMPT
    assert "rack" in planner._REVIEW_PROMPT and '"drop"' in planner._REVIEW_PROMPT


def test_desk_category_check_looks_at_first_ten(monkeypatch):
    seen = []

    async def chat(messages, tools=None):
        seen.append(messages[0]["content"].count('"id":'))
        return {"message": {"content": '{"reject": [0]}'}}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    cands = {"desk": [p(f"d{i}", f"Desk {i}") for i in range(14)]}
    out = asyncio.run(planner.check_categories([{"slot": "desk", "query": "desk"}], cands, {}))
    assert seen == [10]
    assert [x["p_key"] for x in out["desk"]] == [f"d{i}" for i in range(1, 10)]


# ── outfit pipeline ──────────────────────────────────────────────────────

def test_outfit_candidates_runs_review_alongside_searches(monkeypatch):
    order = []

    async def chat(messages, tools=None):
        order.append("review-start")
        await asyncio.sleep(0.05)
        order.append("review-end")
        return {"message": {"content": json.dumps({
            "keep": [2, 3], "correct": [{"id": 0, "description": "rust red duffle coat"}],
            "drop": [1], "missing": [{"slot": "socks", "layer": "socks",
                                      "description": "white crew socks"}]})}}

    async def fake_text(query, *, domain=None, min_price=None, max_price=None, gender=None, top_k=10):
        order.append(f"text:{query}")
        names = {"brown jacket": ["Brown Zip Jacket"], "black shoe": ["Black Leather Loafers"],
                 "rust red duffle coat": ["Rust Duffle Coat with Toggles", "Red Wool Scarf"],
                 "white crew socks": ["White Crew Socks 6 Pack"], "black shorts": ["Black Shorts"]}
        return {"results": [p(f"{query[:4]}{i}", n) for i, n in enumerate(names.get(query, []))]}

    async def no_photo(url):
        raise RuntimeError("offline")

    async def no_palettes(urls, fetch):
        return {u: None for u in urls}

    monkeypatch.setattr(planner.llm_client, "chat", chat)
    monkeypatch.setattr(planner, "search_text", fake_text)
    monkeypatch.setattr(planner, "_photo", no_photo)
    monkeypatch.setattr(planner.matching, "product_palettes", no_palettes)
    report = {}
    slots, cands, scores = asyncio.run(planner.outfit_candidates(
        "https://x/o.jpg", outfit_slots(), 150, report, enhanced=True))

    # Early searches start before the review answers; the corrected one after it.
    assert order.index("text:black shoe") < order.index("review-end")
    assert order.index("text:rust red duffle coat") > order.index("review-end")
    assert [s["slot"] for s in slots] == ["jacket", "shoe", "socks"]   # shorts dropped
    # Corrected: its text results first; the scarf fails the coat keyword rule.
    assert [x["product_name"] for x in cands["jacket"]][0] == "Rust Duffle Coat with Toggles"
    assert "Red Wool Scarf" in report["jacket"]["category_rejected"]
    # Kept slot: image results, then its early text search.
    assert {x["p_key"] for x in cands["shoe"]} == {"s1", "blac0"}  # s2: near-duplicate name
    assert [x["product_name"] for x in cands["socks"]] == ["White Crew Socks 6 Pack"]
    assert all(len(scores[k]) == len(cands[k]) for k in cands)
    assert report["_colour"]["error"].startswith("photo not loaded")
