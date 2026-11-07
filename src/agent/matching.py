"""How well each candidate matches the photo — no LLM calls.

    category_filter(slot, products)  -> products whose name is the slot's item type
    palette(image, box=None)         -> dominant colours: k-means (k=3) in Lab
    score_candidates(...)            -> candidates re-ordered by quality, plus scores

quality = 0.40 x rank + 0.35 x colour + 0.25 x text, each 0-1. A part that cannot be
computed (no box and no colour words, a product image that would not load, a
description without attribute words) is left out and its weight shared by the rest.

  rank    1/(1+position) in the order the searches returned the candidates
  colour  the item's palette (cropped from the photo by its detection box) against the
          product photo's palette, white background removed; for an item the review
          added (no box), the colour words in its description against the colour names
          nearest the product's palette
  text    BM25 of the product name against the slot's description, mixed with a score
          for attribute words (striped, plaid, leather, high-top...): present in both is
          a plus, in the description but not the name is a minus
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import re
from collections import OrderedDict

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

W_RANK, W_COLOUR, W_TEXT = 0.40, 0.35, 0.25
COLOUR_TOP_N = 15          # candidates per slot whose product image is analysed
THUMB = 128                # product images are shrunk to this before analysis
MAX_CONCURRENT_IMAGES = 8
IMAGE_TIMEOUT_S = 12.0
MERGE_DE = 12.0            # palette colours closer than this are one colour

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower().replace("-", " "))


# ── category rules (outfit) ──────────────────────────────────────────────
# A slot's item type -> words a product name of that type contains. Overlaps are fine
# (a shawl is a scarf and also passes for a cape).
CATEGORIES: dict[str, set[str]] = {
    "outer": {"coat", "coats", "jacket", "jackets", "blazer", "parka", "trench", "overcoat",
              "peacoat", "shacket", "anorak", "windbreaker", "puffer", "cape", "capes",
              "poncho", "ponchos", "cloak", "duffle", "duffel", "gilet", "outerwear",
              "shawl", "wrap"},
    "top": {"shirt", "shirts", "tshirt", "tee", "tees", "top", "tops", "blouse", "tank",
            "camisole", "cami", "polo", "sweater", "sweaters", "pullover", "jumper",
            "cardigan", "hoodie", "sweatshirt", "henley", "tunic", "knit", "bodysuit",
            "turtleneck", "vest", "undershirt", "singlet"},
    "pants": {"pants", "pant", "trousers", "trouser", "jeans", "jean", "joggers", "jogger",
              "leggings", "slacks", "chinos", "culottes", "sweatpants", "palazzo", "denim",
              "overalls"},
    "shorts": {"shorts", "short", "jorts", "bermuda"},
    "skirt": {"skirt", "skirts", "skort"},
    "dress": {"dress", "dresses", "gown", "jumpsuit", "romper", "playsuit"},
    "shoes": {"shoe", "shoes", "sneaker", "sneakers", "trainer", "trainers", "boot", "boots",
              "loafer", "loafers", "heel", "heels", "pumps", "sandal", "sandals", "flats",
              "mules", "mule", "clogs", "clog", "oxford", "oxfords", "derby", "moccasin",
              "moccasins", "slippers", "espadrilles", "footwear"},
    "socks": {"sock", "socks"},
    "hat": {"hat", "hats", "cap", "caps", "beanie", "beret", "fedora", "newsboy", "cabbie",
            "visor", "bucket", "headwear"},
    "bag": {"bag", "bags", "tote", "handbag", "purse", "backpack", "crossbody", "clutch",
            "satchel", "briefcase", "pouch", "wallet", "shoulder"},
    "scarf": {"scarf", "scarves", "shawl", "wrap", "stole", "snood", "bandana"},
    "earrings": {"earring", "earrings", "hoop", "hoops", "stud", "studs", "huggie"},
    "necklace": {"necklace", "pendant", "choker", "chain"},
    "bracelet": {"bracelet", "bangle", "bangles", "cuff"},
    "ring": {"ring", "rings", "band"},
    "belt": {"belt", "belts"},
    "glasses": {"glasses", "sunglasses", "eyeglasses", "shades"},
    "watch": {"watch", "watches"},
}
# Words in a slot's name or description -> its category. The last match before "with"
# wins, so "rust duffle coat with toggle buttons" is a coat and "mini skirt" a skirt.
_SLOT_WORDS: dict[str, str] = {}
for _cat, _ws in CATEGORIES.items():
    for _w in _ws:
        _SLOT_WORDS.setdefault(_w, _cat)
_SLOT_WORDS.update({"coat": "outer", "jacket": "outer", "cape": "outer", "poncho": "outer",
                    "shawl": "scarf", "wrap": "scarf", "short": "shorts", "denim": "pants",
                    "knit": "top", "band": "ring", "chain": "necklace", "bucket": "hat",
                    "shoulder": "bag", "top": "top", "vest": "top"})


def slot_category(slot: dict) -> str | None:
    for text in (slot.get("query") or "", slot.get("item") or "", slot["slot"]):
        head = text.lower().split(" with ")[0]
        found = [_SLOT_WORDS[w] for w in words(head) if w in _SLOT_WORDS]
        if found:
            return found[-1]
    return None


def category_filter(slot: dict, products: list[dict]) -> tuple[list[dict], list[str]]:
    """(kept, rejected names). A product is kept when its name has a word of the slot's
    category. Unknown category: everything is kept. If nothing would be left, all are
    kept — a rule that empties a slot is more likely wrong than every product is."""
    cat = slot_category(slot)
    if cat is None:
        return products, []
    allowed = CATEGORIES[cat]
    kept = [p for p in products if allowed & set(words(p.get("product_name") or ""))]
    if not kept:
        return products, []
    rejected = [p.get("product_name") for p in products if p not in kept]
    return kept, rejected


# ── colour ───────────────────────────────────────────────────────────────

def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(n, 3) sRGB 0-255 -> CIE Lab (D65)."""
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124564, 0.2126729, 0.0193339],
                        [0.3575761, 0.7151522, 0.1191920],
                        [0.1804375, 0.0721750, 0.9503041]])
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]),
                     200 * (f[:, 1] - f[:, 2])], axis=1)


def _kmeans(points: np.ndarray, k: int = 3, iters: int = 12) -> list[tuple[np.ndarray, float]]:
    """[(centre, share)] sorted by share. Deterministic start: spread over lightness."""
    if len(points) < k:
        return [(points.mean(axis=0), 1.0)] if len(points) else []
    order = np.argsort(points[:, 0])
    centres = points[order[np.linspace(0, len(points) - 1, k).astype(int)]].copy()
    for _ in range(iters):
        labels = np.argmin(((points[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
        for j in range(k):
            members = points[labels == j]
            if len(members):
                centres[j] = members.mean(axis=0)
    labels = np.argmin(((points[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
    shares = np.bincount(labels, minlength=k) / len(points)
    found = sorted(((centres[j], float(shares[j])) for j in range(k) if shares[j] > 0),
                   key=lambda cs: -cs[1])
    # k is fixed at 3 even for a one-colour product: merge centres that are nearly the
    # same colour (ΔE < MERGE_DE) so one colour is not reported as three.
    merged: list[tuple[np.ndarray, float]] = []
    for c, sh in found:
        for i, (mc, ms) in enumerate(merged):
            if float(np.linalg.norm(c - mc)) < MERGE_DE:
                merged[i] = ((mc * ms + c * sh) / (ms + sh), ms + sh)
                break
        else:
            merged.append((c, sh))
    return sorted(merged, key=lambda cs: -cs[1])


def palette(img: Image.Image, box: list[float] | None = None, *,
            drop_white: bool = False, inset: float = 0.15) -> list[tuple[np.ndarray, float]]:
    """Dominant colours of an image, or of the normalized `box` region of it.

    The box is shrunk by `inset` on each side so the background around a detected item
    weighs less. `drop_white` removes the near-white studio background of product
    photos (unless that would remove almost everything: a white product)."""
    img = img.convert("RGB")
    if box:
        w, h = img.size
        x1, y1, x2, y2 = box
        dx, dy = (x2 - x1) * inset, (y2 - y1) * inset
        img = img.crop((int((x1 + dx) * w), int((y1 + dy) * h),
                        max(int((x2 - dx) * w), int((x1 + dx) * w) + 1),
                        max(int((y2 - dy) * h), int((y1 + dy) * h) + 1)))
    img.thumbnail((48, 48))
    lab = _srgb_to_lab(np.asarray(img).reshape(-1, 3))
    if drop_white:
        bg = (lab[:, 0] > 90) & (np.hypot(lab[:, 1], lab[:, 2]) < 8)
        if bg.mean() < 0.9:
            lab = lab[~bg]
    return _kmeans(lab)


def palette_similarity(a: list, b: list) -> float | None:
    """0-1. Each colour of `a` is matched to its nearest in `b` (CIE76 ΔE), weighted by
    share, both ways, averaged. ΔE 0 -> 1.0, ΔE >= 60 -> 0."""
    if not a or not b:
        return None

    def one_way(x, y):
        return sum(s * min(float(np.linalg.norm(c - d)) for d, _ in y) for c, s in x)

    d = (one_way(a, b) + one_way(b, a)) / 2
    return max(0.0, 1.0 - d / 60.0)


# Colour names with a reference Lab colour, and families that count as the same.
NAMED = {
    "black": (12, 0, 0), "white": (97, 0, 2), "grey": (60, 0, 0), "gray": (60, 0, 0),
    "silver": (78, 0, 0), "cream": (92, 1, 12), "ivory": (95, 0, 8), "beige": (82, 3, 16),
    "tan": (68, 8, 25), "khaki": (65, 2, 28), "camel": (62, 12, 35), "brown": (38, 15, 25),
    "chocolate": (28, 12, 16), "rust": (45, 38, 45), "burgundy": (28, 40, 12),
    "maroon": (26, 38, 14), "red": (48, 65, 45), "pink": (80, 22, 4), "orange": (66, 40, 66),
    "yellow": (88, -5, 75), "gold": (73, 5, 58), "olive": (50, -12, 40),
    "green": (52, -45, 35), "navy": (20, 10, -35), "blue": (45, 10, -55),
    "denim": (50, -2, -22), "purple": (38, 45, -40), "lilac": (72, 15, -18),
}
_FAMILY = {"gray": "grey", "silver": "grey", "ivory": "cream", "khaki": "beige",
           "camel": "tan", "chocolate": "brown", "maroon": "burgundy", "denim": "blue",
           "navy": "navy", "gold": "yellow", "lilac": "purple"}
_NEAR = {frozenset(p) for p in [("cream", "white"), ("cream", "beige"), ("beige", "tan"),
                                ("tan", "brown"), ("rust", "red"), ("rust", "brown"),
                                ("rust", "orange"), ("burgundy", "red"), ("navy", "blue"),
                                ("navy", "black"), ("olive", "green"), ("olive", "brown"),
                                ("pink", "red"), ("grey", "black"), ("grey", "white")]}
_NAMED_LAB = {n: np.array(v, dtype=float) for n, v in NAMED.items()}


def family(name: str) -> str:
    return _FAMILY.get(name, name)


def colour_words(text: str) -> set[str]:
    return {family(w) for w in words(text) if w in NAMED}


def nearest_name(lab: np.ndarray) -> str:
    return family(min(_NAMED_LAB, key=lambda n: float(np.linalg.norm(_NAMED_LAB[n] - lab))))


def words_similarity(wanted: set[str], pal: list) -> float | None:
    """0-1: the share of the product's palette whose colour name is one of `wanted`
    (a neighbouring family counts half)."""
    if not wanted or not pal:
        return None
    total = 0.0
    for lab, share in pal:
        n = nearest_name(lab)
        if n in wanted:
            total += share
        elif any(frozenset((n, w)) in _NEAR for w in wanted):
            total += share / 2
    return min(1.0, total)


# ── text: BM25 + attribute words ─────────────────────────────────────────

def bm25(query: str, docs: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """BM25 of `query` against each doc, scaled so the best doc is 1 (all 0 if none hit)."""
    toks = [words(d) for d in docs]
    q = set(words(query)) - {"a", "an", "the", "and", "with", "of", "in", "for"}
    if not toks or not q:
        return [0.0] * len(docs)
    n, avg = len(toks), (sum(map(len, toks)) / len(toks)) or 1
    df = {t: sum(1 for d in toks if t in d) for t in q}
    out = []
    for d in toks:
        s = 0.0
        for t in q:
            tf = d.count(t)
            if tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    top = max(out)
    return [s / top if top else 0.0 for s in out]


# Attribute -> phrases that express it in a product name.
ATTRIBUTES = {
    "striped": ["stripe", "stripes", "striped", "pinstripe", "breton"],
    "plaid": ["plaid", "tartan", "check", "checked", "checkered", "gingham", "windowpane"],
    "houndstooth": ["houndstooth", "dogtooth"],
    "argyle": ["argyle", "diamond"],
    "floral": ["floral", "flower"],
    "leather": ["leather", "pu leather", "faux leather", "vegan leather"],
    "suede": ["suede"],
    "denim": ["denim", "jean", "jeans"],
    "knit": ["knit", "knitted", "ribbed"],
    "wool": ["wool", "woolen", "woollen", "cashmere"],
    "corduroy": ["corduroy", "cord"],
    "high top": ["high top", "hi top", "high tops"],
    "low top": ["low top"],
    "platform": ["platform", "chunky"],
    "wide leg": ["wide leg", "palazzo", "baggy", "flare", "flared"],
    "straight leg": ["straight leg", "straight"],
    "mini": ["mini"], "midi": ["midi"], "maxi": ["maxi"],
    "cropped": ["cropped", "crop"],
    "oversized": ["oversized", "oversize", "loose"],
    "toggle": ["toggle", "duffle", "duffel"],
    "cape": ["cape", "poncho", "cloak"],
    "hoop": ["hoop", "hoops", "huggie"],
    "newsboy": ["newsboy", "baker boy", "cabbie", "gatsby"],
    "fedora": ["fedora"],
    "tote": ["tote"],
    "loafer": ["loafer", "loafers"],
    "sleeveless": ["sleeveless", "tank"],
    "short sleeve": ["short sleeve"],
    "long sleeve": ["long sleeve"],
}


def _has(text: str, phrases: list[str]) -> bool:
    t = " " + " ".join(words(text)) + " "
    return any(f" {' '.join(words(p))} " in t for p in phrases)


def wanted_attributes(description: str) -> list[str]:
    return [a for a, ps in ATTRIBUTES.items() if _has(description, ps)]


def attribute_score(attrs: list[str], name: str) -> float | None:
    """0.5 + 0.5 * (hits - misses) / n, so all present -> 1, all missing -> 0."""
    if not attrs:
        return None
    hits = sum(1 for a in attrs if _has(name, ATTRIBUTES[a]))
    return 0.5 + 0.5 * (2 * hits - len(attrs)) / len(attrs)


def text_scores(description: str, names: list[str]) -> list[float]:
    b = bm25(description, names)
    attrs = wanted_attributes(description)
    out = []
    for name, s in zip(names, b):
        a = attribute_score(attrs, name)
        out.append(s if a is None else 0.5 * s + 0.5 * a)
    return out


# ── product images ───────────────────────────────────────────────────────

_CACHE: "OrderedDict[str, list | None]" = OrderedDict()
_CACHE_MAX = 3000


def _decode_thumb(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (THUMB * 2, THUMB * 2))  # JPEG: decode at a lower resolution
    img = img.convert("RGB")
    img.thumbnail((THUMB, THUMB))
    return img


async def product_palettes(urls: list[str], fetch) -> dict[str, list | None]:
    """Palette per image URL (None when it would not load), fetched with `fetch(url) ->
    (bytes, content_type)` — the image proxy — at most MAX_CONCURRENT_IMAGES at once,
    cached by URL across requests. Gives up on what is unfinished after IMAGE_TIMEOUT_S."""
    gate = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)
    todo = [u for u in dict.fromkeys(urls) if u and u not in _CACHE]

    async def one(url):
        async with gate:
            try:
                data, _ = await fetch(url)
                pal = await asyncio.to_thread(
                    lambda: palette(_decode_thumb(data), drop_white=True))
            except Exception as e:  # unreachable host, not an image, decode error
                log.info("product image %s: %s", url[:80], e)
                pal = None
        _CACHE[url] = pal
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)

    if todo:
        tasks = [asyncio.create_task(one(u)) for u in todo]
        done, pending = await asyncio.wait(tasks, timeout=IMAGE_TIMEOUT_S)
        for t in pending:
            t.cancel()
    return {u: _CACHE.get(u) for u in urls}


# ── scoring ──────────────────────────────────────────────────────────────

def combine(parts: dict[str, float | None]) -> float:
    weights = {"rank": W_RANK, "colour": W_COLOUR, "text": W_TEXT}
    have = {k: v for k, v in parts.items() if v is not None}
    total = sum(weights[k] for k in have)
    return sum(weights[k] * v for k, v in have.items()) / total if total else 0.0


def score_slot(slot: dict, products: list[dict], item_palette: list | None,
               palettes: dict[str, list | None]) -> list[tuple[dict, dict]]:
    """[(product, {"rank", "colour", "text", "quality"})] sorted by quality, best first.
    Colour is computed for the first COLOUR_TOP_N products only."""
    names = [p.get("product_name") or "" for p in products]
    texts = text_scores(slot.get("query") or slot["slot"], names)
    wanted = colour_words(slot.get("query") or "")
    scored = []
    for i, (p, t) in enumerate(zip(products, texts)):
        colour = None
        if i < COLOUR_TOP_N:
            pal = palettes.get(p.get("img_url"))
            colour = (palette_similarity(item_palette, pal) if item_palette
                      else words_similarity(wanted, pal))
        parts = {"rank": 1 / (1 + i), "colour": colour,
                 "text": t if (slot.get("query") and names[i]) else None}
        parts = {k: (round(v, 3) if v is not None else None) for k, v in parts.items()}
        parts["quality"] = round(combine(parts), 4)
        scored.append((p, parts))
    scored.sort(key=lambda ps: -ps[1]["quality"])
    return scored


def item_palette(photo: Image.Image | None, slot: dict) -> list | None:
    boxes = slot.get("boxes") or []
    if photo is None or not boxes:
        return None
    # Merged detections (two shoes): pool both crops.
    pal = []
    for box in boxes:
        pal += [(c, s / len(boxes)) for c, s in palette(photo, box)]
    return pal
