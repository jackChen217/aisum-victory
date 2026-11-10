"""What the page shows when Google sign-in fails (static/authmsg.js, run in Node; skipped
where Node is not installed), and that auth.js locks the button while the popup is open."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "static"
MODULE = (STATIC / "authmsg.js").as_uri()
FRIENDLY = "Sign-in didn't complete. Please try again."

CASES = {
    # (error, location) -> key
    "cancelled": ({"code": "auth/cancelled-popup-request"}, {"hostname": "app-x.run.app", "host": "app-x.run.app"}),
    "closed": ({"code": "auth/popup-closed-by-user"}, {"hostname": "app-x.run.app", "host": "app-x.run.app"}),
    "network": ({"code": "auth/network-request-failed"}, {"hostname": "app-x.run.app", "host": "app-x.run.app"}),
    "blocked": ({"code": "auth/popup-blocked"}, {"hostname": "localhost", "host": "localhost:8000"}),
    "no_code": ({"message": "Failed to fetch"}, {"hostname": "app-x.run.app", "host": "app-x.run.app"}),
    "domain_run": ({"code": "auth/unauthorized-domain"}, {"hostname": "app-x.run.app", "host": "app-x.run.app"}),
    "domain_ip": ({"code": "auth/unauthorized-domain"}, {"hostname": "127.0.0.1", "host": "127.0.0.1:8000"}),
    "domain_localhost": ({"code": "auth/unauthorized-domain"}, {"hostname": "localhost", "host": "localhost:8000"}),
}


@pytest.fixture(scope="module")
def message():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    script = (f"const {{signInMessage}} = await import({json.dumps(MODULE)});"
              "const cases = JSON.parse(process.argv[1]);"
              "const out = {}; for (const [k, [e, loc]] of Object.entries(cases)) out[k] = signInMessage(e, loc);"
              "console.log(JSON.stringify(out));")
    done = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(CASES)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


@pytest.mark.parametrize("key", ["cancelled", "closed"])
def test_cancelled_or_closed_popup_shows_nothing(message, key):
    assert message[key] is None


@pytest.mark.parametrize("key", ["network", "blocked", "no_code", "domain_localhost"])
def test_other_errors_show_only_the_friendly_sentence(message, key):
    assert message[key] == FRIENDLY


@pytest.mark.parametrize("key", list(CASES))
def test_no_raw_firebase_code_is_ever_shown(message, key):
    assert "auth/" not in (message[key] or "")
    assert "Failed to fetch" not in (message[key] or "")


def test_unauthorized_domain_keeps_what_to_do(message):
    assert message["domain_run"].startswith(FRIENDLY)
    assert "app-x.run.app is not on the sign-in list" in message["domain_run"]
    assert message["domain_ip"].startswith(FRIENDLY)
    assert "http://localhost:8000" in message["domain_ip"]


def test_auth_js_routes_errors_through_authmsg_and_locks_the_button():
    src = (STATIC / "auth.js").read_text(encoding="utf-8")
    assert "from '/static/authmsg.js'" in src
    # The old path wrote `auth <where>: <code>` into the page.
    assert "auth ${where}" not in src
    assert "console.debug" in src
    sign_in = src[src.index("async function signIn"):]
    sign_in = sign_in[:sign_in.index("button.addEventListener")]
    assert re.search(r"if \(popupOpen\) return;", sign_in)
    assert sign_in.index("button.disabled = true") < sign_in.index("signInWithPopup")
    assert "finally" in sign_in and "button.disabled = false" in sign_in.split("finally", 1)[1]
