"""Turn a photo into shopping slots, then find candidate products for each slot.

    plan_slots(image_url, scenario, notes, budget)  -> [slot, ...]
    find_candidates(slots, budget, report)          -> {slot: [product, ...]}

A slot is {"slot", "required", "weight" (1-5), "query", "domain"}, the shape
optimizer.optimize() takes. Outfit slots also carry "candidates" (the products image
search returned for that item), "source", "image_url", "item" and "boxes", so
find_candidates can skip searching again and re-check weak slots against the photo.

desk / renter: one DeepSeek call with the image decides the slots.
outfit:        no LLM to plan. search_products_by_image splits the photo into items;
               DeepSeek is called only to rescue a slot whose candidates look weak.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re

import httpx

from .. import llm_client
from ..llm_client import LlmError
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
                      gender: str | None = None, top_k: int = 10) -> dict:
    """search_products_by_text -> {"results": [...], "applied_filters", "ignored_filters"}.

    RATE_LIMITED: retried once after a short `retry_after_s`; otherwise raised (McpError).
    """
    args = {"query": query, "domain": domain, "min_price": min_price,
            "max_price": max_price, "gender": gender, "top_k": top_k}
    mcp = await _client()
    return await mcp.call_waiting_once("search_products_by_text",
                                       {k: v for k, v in args.items() if v is not None})


async def search_image(image_url: str, *, min_price: float | None = None,
                       max_price: float | None = None, gender: str | None = None,
                       top_k: int = 15) -> dict:
    """search_products_by_image -> {"detections": [...], "note"?, "ignored_filters", ...}."""
    args = {"image_url": image_url, "min_price": min_price, "max_price": max_price,
            "gender": gender, "top_k": top_k}
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


GENDERS = {"F", "M", "KIDS", "UNISEX"}


async def plan_slots(image_url: str, scenario: str, notes: str = "",
                     budget: float | None = None, gender: str | None = None) -> list[dict]:
    """`budget` caps image-search prices (outfit); desk / renter ignore it here and
    apply it in find_candidates.

    `gender` (F / M / KIDS / UNISEX) only when the USER chose it — never inferred: many
    products carry no gender, so a guessed filter silently removes correct results.
    Used by outfit only; it is passed to image search and kept on each slot so the
    follow-up text searches use it too."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}")
    if gender is not None and gender not in GENDERS:
        raise ValueError(f"gender must be one of {', '.join(sorted(GENDERS))} or None")
    if scenario == "outfit":
        return await _outfit_slots(image_url, budget, gender)
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


async def _outfit_slots(image_url: str, budget: float | None = None,
                        gender: str | None = None) -> list[dict]:
    res = await search_image(image_url, max_price=budget, gender=gender)
    detections = res.get("detections") or []
    if not detections:
        raise PlanError(res.get("note") or "No fashion items were detected in the photo.")

    # A left and a right shoe come back as two detections with the same item and
    # item_detail. They are one thing to buy: merge them, products in detection order.
    merged: dict[tuple[str, str], dict] = {}
    for d in detections:
        item = (d.get("item") or "item").strip().lower()
        detail = (d.get("item_detail") or item).strip().lower()
        group = merged.setdefault((item, detail), {"item": item, "detail": detail,
                                                   "boxes": [], "products": []})
        if d.get("box"):
            group["boxes"].append(d["box"])
        group["products"] += d.get("products") or []

    slots, counts = [], {}
    for g in merged.values():
        base = re.sub(r"[^a-z0-9]+", "_", g["item"]).strip("_") or "item"
        counts[base] = counts.get(base, 0) + 1
        # Same item, different detail (a black and a brown shoe) stay separate slots.
        name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        slot = {"slot": name, "required": True, "weight": 3, "query": g["detail"],
                "domain": list(SCENARIOS["outfit"]["domain"]),
                "candidates": g["products"], "source": "image_search",
                "image_url": image_url, "item": g["item"], "boxes": g["boxes"]}
        if gender:
            slot["gender"] = gender
        if res.get("ignored_filters"):
            slot["ignored_filters"] = res["ignored_filters"]
        slots.append(slot)
    return slots


# ── find_candidates ──────────────────────────────────────────────────────

# Mojibake: UTF-8 bytes shown as cp1252 ("15â€ 16:10") or as latin-1, where the
# continuation bytes become invisible C1 control characters ("Multiâ\x80\x91Pocket").
_GARBLED = re.compile("â€|â„|Ã.|Â|â[\x80-\x9f]|�")
_NEAR_DUP = 0.9

# A run that starts like a UTF-8 lead byte and continues like continuation bytes, in
# either the cp1252 or the latin-1 reading.
_CP1252_HIGH = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ"
_RUN = re.compile("[Â-ô][\u0080-¿" + _CP1252_HIGH + "]+")

# Sequences that lost a byte and cannot be decoded, longest first.
_MOJIBAKE = [("â€\x9d", "”"), ("Â ", " "), ("Â", ""), ("â€", "”")]


def _as_byte(ch: str) -> bytes:
    try:
        return ch.encode("cp1252")
    except UnicodeEncodeError:
        return ch.encode("latin-1")  # C1 controls \x80-\x9f, which cp1252 lacks


def _decode_run(m: re.Match) -> str:
    try:
        return b"".join(_as_byte(c) for c in m.group(0)).decode("utf-8")
    except UnicodeError:
        return m.group(0)


def fix_mojibake(name: str) -> str | None:
    """The name with mojibake repaired, the name itself when it has none, or None when
    it cannot be repaired (the caller drops that product)."""
    if not _GARBLED.search(name):
        return name
    fixed = _RUN.sub(_decode_run, name)
    for bad, good in _MOJIBAKE:
        fixed = fixed.replace(bad, good)
    fixed = " ".join(fixed.split())
    return None if _GARBLED.search(fixed) else fixed


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def clean(products: list[dict]) -> list[dict]:
    """Keep order; drop repeated p_keys and names nearly identical to one already kept
    (the first, i.e. best-ranked, wins). Mojibake names are repaired (the product is
    copied, never edited in place); one that cannot be repaired is dropped."""
    out, keys, names = [], set(), []
    for p in products:
        key, name = p.get("p_key"), p.get("product_name") or ""
        if key and key in keys:
            continue
        fixed = fix_mojibake(name)
        if fixed is None:
            continue
        if fixed != name:
            p = {**p, "product_name": fixed}
            name = fixed
        n = _norm(name)
        if any(difflib.SequenceMatcher(None, n, m).ratio() >= _NEAR_DUP for m in names):
            continue
        keys.add(key)
        names.append(n)
        out.append(p)
    return out


# Self-correction: when a slot's candidates from a tool look weak, DeepSeek re-reads
# the photo for a sharper description and a text search adds to the candidates.
# Thresholds are per tool because score scales differ between tools.
MIN_CANDIDATES = 5
MIN_TOP_SCORE = {"image_search": 0.45}

_REFINE_PROMPT = (
    "Look at the {detail} ({item}) worn in this photo{where}. Write ONE English "
    "product search query that describes exactly that item: colour, pattern, material, "
    "cut or style, and the item type, in 4-12 words. Example: \"cream houndstooth "
    "oversized wool blanket scarf\". No brand guesses, no price. Reply with only the query."
)


def _weak(slot: dict, products: list[dict]) -> list[str]:
    """Why this slot's candidates need help: few_candidates and/or low_score."""
    reasons = []
    if len(products) < MIN_CANDIDATES:
        reasons.append("few_candidates")
    cutoff = MIN_TOP_SCORE.get(slot.get("source"))
    top = products[0].get("score") if products else None
    if cutoff is not None and (not isinstance(top, (int, float)) or top < cutoff):
        reasons.append("low_score")
    return reasons


async def _refine_query(slot: dict) -> str:
    """One DeepSeek call on the original image -> a precise text-search query."""
    boxes = slot.get("boxes") or []
    where = (f", inside the normalized box {boxes[0]} ([x1, y1, x2, y2], origin top-left)"
             if boxes else "")
    prompt = _REFINE_PROMPT.format(detail=slot.get("query") or slot["slot"],
                                   item=slot.get("item") or slot["slot"], where=where)
    reply = await llm_client.chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": slot["image_url"]}},
    ]}])
    content = reply["message"].get("content") or ""
    line = next((ln for ln in content.splitlines() if ln.strip()), "")
    query = line.strip().strip("`\"' ").strip()[:200]
    if not query:
        raise LlmError("the model returned an empty description")
    return query


async def find_candidates(slots: list[dict], budget: float,
                          report: dict | None = None) -> dict[str, list[dict]]:
    """Search every slot at once; return cleaned candidates per slot.

    Slots that already carry candidates (outfit) are not searched again. If those
    candidates look weak (fewer than MIN_CANDIDATES after cleaning, or the top score
    under the tool's MIN_TOP_SCORE), DeepSeek describes the item from the photo and a
    fashion text search adds to them. With a low top score the text results go first,
    since the image results' order is not trusted; otherwise they go after.

    A slot whose search fails maps to [] (the optimizer then reports no_candidates).
    Pass a dict as `report` to learn what happened per slot:
      error            "CODE: message" — the search failed
      ignored_filters  filters the server did not apply
      refined          {"reasons", "query", "added"} — self-correction ran
      refine_error     self-correction was needed but failed; candidates unchanged
    """
    report = report if report is not None else {}
    gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    def note(slot: dict, key: str, value) -> None:
        report.setdefault(slot["slot"], {})[key] = value

    async def text(query: str, domain, gender=None) -> dict:
        async with gate:
            return await search_text(query, domain=domain or None, max_price=budget,
                                     gender=gender, top_k=TEXT_TOP_K)

    async def one(slot: dict) -> list[dict]:
        if slot.get("ignored_filters"):
            note(slot, "ignored_filters", slot["ignored_filters"])
        if not slot.get("candidates") and not slot.get("image_url"):
            res = await text(slot["query"], slot.get("domain"), slot.get("gender"))
            if res.get("ignored_filters"):
                note(slot, "ignored_filters", res["ignored_filters"])
            return clean(res.get("results") or [])

        products = clean(slot.get("candidates") or [])
        reasons = _weak(slot, products)
        if not reasons or not slot.get("image_url"):
            return products
        try:
            query = await _refine_query(slot)
            res = await text(query, ["fashion"], slot.get("gender"))
        except (LlmError, httpx.HTTPError, McpError) as e:
            note(slot, "refine_error", f"{type(e).__name__}: {e}")
            log.warning("find_candidates: refining %s failed: %s", slot["slot"], e)
            return products
        extra = res.get("results") or []
        merged = clean(extra + products if "low_score" in reasons else products + extra)
        note(slot, "refined", {"reasons": reasons, "query": query,
                               "added": len(merged) - len(products)})
        return merged

    results = await asyncio.gather(*(one(s) for s in slots), return_exceptions=True)
    out = {}
    for slot, res in zip(slots, results):
        if isinstance(res, BaseException):
            if not isinstance(res, McpError):
                raise res
            note(slot, "error", f"{res.code}: {res}")
            log.warning("find_candidates: %s failed: %s", slot["slot"], res)
            res = []
        out[slot["slot"]] = res
    return out


# ── category check ───────────────────────────────────────────────────────

_CATEGORY_PROMPT = (
    "You check shopping search results for ONE slot of a shopping list. Decide which "
    "candidates are NOT the kind of product the slot asks for, judging only from the "
    "product name. Reject a candidate only when its name clearly shows a different "
    "product type: a mouse pad for 'desk', replacement speaker parts for 'speaker', "
    "keycaps for 'keyboard'. Do not reject for colour, style, price or brand, and keep "
    "anything you are unsure about.\n"
    'Reply with ONLY a JSON object: {"reject": [ids]}, e.g. {"reject": [3, 7]} or '
    '{"reject": []}.\n\nSLOT:\n'
)
_CATEGORY_NAME_CHARS = 90
MAX_CONCURRENT_CHECKS = 4


async def check_categories(slots: list[dict], candidates: dict[str, list[dict]],
                           report: dict | None = None) -> dict[str, list[dict]]:
    """Drop candidates that are the wrong product type: one DeepSeek call per slot,
    at most MAX_CONCURRENT_CHECKS at once.

    Returns new candidate lists; order is kept. A slot whose call fails, or whose reply
    does not parse, keeps its candidates (report[slot]["category_error"]). Rejections
    are listed in report[slot]["category_rejected"] (product names), and
    report["_category_check"] = {"rejected": n, "failed": [slots]}.
    """
    report = report if report is not None else {}
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def one(slot: dict) -> set[int]:
        items = candidates.get(slot["slot"]) or []
        listing = {"slot": slot["slot"], "looking_for": slot.get("query") or slot["slot"],
                   "candidates": [{"id": i, "name": (p.get("product_name") or "")[:_CATEGORY_NAME_CHARS]}
                                  for i, p in enumerate(items)]}
        async with gate:
            reply = await llm_client.chat([{"role": "user", "content":
                                            _CATEGORY_PROMPT + json.dumps(listing, ensure_ascii=False)}])
        return _parse_rejections(reply["message"].get("content") or "", len(items))

    todo = [s for s in slots if candidates.get(s["slot"])]
    results = await asyncio.gather(*(one(s) for s in todo), return_exceptions=True)
    out, total, failed = dict(candidates), 0, []
    for slot, res in zip(todo, results):
        name = slot["slot"]
        if isinstance(res, BaseException):
            if not isinstance(res, (LlmError, httpx.HTTPError, ValueError)):
                raise res
            failed.append(name)
            report.setdefault(name, {})["category_error"] = f"{type(res).__name__}: {res}"
            log.warning("check_categories: %s kept as is: %s", name, res)
            continue
        if res:
            items = candidates[name]
            out[name] = [p for i, p in enumerate(items) if i not in res]
            report.setdefault(name, {})["category_rejected"] = [
                items[i].get("product_name") for i in sorted(res)]
            total += len(res)
    report["_category_check"] = {"rejected": total, "failed": failed}
    return out


def _parse_rejections(content: str, n: int) -> set[int]:
    """Ids to reject from {"reject": [ids]}; ids outside 0..n-1 or not ints are ignored."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON ({e})") from None
    ids = data.get("reject") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        raise ValueError('the reply has no "reject" list')
    return {i for i in ids if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n}
