"""Regenerate static/ghostcart-qr.svg, the QR code on the Download image lookboard.

The code holds the page address and nothing else (no query parameters). Error
correction Q (about 25% of the code can be damaged), 4-module white border, black on
white. When the address changes, edit URL below and run:

    .venv/bin/python -m pip install -r requirements-dev.txt   # segno, once
    .venv/bin/python scripts/make_qr.py

then check the result: .venv/bin/python -m pytest tests/test_lookboard_qr.py
(the test reads the code back and compares it with URL in static/ghostcart.js too).
"""
from pathlib import Path

import segno

URL = "https://app-103901851610.us-central1.run.app/ghostcart"
OUT = Path(__file__).resolve().parent.parent / "static" / "ghostcart-qr.svg"


def main() -> None:
    # error="q" with boost_error=False: exactly Q, not raised to H when it would fit.
    qr = segno.make_qr(URL, error="q", boost_error=False)
    # scale 1, so the SVG's width/height equal the module count; the page scales it by
    # a whole number of pixels per module, which keeps every edge sharp.
    qr.save(OUT, kind="svg", scale=1, border=4, dark="#000", light="#fff",
            xmldecl=False, svgns=True, nl=True)
    print(f"{OUT.name}: version {qr.version}, error {qr.error}, "
          f"{qr.symbol_size(scale=1, border=4)[0]} modules with border")


if __name__ == "__main__":
    main()
