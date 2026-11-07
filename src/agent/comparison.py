"""Outfit ablation, four groups on the same photo and budget:

A raw       image search with no filters, literal products[0] per detection
B budget    image search with max_price=budget, literal products[0] per detection
C enhanced  B's response + the old experimental pipeline (review, text search,
            category rules, colour/BM25 re-rank) + whole-outfit knapsack
D default   B's response + the /ghostcart default: paired detections merged,
            AISUM order kept (quality = rank), whole-outfit knapsack

Each stage is a separate request so a slow/failed experiment cannot hide baselines.
Raw responses are retained for inspection; A/B never clean, merge or re-rank them.
"""
from __future__ import annotations

import copy
import time
import uuid

from . import orchestrator, planner
from .optimizer import _cents, optimize

SESSIONS: dict[tuple[str, str], dict] = {}
MAX_PER_USER = 10
ENHANCED_TIMEOUT_SECONDS = 120


class ComparisonNotFound(KeyError):
    pass


def _session(uid: str, comparison_id: str | None, image_url: str, budget: float):
    if comparison_id:
        state = SESSIONS.get((uid, comparison_id))
        if state is None:
            raise ComparisonNotFound(comparison_id)
        if state["image_url"] != image_url or state["budget"] != budget:
            raise planner.PlanError("Comparison image or budget changed; start a new comparison.")
    else:
        comparison_id = uuid.uuid4().hex
        state = {"image_url": image_url, "budget": budget, "variants": {}}
        SESSIONS[(uid, comparison_id)] = state
    state["touched"] = time.time()
    mine = sorted((s["touched"], key) for key, s in SESSIONS.items() if key[0] == uid)
    for _, key in mine[:-MAX_PER_USER]:
        del SESSIONS[key]
    return comparison_id, state


def _native_first(response: dict, budget: float) -> dict:
    plan, slots, missing, provenance = {}, [], [], {}
    # Deliberately keep both shoe detections, missing prices and imperfect titles.
    # These are the literal first products, not a hidden "first usable" baseline.
    for index, detection in enumerate(response.get("detections") or []):
        slot = f"{index + 1}_{detection.get('item') or 'item'}"
        slots.append({"slot": slot, "query": detection.get("item_detail"),
                      "box": detection.get("box"), "detection_id": index})
        products = detection.get("products") or []
        if products:
            plan[slot] = copy.deepcopy(products[0])
            provenance[slot] = {"detection_id": index, "native_rank": 1}
        else:
            missing.append({"slot": slot, "reason": "no_candidates"})
    cents = [_cents(p.get("product_price")) for p in plan.values()]
    unknown = sum(p is None for p in cents)
    total = sum(p for p in cents if p is not None) / 100
    return {"plan": plan, "slots": slots, "total": total,
            "unknown_prices": unknown, "over_budget": total > budget,
            "total_budget_enforced": False, "infeasible": missing,
            "provenance": provenance, "report": {"note": response.get("note"),
                "applied_filters": response.get("applied_filters", {}),
                "ignored_filters": response.get("ignored_filters", [])},
            "raw_response": copy.deepcopy(response)}


def _provenance(plan: dict, source: dict) -> dict:
    out = {}
    for slot, product in plan.items():
        found = []
        for index, detection in enumerate(source.get("detections") or []):
            for rank, candidate in enumerate(detection.get("products") or [], 1):
                if candidate.get("p_key") == product.get("p_key"):
                    found.append({"detection_id": index, "native_rank": rank})
        out[slot] = {"image_matches": found, "source": "image_search" if found else "extra_search"}
    return out


async def _optimized(state: dict, image_url: str, budget: float, gender: str | None,
                     *, enhanced: bool, started: float) -> dict:
    """C and D: B's candidate pool → slots (left/right shoe merged) → candidates →
    whole-outfit knapsack. D is exactly the /ghostcart default outfit path minus the
    template explanation; C turns on the old experimental pipeline."""
    # Reuse exactly B's response unless a user-selected gender needs its own search.
    # A/B never receive this extra condition.
    prior = state["variants"].get("budget")
    reused = prior is not None and gender is None
    response = (copy.deepcopy(prior["raw_response"]) if reused else
                await planner.search_image(image_url, max_price=budget, gender=gender))
    detected = time.perf_counter()
    slots = planner._outfit_slots(copy.deepcopy(response), image_url, gender)
    report = {"_matching": {"method": "experimental_rank_colour_text" if enhanced
                            else "image_rank", "enhanced": enhanced},
              "_comparison": {"reused_budget_search": reused}}
    if slots:
        slots, candidates, scores = await planner.outfit_candidates(
            image_url, slots, budget, report=report, enhanced=enhanced)
    else:
        candidates, scores = {}, {}
        report["note"] = response.get("note") or "No fashion detections; nothing to optimize."
    searched = time.perf_counter()
    quality = {slot: [m["quality"] for m in values] for slot, values in scores.items()}
    result = optimize(slots, candidates, budget, [], quality)
    result.update(slots=[{k: s[k] for k in orchestrator._PUBLIC_SLOT_KEYS if k in s}
                         for s in slots], report=report,
                  match=orchestrator._matches(result["plan"], candidates, scores),
                  provenance=_provenance(result["plan"], response),
                  total_budget_enforced=True, unknown_prices=0,
                  over_budget=result["total"] > budget,
                  request_args={"image_url": image_url, "max_price": budget,
                                "top_k": 15, **({"gender": gender} if gender else {})},
                  raw_response=copy.deepcopy(response),
                  timings_ms={"additional_image_search": round((detected - started) * 1000, 2),
                              "candidates": round((searched - detected) * 1000, 2),
                              "total": round((time.perf_counter() - started) * 1000, 2)})
    # Inclusive cost makes reuse visible instead of making C/D look artificially fast.
    result["timings_ms"]["including_reused_search"] = round(
        result["timings_ms"]["total"] + (prior["timings_ms"]["total"] if reused else 0), 2)
    result["warnings"] = [f"{key}: {value['error']}" for key, value in report.items()
                          if isinstance(value, dict) and value.get("error")]
    return result


async def stage(uid: str, image_url: str, budget: float, mode: str,
                comparison_id: str | None = None, gender: str | None = None) -> dict:
    comparison_id, state = _session(uid, comparison_id, image_url, budget)
    started = time.perf_counter()
    if mode in ("raw", "budget"):
        args = {} if mode == "raw" else {"max_price": budget}
        response = await planner.search_image(image_url, **args)
        result = _native_first(response, budget)
        result["request_args"] = {"image_url": image_url, "top_k": 15, **args}
        result["timings_ms"] = {"total": round((time.perf_counter() - started) * 1000, 2)}
    else:
        result = await _optimized(state, image_url, budget, gender,
                                  enhanced=(mode == "enhanced"), started=started)
    result["comparison_id"] = comparison_id
    result["mode"] = mode
    state["variants"][mode] = result
    state["touched"] = time.time()
    return result


def image_url_for(uid: str, comparison_id: str, variant: str, slot: str) -> str:
    state = SESSIONS.get((uid, comparison_id))
    if state is None:
        raise ComparisonNotFound(comparison_id)
    product = state["variants"].get(variant, {}).get("plan", {}).get(slot)
    if not product or not product.get("img_url"):
        raise KeyError(slot)
    return product["img_url"]
