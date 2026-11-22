"""static/resultmarks.js: which boxes and numbered dots the result photo shows (run in
Node; skipped where Node is not installed). One number per cart item, in photo order."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (Path(__file__).parent.parent / "static" / "resultmarks.js").as_uri()


def box(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def det(slot, b, score):
    return {"slot": slot, "bounding_box": b, "score": score}


def product(slot):
    return {"slot": slot, "id": f"p-{slot}"}


def run(expr, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = (f"const m = await import({json.dumps(MODULE)});"
              "const data = JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify({expr}));")
    done = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(payload)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


def marks(data):
    return run("m.cartMarks(data)", data)


def test_cart_is_numbered_top_to_bottom_not_in_plan_order():
    data = {"matched_products": [product("shoe"), product("pants"), product("hat"), product("top")],
            "detected_items": [det("shoe", box(.4, .85, .5, .95), .6),
                               det("pants", box(.35, .5, .65, .85), .8),
                               det("hat", box(.42, .02, .58, .1), .7),
                               det("top", box(.3, .15, .7, .5), .9)]}
    got = marks(data)
    assert [(m["n"], m["product"]["slot"]) for m in got] == [(1, "hat"), (2, "top"), (3, "pants"), (4, "shoe")]
    top = got[1]
    assert top["dots"] == [{"x": .5, "y": .15}]  # top centre of the box


def test_left_and_right_shoe_share_one_number():
    near = {"matched_products": [product("shoe")],
            "detected_items": [det("shoe", box(.40, .85, .48, .95), .60),
                               det("shoe", box(.50, .84, .58, .94), .55)]}
    got = marks(near)
    assert len(got) == 1 and got[0]["n"] == 1
    assert len(got[0]["boxes"]) == 2
    assert got[0]["dots"] == [{"x": .49, "y": .84}]   # one dot over the pair

    apart = {"matched_products": [product("shoe")],
             "detected_items": [det("shoe", box(.10, .85, .18, .95), .60),
                                det("shoe", box(.70, .84, .78, .94), .50)]}
    got = marks(apart)
    assert len(got[0]["boxes"]) == 2 and len(got[0]["dots"]) == 2   # one on each foot


def test_duplicate_and_stray_boxes_are_dropped():
    data = {"matched_products": [product("pants"), product("bag")],
            "detected_items": [
                det("pants", box(.37, .45, .63, .86), .87),
                det("pants", box(.30, .22, .71, .87), .85),     # full-body box around the pants
                det("bag", box(.17, .45, .42, .96), .53),
                det("bag", box(.78, .61, .93, 1.0), .43),       # smaller bag, far side
                det("bag", box(.87, .57, 1.0, 1.0), .0),
            ]}
    got = {m["product"]["slot"]: m for m in marks(data)}
    assert got["pants"]["boxes"] == [box(.37, .45, .63, .86)]
    assert got["bag"]["boxes"] == [box(.17, .45, .42, .96)]


def test_unmatched_items_get_no_number_and_boxless_items_go_last():
    data = {"matched_products": [product("belt"), product("coat")],
            "detected_items": [det("coat", box(.3, .2, .7, .8), .9),
                               det("glove", box(.1, .5, .2, .6), .4)]}
    got = marks(data)
    assert [(m["n"], m["product"]["slot"], m["dots"]) for m in got][1] == (2, "belt", [])
    assert all(m["product"]["slot"] != "glove" for m in got)


def test_dots_that_would_touch_are_moved_apart():
    got = run("m.spreadDots(data, 26, 500)", [{"x": 100, "y": 50}, {"x": 110, "y": 52}, {"x": 300, "y": 50}])
    assert got[0] == {"x": 100, "y": 50}
    assert got[1]["y"] >= 76
    assert got[2] == {"x": 300, "y": 50}
