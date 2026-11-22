"""`?format=ui` for /api/plan and /api/replan: the result in the frontend's shape.

Built from the normal response plus the saved session; the normal fields are not
changed. The field names follow the frontend's list (category, category_label, title,
budget, total_price, is_over_budget, original_image, comparison_image,
matched_products, detected_items, unmatched_items) plus session_id and 3 alternatives
per matched product, each in the same shape as a matched product.

Prices, names and links are AISUM's, untouched (click_url keeps its affiliate tag).
"""
from __future__ import annotations

CATEGORY_LABELS = {"outfit": "Outfit", "desk": "Desk", "room": "Room"}


def _label(slot: str) -> str:
    return slot.replace("_", " ").strip().capitalize()


def _box(box) -> dict | None:
    """Normalized [x1, y1, x2, y2] (0-1, origin top-left) -> named fields."""
    if not box or len(box) != 4:
        return None
    x1, y1, x2, y2 = box
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": round(x2 - x1, 4),
            "height": round(y2 - y1, 4), "normalized": True}


def _product(slot: str, product: dict, match: dict | None = None,
             size: dict | None = None) -> dict:
    out = {
        "id": product.get("p_key"),
        "slot": slot,
        "item_label": _label(slot),
        "name": product.get("product_name"),
        "brand": product.get("brand"),
        "price": product.get("product_price"),
        "image_url": product.get("img_url"),
        "product_url": product.get("click_url"),
        "score": product.get("score"),
        "rank": round(1 / match["rank"] - 1) + 1 if match and match.get("rank") else None,
    }
    if size is not None:
        out["size"] = size
    return out


def _alternatives(data: dict, slot: str) -> list[dict]:
    return [_product(slot, a["product"], a.get("match"), a.get("size"))
            for a in (data.get("alternatives") or {}).get(slot, [])]


def to_ui(data: dict, session: dict, comparison_image: str | None = None) -> dict:
    """`data`: what orchestrator.plan / replan returned; `session`: the saved session."""
    scenario = session["scenario"]
    budget = data.get("budget", session.get("budget"))
    plan, match, sizes = data["plan"], data.get("match") or {}, data.get("sizes") or {}
    slots = session["slots"] + session.get("unmatched", [])
    unmatched = {d["slot"]: d for d in data.get("dropped", []) if d["reason"] == "unmatched"}

    matched = []
    for slot, product in plan.items():
        entry = _product(slot, product, match.get(slot), sizes.get(slot))
        entry["alternatives"] = _alternatives(data, slot)
        matched.append(entry)
    by_slot = {m["slot"]: m for m in matched}

    reasons = {d["slot"]: d["reason"] for d in data.get("dropped", [])}
    reasons.update({i["slot"]: i.get("reason", "infeasible") for i in data.get("infeasible", [])})
    detected = []
    for s in slots:
        status = ("matched" if s["slot"] in by_slot else
                  "unmatched" if s["slot"] in unmatched else reasons.get(s["slot"], "not_picked"))
        detections = s.get("detections") or [{"id": None, "item": s.get("item"),
                                               "item_detail": s.get("query"), "box": None}]
        for d in detections:
            detected.append({
                "id": f"{s['slot']}:{d['id']}" if d.get("id") is not None else s["slot"],
                "slot": s["slot"],
                "item": d.get("item") or s.get("item") or s["slot"],
                "item_label": _label(s["slot"]),
                "description": d.get("item_detail") or s.get("query"),
                "bounding_box": _box(d.get("box")),
                # Best AISUM score of this detection's own products (display only: the
                # page uses it to tell a duplicate or false box from the real one).
                "score": d.get("top_score"),
                "status": status,
                "matched_product": by_slot.get(s["slot"]),
            })

    unmatched_items = []
    for slot, d in unmatched.items():
        s = next((x for x in slots if x["slot"] == slot), {})
        unmatched_items.append({
            "slot": slot,
            "item": s.get("item") or slot,
            "item_label": _label(slot),
            "description": s.get("query"),
            "bounding_boxes": [b for b in map(_box, s.get("boxes") or []) if b],
            "reason": d["reason"],
            "message": d.get("message"),
            "alternatives": _alternatives(data, slot),
        })

    total = data["total"]
    label = CATEGORY_LABELS.get(scenario, scenario.capitalize())
    return {
        "session_id": data["session_id"],
        "category": scenario,
        "category_label": label,
        "title": f"{label} under ${budget:g}",
        "budget": budget,
        "total_price": total,
        "is_over_budget": total > budget,
        "original_image": session["image_url"],
        # No server-side composite exists; the page draws the Lookboard itself.
        "comparison_image": comparison_image,
        "matched_products": matched,
        "detected_items": detected,
        "unmatched_items": unmatched_items,
        "explanation": data.get("explanation"),
    }
