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
import time
import uuid

import httpx

from .. import llm_client
from ..llm_client import LlmError
from . import planner
from .optimizer import diff_plans, optimize

log = logging.getLogger(__name__)

SESSIONS: dict[tuple[str, str], dict] = {}
MAX_SESSIONS_PER_USER = 20

_PUBLIC_SLOT_KEYS = ("slot", "required", "weight", "query", "domain")

_EXPLAIN_RULES = (
    "Rules: 2-4 short sentences, plain English, no lists, no markdown. Use only facts "
    "in the data: product names, prices, slots, budget. Never invent materials, sizes, "
    "compatibility or stock; if the data does not say, do not claim it. Mention any "
    "required slot that could not be filled and why."
)


class SessionNotFound(KeyError):
    pass


# ── public API ───────────────────────────────────────────────────────────

async def plan(uid: str, image_url: str, scenario: str, budget: float,
               owned: list[str] | None = None, notes: str = "") -> dict:
    owned = list(owned or [])
    slots = await planner.plan_slots(image_url, scenario, notes, budget=budget)
    report: dict = {}
    candidates = await planner.find_candidates(slots, budget, report=report)
    result = optimize(slots, candidates, budget, owned)
    explanation = await _explain(_plan_prompt(scenario, notes, budget, result))

    session_id = uuid.uuid4().hex
    _save(uid, session_id, {
        "scenario": scenario, "image_url": image_url, "notes": notes,
        "search_budget": budget, "budget": budget, "owned": owned,
        "slots": slots, "candidates": candidates, "plan": result["plan"],
    })
    return {
        "session_id": session_id,
        "slots": [{k: s[k] for k in _PUBLIC_SLOT_KEYS if k in s} for s in slots],
        "plan": result["plan"],
        "dropped": result["dropped"],
        "infeasible": result["infeasible"],
        "total": result["total"],
        "explanation": explanation,
        "report": report,
    }


async def replan(uid: str, session_id: str, budget: float,
                 owned: list[str] | None = None) -> dict:
    s = SESSIONS.get((uid, session_id))
    if s is None:
        raise SessionNotFound(session_id)
    owned = list(owned if owned is not None else s["owned"])
    result = optimize(s["slots"], s["candidates"], budget, owned)
    changes = diff_plans(s["plan"], result["plan"])
    explanation = await _explain(_replan_prompt(s, budget, owned, result, changes))

    s.update(budget=budget, owned=owned, plan=result["plan"], touched=time.time())
    out = {
        "session_id": session_id,
        "plan": result["plan"],
        "dropped": result["dropped"],
        "infeasible": result["infeasible"],
        "total": result["total"],
        "changes": changes,
        "explanation": explanation,
    }
    if budget > s["search_budget"]:
        # Candidates were searched with max_price = the first budget, so nothing pricier
        # is in them. Say so rather than let a bigger budget look like it changed nothing.
        out["note"] = (f"Candidates were searched up to ${s['search_budget']:g}; "
                       "a new plan is needed to look at pricier items.")
    return out


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


async def _explain(prompt: str) -> str:
    """One DeepSeek call. A failure must not lose the plan: fall back to plain text."""
    try:
        reply = await llm_client.chat([{"role": "user", "content": prompt}])
        text = (reply["message"].get("content") or "").strip()
        if text:
            return text
    except (LlmError, httpx.HTTPError) as e:
        log.warning("explanation failed: %s", e)
    return "The plan below is the best fit for your budget. (No explanation available.)"
