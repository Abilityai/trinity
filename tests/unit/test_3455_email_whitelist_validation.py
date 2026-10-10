"""#3455 / #3456 — the email whitelist validates what it stores, and can remove it.

Two defects from the same UI sweep, on one router:

* **#3455** — `POST /api/settings/email-whitelist` only lower-cased its input, so
  `a@`, `@b.com` and a 10 000-character string were all stored (200) as
  "emails". The whitelist is matched by exact, lower-cased address
  (`db/email_auth.py::is_email_whitelisted`) — there is no domain or wildcard
  entry form — so anything that is not one address is a row that can never
  match a login and only damages the table it is listed in.
* **#3456** — `DELETE /api/settings/email-whitelist/{email}` had no path
  converter. Starlette decodes `%2F` before routing, so a stored value
  containing `/` matched no route at all and answered 404 with the row still
  there. A slash is a legal local-part character, so this is not only about
  rows that predate validation.

Driven through the real composed settings router (so the `/{key}` catch-all and
every sibling are in play — Invariant #4) with the auth dependency overridden
and the whitelist table replaced by an in-memory stand-in.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""
import types
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Match

import routers.settings as rs  # the composed router
# #1028: collaborators are patched on the module that owns them.
import routers.settings.whitelist as rs_whitelist
from dependencies import get_current_user

pytestmark = pytest.mark.unit

URL = "/api/settings/email-whitelist"


def _principal(role="admin"):
    return types.SimpleNamespace(
        id=1, username="admin", email="admin@example.com", role=role,
        agent_name=None, connector_agent=None, mcp_scope=None,
    )


class _Whitelist:
    """The three accessors the router uses, with the DB layer's own matching
    rule (case-insensitive exact match) so a delete is judged the way
    `db/email_auth.py` judges it."""

    def __init__(self, rows=()):
        self.rows = list(rows)

    def list_whitelist(self, limit=100):
        return [{"id": i, "email": e} for i, e in enumerate(self.rows)]

    def add_to_whitelist(self, email, added_by, source="manual", default_role="user"):
        if any(r.lower() == email.lower() for r in self.rows):
            return False
        self.rows.append(email)
        return True

    def remove_from_whitelist(self, email):
        keep = [r for r in self.rows if r.lower() != email.lower()]
        removed = len(keep) != len(self.rows)
        self.rows = keep
        return removed


@pytest.fixture
def env(monkeypatch):
    app = FastAPI()
    app.include_router(rs.router)
    principal = {"user": _principal()}
    app.dependency_overrides[get_current_user] = lambda: principal["user"]

    table = _Whitelist()
    monkeypatch.setattr(rs_whitelist, "db", table)

    e = types.SimpleNamespace()
    e.client = TestClient(app, raise_server_exceptions=False)
    e.table = table
    e.as_ = lambda p: principal.__setitem__("user", p)
    return e


# ---------------------------------------------------------------------------
# #3455 — POST refuses what is not one email address, by name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, reason",
    [
        ("a@", "single address"),            # the issue's own repro
        ("@b.com", "single address"),        # the issue's own repro
        ("not-an-email", "single address"),
        ("a@b@example.com", "single address"),
        ("@", "single address"),
        ("", "required"),
        ("   ", "required"),
        ("user name@example.com", "whitespace"),
        ("user@exam\tple.com", "whitespace"),
        ("user@example.com\x00", "control"),
        # the issue's own repro
        pytest.param("a" * 10_000, "too long", id="10000-chars"),
        pytest.param("a" * 250 + "@example.com", "too long", id="262-chars"),
    ],
)
def test_a_value_that_is_not_one_address_is_refused_with_a_named_reason(env, value, reason):
    resp = env.client.post(URL, json={"email": value})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    # A string the UI can show as-is — not Pydantic's error array, not a 500.
    assert isinstance(detail, str)
    assert reason in detail
    assert env.table.rows == [], "a refused value must not reach the table"


def test_the_refusal_does_not_echo_an_oversized_value(env):
    """The reason is rendered next to the field; a 10 000-character echo would
    reproduce in the error the very overflow the refusal exists to prevent."""
    resp = env.client.post(URL, json={"email": "a" * 10_000})

    assert resp.status_code == 422
    assert len(resp.json()["detail"]) < 200


@pytest.mark.parametrize(
    "body",
    [{}, {"email": None}, {"email": 7}, ["user@example.com"], "user@example.com"],
)
def test_a_malformed_body_is_a_422_not_a_500(env, body):
    resp = env.client.post(URL, json=body)

    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], str)
    assert env.table.rows == []


def test_a_body_that_is_not_json_is_a_422_not_a_500(env):
    resp = env.client.post(
        URL, content=b"not json", headers={"Content-Type": "application/json"}
    )

    assert resp.status_code == 422, resp.text
    assert env.table.rows == []


@pytest.mark.parametrize(
    "sent, stored",
    [
        ("user@example.com", "user@example.com"),
        ("  User@Example.COM \n", "user@example.com"),   # trimmed + lower-cased
        ("first.last+tag@sub.example.com", "first.last+tag@sub.example.com"),
        # A slash is a legal local-part character — accepted here, and so it
        # must be removable below (#3456).
        ("team/ops@example.com", "team/ops@example.com"),
        # A self-hosted install whitelists intranet hosts; no dot is required.
        ("user@localhost", "user@localhost"),
        pytest.param("a" * 242 + "@example.com", "a" * 242 + "@example.com", id="exactly-254"),
    ],
)
def test_a_valid_address_is_stored_trimmed_and_lower_cased(env, sent, stored):
    resp = env.client.post(URL, json={"email": sent})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "email": stored}
    assert env.table.rows == [stored]


def test_a_duplicate_that_differs_only_by_whitespace_is_still_a_409(env):
    env.table.rows.append("user@example.com")

    resp = env.client.post(URL, json={"email": " USER@example.com "})

    assert resp.status_code == 409
    assert env.table.rows == ["user@example.com"]


def test_validation_does_not_come_before_the_admin_gate(env):
    env.as_(_principal(role="user"))

    resp = env.client.post(URL, json={"email": "a@"})

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# #3456 — DELETE removes any stored value, including one holding a slash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stored",
    [
        "team/ops@example.com",        # valid, and still unremovable before the fix
        "not/an/email",                # a bad row that predates validation
        "/leading@example.com",
        "trailing/",
        "a//b",
        "https://example.com/path?x=1#frag",
        "100% not an email",
        # the #3455 repro row has to go too
        pytest.param("a" * 10_000, id="10000-chars"),
    ],
)
def test_a_stored_value_is_removable_when_sent_url_encoded(env, stored):
    env.table.rows.extend(["keep@example.com", stored])

    resp = env.client.delete(f"{URL}/{quote(stored, safe='')}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "email": stored}
    assert env.table.rows == ["keep@example.com"]


def test_a_plain_address_is_still_removable(env):
    env.table.rows.extend(["keep@example.com", "user@example.com"])

    resp = env.client.delete(f"{URL}/{quote('user@example.com', safe='')}")

    assert resp.status_code == 200
    assert env.table.rows == ["keep@example.com"]


def test_removing_an_absent_value_is_still_a_404(env):
    env.table.rows.append("keep@example.com")

    resp = env.client.delete(f"{URL}/{quote('gone/now@example.com', safe='')}")

    assert resp.status_code == 404
    assert env.table.rows == ["keep@example.com"]


def test_delete_keeps_its_admin_gate(env):
    env.table.rows.append("team/ops@example.com")
    env.as_(_principal(role="user"))

    resp = env.client.delete(f"{URL}/{quote('team/ops@example.com', safe='')}")

    assert resp.status_code == 403
    assert env.table.rows == ["team/ops@example.com"]


def test_the_wider_delete_route_claims_no_sibling(env):
    """A `:path` parameter matches across segments, which is exactly how a
    route starts answering for its neighbours (Invariant #4). Every other
    settings route, at every method, must still be out of its reach."""
    routes = list(rs.router.routes)
    delete = next(r for r in routes if r.name == "remove_email_from_whitelist")

    claimed = []
    for other in routes:
        if other is delete:
            continue
        concrete = other.path.replace("{key}", "x").replace("{ref}", "x")
        for method in other.methods:
            scope = {"type": "http", "method": method, "path": concrete}
            if delete.matches(scope)[0] != Match.NONE:
                claimed.append(f"{method} {concrete}")
    assert claimed == []


def test_the_list_route_still_answers_beside_the_wider_delete(env):
    env.table.rows.append("user@example.com")

    resp = env.client.get(URL)

    assert resp.status_code == 200
    assert [r["email"] for r in resp.json()["whitelist"]] == ["user@example.com"]


def test_a_sibling_static_delete_is_not_routed_to_the_whitelist(env):
    """`DELETE /api/settings/<key>` belongs to the generic catch-all; if the
    whitelist route had widened past its own prefix this would answer with the
    whitelist's "not found in whitelist" 404 instead."""
    resp = env.client.delete("/api/settings/github-templates")

    assert "whitelist" not in resp.text
