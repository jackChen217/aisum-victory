"""Who counts as a real user, and one log line per plan for the stats page.

Contest scoring counts distinct Google accounts that are not on the team, so the stats
page (src/stats.py) needs per-user, per-day usage. Cloud Run's own request log has no
uid and no request body, so `/api/plan` writes one structured line per call:

    {"event": "ghostcart_plan", "uid": "...", "scenario": "outfit", "excluded": false,
     "status": 200, "time": "2026-11-18T15:04:05Z"}

Never an email or a name. `excluded` says whether the caller was on TEAM_EMAILS or an
@aisum.com account at the time of the call, so the counts can leave them out even
when the user list can't be read.

Cloud Run turns a JSON line on stdout into a log entry's jsonPayload.
"""
import json
import os
import sys
from datetime import datetime, timezone

PLAN_EVENT = "ghostcart_plan"

# Who may open /admin/stats. Comma-separated, verified Google emails.
ADMIN_EMAILS_ENV = "ADMIN_EMAILS"
# Who is left out of the counts as a teammate. Comma-separated emails. Unset for now:
# teammates are counted like everyone else. Set it on Cloud Run to exclude them again.
TEAM_EMAILS_ENV = "TEAM_EMAILS"
EXCLUDED_DOMAIN = "@aisum.com"
# Accounts made before scoring started don't count: 2026-11-08 13:00 PT (PST, UTC-8).
VALID_SINCE = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)


def _emails(env: str) -> set[str]:
    raw = os.environ.get(env, "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def admin_emails() -> set[str]:
    return _emails(ADMIN_EMAILS_ENV)


def team_emails() -> set[str]:
    return _emails(TEAM_EMAILS_ENV)


def is_admin(claims: dict) -> bool:
    email = (claims.get("email") or "").lower()
    return bool(email) and claims.get("email_verified") is True and email in admin_emails()


def is_aisum_email(email: str | None) -> bool:
    return (email or "").lower().endswith(EXCLUDED_DOMAIN)


def is_team_email(email: str | None) -> bool:
    return (email or "").lower() in team_emails()


def is_excluded_email(email: str | None) -> bool:
    return is_aisum_email(email) or is_team_email(email)


def log_plan(uid: str, scenario: str, claims: dict | None, status: int) -> None:
    line = {
        "severity": "INFO",
        "message": f"{PLAN_EVENT} {scenario} {status}",
        "event": PLAN_EVENT,
        "uid": uid,
        "scenario": scenario,
        "excluded": is_excluded_email((claims or {}).get("email")),
        "status": status,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(line), file=sys.stdout, flush=True)
