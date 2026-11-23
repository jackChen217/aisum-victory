"""Numbers for /admin/stats: valid users and searches per day, aggregates only.

Three sources, each read with the server's own Google credentials (the Cloud Run
service account; locally, your application-default credentials):

- Firebase Auth user list (Identity Toolkit `accounts:batchGet`, the call the Admin
  SDK's list_users makes; needs firebaseauth.users.get). Gives sign-up times.
  If it can't be read, the page shows no user numbers until it can.
- The structured `ghostcart_plan` log lines from src/usage.py (needs
  logging.logEntries.list). Per user, per type. Only exist from the deploy that
  added them.
- Cloud Run's request log for POST /api/plan, for the days before that: daily totals
  only — no uid, no type, teammates included. Kept 30 days by default.
- The `ghostcart_source` and `ghostcart_finch_task` lines (src/finch_task.py): new
  users by where they came from (?src=finch), and Finch tasks completed.

Emails are read only to filter users out, inside this module; nothing that leaves it
names a person. The result is cached for 10 minutes.
"""
import asyncio
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import httpx

from . import usage
from .config import FIREBASE_PROJECT_ID

CACHE_SECONDS = 600
# Days are counted in US Eastern time, the contest's clock. The whole scoring window
# (11/8 – 12/3) is standard time, so a fixed offset is exact and needs no tz database.
TZ = timezone(timedelta(hours=-5), "ET")
SCENARIOS = ("outfit", "desk", "room")

# Logs are read from the first day of the contest (11/7 00:00 ET); the chart axis
# starts on the day of the first sign-up.
LOG_SINCE = datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc)

# Chart annotations, one per day (ET). Add an entry for each launch or push.
EVENTS = [
    # Scoring started 11/8 4 PM ET.
    {"date": "2026-11-08", "label": "Launch"},
]

_cache: dict = {"until": 0.0, "data": None}
_lock = asyncio.Lock()


class SourceError(Exception):
    """One data source could not be read; the page says which and why."""


# ── Google calls ─────────────────────────────────────────────────────────

def _access_token() -> str:
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        try:
            message = r.json()["error"]["message"]
        except Exception:
            message = r.text[:200]
        raise SourceError(f"{what}: HTTP {r.status_code}: {message}")


async def fetch_users(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    """[{uid, created, aisum, team}] — the email never leaves this function."""
    url = (f"https://identitytoolkit.googleapis.com/v1/projects/"
           f"{FIREBASE_PROJECT_ID}/accounts:batchGet")
    users, token = [], None
    while True:
        params = {"maxResults": 1000, **({"nextPageToken": token} if token else {})}
        r = await client.get(url, params=params, headers=headers)
        _raise_for(r, "Firebase Auth user list")
        body = r.json()
        for u in body.get("users", []):
            users.append({
                "uid": u["localId"],
                "created": datetime.fromtimestamp(int(u["createdAt"]) / 1000, timezone.utc),
                "aisum": usage.is_aisum_email(u.get("email")),
                "team": usage.is_team_email(u.get("email")),
            })
        token = body.get("nextPageToken")
        if not token:
            return users


async def _log_entries(client, headers, filter_: str, fields: str) -> list[dict]:
    entries, token = [], None
    while True:
        body = {"resourceNames": [f"projects/{FIREBASE_PROJECT_ID}"], "filter": filter_,
                "orderBy": "timestamp asc", "pageSize": 1000}
        if token:
            body["pageToken"] = token
        r = await client.post("https://logging.googleapis.com/v2/entries:list",
                              params={"fields": f"entries({fields}),nextPageToken"},
                              json=body, headers=headers)
        _raise_for(r, "Cloud Logging")
        data = r.json()
        entries += data.get("entries", [])
        token = data.get("nextPageToken")
        if not token:
            return entries


async def fetch_plan_events(client, headers, since: datetime) -> list[dict]:
    """The structured lines from src/usage.py: [{time, uid, scenario, excluded}]."""
    rows = await _log_entries(
        client, headers,
        f'resource.type="cloud_run_revision" AND jsonPayload.event="{usage.PLAN_EVENT}" '
        f'AND timestamp>="{_iso(since)}"',
        "timestamp,jsonPayload")
    return [{"time": _parse(e["timestamp"]), "uid": e["jsonPayload"].get("uid"),
             "scenario": e["jsonPayload"].get("scenario"),
             "excluded": bool(e["jsonPayload"].get("excluded"))} for e in rows]


async def fetch_events(client, headers, event: str, since: datetime) -> list[dict]:
    """[{time, uid, source}] for one structured event (source lines, task lines)."""
    rows = await _log_entries(
        client, headers,
        f'resource.type="cloud_run_revision" AND jsonPayload.event="{event}" '
        f'AND timestamp>="{_iso(since)}"',
        "timestamp,jsonPayload")
    return [{"time": _parse(e["timestamp"]), "uid": e["jsonPayload"].get("uid"),
             "source": e["jsonPayload"].get("source")} for e in rows]


async def fetch_request_log(client, headers, since: datetime) -> list[datetime]:
    """Times of signed-in POST /api/plan requests in Cloud Run's request log."""
    rows = await _log_entries(
        client, headers,
        'resource.type="cloud_run_revision" AND log_id("run.googleapis.com/requests") '
        'AND httpRequest.requestMethod="POST" AND httpRequest.requestUrl:"/api/plan" '
        f'AND NOT httpRequest.status=401 AND timestamp>="{_iso(since)}"',
        "timestamp")
    return [_parse(e["timestamp"]) for e in rows]


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    # Cloud Logging gives nanoseconds; fromisoformat takes at most microseconds.
    head, _, frac = s.rstrip("Z").partition(".")
    return datetime.fromisoformat(head + ("." + frac[:6] if frac else "")).replace(
        tzinfo=timezone.utc)


# ── Aggregation (pure) ───────────────────────────────────────────────────

def _day(t: datetime) -> date:
    return t.astimezone(TZ).date()


def _per_day(times, days) -> tuple[list[int], list[int]]:
    """New per day and the running total. Anything before the first day counts on it."""
    index = {d: i for i, d in enumerate(days)}
    new = [0] * len(days)
    for t in times:
        d = max(_day(t), days[0])
        if d in index:
            new[index[d]] += 1
    cumulative, running = [], 0
    for n in new:
        running += n
        cumulative.append(running)
    return new, cumulative


# A source counts for a new user only if it was recorded this soon after the account
# was made (the page sends it right after the first sign-in). Older accounts that
# arrive through a source later are counted as existing, not new.
SOURCE_WINDOW = timedelta(hours=1)


def by_source(official: dict, sources: list[dict] | None,
              tasks: list[dict] | None) -> dict:
    """New official users per source ("direct" = no source), existing users who came
    back through a source, and Finch tasks completed (official users only)."""
    if sources is None:
        return {"available": False}
    first: dict[str, dict] = {}
    for e in sorted(sources, key=lambda e: e["time"]):
        if e["uid"] and e["source"]:
            first.setdefault(e["uid"], e)
    new, existing = Counter(), Counter()
    for uid, created in official.items():
        e = first.get(uid)
        if e and e["time"] - created <= SOURCE_WINDOW:
            new[e["source"]] += 1
        else:
            new["direct"] += 1
            if e:
                existing[e["source"]] += 1
    done = {e["uid"] for e in tasks or [] if e["uid"] in official}
    return {"available": True, "new_users": dict(new), "existing_users": dict(existing),
            "finch_completed": len(done) if tasks is not None else None}


def aggregate(users: list[dict] | None, events: list[dict] | None,
              request_log: list[datetime] | None, now: datetime,
              errors: dict[str, str], sources: list[dict] | None = None,
              tasks: list[dict] | None = None) -> dict:
    """Everything the page draws. `None` for a source that could not be read.

    Two user counts:
    - all sign-ups: every account except @aisum.com;
    - official (estimate of the organisers' rule): created on or after 11/8 13:00 PT,
      not @aisum.com, and not on TEAM_EMAILS (unset for now, so teammates count).
    """
    notes = []
    events = events or []
    counted_events = [e for e in events if not e["excluded"] and e["uid"]]

    signups = [u for u in users or [] if not u["aisum"]]
    official = {u["uid"]: u["created"] for u in signups
                if not u["team"] and u["created"] >= usage.VALID_SINCE}

    # The axis starts on the first sign-up's day (or 11/8 when there is no user list).
    firsts = [_day(u["created"]) for u in signups] + [_day(usage.VALID_SINCE)]
    start, today = min(firsts), _day(now)
    days = [start + timedelta(n) for n in range((today - start).days + 1)]
    index = {d: i for i, d in enumerate(days)}

    all_new, all_cum = _per_day([u["created"] for u in signups], days)
    off_new, off_cum = _per_day(official.values(), days)

    # Searches per day by type from the structured lines (teammates on TEAM_EMAILS and
    # @aisum.com left out) …
    by_type = {s: [0] * len(days) for s in SCENARIOS}
    for e in counted_events:
        d = _day(e["time"])
        if d in index and e["scenario"] in by_type:
            by_type[e["scenario"]][index[d]] += 1
    # … and before the first structured line, request-log totals (no type, no uid).
    structured_since = min((e["time"] for e in events), default=None)
    history = [0] * len(days)
    for t in request_log or []:
        if (structured_since is None or t < structured_since) and _day(t) in index:
            history[index[_day(t)]] += 1
    if request_log is not None and any(history):
        notes.append("Searches before per-user logging started are daily totals from "
                     "Cloud Run's request log: no type, no user, teammates included.")

    # Returning: official users with two or more plans.
    plans_per_user = Counter(e["uid"] for e in counted_events if e["uid"] in official)
    returning = sum(1 for n in plans_per_user.values() if n >= 2)
    total = len(official)

    return {
        "generated_at": _iso(now),
        "tz": "ET (UTC−5)",
        "valid_since": _iso(usage.VALID_SINCE),
        "days": [d.isoformat() for d in days],
        "users": {
            "available": users is not None,
            "all": {"total": len(signups), "new": all_new, "cumulative": all_cum},
            "official": {"total": total, "new": off_new, "cumulative": off_cum},
        },
        "searches": {**by_type, "request_log": history,
                     "structured_since": structured_since and _iso(structured_since)},
        "returning": {"count": returning, "active": len(plans_per_user), "total": total,
                      "share": returning / total if total else None},
        "sources": (by_source(official, sources, tasks) if users is not None
                    else {"available": False}),
        "team_excluded": bool(usage.team_emails()),
        "events": EVENTS,
        "notes": notes,
        "errors": errors,
    }


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def log_source_error(source: str, message: str) -> None:
    """One JSON line per failed source, so failures can be found in Cloud Logging.
    Anything shaped like an email is masked, in case an upstream message carries one."""
    print(json.dumps({"severity": "WARNING", "message": f"admin_stats {source} failed",
                      "event": "admin_stats_source_error", "source": source,
                      "error": _EMAIL.sub("<email>", message)[:500]}),
          file=sys.stdout, flush=True)


# ── Entry point ──────────────────────────────────────────────────────────

async def _collect() -> dict:
    errors: dict[str, str] = {}
    users = events = request_log = None
    try:
        headers = {"Authorization": f"Bearer {await asyncio.to_thread(_access_token)}",
                   "x-goog-user-project": FIREBASE_PROJECT_ID}
    except Exception as e:
        errors["credentials"] = f"No Google credentials: {type(e).__name__}"
        log_source_error("credentials", errors["credentials"])
        return aggregate(None, None, None, datetime.now(timezone.utc), errors)
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(
            fetch_users(client, headers), fetch_plan_events(client, headers, LOG_SINCE),
            fetch_request_log(client, headers, LOG_SINCE),
            fetch_events(client, headers, usage.SOURCE_EVENT, LOG_SINCE),
            fetch_events(client, headers, usage.FINCH_TASK_EVENT, LOG_SINCE),
            return_exceptions=True)
    names = ("users", "plan_events", "request_log", "sources", "finch_tasks")
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            errors[name] = (str(result) if isinstance(result, SourceError)
                            else f"{type(result).__name__}: {result}")
            log_source_error(name, errors[name])
    users, events, request_log, sources, tasks = (
        None if isinstance(r, BaseException) else r for r in results)
    return aggregate(users, events, request_log, datetime.now(timezone.utc), errors,
                     sources, tasks)


async def created_times() -> dict | None:
    """{uid: account creation time} from the Firebase user list, None if it can't be
    read (for the Finch code check; emails never leave fetch_users)."""
    try:
        headers = {"Authorization": f"Bearer {await asyncio.to_thread(_access_token)}",
                   "x-goog-user-project": FIREBASE_PROJECT_ID}
        async with httpx.AsyncClient(timeout=30) as client:
            users = await fetch_users(client, headers)
    except Exception as e:
        log_source_error("users", str(e) if isinstance(e, SourceError) else type(e).__name__)
        return None
    return {u["uid"]: u["created"] for u in users}


async def get_stats() -> dict:
    """Cached for 10 minutes; concurrent requests share one collection."""
    async with _lock:
        if _cache["data"] is None or time.monotonic() >= _cache["until"]:
            _cache["data"] = await _collect()
            _cache["until"] = time.monotonic() + CACHE_SECONDS
        return _cache["data"]
