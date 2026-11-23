"""Finch bounty task (src/finch_task.py, static/source.js): source capture, completion
codes, the admin code check and new users by source. AISUM is faked; the store is a
temporary folder."""
import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from PIL import Image

from src import finch_task, gallery, main, stats, usage
from src.agent import limits, orchestrator, outfit, planner
from src.auth import require_user

ROOT = Path(__file__).parent.parent
SECRET = "test-code-secret"
CODE_RE = re.compile(r"GC-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6}")
OUTFIT_LOOK = next(x["id"] for x in json.loads((ROOT / "static" / "gallery.json").read_text())["looks"]
                   if x["category"] == "outfit")


def prod(key, price, score=0.8):
    return {"p_key": key, "product_name": f"Item {key}", "product_price": price,
            "img_url": f"https://img/{key}.jpg", "click_url": f"https://shop/{key}", "score": score}


DETECTIONS = {"detections": [
    {"item": "coat", "item_detail": "grey coat", "box": [0.2, 0.2, 0.6, 0.7],
     "products": [prod("coat1", 60)]},
]}
PLAN = {"image_url": "https://images.unsplash.com/photo-1?w=1080", "scenario": "outfit",
        "budget": 200, "gender": "F"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    limits.reset()
    gallery.reset()
    finch_task.reset()
    gallery._store = gallery.LocalStore(tmp_path)
    state = {"detections": DETECTIONS, "sources": [], "tasks": []}

    async def search_image(url, **kw):
        return state["detections"] if "category" not in kw else {"detections": []}

    async def photo(url):
        return Image.new("RGB", (800, 1200), "white")

    async def save(data):
        return {"url": "https://bucket/crop.jpg"}

    monkeypatch.setattr(planner, "search_image", search_image)
    monkeypatch.setattr(planner, "_photo", photo)
    monkeypatch.setattr(outfit.uploads, "save", save)
    monkeypatch.setattr(usage, "log_source", lambda uid, src: state["sources"].append((uid, src)))
    monkeypatch.setattr(usage, "log_finch_task", lambda uid, how: state["tasks"].append((uid, how)))
    monkeypatch.setattr(usage, "log_plan", lambda *a, **kw: None)
    monkeypatch.setattr(usage, "log_gallery", lambda *a, **kw: None)
    monkeypatch.setenv(finch_task.SECRET_ENV, SECRET)
    yield state
    orchestrator.SESSIONS.clear()
    gallery.reset()
    finch_task.reset()
    limits.reset()


@pytest.fixture
def client(env):
    """Signed in as the uid in the X-Test-Uid header."""
    # FastAPI inspects the override's signature: it needs a Request parameter.
    def as_user(request: Request) -> str:
        return request.headers["x-test-uid"]
    main.app.dependency_overrides[require_user] = as_user
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def as_(uid):
    return {"X-Test-Uid": uid}


def code_of(client, uid):
    r = client.get("/api/me/finch-code", headers=as_(uid))
    assert r.status_code == 200
    return r.json()["code"]


def test_signed_in_on_a_new_device_the_server_knows_the_source_and_code(client):
    assert client.get("/api/me/finch-code", headers=as_("u1")).json() == {"source": None, "code": None}
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    client.post("/api/plan", json=PLAN, headers=as_("u1"))
    finch_task.reset()                    # nothing cached: as after a restart
    body = client.get("/api/me/finch-code", headers=as_("u1")).json()
    assert body == {"source": "finch", "code": finch_task.code_for("u1")}


# ── codes ────────────────────────────────────────────────────────────────

def test_code_is_stable_per_user_and_depends_on_the_secret(env):
    a = finch_task.code_for("user-a")
    assert CODE_RE.fullmatch(a)
    assert finch_task.code_for("user-a") == a
    assert finch_task.code_for("user-b") != a
    assert finch_task.code_for("user-a", b"other-secret") != a


def test_no_secret_no_code(env, monkeypatch):
    monkeypatch.delenv(finch_task.SECRET_ENV)
    assert finch_task.code_for("user-a") is None


def test_pasted_codes_are_normalized():
    assert finch_task.normalize(" gc-7k3p9q ") == "GC-7K3P9Q"
    assert finch_task.normalize("GC 7K3P9Q") == "GC-7K3P9Q"
    assert finch_task.normalize("7K3P9Q") == "GC-7K3P9Q"


# ── source ───────────────────────────────────────────────────────────────

def test_source_is_recorded_and_logged_once_per_account(client, env):
    r = client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    assert r.json() == {"source": "finch", "recorded": True}
    r = client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    assert r.json() == {"source": "finch", "recorded": False}
    assert env["sources"] == [("u1", "finch")]


def test_unknown_source_is_refused(client, env):
    r = client.post("/api/me/source", json={"source": "spam"}, headers=as_("u1"))
    assert r.status_code == 422
    assert env["sources"] == []


def test_source_line_has_uid_and_source_and_no_email(capsys):
    usage.log_source("u1", "finch")
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == usage.SOURCE_EVENT and line["uid"] == "u1" and line["source"] == "finch"
    assert line["event"] != usage.PLAN_EVENT
    assert "email" not in json.dumps(line)
    usage.log_finch_task("u1", "plan")
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == usage.FINCH_TASK_EVENT and "email" not in json.dumps(line)


# ── when the code shows ──────────────────────────────────────────────────

def test_no_code_without_src_finch_even_after_an_outfit(client, env):
    assert client.post("/api/plan", json=PLAN, headers=as_("plain")).status_code == 200
    assert code_of(client, "plain") is None
    assert env["tasks"] == []


def test_no_code_for_a_finch_user_who_only_signed_in(client):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    assert code_of(client, "u1") is None


def test_no_code_when_the_plan_failed(client, env):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    env["detections"] = {"detections": [], "note": "No fashion items."}
    assert client.post("/api/plan", json=PLAN, headers=as_("u1")).status_code == 422
    assert code_of(client, "u1") is None


def test_finch_user_gets_the_same_code_after_an_outfit_plan(client, env):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    assert client.post("/api/plan", json=PLAN, headers=as_("u1")).status_code == 200
    code = code_of(client, "u1")
    assert CODE_RE.fullmatch(code) and code == finch_task.code_for("u1")
    client.post("/api/plan", json=PLAN, headers=as_("u1"))
    finch_task.reset()                              # also after a restart (memory cache)
    assert code_of(client, "u1") == code
    assert env["tasks"] == [("u1", "plan")]         # logged once


def test_opening_an_outfit_gallery_look_counts(client, env):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u2"))
    r = client.post(f"/api/gallery/{OUTFIT_LOOK}/open?format=ui&refresh=0", headers=as_("u2"))
    assert r.status_code == 200
    assert code_of(client, "u2") == finch_task.code_for("u2")
    assert env["tasks"] == [("u2", "gallery")]


def test_no_code_shown_without_the_secret(client, monkeypatch):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    client.post("/api/plan", json=PLAN, headers=as_("u1"))
    monkeypatch.delenv(finch_task.SECRET_ENV)
    assert code_of(client, "u1") is None


def test_a_broken_store_never_costs_the_user_their_plan(client, env, monkeypatch):
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))

    async def broken(key, text):
        raise OSError("disk full")
    monkeypatch.setattr(gallery._store, "create", broken)
    assert client.post("/api/plan", json=PLAN, headers=as_("u1")).status_code == 200


# ── admin check ──────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, monkeypatch):
    main.app.dependency_overrides[main.require_admin] = lambda: {"email_verified": True}
    now = datetime.now(timezone.utc)
    created = {"u1": now - timedelta(seconds=30),          # signed up via the Finch link
               "u3": now - timedelta(days=2),              # old account, came via Finch
               "u2": now, "plain": now}
    state = {"created": created}

    async def created_times():
        return state["created"]
    monkeypatch.setattr(stats, "created_times", created_times)
    yield client, state


def test_admin_check_marks_duplicates_and_new_vs_existing_only(admin, env):
    client, _ = admin
    for uid in ("u1", "u2", "u3"):
        client.post("/api/me/source", json={"source": "finch"}, headers=as_(uid))
    for uid in ("u1", "u3", "plain"):                        # u2 never planned; plain isn't Finch
        client.post("/api/plan", json=PLAN, headers=as_(uid))
    good, old = finch_task.code_for("u1"), finch_task.code_for("u3")
    pasted = [good.lower(), finch_task.code_for("u2"), finch_task.code_for("plain"),
              "GC-ZZZZZZ", good, old, good]
    r = client.post("/api/admin/finch-codes", json={"codes": pasted})
    assert r.status_code == 200
    body = r.json()
    rows = body["results"]
    assert [x["valid"] for x in rows] == [True, False, False, False, True, True, True]
    assert [x["count"] for x in rows] == [3, 1, 1, 1, 3, 1, 3]
    assert [x["duplicate"] for x in rows] == [True, False, False, False, True, False, True]
    assert [x["category"] for x in rows] == ["finch_new", None, None, None, "finch_new",
                                             "existing", "finch_new"]
    assert rows[0]["code"] == good                                   # normalized
    assert (body["total"], body["distinct"], body["valid"], body["invalid"],
            body["duplicates"]) == (7, 5, 2, 3, 1)
    assert body["categories"] == {"finch_new": 1, "existing": 1}
    assert body["users_available"] is True
    text = json.dumps(body)
    for secret_ish in ("u1", "u3", "plain", "uid", "email", "@"):
        assert secret_ish not in text
    assert set(rows[0]) == {"code", "valid", "count", "duplicate", "category"}


def test_admin_check_without_the_user_list_says_unknown(admin):
    client, state = admin
    state["created"] = None
    client.post("/api/me/source", json={"source": "finch"}, headers=as_("u1"))
    client.post("/api/plan", json=PLAN, headers=as_("u1"))
    body = client.post("/api/admin/finch-codes",
                       json={"codes": [finch_task.code_for("u1")]}).json()
    assert body["results"][0]["category"] == "unknown" and body["users_available"] is False


def test_admin_check_needs_the_secret(admin, monkeypatch):
    client, _ = admin
    monkeypatch.delenv(finch_task.SECRET_ENV)
    assert client.post("/api/admin/finch-codes", json={"codes": ["GC-AAAAAA"]}).status_code == 503


def test_code_check_is_404_for_everyone_else(env):
    with TestClient(main.app) as c:
        r = c.post("/api/admin/finch-codes", json={"codes": ["GC-AAAAAA"]})
    assert r.status_code == 404


# ── stats: new users by source ───────────────────────────────────────────

def test_new_users_by_source_counts_only_fresh_accounts():
    t0 = usage.VALID_SINCE + timedelta(days=2)
    official = {"new-finch": t0, "old-account": t0 - timedelta(days=1), "plain": t0,
                "late-finch": t0}
    sources = [
        {"uid": "new-finch", "source": "finch", "time": t0 + timedelta(seconds=5)},
        {"uid": "old-account", "source": "finch", "time": t0},          # came back via Finch
        {"uid": "late-finch", "source": "finch", "time": t0 + timedelta(hours=2)},
        {"uid": "not-official", "source": "finch", "time": t0},
    ]
    tasks = [{"uid": "new-finch"}, {"uid": "not-official"}]
    out = stats.by_source(official, sources, tasks)
    assert out["new_users"] == {"finch": 1, "direct": 3}
    assert out["existing_users"] == {"finch": 2}
    assert out["finch_completed"] == 1
    assert stats.by_source(official, None, None) == {"available": False}


def test_aggregate_includes_sources():
    now = usage.VALID_SINCE + timedelta(days=3)
    users = [{"uid": "a", "created": usage.VALID_SINCE + timedelta(hours=1),
              "aisum": False, "team": False}]
    src = [{"uid": "a", "source": "finch", "time": usage.VALID_SINCE + timedelta(hours=1, seconds=3)}]
    out = stats.aggregate(users, [], [], now, {}, src, [])
    assert out["sources"]["new_users"] == {"finch": 1}
    assert stats.aggregate(None, [], [], now, {})["sources"] == {"available": False}


# ── static/source.js (Node) ──────────────────────────────────────────────

MODULE = (ROOT / "static" / "source.js").as_uri()


def node(script: str):
    exe = shutil.which("node")
    if not exe:
        pytest.skip("Node is not installed")
    done = subprocess.run([exe, "--input-type=module", "-e",
                           f"const m = await import({json.dumps(MODULE)});\n{script}"],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


MEMORY = ("const store = () => { const d = {}; return { getItem: (k) => (k in d ? d[k] : null),"
          " setItem: (k, v) => { d[k] = String(v); } }; };")


def test_page_keeps_src_finch_across_sign_in_and_ignores_other_sources():
    out = node(MEMORY + """
const s = store();
const first = m.rememberSource('?src=finch&category=outfit', s);
const later = m.rememberSource('?view=result', s);          // after sign-in / history push
const other = m.rememberSource('?src=evil', store());
const none = m.rememberSource('', store());
m.markSent(s, 'u1');
console.log(JSON.stringify([first, later, other, none, m.wasSent(s, 'u1'), m.wasSent(s, 'u2')]));
""")
    assert out == ["finch", "finch", None, None, True, False]


def test_page_survives_storage_that_throws():
    out = node("""
const bad = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
console.log(JSON.stringify([m.rememberSource('?src=finch', bad), m.rememberSource('', bad),
  m.rememberSource('?src=finch', null), m.wasSent(bad, 'u'), (m.markSent(bad, 'u'), 'ok')]));
""")
    assert out == ["finch", None, "finch", False, "ok"]


def test_copied_in_app_link_keeps_src_finch():
    out = node(MEMORY + """
const s = store(); m.rememberSource('?src=finch', s);
const base = 'https://app.example/ghostcart';
console.log(JSON.stringify([
  m.linkWithSource(base + '?view=result', s),          // remembered: added
  m.linkWithSource(base + '?src=finch', s),            // already there: unchanged
  m.linkWithSource(base + '?view=result', store()),    // nothing remembered
  m.linkWithSource(base, null),                        // no storage
]));
""")
    assert out == ["https://app.example/ghostcart?view=result&src=finch",
                   "https://app.example/ghostcart?src=finch",
                   "https://app.example/ghostcart?view=result",
                   "https://app.example/ghostcart"]
