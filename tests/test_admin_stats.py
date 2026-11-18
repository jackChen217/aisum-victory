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

USERS = [
    {"uid": "a", "created": AFTER, "excluded": False},
    {"uid": "b", "created": DAY2, "excluded": False},
    {"uid": "c", "created": DAY2, "excluded": False},
    {"uid": "team", "created": DAY2, "excluded": True},
    {"uid": "early", "created": usage.VALID_SINCE - timedelta(minutes=1), "excluded": False},
]


def _ev(t, uid, scenario, excluded=False):
    return {"time": t, "uid": uid, "scenario": scenario, "excluded": excluded}


def test_users_new_and_cumulative():
    out = stats.aggregate(USERS, [], [], NOW, {})
    assert out["days"] == ["2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    assert out["users"]["new"] == [1, 0, 2, 0, 0]
    assert out["users"]["cumulative"] == [1, 1, 3, 3, 3]
    assert out["users"]["total"] == 3 and not out["users"]["approximate"]


def test_searches_by_type_history_and_returning():
    events = [_ev(DAY2, "a", "outfit"), _ev(DAY2 + timedelta(hours=1), "a", "desk"),
              _ev(DAY2, "b", "room"), _ev(DAY2, "team", "outfit", excluded=True),
              _ev(DAY2, "early", "outfit")]
    history = [AFTER, AFTER + timedelta(hours=2), DAY2 + timedelta(hours=5)]  # last one after logging began
    out = stats.aggregate(USERS, events, history, NOW, {})
    s = out["searches"]
    assert (s["outfit"][2], s["desk"][2], s["room"][2]) == (1, 1, 1)
    assert s["request_log"] == [2, 0, 0, 0, 0]
    assert out["returning"] == {"count": 1, "active": 2, "total": 3, "share": 1 / 3}


def test_first_plan_fallback_is_marked_approximate():
    events = [_ev(AFTER, "x", "outfit"), _ev(DAY2, "x", "desk"), _ev(DAY2, "y", "room"),
              _ev(DAY2, "team", "room", excluded=True)]
    out = stats.aggregate(None, events, None, NOW, {"users": "HTTP 403"})
    assert out["users"]["approximate"] and out["users"]["source"] == "first_plan"
    assert out["users"]["new"] == [1, 0, 1, 0, 0]
    assert out["errors"] == {"users": "HTTP 403"}
    assert any("approximate" in n for n in out["notes"])


def test_output_names_nobody():
    users = [{"uid": "a", "created": AFTER, "excluded": False}]
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


def test_teammate_plan_is_flagged(monkeypatch, capsys):
    usage.log_plan("u", "room", {"email": TEAM}, 422)
    line = json.loads(capsys.readouterr().out)
    assert line["excluded"] is True and TEAM not in json.dumps(line)
