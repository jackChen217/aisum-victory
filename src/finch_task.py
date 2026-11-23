"""Finch bounty task: where a user came from, and a completion code to submit on Finch.

1. /ghostcart?src=finch: the page keeps "finch" in localStorage (it survives sign-in)
   and, once signed in, sends it to POST /api/me/source. The first source an account
   arrives with is recorded once (store key finch/source/<uid>) and logged once as
   `ghostcart_source` (uid + source, never an email). /admin/stats counts new users by
   source from those lines.
2. An account whose source is finch, after one successful Outfit plan (own photo or a
   gallery look), is marked done (finch/done/<uid>, logged once as
   `ghostcart_finch_task`). GET /api/me/finch-code then returns its code.
3. Code = "GC-" + 6 characters of HMAC-SHA256(FINCH_CODE_SECRET, uid): the same for the
   same user every time, unguessable without the secret. No secret -> no codes.
4. Admins paste codes on /admin/stats; each is valid if it is the code of an account
   marked done. Back come valid / invalid, how often each code was pasted, and for a
   valid code whether its account is a new user Finch brought (source recorded within
   stats.SOURCE_WINDOW of the account being created) or an existing account. Never
   whose code it is.

Records are create-only objects in the upload bucket on Cloud Run (prefix finch/,
which the bucket's lifecycle rules do not delete), a local folder otherwise.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

from . import gallery, stats, usage

log = logging.getLogger(__name__)

SOURCES = ("finch",)            # accepted ?src= values; anything else is ignored
SECRET_ENV = "FINCH_CODE_SECRET"
CODE_PREFIX = "GC-"
CODE_LEN = 6                    # 32^6 ≈ 1.07e9 codes
# No 0/O or 1/I: codes are read off a screen and typed.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_VERIFY = 500

_source_cache: dict[str, str | None] = {}   # uid -> recorded source (None = none)


def _source_key(uid: str) -> str:
    return f"finch/source/{uid}"


def _done_key(uid: str) -> str:
    return f"finch/done/{uid}"


# ── codes ────────────────────────────────────────────────────────────────

def _secret() -> bytes | None:
    s = os.environ.get(SECRET_ENV, "").strip()
    return s.encode() if s else None


def code_for(uid: str, secret: bytes | None = None) -> str | None:
    secret = secret or _secret()
    if not secret:
        return None
    digest = int.from_bytes(hmac.new(secret, uid.encode(), hashlib.sha256).digest(), "big")
    chars = []
    for _ in range(CODE_LEN):
        digest, i = divmod(digest, len(ALPHABET))
        chars.append(ALPHABET[i])
    return CODE_PREFIX + "".join(chars)


def normalize(code: str) -> str:
    """"gc-7k3p9q", " GC 7K3P9Q " -> "GC-7K3P9Q"."""
    raw = re.sub(r"[\s-]", "", (code or "").upper())
    if raw.startswith("GC"):
        raw = raw[2:]
    return CODE_PREFIX + raw


# ── records ──────────────────────────────────────────────────────────────

async def source_of(uid: str) -> str | None:
    if uid not in _source_cache:
        text = await gallery.store().read(_source_key(uid))
        _source_cache[uid] = json.loads(text)["source"] if text else None
    return _source_cache[uid]


async def record_source(uid: str, source: str) -> dict:
    """Keep the first source this account arrived with. -> {"source", "recorded"}:
    recorded is True only the first time (that is when the log line is written)."""
    if source not in SOURCES:
        raise ValueError(source)
    existing = await source_of(uid)
    if existing:
        return {"source": existing, "recorded": False}
    created = await gallery.store().create(
        _source_key(uid), json.dumps({"source": source, "at": time.time()}))
    if created:
        _source_cache[uid] = source
        usage.log_source(uid, source)
        return {"source": source, "recorded": True}
    _source_cache.pop(uid, None)          # another request won the race: read theirs
    return {"source": await source_of(uid), "recorded": False}


async def mark_done(uid: str, how: str) -> bool:
    """After a successful Outfit plan. Only for accounts whose source is finch; logged
    once. Never raises: a record failure must not cost the user their plan."""
    try:
        if await source_of(uid) != "finch":
            return False
        created = await gallery.store().create(
            _done_key(uid), json.dumps({"at": time.time(), "how": how}))
        if created:
            usage.log_finch_task(uid, how)
        return True
    except Exception as e:
        log.warning("finch task record failed: %s: %s", type(e).__name__, e)
        return False


async def is_done(uid: str) -> bool:
    return await gallery.store().read(_done_key(uid)) is not None


async def code_if_done(uid: str) -> str | None:
    if not _secret() or await source_of(uid) != "finch" or not await is_done(uid):
        return None
    return code_for(uid)


async def _category(uid: str, created: dict | None) -> str:
    """"finch_new" / "existing" by the stats page's rule; "unknown" when the account's
    creation time can't be read (no user list)."""
    if not created or uid not in created:
        return "unknown"
    text = await gallery.store().read(_source_key(uid))
    if not text:
        return "unknown"
    at = datetime.fromtimestamp(json.loads(text)["at"], timezone.utc)
    return "finch_new" if at - created[uid] <= stats.SOURCE_WINDOW else "existing"


async def verify(codes: list[str], created: dict | None = None) -> dict:
    """Check pasted codes. `created`: {uid: account creation time} from the Firebase
    user list, None if it couldn't be read.

    -> {"results": [{"code", "valid", "count", "duplicate", "category"}], "total",
        "distinct", "valid", "invalid", "duplicates", "categories", "users_available"}.
    Every pasted line keeps its row; `count` is how often that code was pasted.
    valid / invalid / categories count distinct codes. No uid or email in the result."""
    secret = _secret()
    if not secret:
        raise RuntimeError(f"{SECRET_ENV} is not set")
    owner = {code_for(uid, secret): uid for uid in await gallery.store().names("finch/done/")}
    pasted = [normalize(c) for c in codes]
    counts = Counter(pasted)
    category = {c: await _category(owner[c], created) for c in counts if c in owner}
    results = [{"code": c, "valid": c in owner, "count": counts[c],
                "duplicate": counts[c] > 1, "category": category.get(c)} for c in pasted]
    valid = sum(1 for c in counts if c in owner)
    return {"results": results, "total": len(pasted), "distinct": len(counts),
            "valid": valid, "invalid": len(counts) - valid,
            "duplicates": sum(1 for n in counts.values() if n > 1),
            "categories": dict(Counter(category.values())),
            "users_available": created is not None}


def reset() -> None:
    """Tests only."""
    _source_cache.clear()
