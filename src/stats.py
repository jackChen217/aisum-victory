"""Numbers for /admin/stats: valid users and searches per day, aggregates only.

Three sources, each read with the server's own Google credentials (the Cloud Run
service account; locally, your application-default credentials):

- Firebase Auth user list (Identity Toolkit `accounts:batchGet`, the call the Admin
  SDK's list_users makes; needs firebaseauth.users.get). Gives sign-up times.
  If it can't be read, sign-ups are approximated by each uid's first plan.
- The structured `ghostcart_plan` log lines from src/usage.py (needs
  logging.logEntries.list). Per user, per type. Only exist from the deploy that
  added them.
- Cloud Run's request log for POST /api/plan, for the days before that: daily totals
  only — no uid, no type, teammates included. Kept 30 days by default.

Emails are read only to filter users out, inside this module; nothing that leaves it
names a person. The result is cached for 10 minutes.
"""
import asyncio
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

# Chart annotations, one per day (ET). Add an entry for each launch or push.
EVENTS = [
    # Posted the evening of 11/9.
    {"date": "2026-11-09", "label": "Launch"},
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
    """[{uid, created (datetime), excluded}] — the email never leaves this function."""
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
                "excluded": usage.is_excluded_email(u.get("email")),
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


def aggregate(users: list[dict] | None, events: list[dict] | None,
              request_log: list[datetime] | None, now: datetime,
              errors: dict[str, str]) -> dict:
    """Everything the page draws. `None` for a source that could not be read."""
    start, today = _day(usage.VALID_SINCE), _day(now)
    days = [start + timedelta(n) for n in range((today - start).days + 1)]
    index = {d: i for i, d in enumerate(days)}
    notes = []

    events = events or []
    counted_events = [e for e in events if not e["excluded"] and e["uid"]]

    # Valid users and the day each one signed up.
    if users is not None:
        valid = {u["uid"]: u["created"] for u in users
                 if not u["excluded"] and u["created"] >= usage.VALID_SINCE}
        users_source, approximate = "firebase_auth", False
    else:
        valid = {}
        for e in counted_events:  # ordered by time, so the first one wins
            valid.setdefault(e["uid"], e["time"])
        users_source, approximate = "first_plan", True
        notes.append("Sign-ups are approximate: the user list could not be read, so each "
                     "user is counted from their first plan, and accounts created before "
                     "11/8 13:00 PT can't be told apart.")

    new = [0] * len(days)
    for created in valid.values():
        if _day(created) in index:
            new[index[_day(created)]] += 1
    cumulative, running = [], 0
    for n in new:
        running += n
        cumulative.append(running)

    # Searches per day by type, valid users only, from the structured lines …
    by_type = {s: [0] * len(days) for s in SCENARIOS}
    valid_events = [e for e in counted_events if e["uid"] in valid]
    for e in valid_events:
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

    plans_per_user = Counter(e["uid"] for e in valid_events)
    returning = sum(1 for n in plans_per_user.values() if n >= 2)
    total = len(valid)

    return {
        "generated_at": _iso(now),
        "tz": "ET (UTC−5)",
        "valid_since": _iso(usage.VALID_SINCE),
        "days": [d.isoformat() for d in days],
        "users": {"source": users_source, "approximate": approximate, "total": total,
                  "new": new, "cumulative": cumulative},
        "searches": {**by_type, "request_log": history,
                     "structured_since": structured_since and _iso(structured_since)},
        "returning": {"count": returning, "active": len(plans_per_user), "total": total,
                      "share": returning / total if total else None},
        "team_excluded": bool(usage.team_emails()),
        "events": EVENTS,
        "notes": notes,
        "errors": errors,
    }


# ── Entry point ──────────────────────────────────────────────────────────

async def _collect() -> dict:
    errors: dict[str, str] = {}
    users = events = request_log = None
    try:
        headers = {"Authorization": f"Bearer {await asyncio.to_thread(_access_token)}",
                   "x-goog-user-project": FIREBASE_PROJECT_ID}
    except Exception as e:
        errors["credentials"] = f"No Google credentials: {type(e).__name__}"
        return aggregate(None, None, None, datetime.now(timezone.utc), errors)
    since = usage.VALID_SINCE
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(
            fetch_users(client, headers), fetch_plan_events(client, headers, since),
            fetch_request_log(client, headers, since), return_exceptions=True)
    for name, result in zip(("users", "plan_events", "request_log"), results):
        if isinstance(result, BaseException):
            errors[name] = (str(result) if isinstance(result, SourceError)
                            else f"{type(result).__name__}: {result}")
    users, events, request_log = (None if isinstance(r, BaseException) else r for r in results)
    return aggregate(users, events, request_log, datetime.now(timezone.utc), errors)


async def get_stats() -> dict:
    """Cached for 10 minutes; concurrent requests share one collection."""
    async with _lock:
        if _cache["data"] is None or time.monotonic() >= _cache["until"]:
            _cache["data"] = await _collect()
            _cache["until"] = time.monotonic() + CACHE_SECONDS
        return _cache["data"]
