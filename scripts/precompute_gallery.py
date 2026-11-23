"""Build the stored plan of every gallery look that is missing or older than 24 hours.

One look at a time, with the full normal pipeline (Desk/Room include the DeepSeek
calls: slot split and category check, about 30–40 s each). Each build is logged as
trigger "precompute".

    .venv/bin/python scripts/precompute_gallery.py                 # local folder store
    .venv/bin/python scripts/precompute_gallery.py --category desk room
    .venv/bin/python scripts/precompute_gallery.py --only navy-suit --force
    .venv/bin/python scripts/precompute_gallery.py --store bucket  # the Cloud Run store

Without --store bucket it writes scratch/local-store/ (what a local server reads).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import gallery, uploads, usage  # noqa: E402
from src.agent import planner  # noqa: E402


async def main(args) -> int:
    if args.store == "bucket":
        name = uploads.bucket_name()
        if not name:
            print("UPLOAD_BUCKET is not set", file=sys.stderr)
            return 2
        gallery._store = gallery.BucketStore(name)
    store = gallery.store()
    looks = [x for x in gallery.catalog()["looks"]
             if (not args.category or x["category"] in args.category)
             and (not args.only or x["id"] in args.only)]
    print(f"{len(looks)} looks -> {store.kind}")
    failed = 0
    for item in looks:
        doc = await store.latest(item["id"])
        age = time.time() - doc["built_at"] if doc else None
        if doc and not args.force and age < gallery.STALE_AFTER_S:
            print(f"  {item['id']:22} fresh ({age / 3600:.1f} h old), skipped")
            continue
        started = time.perf_counter()
        try:
            new = await gallery.build_full(item)
            await store.put(item["id"], new)
            n = len(new["session"]["plan"])
            print(f"  {item['id']:22} built in {time.perf_counter() - started:.1f} s, {n} items")
            usage.log_gallery("_precompute", item["id"], item["category"], "precompute", "ok")
        except Exception as e:
            failed += 1
            print(f"  {item['id']:22} FAILED: {type(e).__name__}: {e}")
            usage.log_gallery("_precompute", item["id"], item["category"], "precompute", "error")
    await planner.aclose()
    return 1 if failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--category", nargs="*", choices=["outfit", "desk", "room"])
    p.add_argument("--only", nargs="*", help="look ids")
    p.add_argument("--force", action="store_true", help="rebuild even if fresh")
    p.add_argument("--store", choices=["local", "bucket"], default="local")
    sys.exit(asyncio.run(main(p.parse_args())))
