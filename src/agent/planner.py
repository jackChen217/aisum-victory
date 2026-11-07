"""Turn a photo into shopping slots, then find candidate products for each slot.

    plan_slots(image_url, scenario, notes)  -> [slot, ...]
    find_candidates(slots, budget)          -> {slot: [product, ...]}

A slot is {"slot", "required", "weight" (1-5), "query", "domain"}, the shape
optimizer.optimize() takes. Outfit slots also carry "candidates", the products image
search already returned for that detection, so find_candidates does not search again.

desk / renter: one DeepSeek call with the image decides the slots.
outfit:        no LLM. search_products_by_image already splits the photo into items.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re

from .. import llm_client
from ..mcp_client import McpClient, McpError

log = logging.getLogger(__name__)

MAX_SLOTS = 8
TEXT_TOP_K = 15
# How many text searches run at once. The server has its own per-tool concurrency
# limit (see /status); staying under it avoids RATE_LIMITED in the first place.
MAX_CONCURRENT_SEARCHES = 4

DOMAINS = {"beauty", "electronics", "fashion", "health", "home_living", "jewelry_bags",
           "kitchen", "office", "sports", "tools", "toys"}

SCENARIOS = {
    "desk": {
        "domain": ["electronics", "office", "home_living"],
        "instructions": (
            "The photo shows a desk or workstation setup the user wants to recreate or "
            "upgrade. Break it into the separate products someone would buy: monitor, "
            "keyboard, mouse, headset, desk, chair, desk mat, lighting (LED strips, lamp), "
            "speakers, webcam, monitor arm, cable management. Only list items visible in "
            "the photo or clearly implied by the user's notes. Usually required: monitor, "
            "keyboard, mouse. Usually optional: lighting, desk mat, decor, accessories. "
            "Do not list the PC itself unless the user asks for it."
        ),
    },
    "renter": {
        "domain": ["home_living", "tools"],
        "instructions": (
            "The photo shows a rental room or apartment the user wants to furnish or "
            "improve without permanent changes (no drilling, no painting, deposit-safe). "
            "Break it into separate products: lighting, rugs, curtains, storage, shelving, "
            "removable wall hooks or decor, bedding, mirrors, plants, small furniture. "
            "Prefer renter-friendly versions (adhesive, freestanding, plug-in). Usually "
            "required: what the room visibly lacks for daily use (lighting, storage). "
            "Usually optional: decor."
        ),
    },
    "outfit": {
        "domain": ["fashion"],
        "instructions": "Handled by search_products_by_image; no LLM call.",
    },
}

_SYSTEM = (
    "You plan a shopping list from a photo. Reply with ONLY a JSON array, no prose, no "
    "code fences. Each element: "
    '{"slot": snake_case item name, unique, "required": true|false, '
    '"weight": integer 1-5 (5 = matters most to the look), '
    '"query": English product description for a text search - colour, style, material, '
    'type, e.g. "black RGB mechanical gaming keyboard", no price or brand guesses, '
    '"domain": one or more of ' + ", ".join(sorted(DOMAINS)) + "}. "
    f"At most {MAX_SLOTS} elements."
)


# ── AISUM adapter ────────────────────────────────────────────────────────
# TODO: 换成 Yining 的 search.py
# A thin layer over src/mcp_client.py so the rest of this module never touches MCP.
# NOTE: the search.py interface was not available when this was written (no CLAUDE.md
# in the repo), so these signatures mirror the MCP tool arguments. Check them against
# search.py when swapping.

_mcp: McpClient | None = None
_session_lock = asyncio.Lock()


def use_client(client: McpClient) -> None:
    """Share an existing client (e.g. FastAPI's app.state.mcp) instead of opening one."""
    global _mcp
    _mcp = client


async def _client() -> McpClient:
    global _mcp
    async with _session_lock:
        if _mcp is None:
            _mcp = McpClient()
        # Open the MCP session once, before concurrent calls race to each open their own.
        await _mcp._ensure_session()
    return _mcp


async def search_text(query: str, *, domain: str | list[str] | None = None,
                      min_price: float | None = None, max_price: float | None = None,
                      top_k: int = 10) -> dict:
    """search_products_by_text -> {"results": [...], "applied_filters", "ignored_filters"}.

    RATE_LIMITED: retried once after a short `retry_after_s`; otherwise raised (McpError).
    """
    args = {"query": query, "domain": domain, "min_price": min_price,
            "max_price": max_price, "top_k": top_k}
    mcp = await _client()
    return await mcp.call_waiting_once("search_products_by_text",
                                       {k: v for k, v in args.items() if v is not None})


async def search_image(image_url: str, *, min_price: float | None = None,
                       max_price: float | None = None, top_k: int = 15) -> dict:
    """search_products_by_image -> {"detections": [...], "note"?, "ignored_filters", ...}."""
    args = {"image_url": image_url, "min_price": min_price, "max_price": max_price,
            "top_k": top_k}
    mcp = await _client()
    return await mcp.call_waiting_once("search_products_by_image",
                                       {k: v for k, v in args.items() if v is not None})


async def aclose() -> None:
    global _mcp
    if _mcp is not None:
        await _mcp.aclose()
        _mcp = None


# ── plan_slots ───────────────────────────────────────────────────────────

class PlanError(RuntimeError):
    pass


async def plan_slots(image_url: str, scenario: str, notes: str = "") -> list[dict]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}")
    if scenario == "outfit":
        return await _outfit_slots(image_url)
    return await _llm_slots(image_url, scenario, notes)


async def _llm_slots(image_url: str, scenario: str, notes: str) -> list[dict]:
    spec = SCENARIOS[scenario]
    text = spec["instructions"]
    if notes:
        text += f"\n\nThe user's notes: {notes}"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    last_error = None
    for _attempt in range(2):  # one retry when the reply does not parse
        reply = await llm_client.chat(messages)
        content = reply["message"].get("content") or ""
        try:
            return _parse_slots(content, spec["domain"])
        except ValueError as e:
            last_error = e
            log.warning("plan_slots: unusable LLM reply (%s), retrying once", e)
            messages += [reply["message"],
                         {"role": "user", "content": f"That reply was not usable: {e}. "
                          "Reply again with ONLY the JSON array."}]
    raise PlanError(f"The model did not return a usable slot list: {last_error}")


def _parse_slots(content: str, default_domain: list[str]) -> list[dict]:
    """Strict-enough JSON parsing: tolerates code fences and a wrapping object,
    normalises every field, and raises ValueError when nothing usable is left."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("no JSON array in the reply")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON ({e})") from None
    if isinstance(data, dict):  # {"slots": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        raise ValueError("the JSON is not an array")

    slots, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", str(item.get("slot", "")).lower()).strip("_")
        query = str(item.get("query") or "").strip()
        if not name or not query or name in seen:
            continue
        seen.add(name)
        try:
            weight = int(round(float(item.get("weight", 3))))
        except (TypeError, ValueError):
            weight = 3
        domain = item.get("domain")
        domain = [domain] if isinstance(domain, str) else domain or []
        domain = [d for d in domain if d in DOMAINS] or list(default_domain)
        slots.append({"slot": name, "required": item.get("required") is True,
                      "weight": min(5, max(1, weight)), "query": query,
                      "domain": domain})
        if len(slots) == MAX_SLOTS:
            break
    if not slots:
        raise ValueError("the array has no valid slots")
    return slots


async def _outfit_slots(image_url: str) -> list[dict]:
    res = await search_image(image_url)
    detections = res.get("detections") or []
    if not detections:
        raise PlanError(res.get("note") or "No fashion items were detected in the photo.")
    slots, counts = [], {}
    for d in detections:
        base = re.sub(r"[^a-z0-9]+", "_", (d.get("item") or "item").lower()).strip("_")
        counts[base] = counts.get(base, 0) + 1
        name = base if counts[base] == 1 else f"{base}_{counts[base]}"  # two shoes
        slots.append({"slot": name, "required": True, "weight": 3,
                      "query": d.get("item_detail") or d.get("item") or name,
                      "domain": list(SCENARIOS["outfit"]["domain"]),
                      "candidates": d.get("products") or []})
    return slots


# ── find_candidates ──────────────────────────────────────────────────────

# Mojibake: UTF-8 decoded as cp1252/latin-1 ("15â€ 16:10"), or a replacement char.
_GARBLED = re.compile("â€|â„|Ã.|Â|�")
_NEAR_DUP = 0.9


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def clean(products: list[dict]) -> list[dict]:
    """Keep order; drop repeated p_keys, garbled names, and names nearly identical to
    one already kept (the first, i.e. best-ranked, wins)."""
    out, keys, names = [], set(), []
    for p in products:
        key, name = p.get("p_key"), p.get("product_name") or ""
        if (key and key in keys) or _GARBLED.search(name):
            continue
        n = _norm(name)
        if any(difflib.SequenceMatcher(None, n, m).ratio() >= _NEAR_DUP for m in names):
            continue
        keys.add(key)
        names.append(n)
        out.append(p)
    return out


async def find_candidates(slots: list[dict], budget: float,
                          report: dict | None = None) -> dict[str, list[dict]]:
    """Text-search every slot at once; return cleaned candidates per slot.

    A slot whose search fails maps to [] (the optimizer then reports no_candidates).
    Pass a dict as `report` to learn why: report[slot] = {"error": "CODE: message"} or
    {"ignored_filters": [...]} when the server did not apply a filter.
    """
    report = report if report is not None else {}
    gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    async def one(slot: dict) -> list[dict]:
        if slot.get("candidates"):
            return slot["candidates"]
        async with gate:
            res = await search_text(slot["query"], domain=slot.get("domain") or None,
                                    max_price=budget, top_k=TEXT_TOP_K)
        if res.get("ignored_filters"):
            report.setdefault(slot["slot"], {})["ignored_filters"] = res["ignored_filters"]
        return res.get("results") or []

    results = await asyncio.gather(*(one(s) for s in slots), return_exceptions=True)
    out = {}
    for slot, res in zip(slots, results):
        if isinstance(res, BaseException):
            if not isinstance(res, McpError):
                raise res
            report.setdefault(slot["slot"], {})["error"] = f"{res.code}: {res}"
            log.warning("find_candidates: %s failed: %s", slot["slot"], res)
            res = []
        out[slot["slot"]] = clean(res)
    return out
