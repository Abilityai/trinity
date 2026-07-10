"""
WhatsApp (Twilio) integration tests — Phase 2 (#467).

Live-backend webhook simulator. Each test:
1. Constructs a Twilio-style form-encoded webhook payload
2. Signs it with HMAC-SHA1 using the test binding's AuthToken (Twilio's exact
   `RequestValidator` algorithm)
3. POSTs to `/api/whatsapp/webhook/{secret}` on the running backend
4. Waits briefly (webhook processing is `asyncio.create_task` fire-and-forget)
5. Asserts on state changes in `whatsapp_chat_links`, `email_login_codes`,
   `access_requests` via direct DB queries against the backend container.

Prerequisites:
- trinity-backend container running at http://localhost:8000
- Test venv has twilio, httpx, pytest installed
"""

import json
import os
import shlex
import subprocess
import time
import uuid
from typing import Optional

import httpx
import pytest
from twilio.request_validator import RequestValidator

BACKEND_URL = os.environ.get("TRINITY_API_URL", "http://localhost:8000")
# The URL Twilio would hit — must match what the backend reconstructs inside
# `_reconstruct_url`. We POST to the real listening address, and the signing
# URL must be identical byte-for-byte.
PUBLIC_WEBHOOK_BASE = f"{BACKEND_URL}/api/whatsapp/webhook"

# Fake Twilio credentials — real enough format that HMAC works, but won't
# authenticate against api.twilio.com for outbound sends (which we don't
# assert on anyway).
_FAKE_ACCOUNT_SID = "AC" + "0" * 32  # Twilio AccountSids start with "AC"
_FAKE_AUTH_TOKEN = "test_auth_token_" + uuid.uuid4().hex
_FAKE_FROM_NUMBER = "whatsapp:+14155238886"  # Twilio Sandbox sender
_TEST_USER_PHONE = "whatsapp:+14155551234"


# =============================================================================
# Container-exec helpers — the backend's SQLite is on a Docker volume, not a
# bind mount, so we run queries inside the container via `docker exec`.
# =============================================================================

# `from database import db` triggers `_ensure_admin_user` which prints
# warnings to stdout. We need a way to separate our intended output from
# arbitrary import-time noise — the sentinel marker + split below does that.
_OUTPUT_MARKER = "===TRINITY_OUTPUT==="


def _backend_py(script: str) -> str:
    """Run a script inside the backend container's Python. Returns the text
    printed AFTER the sentinel marker (caller is expected to print
    `_OUTPUT_MARKER` on its own line before any data they want returned)."""
    result = subprocess.run(
        ["docker", "exec", "-i", "trinity-backend", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"backend python failed (rc={result.returncode}): "
            f"stderr={result.stderr!r} stdout={result.stdout!r}"
        )
    stdout = result.stdout
    if _OUTPUT_MARKER in stdout:
        return stdout.split(_OUTPUT_MARKER, 1)[1].strip()
    # No marker → caller didn't need to import the backend; return raw stdout
    return stdout.strip()


def _db_query(sql: str) -> list:
    """Execute a read-only SQL query inside the backend container. Returns rows."""
    # Embed SQL as a repr'd string to dodge quote issues. No backend imports
    # needed — sqlite3 is stdlib, so no marker required.
    script = (
        "import sqlite3, json; "
        "c = sqlite3.connect('/data/trinity.db'); "
        f"rows = c.execute({sql!r}).fetchall(); "
        "print(json.dumps(rows))"
    )
    output = _backend_py(script)
    return json.loads(output or "[]")


def _db_write(script: str) -> str:
    """Run a DB-mutating python script inside the backend container via the
    `db` facade, so encryption/UPSERT paths match production."""
    return _backend_py(script)


# =============================================================================
# Fixtures — module-scoped binding on top of the standard `created_agent`
# =============================================================================


@pytest.fixture(scope="module")
def whatsapp_binding(created_agent) -> dict:
    """Create a WhatsApp binding for the module's test agent.

    Uses the DB facade directly (not the public API) because the API validates
    credentials against Twilio — we're using fake creds. Returns the binding
    dict including the `webhook_secret` for signing.
    """
    agent_name = created_agent["name"]

    # Insert binding via db facade so encryption + secret generation paths run
    script = (
        "from database import db; "
        f"b = db.create_whatsapp_binding({agent_name!r}, {_FAKE_ACCOUNT_SID!r}, "
        f"{_FAKE_AUTH_TOKEN!r}, {_FAKE_FROM_NUMBER!r}, None, 'Test Sender', "
        "'integration-test'); "
        f"import json; print({_OUTPUT_MARKER!r}); print(json.dumps(b))"
    )
    binding = json.loads(_backend_py(script))
    assert binding["webhook_secret"], "binding creation returned no secret"

    yield binding

    # Teardown: clear access_requests from this test + delete binding.
    # (The agent itself is cleaned by `created_agent` fixture.)
    teardown = (
        "from database import db; "
        f"db.delete_whatsapp_binding({agent_name!r}); "
        "import sqlite3; c = sqlite3.connect('/data/trinity.db'); "
        f"c.execute(\"DELETE FROM access_requests WHERE agent_name = ?\", ({agent_name!r},)); "
        f"c.commit(); c.close(); print({_OUTPUT_MARKER!r})"
    )
    try:
        _backend_py(teardown)
    except Exception as e:
        print(f"[whatsapp_binding teardown] {e}")


@pytest.fixture(autouse=True)
def reset_chat_state_between_tests(whatsapp_binding):
    """Clear per-user verification + pending state between tests so each test
    sees a clean starting point."""
    binding_id = whatsapp_binding["id"]
    agent_name = whatsapp_binding["agent_name"]

    def _reset():
        script = (
            "from database import db; "
            f"db.clear_whatsapp_verified_email({binding_id}, {_TEST_USER_PHONE!r}); "
            "from adapters.whatsapp_adapter import _clear_pending_login; "
            f"_clear_pending_login({binding_id}, {_TEST_USER_PHONE!r}); "
            "import sqlite3; c = sqlite3.connect('/data/trinity.db'); "
            f"c.execute(\"DELETE FROM access_requests WHERE agent_name = ?\", ({agent_name!r},)); "
            f"c.execute(\"UPDATE agent_ownership SET require_email=0, open_access=0 "
            f"WHERE agent_name = ?\", ({agent_name!r},)); "
            f"c.commit(); c.close(); print({_OUTPUT_MARKER!r})"
        )
        _backend_py(script)

    _reset()
    yield
    _reset()


# =============================================================================
# Webhook POST helpers
# =============================================================================


def _build_twilio_payload(body: str, from_phone: str, binding_id: int) -> dict:
    """Build the form params Twilio would POST for an inbound WhatsApp message."""
    return {
        "From": from_phone,
        "To": _FAKE_FROM_NUMBER,
        "Body": body,
        "MessageSid": f"SM{uuid.uuid4().hex[:30]}",
        "ProfileName": "Integration Test",
        "NumMedia": "0",
        "AccountSid": _FAKE_ACCOUNT_SID,
    }


def _post_webhook(binding: dict, body: str, from_phone: str = _TEST_USER_PHONE) -> int:
    """Sign + POST a webhook like Twilio would. Returns HTTP status."""
    secret = binding["webhook_secret"]
    url = f"{PUBLIC_WEBHOOK_BASE}/{secret}"
    payload = _build_twilio_payload(body, from_phone, binding["id"])
    signature = RequestValidator(_FAKE_AUTH_TOKEN).compute_signature(url, payload)
    resp = httpx.post(
        url,
        data=payload,
        headers={
            "X-Twilio-Signature": signature,
            # Uvicorn runs with --proxy-headers; tests hit localhost directly
            # so no X-Forwarded-Proto is required.
        },
        timeout=10,
    )
    return resp.status_code


# =============================================================================
# Polling helpers — the webhook handler is fire-and-forget; give the async
# task up to `timeout_s` to write to the DB before asserting.
# =============================================================================


def _wait_for(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.2):
    """Poll a predicate callable until it returns truthy or timeout."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    return last


def _verified_email_for(binding_id: int, phone: str) -> Optional[str]:
    rows = _db_query(
        f"SELECT verified_email FROM whatsapp_chat_links "
        f"WHERE binding_id = {binding_id} AND wa_user_phone = '{phone}'"
    )
    if rows and rows[0][0]:
        return rows[0][0]
    return None


def _latest_login_code(email: str) -> Optional[str]:
    rows = _db_query(
        f"SELECT code FROM email_login_codes WHERE email = '{email}' "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    return rows[0][0] if rows else None


def _pending_login_matches(binding_id: int, phone: str, email: str) -> bool:
    """Check Redis for the pending-login key the adapter sets after /login email.

    The adapter calls `_set_pending_login` AFTER `db.create_login_code`, so
    observing the code in the DB does not imply the pending key is also set.
    Tests must wait for THIS before POSTing `/login <code>`.

    Read through the backend container's own Redis client (the same
    `get_redis_client()` the adapter uses) rather than a bare `redis-cli GET`:
    the platform now runs Redis with mandatory ACL auth (#589), so an
    unauthenticated `redis-cli` returns `NOAUTH Authentication required.` with
    exit code 0 — which a naive stdout comparison silently treats as a
    never-matching value. Going through the backend uses the authenticated
    `REDIS_URL` (correct user + DB) with no hardcoded password.
    """
    script = (
        "from adapters.whatsapp_adapter import _get_pending_login; "
        f"val = _get_pending_login({binding_id}, {phone!r}); "
        f"print({_OUTPUT_MARKER!r}); print(val or '')"
    )
    return _backend_py(script) == email.lower()


def _access_request(agent_name: str, email: str) -> Optional[dict]:
    rows = _db_query(
        f"SELECT channel, status FROM access_requests "
        f"WHERE agent_name = '{agent_name}' AND email = '{email}'"
    )
    if not rows:
        return None
    return {"channel": rows[0][0], "status": rows[0][1]}


def _set_policy(agent_name: str, require_email: int, open_access: int):
    _backend_py(
        "import sqlite3; c = sqlite3.connect('/data/trinity.db'); "
        f"c.execute(\"UPDATE agent_ownership SET require_email={require_email}, "
        f"open_access={open_access} WHERE agent_name = ?\", ({agent_name!r},)); "
        f"c.commit(); c.close(); print({_OUTPUT_MARKER!r})"
    )


# =============================================================================
# The tests
# =============================================================================


@pytest.mark.slow
class TestWhatsAppLoginFlow:
    """End-to-end /login flow: email → code → verified."""

    def test_login_email_creates_code_and_pending(self, whatsapp_binding):
        """POST /login <email> creates an email_login_codes row."""
        test_email = f"login-ok-{uuid.uuid4().hex[:6]}@example.com"

        status = _post_webhook(whatsapp_binding, f"/login {test_email}")
        assert status == 200, f"webhook rejected: HTTP {status}"

        # A code row should appear shortly
        code = _wait_for(lambda: _latest_login_code(test_email))
        assert code and len(code) == 6 and code.isdigit(), f"no code row: {code!r}"

    def test_login_code_verifies_and_writes_email(self, whatsapp_binding):
        """Full round-trip: /login email → /login code → verified_email set."""
        agent_name = whatsapp_binding["agent_name"]
        binding_id = whatsapp_binding["id"]
        test_email = f"verify-{uuid.uuid4().hex[:6]}@example.com"

        # Step 1 — request code, wait for pending-login in Redis to avoid
        # the known race where the DB has the code but Redis not yet.
        assert _post_webhook(whatsapp_binding, f"/login {test_email}") == 200
        assert _wait_for(
            lambda: _pending_login_matches(binding_id, _TEST_USER_PHONE, test_email),
            timeout_s=10.0,
        ), "pending-login Redis key never appeared"
        code = _latest_login_code(test_email)
        assert code, "no login code generated"

        # Step 2 — submit code
        assert _post_webhook(whatsapp_binding, f"/login {code}") == 200

        # verified_email should now be set for this phone
        verified = _wait_for(
            lambda: _verified_email_for(binding_id, _TEST_USER_PHONE)
        )
        assert verified == test_email.lower(), f"expected {test_email!r}, got {verified!r}"

    def test_login_invalid_code_does_not_verify(self, whatsapp_binding):
        """A wrong 6-digit code must NOT write verified_email."""
        binding_id = whatsapp_binding["id"]
        test_email = f"badcode-{uuid.uuid4().hex[:6]}@example.com"

        _post_webhook(whatsapp_binding, f"/login {test_email}")
        _wait_for(
            lambda: _pending_login_matches(binding_id, _TEST_USER_PHONE, test_email),
            timeout_s=10.0,
        )

        # Submit an obviously-wrong code
        assert _post_webhook(whatsapp_binding, "/login 000000") == 200
        time.sleep(1)  # allow processing
        assert _verified_email_for(binding_id, _TEST_USER_PHONE) is None


@pytest.mark.slow
class TestWhatsAppAccessGate:
    """Post-verification gate — inlined into /login so user learns status immediately."""

    def test_verified_on_restrictive_policy_creates_access_request(
        self, whatsapp_binding
    ):
        """require_email + not-shared → verification creates a pending access_requests row."""
        agent_name = whatsapp_binding["agent_name"]
        binding_id = whatsapp_binding["id"]
        test_email = f"pending-{uuid.uuid4().hex[:6]}@example.com"

        _set_policy(agent_name, require_email=1, open_access=0)

        _post_webhook(whatsapp_binding, f"/login {test_email}")
        assert _wait_for(
            lambda: _pending_login_matches(binding_id, _TEST_USER_PHONE, test_email),
            timeout_s=10.0,
        ), "pending-login Redis key never appeared"
        code = _latest_login_code(test_email)
        assert code, "no code generated"
        _post_webhook(whatsapp_binding, f"/login {code}")

        # verified_email written
        assert _wait_for(
            lambda: _verified_email_for(binding_id, _TEST_USER_PHONE)
        ) == test_email.lower()

        # access_requests row created with channel='whatsapp'
        req = _wait_for(lambda: _access_request(agent_name, test_email.lower()))
        assert req == {"channel": "whatsapp", "status": "pending"}

    def test_verified_on_open_access_no_request(self, whatsapp_binding):
        """open_access=1 → verification does NOT create an access_requests row."""
        agent_name = whatsapp_binding["agent_name"]
        binding_id = whatsapp_binding["id"]
        test_email = f"openacc-{uuid.uuid4().hex[:6]}@example.com"

        _set_policy(agent_name, require_email=0, open_access=1)

        _post_webhook(whatsapp_binding, f"/login {test_email}")
        assert _wait_for(
            lambda: _pending_login_matches(binding_id, _TEST_USER_PHONE, test_email),
            timeout_s=10.0,
        ), "pending-login Redis key never appeared"
        code = _latest_login_code(test_email)
        assert code
        _post_webhook(whatsapp_binding, f"/login {code}")

        assert _wait_for(
            lambda: _verified_email_for(binding_id, _TEST_USER_PHONE)
        ) == test_email.lower()

        # Give the handler time; there should still be NO access_requests row
        time.sleep(1)
        assert _access_request(agent_name, test_email.lower()) is None


@pytest.mark.slow
class TestWhatsAppLogoutWhoami:
    """/logout + /whoami don't crash, and /logout clears verified_email."""

    def test_logout_clears_verified_email(self, whatsapp_binding):
        agent_name = whatsapp_binding["agent_name"]
        binding_id = whatsapp_binding["id"]
        test_email = f"logout-{uuid.uuid4().hex[:6]}@example.com"

        _set_policy(agent_name, require_email=0, open_access=1)

        # Verify
        _post_webhook(whatsapp_binding, f"/login {test_email}")
        assert _wait_for(
            lambda: _pending_login_matches(binding_id, _TEST_USER_PHONE, test_email),
            timeout_s=10.0,
        ), "pending-login Redis key never appeared"
        code = _latest_login_code(test_email)
        _post_webhook(whatsapp_binding, f"/login {code}")
        assert _wait_for(
            lambda: _verified_email_for(binding_id, _TEST_USER_PHONE)
        ) == test_email.lower()

        # Logout
        assert _post_webhook(whatsapp_binding, "/logout") == 200

        # verified_email should now be NULL
        def _gone():
            return _verified_email_for(binding_id, _TEST_USER_PHONE) is None

        assert _wait_for(_gone), "verified_email was not cleared by /logout"

    def test_whoami_accepts_both_states(self, whatsapp_binding):
        """/whoami webhook returns 200 regardless of verification state."""
        assert _post_webhook(whatsapp_binding, "/whoami") == 200

    def test_unknown_command_falls_through_to_router(self, whatsapp_binding):
        """A /unknown command returns None from handle_command; webhook still 200s
        and falls through to the normal message router (which will fail to
        execute against our fake agent but must not crash the webhook)."""
        assert _post_webhook(whatsapp_binding, "/unknown_command") == 200


@pytest.mark.slow
class TestWhatsAppWebhookSecurity:
    """Negative tests — signature validation + unknown secret handling."""

    def test_bad_signature_returns_403(self, whatsapp_binding):
        """A payload signed with a wrong AuthToken must be rejected."""
        secret = whatsapp_binding["webhook_secret"]
        url = f"{PUBLIC_WEBHOOK_BASE}/{secret}"
        payload = _build_twilio_payload("/whoami", _TEST_USER_PHONE, whatsapp_binding["id"])
        # Sign with the WRONG token
        bad_sig = RequestValidator("wrong-token").compute_signature(url, payload)
        resp = httpx.post(
            url, data=payload, headers={"X-Twilio-Signature": bad_sig}, timeout=10
        )
        assert resp.status_code == 403

    def test_unknown_secret_returns_200_no_leak(self, whatsapp_binding):
        """Unknown webhook secret returns 200 (don't leak binding existence)."""
        url = f"{PUBLIC_WEBHOOK_BASE}/{'x' * 40}"
        payload = _build_twilio_payload("/whoami", _TEST_USER_PHONE, 0)
        sig = RequestValidator(_FAKE_AUTH_TOKEN).compute_signature(url, payload)
        resp = httpx.post(
            url, data=payload, headers={"X-Twilio-Signature": sig}, timeout=10
        )
        assert resp.status_code == 200
