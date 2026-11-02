"""trinity-enterprise#1066 — a secret pasted as a plain chat message is refused
before admission.

The secret field is where a credential goes; the composer is where it must not.
A message that LOOKS like a credential — a token with a known prefix, or a
`KEY=value` pair whose key names a credential — is refused by both Workspace
chat routes with 422 `secret_in_message` BEFORE anything is persisted, titled,
dispatched or billed: no session row, no execution row, no idempotency key
consumed. The refusal names the SHAPE that matched and points at the secret
field; it never carries the text.

The shapes are `utils/credential_sanitizer.SECRET_VALUE_PATTERNS` — the list
the execution-log redaction already runs — gaining two service-token shapes
(`ops_…`, `dop_v1_…`) so logs learn them at the same time. The `Bearer` /
`Basic` entries are deliberately NOT part of the guard: "Bearer" is a word a
person types in prose about an API, and a false refusal there teaches people
to route around the guard.

Driven through the real router with the real `get_portal_principal`; the
roster, the rate limiter and the service's two chat entry points are stood in
for and RECORDED, so "nothing was admitted" is an assertion over what the
service was asked to do, not over a mock's return value.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

PERSON = "person-1066-guard@example.com"
AGENT = "agent-1066-guard"
TOKEN = "portal-session-token"

# One value per shape the guard must catch. Fabricated; each matches only by
# prefix and length, which is all the sanitizer ever looks at.
SECRETS = [
    pytest.param("sk-ant-api03-" + "A" * 40, "sk", id="sk-family"),
    pytest.param("ghp_" + "b" * 36, "github", id="github-pat"),
    pytest.param("github_pat_" + "c" * 60, "github", id="github-fine-grained"),
    pytest.param("xoxb-1234-5678-" + "d" * 24, "slack", id="slack-bot"),
    pytest.param("AKIA" + "E" * 16, "aws", id="aws-access-key"),
    pytest.param("trinity_mcp_" + "f" * 32, "trinity_mcp", id="trinity-mcp-key"),
    pytest.param("ops_" + "g" * 40, "ops", id="service-account-ops"),
    pytest.param("dop_v1_" + "0123456789abcdef" * 4, "dop", id="service-token-dop"),
    pytest.param("here you go: OP_SERVICE_ACCOUNT_TOKEN=ops_" + "h" * 40, "ops", id="prefixed-in-prose"),
    pytest.param("VAULT_TOKEN=hvs.AAAAAAAAAAAAAAAAAAAAAAAA", "env_pair", id="sensitive-env-pair"),
    pytest.param('export MY_API_KEY="qwertyuiop1234567890"', "env_pair", id="quoted-env-pair"),
]

PROSE = [
    pytest.param("Please add a Bearer token header to the request", id="bearer-in-prose"),
    pytest.param("Use Basic auth against the staging API", id="basic-in-prose"),
    pytest.param("the sk-SK locale and ticket sk-42 are unrelated", id="short-sk"),
    pytest.param("set DEBUG_TOKEN=1 in the config", id="short-pair-value"),
    pytest.param("the author=bob field is harmless", id="harmless-key"),
    pytest.param("What is my current API key called?", id="plain-question"),
    pytest.param("ops_team meeting at 3", id="ops-prefix-too-short"),
]


# ---------------------------------------------------------------------------
# The shape detector itself
# ---------------------------------------------------------------------------


class TestSecretShape:
    @pytest.mark.parametrize("text,shape", SECRETS)
    def test_each_shape_is_named(self, text, shape):
        from utils.credential_sanitizer import secret_shape
        assert secret_shape(text) == shape

    @pytest.mark.parametrize("text", PROSE)
    def test_prose_about_credentials_is_not_a_secret(self, text):
        from utils.credential_sanitizer import secret_shape
        assert secret_shape(text) is None

    def test_every_redaction_pattern_is_named_or_excluded_by_the_guard(self):
        """The partition is complete: a shape added to `SECRET_VALUE_PATTERNS`
        must be given a name here or excluded on purpose — never silently left
        out of the guard while redaction already knows it."""
        from utils import credential_sanitizer as cs
        assert set(cs.SECRET_VALUE_PATTERNS) == set(cs._GUARD_SHAPES) | set(cs._GUARD_EXCLUDED)
        assert not set(cs._GUARD_SHAPES) & set(cs._GUARD_EXCLUDED)
        assert {p for p in cs._GUARD_EXCLUDED} == {p for p in cs.SECRET_VALUE_PATTERNS
                                                   if p.startswith(("Bearer", "Basic"))}

    def test_empty_is_none(self):
        from utils.credential_sanitizer import secret_shape
        assert secret_shape("") is None and secret_shape(None) is None

    @pytest.mark.parametrize("value", [
        "ops_" + "x" * 40,
        "dop_v1_" + "9" * 64,
    ])
    def test_the_two_new_shapes_are_redacted_from_logs_too_in_both_copies(self, value):
        """The shapes live in the list the redaction runs, in BOTH the backend
        and the agent-image copy (Invariant #5) — not in a guard-private list."""
        for rel in ("src/backend/utils/credential_sanitizer.py",
                    "docker/base-image/agent_server/utils/credential_sanitizer.py"):
            spec = importlib.util.spec_from_file_location("_cs_" + rel.replace("/", "_"), _REPO / rel)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            out = mod.sanitize_text(f"login failed for {value} (req_9)")
            assert value not in out and "req_9" in out, rel


# ---------------------------------------------------------------------------
# The two routes
# ---------------------------------------------------------------------------

_APP = None


def _app():
    global _APP
    if _APP is None:
        from fastapi import FastAPI
        from client_portal.router import router
        app = FastAPI()
        app.include_router(router)
        _APP = app
    return _APP


@pytest.fixture
def door(monkeypatch):
    from fastapi.testclient import TestClient
    from client_portal import db as portal_db
    from client_portal import portal_auth as pa
    from client_portal import router as r
    from client_portal import service
    from services import idempotency_service, rate_limiter

    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)
    monkeypatch.setattr(pa, "portal_session_needs_rotation", lambda t: False)
    monkeypatch.setattr(pa, "portal_session_minted_by", lambda t: "otp")
    monkeypatch.setattr(pa, "decode_portal_session", lambda t: PERSON if t == TOKEN else None)
    monkeypatch.setattr(r, "_require_roster", lambda *a, **k: None)
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(service, "validate_requested_model", lambda *a, **k: None)
    monkeypatch.setattr(service, "reply_context", lambda *a, **k: None)
    monkeypatch.setattr(service, "resolve_chat_tags", lambda *a, **k: [])
    monkeypatch.setattr(service, "validated_open_canvas", lambda *a, **k: None)

    async def no_attachments(*a, **k):
        return None
    monkeypatch.setattr(service, "resolve_turn_attachments", no_attachments)

    admitted = []

    async def portal_chat(agent_name, message, email, **kw):
        admitted.append(("chat", message))
        return {"session_id": "s1", "response": "ok", "execution_id": "e1"}

    async def start_portal_turn(agent_name, message, email, **kw):
        admitted.append(("stream", message))
        return {"execution_id": "e2", "session_id": "s1"}
    monkeypatch.setattr(service, "portal_chat", portal_chat)
    monkeypatch.setattr(service, "start_portal_turn", start_portal_turn)

    keys = []
    real_begin = idempotency_service.begin

    def begin(scope, key):
        keys.append((scope, key))
        return SimpleNamespace(replay=False, in_flight=False, execution_id=None, snapshot=None)
    monkeypatch.setattr(idempotency_service, "begin", begin)
    monkeypatch.setattr(idempotency_service, "complete", lambda *a, **k: None)
    monkeypatch.setattr(idempotency_service, "fail", lambda *a, **k: None)

    client = TestClient(_app(), raise_server_exceptions=True)
    return SimpleNamespace(client=client, admitted=admitted, keys=keys)


def _post(door, route, message, headers=None):
    h = {"Authorization": f"Bearer {TOKEN}"}
    h.update(headers or {})
    return door.client.post(f"/api/enterprise/client-portal/agents/{AGENT}/{route}",
                            json={"message": message}, headers=h)


class TestBothRoutesRefuseBeforeAdmission:
    @pytest.mark.parametrize("route", ["chat", "chat/stream"])
    @pytest.mark.parametrize("text,shape", SECRETS)
    def test_a_secret_is_a_named_422_and_nothing_is_admitted(self, door, route, text, shape):
        res = _post(door, route, text, headers={"Idempotency-Key": "k-1"})
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["code"] == "secret_in_message" and detail["shape"] == shape
        assert "secret field" in detail["message"]
        # never the text: not the whole message, not the token inside it
        token = text.split("=")[-1].split(":")[-1].strip().strip('"')
        assert token not in res.text and text not in res.text
        assert door.admitted == []
        assert door.keys == [], "a refused message must not consume the Idempotency-Key"

    @pytest.mark.parametrize("route", ["chat", "chat/stream"])
    @pytest.mark.parametrize("text", PROSE)
    def test_prose_is_admitted(self, door, route, text):
        res = _post(door, route, text)
        assert res.status_code in (200, 202), res.text
        assert [m for _, m in door.admitted] == [text]

    def test_the_refusal_comes_after_the_person_check(self, door):
        """An anonymous caller is 401 before the message is even looked at —
        the guard is not an oracle for whether a value looks like a secret."""
        res = door.client.post(f"/api/enterprise/client-portal/agents/{AGENT}/chat",
                               json={"message": "ghp_" + "b" * 36})
        assert res.status_code == 401 and door.admitted == []
