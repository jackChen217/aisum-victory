"""Pick one product per slot under a budget — a multiple-choice knapsack.

Pure functions: no network, no LLM, no state. Products are AISUM product dicts
(`p_key`, `product_name`, `product_price`, ...) and come back unchanged.

Why quality comes from rank and not from `score`: scene-match scores cluster in
0.97–1.0, so they do not separate candidates. The order the search returned them
in does, so quality = 1/(1+rank): 1, 1/2, 1/3, ... The steep drop makes leaving the top
result cost real objective, so the optimizer does not trade it away for a few dollars.

    optimize(slots, candidates, budget, owned) -> {plan, dropped, total, infeasible, objective}
    diff_plans(old_plan, new_plan)             -> {slot: added | removed | replaced | unchanged}
"""
from __future__ import annotations

import math

OVER_BUDGET = "over_budget"
OWNED = "owned"
NO_CANDIDATES = "no_candidates"

_EPS = 1e-9


def _cents(price) -> int | None:
    """Price in whole cents, or None when it is missing or not a usable number."""
    if price is None or isinstance(price, bool):
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value * 100)


def _usable(products: list[dict]) -> list[tuple[dict, int]]:
    """Drop products with no price and refurbished ("Renewed") listings; keep order."""
    out = []
    for p in products or []:
        cents = _cents(p.get("product_price"))
        if cents is None or "renewed" in (p.get("product_name") or "").lower():
            continue
        out.append((p, cents))
    return out


def _options(products: list[tuple[dict, int]]) -> list[tuple[int, float, dict]]:
    """(cents, quality, product) per candidate, quality = 1/(1+rank), rank 0 = first.

    Among candidates with the same price only the best-ranked one can ever be chosen,
    so the rest are dropped here to keep the DP small.
    """
    best: dict[int, tuple[int, float, dict]] = {}
    for rank, (p, cents) in enumerate(products):
        if cents not in best:
            best[cents] = (cents, 1 / (1 + rank), p)
    return list(best.values())


def _solve(order: list[tuple[str, bool, float, list]], budget_c: int):
    """Exact DP over slots. States are a Pareto frontier of (cost -> value, picks).

    Equivalent to the classic dp[cost] table, but only reachable costs are stored, so
    a $3,000 budget does not mean 300,000 cells per slot. Returns (value, cost, picks)
    for the best plan, or None when the required slots cannot all fit.
    """
    # picks is a linked list (slot, product, rest) so states share history.
    states: list[tuple[int, float, tuple | None]] = [(0, 0.0, None)]
    for slot, required, weight, options in order:
        nxt: dict[int, tuple[float, tuple | None]] = {}

        def offer(cost, value, picks):
            if cost <= budget_c and (cost not in nxt or value > nxt[cost][0] + _EPS):
                nxt[cost] = (value, picks)

        for cost, value, picks in states:
            if not required:
                offer(cost, value, picks)
            for p_cents, quality, product in options:
                offer(cost + p_cents, value + weight * quality, (slot, product, picks))
        # Keep only states that beat every cheaper state.
        states, best = [], -math.inf
        for cost in sorted(nxt):
            value, picks = nxt[cost]
            if value > best + _EPS:
                states.append((cost, value, picks))
                best = value
        if not states:
            return None
    cost, value, picks = states[-1]  # highest value; cheapest among equals
    chosen = {}
    while picks is not None:
        slot, product, picks = picks
        chosen[slot] = product
    return value, cost, chosen


def optimize(slots: list[dict], candidates: dict[str, list[dict]], budget: float,
             owned: list[str] | None = None) -> dict:
    """Choose at most one product per slot, total price <= budget, maximising
    sum(weight * quality).

    slots:      [{"slot": "keyboard", "required": True, "weight": 1.0}, ...]
    candidates: {"keyboard": [product, ...], ...} — in the order the search ranked them
    budget:     US dollars
    owned:      slot names the user already has; those slots are skipped entirely

    Required slots must be filled. When they cannot all be — no usable candidate, or
    together they cost more than the budget — the ones that cannot are reported in
    `infeasible` and the rest of the plan is still built. Which required slot gives way
    is decided by: lowest weight first, then the most expensive cheapest option, then
    the later slot in `slots`.

    Returns:
      plan:       {slot: product}
      dropped:    [{"slot", "reason"}] for every slot not in plan —
                  owned | no_candidates | over_budget
      total:      dollars, sum of the plan's prices
      infeasible: [{"slot", "reason", "min_price"}] — required slots left unfilled
      objective:  the maximised sum(weight * quality)
    """
    owned_set = set(owned or [])
    budget_c = _cents(budget) or 0
    dropped: list[dict] = []
    infeasible: list[dict] = []
    order: list[tuple[str, bool, float, list]] = []

    for s in slots:
        name, required = s["slot"], bool(s.get("required", False))
        weight = float(s.get("weight", 1.0))
        if name in owned_set:
            dropped.append({"slot": name, "reason": OWNED})
            continue
        options = _options(_usable(candidates.get(name, [])))
        if not options:
            dropped.append({"slot": name, "reason": NO_CANDIDATES})
            if required:
                infeasible.append({"slot": name, "reason": NO_CANDIDATES, "min_price": None})
            continue
        affordable = [o for o in options if o[0] <= budget_c]
        if not affordable:
            dropped.append({"slot": name, "reason": OVER_BUDGET})
            if required:
                infeasible.append({"slot": name, "reason": OVER_BUDGET,
                                   "min_price": min(o[0] for o in options) / 100})
            continue
        order.append((name, required, weight, affordable))

    # Each required slot fits alone, but maybe not all together: relax one at a time.
    while (result := _solve(order, budget_c)) is None:
        position = {name: i for i, (name, *_rest) in enumerate(order)}
        req = [o for o in order if o[1]]
        victim = min(req, key=lambda o: (o[2], -min(c for c, _, _ in o[3]),
                                         -position[o[0]]))
        order = [(n, r and n != victim[0], w, opts) for n, r, w, opts in order]
        infeasible.append({"slot": victim[0], "reason": OVER_BUDGET,
                           "min_price": min(c for c, _, _ in victim[3]) / 100})

    value, cost, chosen = result
    for name, _required, _w, _opts in order:
        if name not in chosen:
            dropped.append({"slot": name, "reason": OVER_BUDGET})
    # After two relaxations the first relaxed slot can fit again as an optional pick.
    infeasible = [i for i in infeasible if i["slot"] not in chosen]

    slot_order = [s["slot"] for s in slots]
    return {
        "plan": {n: chosen[n] for n in slot_order if n in chosen},
        "dropped": sorted(dropped, key=lambda d: slot_order.index(d["slot"])),
        "total": cost / 100,
        "infeasible": sorted(infeasible, key=lambda d: slot_order.index(d["slot"])),
        "objective": round(value, 9),
    }


ADDED, REMOVED, REPLACED, UNCHANGED = "added", "removed", "replaced", "unchanged"


def _identity(product: dict):
    return product.get("p_key") or product.get("product_name")


def diff_plans(old: dict, new: dict) -> dict[str, str]:
    """Slot-by-slot change between two plans.

    Accepts plans ({slot: product}) or whole optimize() results (their `plan` is used).
    Products are compared by p_key, falling back to product_name.
    """
    old = old.get("plan", old) if isinstance(old.get("plan"), dict) else old
    new = new.get("plan", new) if isinstance(new.get("plan"), dict) else new
    out = {}
    for slot in list(old) + [s for s in new if s not in old]:
        if slot not in new:
            out[slot] = REMOVED
        elif slot not in old:
            out[slot] = ADDED
        elif _identity(old[slot]) == _identity(new[slot]):
            out[slot] = UNCHANGED
        else:
            out[slot] = REPLACED
    return out
