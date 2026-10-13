"""Reads and sign-ins a chain needs from the stack itself (trinity-enterprise#795).

Two things the API does not give a test, both read where the stack runs —
through the Docker socket the tier already needs (``journeys.conftest.agent_mcp_key``
reads an agent's key the same way). Nothing here adds a door to the product.

* **A real person's session.** A person signs in by the real email-code flow
  (``/api/auth/email/request`` → ``/api/auth/email/verify``). The code is read
  from the platform's own ``email_login_codes`` row instead of a mailbox — the
  verify call, the user it signs in and the token it returns are the
  platform's, unchanged.
* **A row the platform wrote** that no route returns: a gate request's state
  (``skill_gate_requests``). The chain passes on that row, never on an agent's
  word.

Values are never logged: a login code is used once and dropped; a token stays
in memory.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import requests

API = os.getenv("TRINITY_API_URL", "http://localhost:8000")
#: The compose service the backend runs as; the container is found by label,
#: not by name, so a renamed project still resolves.
BACKEND_SERVICE = os.getenv("CHAIN_BACKEND_SERVICE", "backend")


def _backend_container():
    import docker  # test-only dependency (requirements-test.txt)

    client = docker.from_env()
    found = client.containers.list(
        filters={"label": f"com.docker.compose.service={BACKEND_SERVICE}"})
    if not found:
        found = [c for c in client.containers.list() if c.name == "trinity-backend"]
    if not found:
        raise AssertionError(
            f"no running container for compose service '{BACKEND_SERVICE}' — the chain "
            f"reads platform rows where the stack runs")
    return found[0]


def backend_json(script: str) -> Any:
    """Run ``script`` with the backend's own Python and DB settings; it prints
    one JSON line, which is returned. Read-only by construction of the callers."""
    code, out = _backend_container().exec_run(["python", "-c", script], workdir="/app")
    text = (out or b"").decode("utf-8", "replace").strip().splitlines()
    if code != 0 or not text:
        raise AssertionError(f"reading the platform's rows failed (exit {code}): "
                             f"{(text[-1] if text else '')[:300]}")
    return json.loads(text[-1])


def gate_request(request_id: str) -> Optional[dict]:
    """The ``skill_gate_requests`` row the gate wrote, or None."""
    return backend_json(
        "import json\n"
        "from database import db\n"
        f"print(json.dumps(db.get_gate_request({request_id!r}), default=str))\n")


def _latest_login_code(email: str) -> Optional[str]:
    return backend_json(
        "import json\n"
        "from sqlalchemy import select\n"
        "from db.engine import get_engine\n"
        "from db.tables import email_login_codes as t\n"
        "with get_engine().connect() as c:\n"
        "    r = c.execute(select(t.c.code).where(t.c.email == "
        f"{email.lower()!r}, t.c.purpose.is_(None), t.c.used_at.is_(None))"
        ".order_by(t.c.created_at.desc()).limit(1)).first()\n"
        "print(json.dumps(r[0] if r else None))\n")


def sign_in_person(email: str) -> str:
    """A session token for ``email`` through the real email-code sign-in. The
    address must already be allowed (the caller provisions it)."""
    r = requests.post(f"{API}/api/auth/email/request", json={"email": email}, timeout=30)
    assert r.status_code == 200, f"requesting a sign-in code answered {r.status_code}: {r.text[:300]}"
    code = _latest_login_code(email)
    assert code, f"the platform wrote no sign-in code for {email} (is the address allowed?)"
    r = requests.post(f"{API}/api/auth/email/verify", json={"email": email, "code": code}, timeout=30)
    assert r.status_code == 200, f"verifying the sign-in code answered {r.status_code}: {r.text[:300]}"
    token = r.json().get("access_token")
    assert token, "the sign-in answered 200 with no access token"
    return token


class Person:
    """A signed-in person: their email, platform user id and their own session."""

    def __init__(self, email: str, user_id: int, token: str):
        self.email = email
        self.user_id = user_id
        self._headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **kw):
        return requests.get(f"{API}{path}", headers=self._headers, timeout=30, **kw)

    def post(self, path: str, **kw):
        return requests.post(f"{API}{path}", headers=self._headers, timeout=30, **kw)

    def patch(self, path: str, **kw):
        return requests.patch(f"{API}{path}", headers=self._headers, timeout=30, **kw)


def email_sign_in_enabled(client) -> bool:
    """Whether the target lets a person sign in by email code (the auth mode
    the platform states, never guessed)."""
    try:
        body = requests.get(f"{API}/api/auth/mode", timeout=10).json()
    except Exception:  # noqa: BLE001
        return False
    return bool(body.get("email_auth_enabled")) and body.get("email_code_login_enabled", True) is not False
