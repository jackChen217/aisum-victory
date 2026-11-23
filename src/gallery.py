"""Gallery looks: cached plans that open instantly, refreshed by real AISUM calls.

- The looks are listed in static/gallery.json (the page reads the same file).
- Each look's last plan (the whole session: slots, candidates, scores) is stored, so
  opening it is a re-optimise (`orchestrator.replan`) with no search.
- Opening a look also starts one background refresh: a real AISUM run whose result
  replaces the stored one for the next viewer. It never changes what this user sees.
  Background work only uses AISUM slots nobody is waiting for (src/agent/limits.py),
  at most one runs at a time, and a look is refreshed at most every REFRESH_EVERY_S.
- A user's own search (/api/plan, /api/upload) cancels that user's refresh first.
- Outfit refresh = the normal outfit plan. Desk / Room refresh redoes only the text
  searches, keeping products that already passed the category check (no LLM call);
  the LLM parts are redone by scripts/precompute_gallery.py.
- A look with nothing stored yet is planned live when opened (the user waits).

Storage: a local folder (scratch/local-store/, not in git) when not on Cloud Run; the
upload bucket on Cloud Run, create-only objects:
    gallery/<look_id>/<epoch_ms>.json   newest wins
    onboarding/<uid>                    the first-visit tour was finished or skipped
    finch/source/<uid>, finch/done/<uid>  Finch task records (src/finch_task.py)
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from pathlib import Path

from . import uploads, usage
from .agent import limits, orchestrator, planner
from .agent.optimizer import optimize
from .config import ROOT

log = logging.getLogger(__name__)

CATALOG_PATH = ROOT / "static" / "gallery.json"
LOCAL_STORE = ROOT / "scratch" / "local-store"
STALE_AFTER_S = 24 * 3600
REFRESH_EVERY_S = 10 * 60      # per look: repeated clicks do not stack refreshes
GALLERY_UID = "_gallery"       # sessions built by refreshes / precompute


class UnknownLook(KeyError):
    pass


# ── catalog ──────────────────────────────────────────────────────────────

_catalog: dict | None = None


def catalog() -> dict:
    global _catalog
    if _catalog is None:
        _catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog


def look(look_id: str) -> dict:
    for item in catalog()["looks"]:
        if item["id"] == look_id:
            return item
    raise UnknownLook(look_id)


def budget_for(item: dict) -> float:
    return float(catalog()["budgets"][item["category"]])


def photo_url(item: dict, width: int = 1080) -> str:
    """The look's photo on Unsplash (default) or Pexels ("source": "pexels"); the page
    builds the same URLs (photo() in static/ghostcart.js)."""
    if item.get("source") == "pexels":
        pid = item["photo"]
        return (f"https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg"
                f"?auto=compress&cs=tinysrgb&w={width}")
    return f"https://images.unsplash.com/{item['photo']}?w={width}&q=85&fm=jpg"


# ── storage ──────────────────────────────────────────────────────────────

class LocalStore:
    """Files under scratch/local-store/ — local runs only."""

    kind = "local folder"

    def __init__(self, root: Path = LOCAL_STORE):
        self.root = root

    async def latest(self, look_id: str) -> dict | None:
        files = sorted((self.root / "gallery" / look_id).glob("*.json"))
        return json.loads(files[-1].read_text()) if files else None

    async def put(self, look_id: str, doc: dict) -> None:
        folder = self.root / "gallery" / look_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{int(time.time() * 1000)}.json").write_text(json.dumps(doc))
        for old in sorted(folder.glob("*.json"))[:-3]:   # keep the last three
            old.unlink()

    async def seen(self, uid: str) -> bool:
        return (self.root / "onboarding" / uid).exists()

    async def mark_seen(self, uid: str) -> None:
        (self.root / "onboarding").mkdir(parents=True, exist_ok=True)
        (self.root / "onboarding" / uid).write_text(str(int(time.time())))

    async def forget_seen(self, uid: str) -> None:
        (self.root / "onboarding" / uid).unlink(missing_ok=True)

    # Small create-once records (src/finch_task.py): key -> text.
    async def read(self, key: str) -> str | None:
        f = self.root / key
        return f.read_text() if f.is_file() else None

    async def create(self, key: str, text: str) -> bool:
        """False if the key already exists (never overwrites)."""
        f = self.root / key
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(f, "x") as out:
                out.write(text)
        except FileExistsError:
            return False
        return True

    async def names(self, prefix: str) -> list[str]:
        """Keys under the folder `prefix` (ending in "/"), without the prefix."""
        folder = self.root / prefix
        return sorted(x.name for x in folder.iterdir() if x.is_file()) if folder.is_dir() else []


class BucketStore:
    """The upload bucket on Cloud Run. Objects are only ever created, never overwritten
    (the service account's roles there are create + read)."""

    kind = "Cloud Storage"

    def __init__(self, bucket: str):
        self.bucket = bucket

    def _b(self):
        return uploads._bucket(self.bucket)

    async def latest(self, look_id: str) -> dict | None:
        def read():
            blobs = list(self._b().list_blobs(prefix=f"gallery/{look_id}/"))
            if not blobs:
                return None
            return json.loads(max(blobs, key=lambda b: b.name).download_as_bytes())
        return await asyncio.to_thread(read)

    async def put(self, look_id: str, doc: dict) -> None:
        def write():
            blob = self._b().blob(f"gallery/{look_id}/{int(time.time() * 1000)}.json")
            blob.upload_from_string(json.dumps(doc), content_type="application/json",
                                    if_generation_match=0)
        await asyncio.to_thread(write)

    async def seen(self, uid: str) -> bool:
        return await asyncio.to_thread(lambda: self._b().blob(f"onboarding/{uid}").exists())

    async def mark_seen(self, uid: str) -> None:
        def write():
            from google.api_core.exceptions import PreconditionFailed
            try:
                self._b().blob(f"onboarding/{uid}").upload_from_string(
                    "1", content_type="text/plain", if_generation_match=0)
            except PreconditionFailed:
                pass  # already recorded
        await asyncio.to_thread(write)

    async def forget_seen(self, uid: str) -> None:
        raise PermissionError("Resetting the tour is local-only.")

    async def read(self, key: str) -> str | None:
        def get():
            from google.api_core.exceptions import NotFound
            try:
                return self._b().blob(key).download_as_text()
            except NotFound:
                return None
        return await asyncio.to_thread(get)

    async def create(self, key: str, text: str) -> bool:
        def write():
            from google.api_core.exceptions import PreconditionFailed
            try:
                self._b().blob(key).upload_from_string(text, content_type="application/json",
                                                       if_generation_match=0)
                return True
            except PreconditionFailed:
                return False
        return await asyncio.to_thread(write)

    async def names(self, prefix: str) -> list[str]:
        def ls():
            return sorted(b.name[len(prefix):] for b in self._b().list_blobs(prefix=prefix))
        return await asyncio.to_thread(ls)


_store = None


def store():
    global _store
    if _store is None:
        name = uploads.bucket_name()
        _store = BucketStore(name) if uploads.on_cloud_run() and name else LocalStore()
    return _store


# ── building a look's plan ───────────────────────────────────────────────

def _take_session(session_id: str) -> dict:
    return orchestrator.SESSIONS.pop((GALLERY_UID, session_id))


def _doc(item: dict, session: dict, how: str) -> dict:
    return {"look_id": item["id"], "category": item["category"], "built_at": time.time(),
            "how": how, "session": session}


async def build_full(item: dict) -> dict:
    """The whole normal plan for a look (Desk/Room include the LLM calls)."""
    data = await orchestrator.plan(GALLERY_UID, photo_url(item), item["category"],
                                   budget_for(item), gender=item.get("gender"))
    return _doc(item, _take_session(data["session_id"]), "full")


async def build_refresh(item: dict, previous: dict | None) -> dict:
    """A refresh: outfit = the normal plan; desk/room = text searches only."""
    if item["category"] == "outfit" or previous is None:
        return await build_full(item)
    old = previous["session"]
    slots = old["slots"]
    fresh = await planner.find_candidates(slots, old["search_budget"])
    candidates = {}
    for s in slots:
        known = {p.get("p_key") for p in old["candidates"].get(s["slot"], [])}
        kept = [p for p in fresh.get(s["slot"], []) if p.get("p_key") in known]
        # Only products that already passed the category check; nothing found -> keep old.
        candidates[s["slot"]] = kept or old["candidates"].get(s["slot"], [])
    candidates, scores = orchestrator._text_ranked(slots, candidates)
    quality = {k: [m["quality"] for m in v] for k, v in scores.items()}
    result = optimize(slots, candidates, old["search_budget"], [], quality)
    session = {**copy.deepcopy(old), "candidates": candidates, "scores": scores,
               "plan": result["plan"], "budget": old["search_budget"], "owned": [],
               "pins": {}}
    session.pop("touched", None)
    return _doc(item, session, "refresh")


# ── opening a look ───────────────────────────────────────────────────────

async def open_look(uid: str, look_id: str, claims: dict | None = None,
                    refresh: bool = True) -> dict:
    """-> orchestrator.replan output for a new session of this user, plus "gallery".
    Runs a live plan when nothing is stored yet. refresh=False: no background refresh
    (the page is only restoring a look it showed before)."""
    item = look(look_id)
    doc = await store().latest(look_id)
    trigger = "gallery_view"
    if doc is None:
        trigger = "gallery_live"
        doc = await build_full(item)
        await store().put(look_id, doc)
    session = copy.deepcopy(doc["session"])
    session_id = uuid.uuid4().hex
    orchestrator._save(uid, session_id, session)
    data = await orchestrator.replan(uid, session_id, session["search_budget"],
                                     owned=[], pins={})
    data["scenario"] = session["scenario"]
    age = time.time() - doc["built_at"]
    data["gallery"] = {"look_id": look_id, "built_at": doc["built_at"],
                       "age_s": round(age), "stale": age > STALE_AFTER_S,
                       "cached": trigger == "gallery_view"}
    usage.log_gallery(uid, look_id, item["category"], trigger, "ok", claims)
    if trigger == "gallery_view" and refresh:
        start_refresh(uid, item, claims)
    return data


# ── background refresh ───────────────────────────────────────────────────

_tasks: dict[str, asyncio.Task] = {}      # uid -> that user's refresh
_last_start: dict[str, float] = {}         # look_id -> when its last refresh started
_one_at_a_time = asyncio.Lock()


def start_refresh(uid: str, item: dict, claims: dict | None = None) -> str:
    """Start a refresh for this look unless one ran recently or another is running.
    Returns what happened: "started", "recent" or "busy"."""
    now = time.monotonic()
    if now - _last_start.get(item["id"], -REFRESH_EVERY_S) < REFRESH_EVERY_S:
        usage.log_gallery(uid, item["id"], item["category"], "gallery_refresh", "skipped_recent", claims)
        return "recent"
    if _one_at_a_time.locked():
        usage.log_gallery(uid, item["id"], item["category"], "gallery_refresh", "skipped_busy", claims)
        return "busy"
    _last_start[item["id"]] = now
    cancel_for(uid)
    _tasks[uid] = asyncio.create_task(_refresh(uid, item, claims))
    return "started"


async def _refresh(uid: str, item: dict, claims: dict | None) -> None:
    outcome = "error"
    try:
        async with _one_at_a_time:
            with limits.background() as state:
                previous = await store().latest(item["id"])
                doc = await build_refresh(item, previous)
            if state["refused"]:
                outcome = "skipped_busy"   # a search was refused a slot: incomplete
                return
            await store().put(item["id"], doc)
            outcome = "ok"
    except limits.Busy:
        outcome = "skipped_busy"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as e:  # a refresh must never hurt the user's page
        log.warning("gallery refresh %s failed: %s: %s", item["id"], type(e).__name__, e)
    finally:
        if _tasks.get(uid) is asyncio.current_task():
            del _tasks[uid]
        usage.log_gallery(uid, item["id"], item["category"], "gallery_refresh", outcome, claims)


def cancel_for(uid: str) -> bool:
    """The user's own search comes first: drop their running refresh, if any."""
    task = _tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


def reset() -> None:
    """Tests only."""
    global _store, _catalog, _one_at_a_time
    _store, _catalog = None, None
    _tasks.clear()
    _last_start.clear()
    _one_at_a_time = asyncio.Lock()
