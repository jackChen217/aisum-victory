"""Valid users per day, from the Firebase Auth user list, using your own credentials.

A stopgap until the Cloud Run service account can read the list for /admin/stats.
Run from the repo root:

    gcloud auth application-default login     # once, if you haven't
    .venv/bin/python scripts/valid_users_by_day.py

Valid user: created on or after 2026-11-08 13:00 PT and not an @aisum.com address
(teammates are counted, as on /admin/stats while TEAM_EMAILS is unset). Days are ET.

Read-only. Emails are looked at only to drop @aisum.com accounts, then discarded:
nothing but counts is printed, and nothing is written to disk.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

import google.auth
import google.auth.transport.requests
import httpx

PROJECT = "aisum-hackathon-juliet"
VALID_SINCE = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)  # 13:00 PST
ET = timezone(timedelta(hours=-5))  # the whole scoring window is standard time
EXCLUDED_DOMAIN = "@aisum.com"


def token() -> str:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def creation_times(project: str) -> tuple[list[datetime], int, int]:
    """Creation times of valid users, plus how many were left out and why. No emails out."""
    url = f"https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:batchGet"
    headers = {"Authorization": f"Bearer {token()}", "x-goog-user-project": project}
    valid, early, aisum, page = [], 0, 0, None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"maxResults": 1000, **({"nextPageToken": page} if page else {})}
            r = client.get(url, params=params, headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"User list: HTTP {r.status_code}: {r.json().get('error', {}).get('message', r.text[:200])}")
            body = r.json()
            for u in body.get("users", []):
                created = datetime.fromtimestamp(int(u["createdAt"]) / 1000, timezone.utc)
                if (u.get("email") or "").lower().endswith(EXCLUDED_DOMAIN):
                    aisum += 1
                elif created < VALID_SINCE:
                    early += 1
                else:
                    valid.append(created)
            page = body.get("nextPageToken")
            if not page:
                return valid, early, aisum


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=PROJECT)
    args = parser.parse_args()

    valid, early, aisum = creation_times(args.project)
    per_day = Counter(t.astimezone(ET).date() for t in valid)
    start, today = VALID_SINCE.astimezone(ET).date(), datetime.now(ET).date()

    print(f"Valid users per day (ET), project {args.project}")
    print(f"{'day':<12}{'new':>5}{'total':>7}")
    total, day = 0, start
    while day <= today:
        total += per_day[day]
        print(f"{day.isoformat():<12}{per_day[day]:>5}{total:>7}")
        day += timedelta(days=1)
    print(f"\nValid users: {total}  (left out: {early} created before 11/8 13:00 PT, "
          f"{aisum} @aisum.com)")


if __name__ == "__main__":
    main()
