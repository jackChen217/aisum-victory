"""Turn a photo into shopping slots, then find candidate products for each slot.

    plan_slots(image_url, scenario, notes, budget)  -> [slot, ...]
    find_candidates(slots, budget, report)          -> {slot: [product, ...]}

A slot is {"slot", "required", "weight" (1-5), "query", "domain"}, the shape
optimizer.optimize() takes. Outfit slots also carry "candidates" (the products image
search returned for that item), "source", "image_url", "item" and "boxes", so
find_candidates can skip searching again and re-check weak slots against the photo.

desk / room: one DeepSeek call with the image decides the slots.
outfit:        image search splits the photo into items. The default baseline keeps
               native candidate order with no extra model calls or image downloads.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re

import io

import httpx

from .. import image_proxy, llm_client
from . import matching
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
    "room": {
        "domain": ["home_living", "tools"],
        "instructions": (
            "The photo shows a room the user wants to recreate or furnish. Break it into "
            "the separate products someone would buy: furniture (sofa, bed, table, "
            "chairs, shelving, storage), lighting, rugs, curtains, bedding, mirrors, wall "
            "decor, plants, small accessories. Only list items visible in the photo or "
            "clearly implied by the user's notes. If the notes say it is a rental, prefer "
            "versions that need no drilling or painting (adhesive, freestanding, "
            "plug-in). Usually required: the main furniture and lighting. Usually "
            "optional: decor and accessories."
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
                       category: str | None = None, top_k: int = 15) -> dict:
    """search_products_by_image -> {"detections": [...], "note"?, "ignored_filters", ...}.
    An unsupported `category` comes back in ignored_filters, not as an error."""
    args = {"image_url": image_url, "min_price": min_price, "max_price": max_price,
            "gender": gender, "category": category, "top_k": top_k}
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
    """`budget` caps image-search prices (outfit); desk / room ignore it here and
    apply it in find_candidates.

    `gender` (F / M / KIDS / UNISEX) only when the USER chose it — never inferred: many
    products carry no gender, so a guessed filter silently removes correct results.
    Used by outfit only; it is passed to image search and kept on each slot so the
    follow-up text searches use it too.

    The user always picks the scenario; nothing is auto-detected. (The /api/plan
    outfit path is outfit.candidates; this outfit branch serves the comparison page.)"""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}")
    if gender is not None and gender not in GENDERS:
        raise ValueError(f"gender must be one of {', '.join(sorted(GENDERS))} or None")
    if scenario == "outfit":
        res = await search_image(image_url, max_price=budget, gender=gender)
        if res.get("detections"):
            return _outfit_slots(res, image_url, gender)
        raise PlanError(res.get("note") or "No fashion items were detected in the photo.")
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


def _outfit_slots(res: dict, image_url: str, gender: str | None = None) -> list[dict]:
    """Image-search detections -> slots. Each slot keeps its detections (numbered in
    the order the tool returned them) so review_outfit can refer to them."""
    detections = res.get("detections") or []

    # A left and a right shoe come back as two detections with the same item and
    # item_detail. They are one thing to buy: merge them, products in detection order.
    merged: dict[tuple[str, str], dict] = {}
    for i, d in enumerate(detections):
        item = (d.get("item") or "item").strip().lower()
        detail = (d.get("item_detail") or item).strip().lower()
        group = merged.setdefault((item, detail), {"item": item, "detail": detail,
                                                   "boxes": [], "products": [],
                                                   "detections": []})
        if d.get("box"):
            group["boxes"].append(d["box"])
        group["products"] += d.get("products") or []
        group["detections"].append({"id": i, "item": item, "item_detail": detail,
                                    "box": d.get("box")})

    slots, counts = [], {}
    for g in merged.values():
        base = re.sub(r"[^a-z0-9]+", "_", g["item"]).strip("_") or "item"
        counts[base] = counts.get(base, 0) + 1
        # Same item, different detail (a black and a brown shoe) stay separate slots.
        name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        slot = {"slot": name, "required": True, "weight": 3, "query": g["detail"],
                "domain": list(SCENARIOS["outfit"]["domain"]),
                "candidates": g["products"], "source": "image_search",
                "image_url": image_url, "item": g["item"], "boxes": g["boxes"],
                "detections": g["detections"]}
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


def clean(products: list[dict], *, near_duplicates: bool = True) -> list[dict]:
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
        if near_duplicates and any(
                difflib.SequenceMatcher(None, n, m).ratio() >= _NEAR_DUP for m in names):
            continue
        keys.add(key)
        names.append(n)
        out.append(p)
    return out


# ── outfit review ────────────────────────────────────────────────────────
# One DeepSeek call that looks at the photo next to what image search detected, and
# returns which detections are right, sharper descriptions for the ones that are vague
# or wrong, and up to MAX_MISSING clearly visible items the detector missed.

MAX_MISSING = 3
LAYERS = {"headwear", "accessory", "inner", "mid", "outer", "top", "bottom", "dress",
          "footwear", "socks", "bag"}

_REVIEW_PROMPT = (
    "You check an automatic clothing detector against the photo. DETECTIONS below are "
    "what it found, numbered; box is [x1, y1, x2, y2], normalized, origin top-left.\n"
    "We only shop for what the main person in the photo is WEARING or HOLDING. Things "
    "hanging on a rack or hook, lying around, worn by someone else, or seen in a mirror "
    "are background, not part of the outfit.\n"
    "Reply with ONLY this JSON, no prose:\n"
    '{"keep": [ids that are correct as described],\n'
    ' "correct": [{"id": id, "description": "..."}],\n'
    ' "drop": [ids that are not worn or held by the person],\n'
    ' "missing": [{"slot": "snake_case_item_type", "layer": "...", "description": "..."}]}\n'
    "Rules:\n"
    "- Every id goes in exactly one of keep, correct or drop. Correct an id when its item "
    "type is wrong (a skirt detected as shorts) or its description misses what a shopper "
    "would search for. Drop an id that is background (a hat on a rack, a coat on a "
    "hanger) or not clothing.\n"
    "- A description is an English shopping query for that one item: colour, pattern "
    "(for stripes: direction and width, e.g. thin vertical stripes), material if clearly "
    "visible, and the specific style name (duffle coat with toggle buttons, high-top "
    "sneakers, leather mini skirt). 4-14 words. No brands, no prices.\n"
    "- missing: clothing, shoes, bags or accessories the person is clearly wearing or "
    "holding that no detection covers (a layer under a jacket, socks, a cap, a belt, a "
    "bag in hand). Not cameras, phones or other gadgets, not background items. Only what "
    "you can clearly see; do not guess hidden items; do not repeat a detected item. "
    "At most "
    f"{MAX_MISSING}. layer is one of: " + ", ".join(sorted(LAYERS)) + ".\n"
    "- If everything is right and nothing is missing, keep all ids and leave the other "
    "lists empty.\n\nDETECTIONS:\n"
)


def _review_detections(slots: list[dict]) -> list[dict]:
    return sorted((d for s in slots for d in s.get("detections") or []),
                  key=lambda d: d["id"])


def _parse_review(content: str, ids: set[int]) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON ({e})") from None
    if not isinstance(data, dict):
        raise ValueError("the JSON is not an object")

    def desc(x) -> str:
        return " ".join(str(x or "").split())[:200]

    keep = {i for i in data.get("keep") or [] if isinstance(i, int) and i in ids}
    drop = {i for i in data.get("drop") or [] if isinstance(i, int) and i in ids} - keep
    correct = {}
    for c in data.get("correct") or []:
        if isinstance(c, dict) and isinstance(c.get("id"), int) and c["id"] in ids:
            if d := desc(c.get("description")):
                correct.setdefault(c["id"], d)
    missing = []
    for m in data.get("missing") or []:
        if not isinstance(m, dict):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", str(m.get("slot", "")).lower()).strip("_")
        if name and (d := desc(m.get("description"))):
            layer = str(m.get("layer", "")).lower()
            missing.append({"slot": name, "layer": layer if layer in LAYERS else None,
                            "description": d})
    drop -= set(correct)
    return {"keep": keep, "correct": correct, "drop": drop, "missing": missing[:MAX_MISSING]}


def _same_item(a: str, b: str) -> bool:
    """"sock" vs "socks", "shoe" vs "shoe_2": the same thing to buy."""
    a, b = re.sub(r"_\d+$", "", a).rstrip("s"), re.sub(r"_\d+$", "", b).rstrip("s")
    return a == b


async def review_outfit(image_url: str, slots: list[dict],
                        report: dict | None = None) -> list[dict]:
    """One DeepSeek call: confirm, correct and complete image-search detections.

    Corrected slots get "refine_query" (and the sharper text as "query"; the
    detector's words stay in "detected_as"); find_candidates text-searches it and merges
    the results in. Missing items become optional slots with no candidates, which
    find_candidates text-searches like any other. Any failure returns `slots`
    unchanged and the plan goes on as before. report["_review"] records
    {"corrected", "added", "removed" (background items dropped), "unconfirmed"} or
    {"error": ...}.
    """
    report = report if report is not None else {}
    detections = _review_detections(slots)
    if not detections:
        return slots
    listing = [{"id": d["id"], "item": d["item"], "item_detail": d["item_detail"],
                "box": d["box"]} for d in detections]
    try:
        reply = await llm_client.chat([{"role": "user", "content": [
            {"type": "text", "text": _REVIEW_PROMPT + json.dumps(listing)},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}])
        review = _parse_review(reply["message"].get("content") or "",
                               {d["id"] for d in detections})
    except (LlmError, httpx.HTTPError, ValueError) as e:
        report["_review"] = {"error": f"{type(e).__name__}: {e}"}
        log.warning("review_outfit failed, using the detections as they are: %s", e)
        return slots

    out, corrected, added, unconfirmed, removed = [], [], [], [], []
    template = slots[0]
    for s in slots:
        s = dict(s)
        ids = [d["id"] for d in s.get("detections") or []]
        if ids and all(i in review["drop"] for i in ids):
            removed.append({"slot": s["slot"], "was": s["query"]})  # background, not worn
            continue
        fix = next((review["correct"][i] for i in ids if i in review["correct"]), None)
        if fix and fix.lower() != s["query"].lower():
            corrected.append({"slot": s["slot"], "from": s["query"], "to": fix})
            s.update(detected_as=s["query"], query=fix, refine_query=fix)
        elif ids and not any(i in review["keep"] or i in review["correct"] for i in ids):
            unconfirmed.append(s["slot"])  # neither kept nor corrected: noted, not dropped
        out.append(s)
    for m in review["missing"]:
        if any(_same_item(m["slot"], s["slot"]) for s in out):
            continue
        slot = {"slot": m["slot"], "required": False, "weight": 2,
                "query": m["description"], "domain": ["fashion"], "source": "review",
                "item": m["slot"].replace("_", " "), "layer": m["layer"]}
        if template.get("gender"):
            slot["gender"] = template["gender"]
        out.append(slot)
        added.append({"slot": m["slot"], "layer": m["layer"], "query": m["description"]})
    report["_review"] = {"corrected": corrected, "added": added, "removed": removed,
                         "unconfirmed": unconfirmed}
    return out


async def find_candidates(slots: list[dict], budget: float,
                          report: dict | None = None) -> dict[str, list[dict]]:
    """Search every slot at once; return cleaned candidates per slot.

    - A slot without candidates (desk, room, or an item review_outfit added) is
      text-searched with its query, domain and gender.
    - A slot with image-search candidates keeps them; if review_outfit corrected it
      ("refine_query"), a fashion text search on the sharper description is merged in,
      its results first — the correction means the detector's reading was off.

    A slot whose search fails maps to [] (the optimizer then reports no_candidates),
    or to its image candidates when only the extra search failed. Pass a dict as
    `report` to learn what happened per slot:
      error            "CODE: message" — the search failed
      ignored_filters  filters the server did not apply
      corrected_search {"query", "added"} — the corrected description's search
    """
    report = report if report is not None else {}
    gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    def note(slot: dict, key: str, value) -> None:
        report.setdefault(slot["slot"], {})[key] = value

    async def text(query: str, domain, gender=None) -> dict:
        async with gate:
            res = await search_text(query, domain=domain or None, max_price=budget,
                                    gender=gender, top_k=TEXT_TOP_K)
        return res

    async def one(slot: dict) -> list[dict]:
        if slot.get("ignored_filters"):
            note(slot, "ignored_filters", slot["ignored_filters"])
        if not slot.get("candidates") and not slot.get("refine_query"):
            res = await text(slot["query"], slot.get("domain"), slot.get("gender"))
            if res.get("ignored_filters"):
                note(slot, "ignored_filters", res["ignored_filters"])
            return clean(res.get("results") or [])

        products = clean(slot.get("candidates") or [])
        if not slot.get("refine_query"):
            return products
        try:
            res = await text(slot["refine_query"], ["fashion"], slot.get("gender"))
        except McpError as e:
            note(slot, "error", f"{e.code}: {e} (corrected search; image results kept)")
            return products
        merged = clean((res.get("results") or []) + products)
        note(slot, "corrected_search", {"query": slot["refine_query"],
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
CATEGORY_CHECK_TOP_N = 10  # only these are checked — and kept; the rest rank too low


async def check_categories(slots: list[dict], candidates: dict[str, list[dict]],
                           report: dict | None = None) -> dict[str, list[dict]]:
    """Desk / room: drop candidates that are the wrong product type — one DeepSeek
    call per slot, at most MAX_CONCURRENT_CHECKS at once, over each slot's first
    CATEGORY_CHECK_TOP_N candidates (the rest are cut). Outfits use the keyword rules
    in matching.category_filter instead, with no LLM call.

    Returns new candidate lists; order is kept. A slot whose call fails, or whose reply
    does not parse, keeps its candidates (report[slot]["category_error"]). Rejections
    are listed in report[slot]["category_rejected"] (product names), and
    report["_category_check"] = {"rejected": n, "failed": [slots]}.
    """
    report = report if report is not None else {}
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def one(slot: dict) -> set[int]:
        items = (candidates.get(slot["slot"]) or [])[:CATEGORY_CHECK_TOP_N]
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
        items = candidates[name][:CATEGORY_CHECK_TOP_N]
        out[name] = [p for i, p in enumerate(items) if i not in res]
        if res:
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


# ── outfit pipeline ──────────────────────────────────────────────────────

async def _photo(image_url: str):
    from PIL import Image
    data, _ = await image_proxy.fetch(image_url)
    return await asyncio.to_thread(lambda: Image.open(io.BytesIO(data)).convert("RGB"))


async def outfit_candidates(image_url: str, slots: list[dict], budget: float,
                            report: dict | None = None, *, enhanced: bool = False
                            ) -> tuple[list[dict], dict[str, list[dict]], dict[str, list[dict]]]:
    """Return (slots, candidates, scores), preserving AIRS order by default.

    enhanced=True enables the previous experimental pipeline described below.

    Review, search, filter and score an outfit. Returns (slots, candidates, scores),
    candidates per slot ordered best first and scores[slot][i] matching them.

    While the review call runs (the slow part), everything that does not depend on it
    runs too: a text search on each detection's own description, the photo download,
    and the colour analysis of the image-search products. After the review:
      corrected slot   text search on the sharper description, first; then image
                       results; the early search is not used (its description was off)
      kept slot        image results, then the early text search
      added slot       text search on the review's description
      dropped slot     removed (background, not worn)
    Then the keyword category rules, then matching.score_slot (rank, colour, text).
    """
    report = report if report is not None else {}
    if not enhanced:
        # Baseline: preserve AIRS candidate order. Near-identical titles can be
        # different colour/pattern variants, so deduplicate only actual product IDs.
        # No second search, review call, or product-image download on this path.
        candidates, scores = {}, {}
        for slot in slots:
            products = clean(slot.get("candidates") or [], near_duplicates=False)
            candidates[slot["slot"]] = products
            scores[slot["slot"]] = [
                {"rank": 1 / (1 + i), "colour": None, "text": None,
                 "quality": 1 / (1 + i)} for i in range(len(products))
            ]
            if slot.get("ignored_filters"):
                report.setdefault(slot["slot"], {})["ignored_filters"] = slot["ignored_filters"]
        report["_matching"] = {"method": "image_rank", "enhanced": False}
        return slots, candidates, scores

    gate = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    async def text(query: str, gender) -> list[dict]:
        async with gate:
            res = await search_text(query, domain=["fashion"], max_price=budget,
                                    gender=gender, top_k=TEXT_TOP_K)
        return res.get("results") or []

    async def safe(coro, slot_name: str, what: str):
        try:
            return await coro
        except McpError as e:
            report.setdefault(slot_name, {})["error"] = f"{e.code}: {e} ({what})"
            return []

    early = {s["slot"]: asyncio.create_task(safe(text(s["query"], s.get("gender")),
                                                 s["slot"], "early text search"))
             for s in slots}
    photo_task = asyncio.create_task(_photo(image_url))
    image_urls = [p.get("img_url") for s in slots
                  for p in clean(s.get("candidates") or [])[:matching.COLOUR_TOP_N]]
    palettes_task = asyncio.create_task(matching.product_palettes(image_urls, image_proxy.fetch))

    pending = [*early.values(), photo_task, palettes_task]
    try:
        reviewed = await review_outfit(image_url, slots, report=report)

        async def gather_slot(s: dict) -> list[dict]:
            image = s.get("candidates") or []
            if s.get("refine_query"):
                first = await safe(text(s["refine_query"], s.get("gender")), s["slot"],
                                   "corrected search")
                merged = clean(first + image)
                report.setdefault(s["slot"], {})["corrected_search"] = {
                    "query": s["refine_query"], "added": len(merged) - len(clean(image))}
                return merged
            if s["slot"] in early:
                return clean(image + await early[s["slot"]])
            return clean(await safe(text(s["query"], s.get("gender")), s["slot"], "added item"))

        lists = await asyncio.gather(*(gather_slot(s) for s in reviewed))
        for t in early.values():
            t.cancel()  # corrected or dropped slots never used theirs

        candidates = {}
        for s, products in zip(reviewed, lists):
            kept, rejected = matching.category_filter(s, products)
            if rejected:
                report.setdefault(s["slot"], {})["category_rejected"] = rejected
            candidates[s["slot"]] = kept
        report["_category_check"] = {"method": "rules",
                                     "rejected": sum(len(v.get("category_rejected", []))
                                                     for k, v in report.items()
                                                     if not k.startswith("_") and isinstance(v, dict))}

        try:
            photo = await photo_task
        except Exception as e:
            photo = None
            report["_colour"] = {"error": f"photo not loaded: {type(e).__name__}: {e}"}
        await palettes_task
        top_urls = [p.get("img_url") for v in candidates.values() for p in v[:matching.COLOUR_TOP_N]]
        palettes = await matching.product_palettes(top_urls, image_proxy.fetch)

        scores, ordered = {}, {}
        for s in reviewed:
            pal = await asyncio.to_thread(matching.item_palette, photo, s)
            scored = matching.score_slot(s, candidates[s["slot"]], pal, palettes)
            ordered[s["slot"]] = [p for p, _ in scored]
            scores[s["slot"]] = [m for _, m in scored]
        report.setdefault("_colour", {}).update(
            product_images=len(set(top_urls)),
            analysed=len({u for u in top_urls if palettes.get(u)}))
        return reviewed, ordered, scores
    finally:
        # A cancelled/timed-out experiment must not leave searches or downloads
        # running behind later batch images. Also consume unused task exceptions.
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
