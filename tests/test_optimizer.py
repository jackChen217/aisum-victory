import itertools
import random

import pytest

from src.agent.optimizer import diff_plans, optimize


def p(key, price, name=None):
    """A product shaped like AISUM's, with only the fields the optimizer reads."""
    return {"p_key": key, "product_name": name or f"Product {key}",
            "product_price": price, "score": 1.0}


SLOTS = [
    {"slot": "monitor", "required": True, "weight": 3},
    {"slot": "keyboard", "required": True, "weight": 2},
    {"slot": "mouse", "required": False, "weight": 1},
]
# Quality from rank: first = 1.0, second = 0.5.
CANDIDATES = {
    "monitor": [p("M1", 300), p("M2", 150)],
    "keyboard": [p("K1", 120), p("K2", 60)],
    "mouse": [p("m1", 50), p("m2", 20)],
}


def keys(plan):
    return {slot: prod["p_key"] for slot, prod in plan.items()}


def test_normal_case_finds_best_combination():
    r = optimize(SLOTS, CANDIDATES, budget=450)
    # 300 + 120 + 20 = 440 → 3*1 + 2*1 + 1*0.5 = 5.5; greedy "best mouse" would bust the budget.
    assert keys(r["plan"]) == {"monitor": "M1", "keyboard": "K1", "mouse": "m2"}
    assert r["total"] == 440
    assert r["objective"] == pytest.approx(5.5)
    assert r["dropped"] == [] and r["infeasible"] == []


def test_quality_falls_off_as_one_over_rank():
    slots = [{"slot": "kb", "required": True, "weight": 6}]
    # Quality 1, 1/2, 1/3, 1/4 — not the linear 1, 3/4, 1/2, 1/4.
    for budget, (key, objective) in {100: ("k1", 6), 60: ("k2", 3), 40: ("k3", 2),
                                     20: ("k4", 1.5)}.items():
        cands = {"kb": [p("k1", 100), p("k2", 60), p("k3", 40), p("k4", 20)]}
        r = optimize(slots, cands, budget)
        assert keys(r["plan"]) == {"kb": key}
        assert r["objective"] == pytest.approx(objective)


def test_top_pick_is_not_traded_for_a_small_saving():
    # Linear quality over 10 candidates made 2nd place cost only 0.1 * weight, so a
    # cheaper keyboard plus a filler item won. With 1/(1+rank) the top keyboard wins.
    slots = [{"slot": "kb", "required": True, "weight": 5},
             {"slot": "pad", "required": False, "weight": 1}]
    cands = {"kb": [p("k1", 100)] + [p(f"k{i}", 70) for i in range(2, 11)],
             "pad": [p(f"m{i}", 30) for i in range(1, 11)]}
    r = optimize(slots, cands, budget=100)
    assert keys(r["plan"]) == {"kb": "k1"}


def test_plan_returns_products_unchanged():
    r = optimize(SLOTS, CANDIDATES, budget=450)
    assert r["plan"]["monitor"] is CANDIDATES["monitor"][0]


def test_budget_too_low_for_a_required_slot():
    r = optimize(SLOTS, CANDIDATES, budget=100)
    # The cheapest monitor alone is $150.
    assert r["infeasible"] == [{"slot": "monitor", "reason": "over_budget", "min_price": 150}]
    assert {"slot": "monitor", "reason": "over_budget"} in r["dropped"]
    # The rest is still planned: 60 + 20 = 80 (60 + 50 would be 110).
    assert keys(r["plan"]) == {"keyboard": "K2", "mouse": "m2"}
    assert r["total"] == 80


def test_required_slots_that_do_not_fit_together():
    # Each fits alone (150, 60) but not both (210 > 200): the lower-weight one gives way.
    r = optimize(SLOTS, CANDIDATES, budget=200)
    assert r["infeasible"] == [{"slot": "keyboard", "reason": "over_budget", "min_price": 60}]
    assert keys(r["plan"]) == {"monitor": "M2", "mouse": "m1"}
    assert r["total"] == 200
    assert r["dropped"] == [{"slot": "keyboard", "reason": "over_budget"}]


def test_optional_slot_dropped_when_budget_runs_out():
    r = optimize(SLOTS, CANDIDATES, budget=210)
    assert keys(r["plan"]) == {"monitor": "M2", "keyboard": "K2"}
    assert r["dropped"] == [{"slot": "mouse", "reason": "over_budget"}]
    assert r["infeasible"] == []


def test_owned_slots_are_skipped():
    r = optimize(SLOTS, CANDIDATES, budget=200, owned=["monitor"])
    assert "monitor" not in r["plan"]
    assert r["dropped"] == [{"slot": "monitor", "reason": "owned"}]
    assert r["infeasible"] == []  # required but owned is not a problem
    assert keys(r["plan"]) == {"keyboard": "K1", "mouse": "m1"}
    assert r["total"] == 170


def test_missing_prices_and_renewed_items_are_removed():
    cands = {
        "monitor": [p("M0", None), p("Mr", 10, "Monitor X (Renewed)"), p("M2", 150)],
        "keyboard": [p("K0", None), p("Kr", 5, "keyboard renewed")],
        "mouse": [p("m0", "n/a"), p("m1", 50)],
    }
    r = optimize(SLOTS, cands, budget=1000)
    assert keys(r["plan"]) == {"monitor": "M2", "mouse": "m1"}
    # M2 is the only usable monitor, so after filtering it ranks first: quality 1.0.
    assert r["objective"] == pytest.approx(3 * 1 + 1 * 1)
    assert r["infeasible"] == [{"slot": "keyboard", "reason": "no_candidates", "min_price": None}]
    assert {"slot": "keyboard", "reason": "no_candidates"} in r["dropped"]


def test_slot_with_no_candidate_list_at_all():
    r = optimize(SLOTS, {"monitor": CANDIDATES["monitor"]}, budget=1000)
    assert keys(r["plan"]) == {"monitor": "M1"}
    assert {"slot": "mouse", "reason": "no_candidates"} in r["dropped"]
    assert [i["slot"] for i in r["infeasible"]] == ["keyboard"]


def test_prices_are_compared_in_cents():
    slots = [{"slot": "a", "required": True, "weight": 1},
             {"slot": "b", "required": True, "weight": 1}]
    cands = {"a": [p("a1", 0.1)], "b": [p("b1", 0.2)]}
    r = optimize(slots, cands, budget=0.3)  # 0.1 + 0.2 > 0.3 in floats
    assert keys(r["plan"]) == {"a": "a1", "b": "b1"}
    assert r["total"] == pytest.approx(0.3)


def _brute_force(slots, cands, budget):
    options = [[None] + [(i, c) for i, c in enumerate(cands[s["slot"]])] for s in slots]
    best = None
    for combo in itertools.product(*options):
        if any(s["required"] and c is None for s, c in zip(slots, combo)):
            continue
        cost = sum(c[1]["product_price"] for c in combo if c)
        if cost > budget + 1e-9:
            continue
        value = sum(s["weight"] / (1 + c[0])
                    for s, c in zip(slots, combo) if c)
        best = value if best is None else max(best, value)
    return best


@pytest.mark.parametrize("seed", range(30))
def test_matches_brute_force(seed):
    rng = random.Random(seed)
    slots = [{"slot": f"s{i}", "required": rng.random() < 0.4,
              "weight": rng.randint(1, 5)} for i in range(4)]
    cands = {s["slot"]: [p(f"{s['slot']}_{j}", rng.randint(10, 300))
                         for j in range(rng.randint(1, 4))] for s in slots}
    budget = rng.randint(100, 800)
    r = optimize(slots, cands, budget)
    expected = _brute_force(slots, cands, budget)
    if r["infeasible"]:
        assert expected is None
    else:
        assert r["objective"] == pytest.approx(expected)
        assert r["total"] <= budget


# ── diff_plans ──────────────────────────────────────────────────────────

def test_diff_plans():
    old = {"monitor": p("M1", 300), "keyboard": p("K1", 120), "mouse": p("m1", 50)}
    new = {"monitor": p("M1", 300), "keyboard": p("K2", 60), "chair": p("c1", 90)}
    assert diff_plans(old, new) == {"monitor": "unchanged", "keyboard": "replaced",
                                    "mouse": "removed", "chair": "added"}


def test_diff_plans_accepts_optimize_results():
    before = optimize(SLOTS, CANDIDATES, budget=450)
    after = optimize(SLOTS, CANDIDATES, budget=210)
    assert diff_plans(before, after) == {"monitor": "replaced", "keyboard": "replaced",
                                         "mouse": "removed"}
    assert diff_plans(after, after) == {"monitor": "unchanged", "keyboard": "unchanged"}
