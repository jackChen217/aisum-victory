"""/admin/stats: 404 for everyone but teammates, aggregates only, one log line per plan."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import main, stats, usage
from src.auth import require_user

TEAM = "mate@example.com"
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
AFTER = usage.VALID_SINCE + timedelta(hours=1)          # 11/8 evening ET
DAY2 = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def team(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", f" {TEAM.upper()} , other@example.com")
    monkeypatch.setenv("TEAM_EMAILS", TEAM)


def _claims(monkeypatch, claims):
    def fake(authorization):
        if not authorization.startswith("Bearer "):
            raise main.HTTPException(401, "no token")
        return claims
    monkeypatch.setattr(main, "verify_token", fake)


# ── Gate ─────────────────────────────────────────────────────────────────

def test_page_is_404_for_get_and_head():
    client = TestClient(main.app)
    get, head = client.get("/admin/stats"), client.head("/admin/stats")
    assert get.status_code == head.status_code == 404
    assert "Not Found" in get.text and head.content == b""


@pytest.mark.parametrize("claims", [
    {"sub": "u", "email": "someone@gmail.com", "email_verified": True},
    {"sub": "u", "email": TEAM, "email_verified": False},
    {"sub": "u"},
])
def test_data_is_404_for_non_teammates(monkeypatch, claims):
    _claims(monkeypatch, claims)
    async def boom():
        raise AssertionError("must not compute stats")
    monkeypatch.setattr(stats, "get_stats", boom)
    client = TestClient(main.app)
    assert client.get("/api/admin/stats").status_code == 404
    assert client.get("/api/admin/stats", headers={"Authorization": "Bearer x"}).status_code == 404


def test_data_for_a_teammate(monkeypatch):
    _claims(monkeypatch, {"sub": "u", "email": TEAM, "email_verified": True})
    async def fake():
        return {"ok": 1}
    monkeypatch.setattr(stats, "get_stats", fake)
    r = TestClient(main.app).get("/api/admin/stats", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200 and r.json() == {"ok": 1}
    assert r.headers["cache-control"] == "private, no-store"


# ── Aggregation ──────────────────────────────────────────────────────────

EARLY = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)  # 11/7 ET, before the cutoff


def _u(uid, created, aisum=False, team=False):
    return {"uid": uid, "created": created, "aisum": aisum, "team": team}


USERS = [
    _u("a", AFTER), _u("b", DAY2), _u("c", DAY2),
    _u("team", DAY2, team=True),
    _u("boss", DAY2, aisum=True),
    _u("early", EARLY),
    _u("cutoff", usage.VALID_SINCE - timedelta(minutes=1)),  # 11/8, one minute too early
]


def _ev(t, uid, scenario, excluded=False):
    return {"time": t, "uid": uid, "scenario": scenario, "excluded": excluded}


def test_axis_starts_on_the_first_sign_up():
    out = stats.aggregate(USERS, [], [], NOW, {})
    assert out["days"] == ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10",
                           "2026-11-11", "2026-11-12"]


def test_all_sign_ups_drop_only_aisum():
    all_ = stats.aggregate(USERS, [], [], NOW, {})["users"]["all"]
    assert all_["new"] == [1, 2, 0, 3, 0, 0]  # early; a + cutoff; b, c, team
    assert all_["cumulative"] == [1, 3, 3, 6, 6, 6] and all_["total"] == 6


def test_official_follows_the_cutoff_and_team_emails():
    official = stats.aggregate(USERS, [], [], NOW, {})["users"]["official"]
    assert official["new"] == [0, 1, 0, 2, 0, 0]
    assert official["cumulative"] == [0, 1, 1, 3, 3, 3] and official["total"] == 3


def test_launch_is_marked_on_11_8():
    assert stats.aggregate(USERS, [], [], NOW, {})["events"] == [
        {"date": "2026-11-08", "label": "Launch"}]


def test_searches_by_type_history_and_returning():
    events = [_ev(DAY2, "a", "outfit"), _ev(DAY2 + timedelta(hours=1), "a", "desk"),
              _ev(DAY2, "b", "room"), _ev(DAY2, "team", "outfit", excluded=True),
              _ev(DAY2, "early", "outfit"), _ev(DAY2, "early", "desk")]
    history = [EARLY, AFTER, AFTER + timedelta(hours=2), DAY2 + timedelta(hours=5)]  # last: after logging began
    out = stats.aggregate(USERS, events, history, NOW, {})
    s = out["searches"]
    assert (s["outfit"][3], s["desk"][3], s["room"][3]) == (2, 2, 1)  # early's count too
    assert s["request_log"] == [1, 2, 0, 0, 0, 0]
    # Returning counts official users only: a (2 plans); early is not official.
    assert out["returning"] == {"count": 1, "active": 2, "total": 3, "share": 1 / 3}


def test_without_the_user_list_users_are_unavailable():
    events = [_ev(AFTER, "x", "outfit"), _ev(DAY2, "x", "desk"), _ev(DAY2, "y", "room"),
              _ev(DAY2, "team", "room", excluded=True)]
    out = stats.aggregate(None, events, None, NOW, {"users": "HTTP 403"})
    assert out["users"]["available"] is False
    assert out["days"][0] == "2026-11-08"
    assert out["errors"] == {"users": "HTTP 403"}
    assert out["searches"]["desk"][2] == 1  # searches still count, minus teammates
    assert out["searches"]["room"][2] == 1


def test_failed_sources_go_to_the_server_log(monkeypatch, capsys):
    async def ok(*a):
        return []
    async def denied(*a):
        raise stats.SourceError("Firebase Auth user list: HTTP 400: INSUFFICIENT_PERMISSION "
                                "for someone@example.com")
    monkeypatch.setattr(stats, "_access_token", lambda: "t")
    monkeypatch.setattr(stats, "fetch_users", denied)
    monkeypatch.setattr(stats, "fetch_plan_events", ok)
    monkeypatch.setattr(stats, "fetch_request_log", ok)
    out = asyncio.run(stats._collect())
    assert out["users"]["available"] is False and set(out["errors"]) == {"users"}
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == "admin_stats_source_error" and line["source"] == "users"
    assert line["severity"] == "WARNING" and "INSUFFICIENT_PERMISSION" in line["error"]
    assert "someone@example.com" not in line["error"] and "<email>" in line["error"]


def test_output_names_nobody():
    users = [_u("a", AFTER)]
    text = json.dumps(stats.aggregate(users, [_ev(AFTER, "a", "outfit")], [], NOW, {}))
    assert "@" not in text and '"a"' not in text


def test_is_excluded_email():
    assert usage.is_excluded_email(TEAM) and usage.is_excluded_email("Boss@AISUM.com")
    assert not usage.is_excluded_email("someone@gmail.com") and not usage.is_excluded_email(None)


def test_stats_are_cached(monkeypatch):
    calls = []
    async def collect():
        calls.append(1)
        return {"n": len(calls)}
    monkeypatch.setattr(stats, "_collect", collect)
    monkeypatch.setitem(stats._cache, "data", None)

    async def twice():
        return await asyncio.gather(stats.get_stats(), stats.get_stats())
    assert asyncio.run(twice()) == [{"n": 1}, {"n": 1}]
    assert len(calls) == 1


# ── Log line ─────────────────────────────────────────────────────────────

def test_plan_logs_uid_and_type_never_email(monkeypatch, capsys):
    async def fake_plan(uid, *args, **kwargs):
        return {"session_id": "s"}
    monkeypatch.setattr(main.orchestrator, "plan", fake_plan)
    main.app.dependency_overrides[require_user] = lambda: "uid-1"
    try:
        r = TestClient(main.app).post("/api/plan", json={
            "image_url": "https://example.com/a.jpg", "scenario": "desk", "budget": 100})
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == 200
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if usage.PLAN_EVENT in l]
    assert len(lines) == 1
    line = lines[0]
    assert (line["uid"], line["scenario"], line["status"], line["excluded"]) == ("uid-1", "desk", 200, False)
    assert "email" not in line


def test_teammates_count_while_team_emails_is_unset(monkeypatch):
    monkeypatch.delenv("TEAM_EMAILS")
    assert not usage.is_excluded_email(TEAM)  # ADMIN_EMAILS alone excludes nobody
    assert usage.is_excluded_email("boss@aisum.com")
    users = [_u("t", AFTER, team=usage.is_team_email(TEAM))]
    out = stats.aggregate(users, [], [], NOW, {})
    assert out["users"]["official"]["total"] == 1 and out["team_excluded"] is False


def test_teammate_plan_is_flagged(monkeypatch, capsys):
    usage.log_plan("u", "room", {"email": TEAM}, 422)
    line = json.loads(capsys.readouterr().out)
    assert line["excluded"] is True and TEAM not in json.dumps(line)
