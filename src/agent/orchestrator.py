"""One plan request end to end, and cheap re-plans from what it found.

    plan(uid, image_url, scenario, budget, owned, notes)
        planner.plan_slots -> planner.find_candidates -> optimizer.optimize
        -> one DeepSeek call for a short explanation. Saves slots + candidates.
    replan(uid, session_id, budget, owned)
        optimize again over the SAVED candidates (no search), diff_plans against the
        previous plan, one DeepSeek call to explain what changed.

Sessions live in a process-local dict keyed by (uid, session_id): a restart forgets
them, and on Cloud Run a re-plan that lands on another instance gets 404 (the page
then asks for a fresh plan). Swap SESSIONS for Firestore if that matters.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

import httpx

from .. import llm_client, uploads
from ..llm_client import LlmError
from . import matching, outfit, planner, sizes
from .optimizer import diff_plans, optimize

log = logging.getLogger(__name__)

SESSIONS: dict[tuple[str, str], dict] = {}
MAX_SESSIONS_PER_USER = 20
# Optional legacy explanations remain available for controlled comparisons.
# The recovery baseline returns a factual summary without another model call.
GENERATE_EXPLANATIONS = False

_PUBLIC_SLOT_KEYS = ("slot", "required", "weight", "query", "domain", "layer", "source",
                     "detected_as", "item", "boxes", "detections", "unmatched")
SCENARIOS = ("outfit", "desk", "room")
OUTFIT_GENDERS = ("F", "M", "UNISEX")

_EXPLAIN_RULES = (
    "Rules: 2-4 short sentences, plain English, no lists, no markdown. Use only facts "
    "in the data: product names, prices, slots, budget. When you describe a product, "
    "use only words that appear in its name in the data, or just the slot name "
    "(\"the shoes\"). NEVER add a colour, material, pattern, size, fit or style that is "
    "not written in that product's name - a slot or photo is not evidence. Never claim "
    "compatibility or stock. Mention any required slot that could not be filled and why."
)

# Descriptive words the model likes to add. If one shows up in an explanation but
# nowhere in the data it was given, the explanation made it up.
_ATTRIBUTE_WORDS = {
    # colours
    "black", "white", "grey", "gray", "beige", "cream", "ivory", "brown", "tan", "camel",
    "navy", "blue", "red", "pink", "purple", "violet", "green", "olive", "khaki",
    "yellow", "orange", "gold", "golden", "silver", "rgb", "neon", "pastel",
    # materials
    "leather", "suede", "wool", "woolen", "cashmere", "cotton", "linen", "silk",
    "satin", "denim", "velvet", "corduroy", "fleece", "knit", "knitted", "mesh",
    "nylon", "polyester", "canvas", "metal", "aluminum", "aluminium", "steel", "wood",
    "wooden", "bamboo", "glass", "plastic", "acrylic", "rubber", "faux", "fur",
    # patterns
    "striped", "stripes", "plaid", "checked", "houndstooth", "floral", "polka",
}


class SessionNotFound(KeyError):
    pass


# ── public API ───────────────────────────────────────────────────────────

ALTERNATIVES = 3


async def plan(uid: str, image_url: str, scenario: str, budget: float,
               owned: list[str] | None = None, notes: str = "",
               gender: str | None = None, desk_width: float | None = None,
               desk_width_unit: str = "in") -> dict:
    """The user picks `scenario` (outfit / desk / room); nothing is auto-detected.
    Outfit needs the user's `gender` (F / M / UNISEX).

    outfit: outfit.candidates (one image search, crop re-search for weak items,
            unmatched items set aside; no LLM, 12 s) -> knapsack.
    desk / room: DeepSeek slots -> text search -> one category check per slot ->
            knapsack; sizes read from product names and compared with `desk_width`.
    The explanation is a template unless GENERATE_EXPLANATIONS is on."""
    if scenario not in SCENARIOS:
        raise planner.PlanError(f"Choose a category: {', '.join(SCENARIOS)}.")
    if scenario == "outfit" and gender not in OUTFIT_GENDERS:
        raise planner.PlanError("Choose Women, Men or Unisex for an outfit.")
    owned = list(owned or [])
    image_url = await uploads.fresh(image_url)  # the page may resend an hour-old link
    started = time.perf_counter()
    report: dict = {}
    unmatched: list[dict] = []
    desk_in = None
    if scenario == "outfit":
        slots, unmatched, candidates, scores, phase = await outfit.candidates(
            image_url, budget, gender, report, started)
        detected = started + phase["image_search"] / 1000
    else:
        gender = None
        desk_in = sizes.to_inches(desk_width, desk_width_unit) if scenario == "desk" else None
        slots = await planner.plan_slots(image_url, scenario, notes, budget=budget)
        detected = time.perf_counter()
        candidates = await planner.find_candidates(slots, budget, report=report)
        candidates = await planner.check_categories(slots, candidates, report=report)
        candidates, scores = _text_ranked(slots, candidates)
    searched = time.perf_counter()
    quality = {k: [m["quality"] for m in v] for k, v in scores.items()}
    result = optimize(slots, candidates, budget, owned, quality)
    optimized = time.perf_counter()
    explanation = _fallback(result, budget, unmatched=unmatched)
    if GENERATE_EXPLANATIONS:
        explanation = await _explain(_plan_prompt(scenario, notes, budget, result), explanation)
    explained = time.perf_counter()

    session_id = uuid.uuid4().hex
    _save(uid, session_id, {
        "scenario": scenario, "image_url": image_url, "notes": notes, "gender": gender,
        "search_budget": budget, "budget": budget, "owned": owned, "pins": {},
        "slots": slots, "unmatched": unmatched, "desk_width_in": desk_in,
        "candidates": candidates, "scores": scores, "plan": result["plan"],
    })
    alternatives = _alternatives(result["plan"], candidates, scores)
    out = {
        "session_id": session_id,
        "scenario": scenario,
        "budget": budget,
        "original_image": image_url,
        "slots": [_public(s) for s in slots + unmatched],
        "plan": result["plan"],
        "match": _matches(result["plan"], candidates, scores),
        "alternatives": alternatives,
        "dropped": result["dropped"] + _unmatched_dropped(unmatched),
        "infeasible": result["infeasible"],
        "total": result["total"],
        "explanation": explanation,
        "report": report,
        "timings_ms": {
            "detect": round((detected - started) * 1000, 2),
            "candidates": round((searched - detected) * 1000, 2),
            "optimize": round((optimized - searched) * 1000, 2),
            "explanation": round((explained - optimized) * 1000, 2),
            "total": round((explained - started) * 1000, 2),
        },
    }
    if scenario != "outfit":
        out.update(_size_fields(result["plan"], alternatives, desk_in))
    return out


async def replan(uid: str, session_id: str, budget: float,
                 owned: list[str] | None = None,
                 pins: dict[str, str] | None = None) -> dict:
    """Re-optimize the saved candidates. `pins` {slot: p_key} must be in the plan:
    that slot is filled with that product and the rest of the budget goes to the
    others. Pins are kept for later re-plans; pass {} to clear them, None to keep."""
    started = time.perf_counter()
    s = SESSIONS.get((uid, session_id))
    if s is None:
        raise SessionNotFound(session_id)
    s["image_url"] = await uploads.fresh(s["image_url"])
    owned = list(owned if owned is not None else s["owned"])
    pins = dict(s.get("pins") or {}) if pins is None else dict(pins)
    unknown = {slot: key for slot, key in pins.items()
               if not any(p.get("p_key") == key for p in s["candidates"].get(slot, []))}
    for slot in unknown:
        pins.pop(slot)

    # Pinning an alternative of an unmatched item brings that item into the plan.
    unmatched = [u for u in s.get("unmatched", []) if u["slot"] not in pins]
    base = s["slots"] + [u for u in s.get("unmatched", []) if u["slot"] in pins]
    slots, candidates, quality = _apply_pins(base, s["candidates"], s["scores"], pins)
    result = optimize(slots, candidates, budget, owned, quality)
    changes = diff_plans(s["plan"], result["plan"])
    explanation = _fallback(result, budget, changes, unmatched=unmatched)
    if GENERATE_EXPLANATIONS:
        explanation = await _explain(_replan_prompt(s, budget, owned, result, changes), explanation)

    s.update(budget=budget, owned=owned, pins=pins, plan=result["plan"],
             touched=time.time())
    alternatives = _alternatives(result["plan"], s["candidates"], s["scores"])
    out = {
        "session_id": session_id,
        "budget": budget,
        "original_image": s["image_url"],
        "plan": result["plan"],
        "match": _matches(result["plan"], s["candidates"], s["scores"]),
        "alternatives": alternatives,
        "pins": pins,
        "dropped": result["dropped"] + _unmatched_dropped(unmatched),
        "infeasible": result["infeasible"],
        "total": result["total"],
        "changes": changes,
        "explanation": explanation,
        "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
    }
    if s["scenario"] != "outfit":
        out.update(_size_fields(result["plan"], alternatives, s.get("desk_width_in")))
    if unknown:
        out["ignored_pins"] = unknown
    if budget > s["search_budget"]:
        # Candidates were searched with max_price = the first budget, so nothing pricier
        # is in them. Say so rather than let a bigger budget look like it changed nothing.
        out["note"] = (f"Candidates were searched up to ${s['search_budget']:g}; "
                       "a new plan is needed to look at pricier items.")
    return out


def _public(slot: dict) -> dict:
    return {k: slot[k] for k in _PUBLIC_SLOT_KEYS if k in slot}


def _unmatched_dropped(unmatched: list[dict]) -> list[dict]:
    return [{"slot": u["slot"], "reason": "unmatched",
             "message": "No similar product found."} for u in unmatched]


def _size_fields(plan: dict, alternatives: dict, desk_in: float | None) -> dict:
    """Desk / room: what each product name says about its size. Marks only; a
    too_large product is not removed from the plan."""
    checked = {slot: sizes.check(p, desk_in) for slot, p in plan.items()}
    for alts in alternatives.values():
        for a in alts:
            a["size"] = sizes.check(a["product"], desk_in)
    return {"sizes": checked, "desk_width_in": desk_in,
            "too_large": [k for k, v in checked.items() if v["status"] == "too_large"],
            "unverified": [k for k, v in checked.items() if v["status"] == "size_unverified"]}


def _text_ranked(slots: list[dict], candidates: dict[str, list[dict]]):
    """Desk / room: rank + text (BM25 and attribute words); no colour."""
    ordered, scores = {}, {}
    for slot in slots:
        scored = matching.score_slot(slot, candidates.get(slot["slot"], []), None, {})
        ordered[slot["slot"]] = [p for p, _ in scored]
        scores[slot["slot"]] = [m for _, m in scored]
    return ordered, scores


def _apply_pins(slots, candidates, scores, pins):
    """A pinned slot becomes required with only the pinned product as candidate."""
    slots = [dict(sl, required=True) if sl["slot"] in pins else sl for sl in slots]
    candidates, quality = dict(candidates), {k: [m["quality"] for m in v]
                                             for k, v in scores.items()}
    for slot, key in pins.items():
        i = next(i for i, p in enumerate(candidates[slot]) if p.get("p_key") == key)
        candidates[slot] = [candidates[slot][i]]
        quality[slot] = [quality[slot][i]]
    return slots, candidates, quality


def _matches(plan: dict, candidates: dict, scores: dict) -> dict:
    """{slot: {"rank", "colour", "text", "quality"}} for the picked products."""
    out = {}
    for slot, product in plan.items():
        for p, m in zip(candidates.get(slot, []), scores.get(slot, [])):
            if p is product or p.get("p_key") == product.get("p_key"):
                out[slot] = m
                break
    return out


def _alternatives(plan: dict, candidates: dict, scores: dict) -> dict:
    """Up to ALTERNATIVES other candidates per slot, best quality first. Slots that are
    not in the plan get them too, so a dropped item can still be pinned."""
    out = {}
    for slot, items in candidates.items():
        picked = (plan.get(slot) or {}).get("p_key")
        alts = [{"product": p, "match": m}
                for p, m in zip(items, scores.get(slot, []))
                if p.get("p_key") != picked and p.get("product_price") is not None]
        out[slot] = alts[:ALTERNATIVES]
    return out


def image_url_for(uid: str, session_id: str, slot: str) -> str:
    """The URL of the session's photo (slot "_photo") or of the product currently
    planned for `slot`. Only URLs already in this user's session can be looked up."""
    s = SESSIONS.get((uid, session_id))
    if s is None:
        raise SessionNotFound(session_id)
    if slot == "_photo":
        return s["image_url"]
    product = s["plan"].get(slot)
    if not product or not product.get("img_url"):
        raise KeyError(slot)
    return product["img_url"]


# ── sessions ─────────────────────────────────────────────────────────────

def _save(uid: str, session_id: str, data: dict) -> None:
    data["touched"] = time.time()
    SESSIONS[(uid, session_id)] = data
    mine = sorted((v["touched"], k) for k, v in SESSIONS.items() if k[0] == uid)
    for _, key in mine[:-MAX_SESSIONS_PER_USER]:
        del SESSIONS[key]


# ── explanations ─────────────────────────────────────────────────────────

def _brief(product: dict) -> dict:
    return {"name": (product.get("product_name") or "")[:120],
            "price": product.get("product_price")}


def _plan_prompt(scenario: str, notes: str, budget: float, result: dict) -> str:
    data = {"scenario": scenario, "user_notes": notes or None, "budget": budget,
            "total": result["total"],
            "picked": {slot: _brief(p) for slot, p in result["plan"].items()},
            "not_picked": result["dropped"], "required_but_unfilled": result["infeasible"]}
    return ("Explain to a shopper, in the second person, why this shopping plan was "
            "picked from their photo and how it uses the budget. " + _EXPLAIN_RULES +
            "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False))


def _replan_prompt(s: dict, budget: float, owned: list[str], result: dict,
                   changes: dict) -> str:
    old = s["plan"]
    data = {"old_budget": s["budget"], "new_budget": budget, "owned_now": owned,
            "new_total": result["total"],
            "changes": {slot: {"change": c,
                               "before": _brief(old[slot]) if slot in old else None,
                               "after": _brief(result["plan"][slot])
                               if slot in result["plan"] else None}
                        for slot, c in changes.items() if c != "unchanged"},
            "unchanged": [slot for slot, c in changes.items() if c == "unchanged"],
            "not_picked": result["dropped"],
            "required_but_unfilled": result["infeasible"]}
    return ("The shopper changed their budget or what they already own, and the plan "
            "was re-optimised over the same candidates. Explain what changed and why, "
            "in the second person. If nothing changed, say so in one sentence. "
            + _EXPLAIN_RULES + "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def invented_attributes(explanation: str, data_text: str) -> set[str]:
    """Colour / material / pattern words in the explanation that the data never says."""
    return (_words(explanation) & _ATTRIBUTE_WORDS) - _words(data_text)


def _fallback(result: dict, budget: float, changes: dict | None = None,
              unmatched: list[dict] | None = None) -> str:
    """Plain, always-true text for when the model's explanation is missing or unusable."""
    n = len(result["plan"])
    text = (f"{n} item{'s' if n != 1 else ''} picked for ${result['total']:.2f} of your "
            f"${budget:g} budget.")
    if changes is not None:
        changed = [s.replace("_", " ") for s, c in changes.items() if c != "unchanged"]
        text += (f" Changed: {', '.join(changed)}." if changed else " Nothing changed.")
    if result["infeasible"]:
        text += " Not filled: " + ", ".join(i["slot"].replace("_", " ")
                                            for i in result["infeasible"]) + "."
    if unmatched:
        text += (" No similar product found for: "
                 + ", ".join(u["slot"].replace("_", " ") for u in unmatched)
                 + " (see the alternatives).")
    return text


async def _explain(prompt: str, fallback: str) -> str:
    """One DeepSeek call, checked. An explanation that adds a colour, material or
    pattern the data does not contain gets one retry naming the words; if it still does,
    or the call fails, the plain `fallback` is used. A failure must not lose the plan."""
    data_text = prompt.split("DATA:", 1)[-1]
    messages = [{"role": "user", "content": prompt}]
    for _attempt in range(2):
        try:
            reply = await llm_client.chat(messages)
        except (LlmError, httpx.HTTPError) as e:
            log.warning("explanation failed: %s", e)
            return fallback
        text = (reply["message"].get("content") or "").strip()
        if not text:
            return fallback
        bad = invented_attributes(text, data_text)
        if not bad:
            return text
        log.warning("explanation added %s, which no product name says", sorted(bad))
        messages += [reply["message"], {"role": "user", "content":
                     f"These words are not in any product name in the data: "
                     f"{', '.join(sorted(bad))}. Rewrite without them, using only words "
                     f"from the product names or the slot names."}]
    return fallback
