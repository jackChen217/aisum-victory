"""Default outfit path (`/api/plan` with scenario "outfit"). No LLM call.

1. search_products_by_image once, with the user's gender and max_price = budget.
2. Detections with the same item + item_detail (left / right shoe) become one slot.
3. A slot whose best score is below SCORE_CUTOFF is searched again: its box, grown by
   CROP_PAD on every side, is cut out of the photo, uploaded, and searched with
   category = the item, the same gender and max_price. The better of the two result
   lists (by best score) is kept. At most MAX_CROP_SEARCHES at once.
4. Still below SCORE_CUTOFF: the slot is "unmatched" ("no similar product found").
   It is not planned; its first candidates are still returned as alternatives.
5. The orchestrator then runs the knapsack: the whole outfit stays within budget.

Everything has to finish DEADLINE_S after the request starts. The first search must
make it (else PlanTimeout); crop searches still running near the deadline are
cancelled and those slots keep their first results.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

from .. import image_proxy, uploads
from . import planner

log = logging.getLogger(__name__)

SCORE_CUTOFF = 0.5
CROP_PAD = 0.10           # of the box's width / height, on each side
CROP_MIN_SIDE = 320       # small crops (shoes) are enlarged to this short side
MAX_CROP_SEARCHES = 3
DEADLINE_S = 12.0
OPTIMIZE_MARGIN_S = 0.4   # left for the knapsack and the response


class PlanTimeout(planner.PlanError):
    pass


def best_score(products: list[dict]) -> float:
    return max((p.get("score") or 0.0 for p in products), default=0.0)


def grow(box: list[float], pad: float = CROP_PAD) -> tuple[float, float, float, float]:
    """A normalized [x1, y1, x2, y2] box grown by `pad` of its size on each side,
    clamped to the image."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    return (max(0.0, x1 - pad * w), max(0.0, y1 - pad * h),
            min(1.0, x2 + pad * w), min(1.0, y2 + pad * h))


def crop_jpeg(photo, box: list[float]) -> bytes:
    from PIL import Image

    x1, y1, x2, y2 = grow(box)
    W, H = photo.size
    part = photo.crop((round(x1 * W), round(y1 * H), round(x2 * W), round(y2 * H)))
    short = min(part.size)
    if 0 < short < CROP_MIN_SIDE:
        scale = CROP_MIN_SIDE / short
        part = part.resize((round(part.width * scale), round(part.height * scale)),
                           Image.Resampling.LANCZOS)
    out = io.BytesIO()
    part.save(out, "JPEG", quality=90)
    return out.getvalue()


def _box(slot: dict) -> list[float] | None:
    """The largest box of the slot's detections (one shoe of a pair is enough)."""
    boxes = [b for b in slot.get("boxes") or [] if b and len(b) == 4]
    return max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), default=None)


async def _crop_search(photo, slot: dict, budget: float, gender: str | None) -> dict:
    data = await asyncio.to_thread(crop_jpeg, photo, _box(slot))
    stored = await uploads.save(data)
    res = await planner.search_image(stored["url"], category=slot["item"],
                                     max_price=budget, gender=gender)
    # Keep the detection that matches best; with category set it should be the item.
    detections = [d for d in res.get("detections") or [] if d.get("products")]
    best = max(detections, key=lambda d: best_score(d["products"]), default=None)
    return {"products": best["products"] if best else [],
            "detected_as": best.get("item_detail") if best else None,
            "ignored_filters": res.get("ignored_filters") or []}


async def candidates(image_url: str, budget: float, gender: str | None,
                     report: dict, started: float | None = None):
    """-> (slots to plan, unmatched slots, candidates, scores, timings_ms).

    candidates / scores cover every slot (unmatched ones too, for alternatives);
    quality = 1 / (1 + rank) in the order kept."""
    started = time.perf_counter() if started is None else started
    deadline = started + DEADLINE_S
    try:
        async with asyncio.timeout(max(0.1, deadline - time.perf_counter())):
            res = await planner.search_image(image_url, max_price=budget, gender=gender)
    except TimeoutError:
        raise PlanTimeout(f"Image search took longer than {DEADLINE_S:g} seconds. "
                          "Please try again.") from None
    searched = time.perf_counter()
    if not res.get("detections"):
        raise planner.PlanError(res.get("note") or "No fashion items were detected in the photo.")
    slots = planner._outfit_slots(res, image_url, gender)
    lists = {s["slot"]: planner.clean(s.get("candidates") or [], near_duplicates=False)
             for s in slots}
    first = {name: best_score(products) for name, products in lists.items()}
    for s in slots:
        entry = report.setdefault(s["slot"], {})
        entry["top_score"] = round(first[s["slot"]], 3)
        if s.get("ignored_filters"):
            entry["ignored_filters"] = s["ignored_filters"]

    weak = [s for s in slots if first[s["slot"]] < SCORE_CUTOFF and _box(s)]
    crop = {"cutoff": SCORE_CUTOFF, "triggered": len(weak), "improved": 0,
            "timed_out": 0, "failed": 0}
    if weak:
        gate = asyncio.Semaphore(MAX_CROP_SEARCHES)

        async def one(slot, photo_task):
            photo = await photo_task
            async with gate:
                return await _crop_search(photo, slot, budget, gender)

        photo_task = asyncio.create_task(planner._photo(image_url))
        tasks = {asyncio.create_task(one(s, photo_task)): s for s in weak}
        left = deadline - OPTIMIZE_MARGIN_S - time.perf_counter()
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, left))
        for t in pending:
            t.cancel()
        if not photo_task.done():
            photo_task.cancel()
        await asyncio.gather(*pending, photo_task, return_exceptions=True)
        for t, s in tasks.items():
            entry = report[s["slot"]]
            if t in pending:
                crop["timed_out"] += 1
                entry["crop_search"] = {"status": "timed_out"}
                continue
            if t.exception() is not None:
                e = t.exception()
                crop["failed"] += 1
                log.warning("crop search failed for %s: %s", s["slot"], e)
                entry["crop_search"] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                continue
            got = t.result()
            products = planner.clean(got["products"], near_duplicates=False)
            score = best_score(products)
            used = score > first[s["slot"]]
            entry["crop_search"] = {"status": "done", "top_score": round(score, 3),
                                    "used": used, "detected_as": got["detected_as"]}
            if got["ignored_filters"]:
                entry["crop_search"]["ignored_filters"] = got["ignored_filters"]
            if used:
                crop["improved"] += 1
                lists[s["slot"]] = products
                first[s["slot"]] = score
    report["_crop_search"] = crop
    report["_matching"] = {"method": "image_rank", "enhanced": False}

    planned, unmatched = [], []
    for s in slots:
        if first[s["slot"]] < SCORE_CUTOFF:
            unmatched.append({**s, "unmatched": True, "required": False})
            report[s["slot"]]["unmatched"] = True
        else:
            planned.append(s)
    scores = {name: [{"rank": 1 / (1 + i), "colour": None, "text": None,
                      "quality": 1 / (1 + i)} for i in range(len(products))]
              for name, products in lists.items()}
    timings = {"image_search": round((searched - started) * 1000, 2),
               "crop_search": round((time.perf_counter() - searched) * 1000, 2)}
    return planned, unmatched, lists, scores, timings
