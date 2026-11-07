"""matching.py: keyword categories, colour, BM25 and attributes, combined quality."""
import asyncio

import pytest
from PIL import Image

from src.agent import matching as m


def prod(name, url=None):
    return {"p_key": name[:8], "product_name": name, "product_price": 10, "img_url": url}


# ── categories ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("slot,cat", [
    ({"slot": "jacket", "query": "rust red wool duffle coat with toggle buttons"}, "outer"),
    ({"slot": "shorts", "query": "black leather mini skirt"}, "skirt"),
    ({"slot": "coat", "query": "beige plaid knit cape with buttons"}, "outer"),
    ({"slot": "shoe", "query": "white chunky platform sneakers"}, "shoes"),
    ({"slot": "hat", "query": "cream checked newsboy cap"}, "hat"),
    ({"slot": "thing", "query": "something odd"}, None),
])
def test_slot_category(slot, cat):
    assert m.slot_category(slot) == cat


def test_category_filter_keeps_right_type_only():
    slot = {"slot": "jacket", "query": "rust duffle coat"}
    kept, rejected = m.category_filter(slot, [
        prod("Men's Wool Duffle Coat"), prod("Corduroy Long Sleeve Shirt"),
        prod("Classic Shacket Jacket"), prod("Plaid Scarf")])
    assert [p["product_name"] for p in kept] == ["Men's Wool Duffle Coat", "Classic Shacket Jacket"]
    assert rejected == ["Corduroy Long Sleeve Shirt", "Plaid Scarf"]


def test_category_filter_never_empties_a_slot():
    slot = {"slot": "hat", "query": "red cap"}
    items = [prod("Something Else"), prod("Another Thing")]
    assert m.category_filter(slot, items) == (items, [])


# ── colour ───────────────────────────────────────────────────────────────

def solid(rgb, size=(60, 60)):
    return Image.new("RGB", size, rgb)


def test_palette_finds_the_colour_and_drops_white_background():
    img = solid((255, 255, 255), (100, 100))
    img.paste(solid((180, 60, 40), (40, 40)), (30, 30))   # rust square on white
    pal = m.palette(img, drop_white=True)
    assert m.nearest_name(pal[0][0]) in {"rust", "red"} and pal[0][1] > 0.6
    assert "white" not in {m.nearest_name(c) for c, _ in pal}   # background removed


def test_palette_box_crop():
    img = solid((20, 30, 90), (100, 100))                  # navy
    img.paste(solid((240, 220, 60), (50, 100)), (50, 0))   # yellow right half
    assert m.nearest_name(m.palette(img, [0.5, 0, 1, 1])[0][0]) == "yellow"
    assert m.nearest_name(m.palette(img, [0, 0, 0.5, 1])[0][0]) == "navy"


def test_palette_similarity_orders_colours():
    rust = m.palette(solid((180, 60, 40)))
    brown = m.palette(solid((110, 75, 50)))
    white = m.palette(solid((250, 250, 250)))
    assert m.palette_similarity(rust, rust) == pytest.approx(1.0)
    assert m.palette_similarity(rust, brown) > m.palette_similarity(rust, white)
    assert m.palette_similarity(rust, []) is None


def test_colour_words_against_palette():
    yellow = m.palette(solid((240, 210, 50)))
    assert m.colour_words("small yellow cartoon tote") == {"yellow"}
    assert m.words_similarity({"yellow"}, yellow) == pytest.approx(1.0)
    assert m.words_similarity({"navy"}, yellow) == 0.0
    assert m.words_similarity(set(), yellow) is None


# ── text ─────────────────────────────────────────────────────────────────

def test_bm25_prefers_matching_names():
    s = m.bm25("pink striped t-shirt", ["Plain White T-Shirt", "Pink Striped Tee T-Shirt", "Jeans"])
    assert s[1] == 1.0 and s[1] > s[0] > s[2] == 0.0


def test_attribute_bonus_and_penalty():
    desc = "pale pink thin horizontal striped t-shirt"
    assert m.wanted_attributes(desc) == ["striped"]
    assert m.attribute_score(["striped"], "Women's Striped Tee") == 1.0
    assert m.attribute_score(["striped"], "Women's Plain White Tee") == 0.0
    assert m.attribute_score([], "anything") is None
    assert m.attribute_score(["high top"], "Hi-Top Canvas Sneakers") == 1.0
    assert m.attribute_score(["plaid", "leather"], "Leather Jacket") == 0.5


# ── combined ─────────────────────────────────────────────────────────────

def test_combine_reweights_missing_parts():
    assert m.combine({"rank": 1.0, "colour": 0.0, "text": 0.0}) == pytest.approx(0.40)
    assert m.combine({"rank": 1.0, "colour": None, "text": 0.0}) == pytest.approx(0.40 / 0.65)
    assert m.combine({"rank": 0.5, "colour": None, "text": None}) == pytest.approx(0.5)


def test_score_slot_lets_colour_and_pattern_beat_rank():
    item = m.palette(solid((235, 200, 205)))                  # pale pink
    pals = {"white": m.palette(solid((250, 250, 250))), "pink": m.palette(solid((232, 198, 204)))}
    slot = {"slot": "shirt", "query": "pale pink thin horizontal striped t-shirt"}
    ranked = m.score_slot(slot, [prod("Plain White T-Shirt", "white"),
                                 prod("Pink Striped T-Shirt", "pink")], item, pals)
    assert ranked[0][0]["product_name"] == "Pink Striped T-Shirt"
    assert ranked[0][1]["colour"] > ranked[1][1]["colour"]


def test_product_palettes_caches_and_survives_failures():
    calls = []

    async def fetch(url):
        calls.append(url)
        if url == "bad":
            raise ValueError("not an image")
        import io
        buf = io.BytesIO()
        solid((200, 30, 30)).save(buf, "PNG")
        return buf.getvalue(), "image/png"

    m._CACHE.clear()
    out = asyncio.run(m.product_palettes(["https://a/1.png", "bad", "https://a/1.png"], fetch))
    assert out["bad"] is None and m.nearest_name(out["https://a/1.png"][0][0]) == "red"
    asyncio.run(m.product_palettes(["https://a/1.png"], fetch))
    assert calls.count("https://a/1.png") == 1


def test_one_colour_is_not_split_into_three():
    pal = m.palette(solid((120, 140, 60)))
    assert len(pal) == 1 and pal[0][1] == pytest.approx(1.0)
