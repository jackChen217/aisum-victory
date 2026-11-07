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
