"""Sizes from product names only (Desk / Room). No product page is ever fetched.

AISUM has no dimension field; whatever size exists is written in `product_name`
("55 x 24 Inch", "140cm", "27\"" ...). parse() reads it with regular expressions, and
check() compares the widest dimension with the user's desk width:

    too_large        the name gives a size wider than the desk
    fits             the name gives a size that is not wider than the desk
    size_unverified  the name gives no size we can read (or no unit)
    not_checked      a size was read, but the user gave no desk width

A size that was read is still only what the title says, never a verified spec.
"""
from __future__ import annotations

import re

_TO_INCH = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4}


def _num(n: int) -> str:
    return rf"(?P<n{n}>\d{{1,4}}(?:\.\d+)?)"


def _unit_re(n: int) -> str:
    # A unit right after a number; "in" must not be the "in" of "2 in 1" / "3-in-1".
    # An optional W / D / H / L label may follow ('23.6"W x 11.8"D').
    return (rf"\s*(?:(?P<in{n}>\"|”|''|-?inch(?:es)?\b|in\b(?![\s-]*\d))|"
            rf"(?P<cm{n}>cm\b|centimet(?:er|re)s?\b)|(?P<mm{n}>mm\b)"
            rf")?(?:\s*\(?(?P<l{n}>[WDHL])\)?\b)?")


_SEP = r"\s*[x×X*]\s*"
_DIMS = re.compile(_num(1) + _unit_re(1) + _SEP + _num(2) + _unit_re(2)
                   + rf"(?:{_SEP}{_num(3)}{_unit_re(3)})?", re.IGNORECASE)
_SINGLE = re.compile(_num(1) + _unit_re(1), re.IGNORECASE)


def _unit(m: re.Match, n: int) -> str | None:
    return next((u for u in ("in", "cm", "mm") if m.group(f"{u}{n}")), None)


def parse(name: str | None) -> dict | None:
    """{"dims_in": [...], "width_in": float, "text": matched text} or None.

    "55 x 24 inch" / '55" x 24"' / "140x60cm": every number of the group gets the
    group's unit (the last one written). A lone "27 inch" or "120cm" counts as one
    dimension. Numbers with no unit anywhere are not sizes (could be a model number).

    width_in, what is compared with the desk: the dimension labelled W if there is one
    ('23.6"W x 11.8"D x 70.9"H'), else the larger of the first two (the footprint; a
    third is taken as height), else a lone dimension (a monitor's 27 inch diagonal,
    which is a little more than its width, so the check errs on the safe side)."""
    if not name:
        return None
    groups: list[tuple[list[float], list[str | None]]] = []
    texts: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _DIMS.finditer(name):
        idx = [i for i in (1, 2, 3) if m.group(f"n{i}")]
        unit = next((u for u in reversed([_unit(m, i) for i in idx]) if u), None)
        if unit is None:
            continue
        groups.append(([float(m.group(f"n{i}")) * _TO_INCH[unit] for i in idx],
                       [(m.group(f"l{i}") or "").upper() or None for i in idx]))
        texts.append(m.group(0).strip())
        spans.append(m.span())
    for m in _SINGLE.finditer(name):
        if any(a <= m.start() < b for a, b in spans):
            continue
        unit = _unit(m, 1)
        if unit is None:
            continue
        groups.append(([float(m.group("n1")) * _TO_INCH[unit]], [None]))
        texts.append(m.group(0).strip())
    groups = [(d, l) for d, l in groups if all(v > 0 for v in d)]
    if not groups:
        return None
    widths = []
    for dims, labels in groups:
        if "W" in labels:
            widths.append(dims[labels.index("W")])
        else:
            widths.append(max(dims[:2]))
    return {"dims_in": [round(v, 2) for d, _ in groups for v in d],
            "width_in": round(max(widths), 2), "text": "; ".join(texts)}


def check(product: dict, desk_width_in: float | None) -> dict:
    size = parse(product.get("product_name"))
    if size is None:
        return {"status": "size_unverified"}
    if desk_width_in is None:
        return {"status": "not_checked", **size}
    status = "too_large" if size["width_in"] > desk_width_in else "fits"
    return {"status": status, **size, "desk_width_in": desk_width_in}


def to_inches(value: float | None, unit: str = "in") -> float | None:
    return None if value is None else round(value * _TO_INCH[unit], 2)
