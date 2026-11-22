"""Check that exported lookboards' QR codes read back as the site address.

Each image is read as saved and again after re-saving as JPEG quality 60 (what a
chat app does to a shared picture). Both must decode to exactly make_qr.URL.

    .venv/bin/python scripts/check_lookboard_qr.py ghostcart-lookboard*.png
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import zxingcpp
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_qr import URL  # noqa: E402


def decode(image: Image.Image) -> list[str]:
    return [r.text for r in zxingcpp.read_barcodes(image.convert("RGB"),
                                                   formats=zxingcpp.BarcodeFormat.QRCode)]


def as_jpeg(image: Image.Image, quality: int = 60) -> Image.Image:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=quality)
    out.seek(0)
    return Image.open(out)


def check(image: Image.Image) -> dict[str, list[str]]:
    """-> what each version decoded to: {"original": [...], "jpeg60": [...]}."""
    return {"original": decode(image), "jpeg60": decode(as_jpeg(image))}


def main(paths: list[str]) -> int:
    bad = 0
    for p in paths:
        found = check(Image.open(p))
        ok = all(v == [URL] for v in found.values())
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {p}: {found}")
    return 1 if bad or not paths else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
