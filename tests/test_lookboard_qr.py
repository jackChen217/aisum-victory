"""The QR code on the Download image lookboard (static/ghostcart-qr.svg).

- the SVG is what scripts/make_qr.py makes from its URL today (no stale file);
- the page prints the same address under the code;
- drawn the way the lookboard draws it (5 px a module, bottom right of a 1200-wide
  board next to a photo), it reads back as the URL, also after JPEG quality 60;
- any real exports in scratch/lookboards/*.png (git-ignored: they show people) pass
  the same check.

Skipped where segno / zxing-cpp are not installed (pip install -r requirements-dev.txt).
"""
import io
import random
import re
import sys
from pathlib import Path

import pytest

segno = pytest.importorskip("segno")
pytest.importorskip("zxingcpp")
from PIL import Image, ImageDraw  # noqa: E402

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_lookboard_qr  # noqa: E402
import make_qr  # noqa: E402

QR_PX = 5


def test_url_is_the_bare_page_address():
    assert make_qr.URL == "https://app-103901851610.us-central1.run.app/ghostcart"


def test_static_svg_is_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(make_qr, "OUT", tmp_path / "qr.svg")
    make_qr.main()
    assert (tmp_path / "qr.svg").read_bytes() == (ROOT / "static" / "ghostcart-qr.svg").read_bytes()


def test_code_is_level_q_with_a_four_module_border():
    qr = segno.make_qr(make_qr.URL, error="q", boost_error=False)
    assert qr.error == "Q"
    modules = qr.symbol_size(scale=1, border=4)[0]
    svg = (ROOT / "static" / "ghostcart-qr.svg").read_text()
    assert f'width="{modules}"' in svg and f'height="{modules}"' in svg
    js = (ROOT / "static" / "ghostcart.js").read_text()
    assert f"const QR_MODULES = {modules};" in js
    assert f"const QR_PX = {QR_PX};" in js
    # At least 160 px even if the 1200-wide board is shrunk to 1080 wide.
    assert modules * QR_PX * 1080 / 1200 >= 160


def test_page_prints_the_same_address():
    js = (ROOT / "static" / "ghostcart.js").read_text()
    assert re.search(r"const SITE_URL = '([^']+)';", js).group(1) == make_qr.URL


def board_like() -> Image.Image:
    """A 1200-wide board: busy 'photo' up top, footer with the code bottom right."""
    rnd = random.Random(7)
    modules = segno.make_qr(make_qr.URL, error="q", boost_error=False).symbol_size(scale=1, border=4)[0]
    size = modules * QR_PX
    board = Image.new("RGB", (1200, 900 + size + 60), "#F8FAFC")
    draw = ImageDraw.Draw(board)
    for _ in range(4000):  # noise where the photo and product tiles go
        x, y = rnd.randrange(0, 1190), rnd.randrange(90, 800)
        draw.rectangle([x, y, x + rnd.randrange(2, 12), y + rnd.randrange(2, 12)],
                       fill=tuple(rnd.randrange(256) for _ in range(3)))
    draw.rectangle([0, 900, 1200, board.height], fill="white")
    qr = segno.make_qr(make_qr.URL, error="q", boost_error=False)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=QR_PX, border=4)
    buf.seek(0)
    board.paste(Image.open(buf).convert("RGB"), (1200 - 40 - size, 930))
    return board


def test_drawn_code_reads_back_also_as_jpeg_60():
    found = check_lookboard_qr.check(board_like())
    assert found == {"original": [make_qr.URL], "jpeg60": [make_qr.URL]}


EXPORTS = sorted((ROOT / "scratch" / "lookboards").glob("*.png"))


@pytest.mark.skipif(not EXPORTS, reason="no exports in scratch/lookboards/")
@pytest.mark.parametrize("path", EXPORTS, ids=[p.name for p in EXPORTS])
def test_real_export_reads_back(path):
    found = check_lookboard_qr.check(Image.open(path))
    assert found == {"original": [make_qr.URL], "jpeg60": [make_qr.URL]}
