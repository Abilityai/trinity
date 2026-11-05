"""Terms of use per instance (abilityai/trinity-enterprise#1107).

Related flow: docs/memory/feature-flows/terms-of-use.md

The terms become an OSS instance setting with a shipped default, and the gate
reaches every signed-in person on every door — an admin arriving through the
panel included — not only invited Workspace-only members (ent#1054).

What is pinned, each against the REAL `get_current_user` wiring and the real
`routers/terms.py` router, on the unit island's real SQLite file:

* the default title/text/version are served when nothing is configured;
* `PUT /api/terms` saves and stamps a NEW version; empty and oversized text are
  refused by name (422); restore-default is one action;
* an admin JWT with no acceptance row is refused 403 `terms_required` on an
  ordinary route and admitted on a `@terms_route`;
* accepting records (version, time, account) and lifts the refusal; a version
  bump refuses again; a stale version is 409;
* the acceptance list is admin-gated and refuses an agent key;
* setup resolves no principal (the admin claim precedes any acceptance), and
  `main.py` registers the router.

Modules are imported inside each test: the unit conftest evicts
`dependencies` between tests.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"

ADMIN = {"id": 41, "username": "admin", "email": "admin@example.com", "role": "admin", "suspended_at": None}
MEMBER = {"id": 42, "username": "pat@example.com", "email": "pat@example.com", "role": "user", "suspended_at": None}


@pytest.fixture(autouse=True)
def clean_terms():
    """No terms configured, no acceptance rows, no provider — before and after."""
    from database import db
    from db.engine import get_engine
    from services import terms_gate

    def reset():
        terms_gate.clear_provider()
        for key in ("terms_required", "terms_version", "terms_text", "terms_title"):
            try:
                db.delete_setting(key)
            except Exception:  # noqa: BLE001
                pass
        with get_engine().begin() as conn:
            conn.exec_driver_sql("DELETE FROM terms_acceptances")

    reset()
    yield
    reset()


@pytest.fixture
def jwt_as(monkeypatch):
    """A real platform JWT whose `sub` resolves to `row`."""
    import database
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    def make(row):
        monkeypatch.setattr(
            database.db, "get_user_by_username", lambda name, *_a, **_k: dict(row, username=name)
        )
        # The SPA's sign-in mints `interactive: true` (ruling A); that is the
        # session the gate asks. A bare token is never asked.
        return jwt.encode(
            {"sub": row["username"], "exp": int(time.time()) + 300, "interactive": True},
            SECRET_KEY, algorithm=ALGORITHM,
        )

    return make


@pytest.fixture
def agent_key(monkeypatch):
    """An agent-scoped MCP key owned by an admin."""
    import database

    monkeypatch.setattr(database.db, "validate_mcp_api_key", lambda *_a, **_k: {
        "scope": "agent", "agent_name": "atlas", "key_id": "k1", "key_name": "agent",
        "user_id": "owner", "user_email": "admin@example.com",
    })
    monkeypatch.setattr(database.db, "get_user_by_email", lambda *_a, **_k: dict(ADMIN))
    monkeypatch.setattr(database.db, "get_agent_ephemeral_info", lambda *_a, **_k: {"is_ephemeral": False})
    return "trinity_mcp_fake_agent_key"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _accept(client, token):
    """The admin's own first visit is not exempt: accept before editing."""
    version = client.get("/api/terms/current", headers=_bearer(token)).json()["version"]
    r = client.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    return version


def _app():
    """The real terms router beside a plain operator route and a marked one,
    all through the real `get_current_user`."""
    import dependencies as deps
    from routers.terms import router as terms_router

    probe = APIRouter(prefix="/api/probe")

    @probe.get("/plain")
    async def plain(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    @probe.get("/marked")
    @deps.terms_route("probe: it must answer before acceptance")
    async def marked(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    @probe.get("/workspace")
    @deps.workspace_route("probe: the Workspace calls it")
    async def workspace(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    app = FastAPI()
    app.include_router(terms_router)
    app.include_router(probe)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# The writer validates before it writes, and writes nothing for no change
# --------------------------------------------------------------------------- #

class TestTheWriterIsAtomic:
    """Merge-train findings on ent#1107: C3 (a 422 on the title must not have
    published the text under the old version), W7 (an identical save stamps
    nothing) and C4 (DELETE on the generic settings route must refuse the four
    keys exactly as PUT does, or the terms route is not the only writer)."""

    def test_a_refused_title_publishes_nothing(self):
        from services import terms_defaults, terms_service
        from services.terms_service import TermsError

        before = terms_service.current_terms()
        with pytest.raises(TermsError) as refused:
            terms_service.set_terms(text="## New text", title="t" * 201)
        assert refused.value.code == "terms_title_too_long"
        after = terms_service.current_terms()
        assert after == before, "a refused save must leave every field as it was"
        assert after["text"] == terms_defaults.DEFAULT_TERMS_TEXT
        assert after["version"] == terms_defaults.DEFAULT_TERMS_VERSION

    def test_an_identical_save_stamps_no_version_and_records_nothing(self):
        from database import db
        from models import User
        from services import terms_service

        author = User(id=41, username="admin", email="admin@example.com", role="admin")
        first = terms_service.set_terms(text="## Custom", title="Custom terms", author=author, now="2026-11-04T10:00:00Z")
        assert first["version"] == "2026-11-04T10:00:00Z"
        again = terms_service.set_terms(text="## Custom", title="Custom terms", author=author, now="2026-11-04T11:00:00Z")
        assert again["version"] == "2026-11-04T10:00:00Z", "the same text is the same version"
        assert db.get_terms_acceptance(41, "2026-11-04T11:00:00Z") is None
        assert db.list_terms_acceptances() and len(db.list_terms_acceptances()) == 1
        # The switch alone is still written on an identical text.
        off = terms_service.set_terms(text="## Custom", title="Custom terms", required=False, author=author)
        assert off["required"] is False and off["version"] == "2026-11-04T10:00:00Z"

    @pytest.mark.parametrize("key", ["terms_required", "terms_version", "terms_text", "terms_title"])
    def test_the_generic_settings_route_refuses_the_keys_on_both_verbs(self, jwt_as, key):
        """PUT and DELETE, executed, on the real settings router: 422 and no row
        change either way, so the terms route is the only writer in fact."""
        from fastapi import FastAPI
        from database import db
        from routers.settings import router as settings_router
        from services import terms_service

        app = FastAPI()
        app.include_router(settings_router)
        app.include_router(__import__("routers.terms", fromlist=["router"]).router)
        client = TestClient(app)
        token = jwt_as(ADMIN)
        terms_service.set_terms(text="## Custom", title="Custom terms", now="2026-11-04T10:00:00Z")
        _accept(client, token)  # the admin's own visit: accept the custom text first
        before = terms_service.current_terms()

        put = client.put(f"/api/settings/{key}", json={"value": "x"}, headers=_bearer(token))
        assert put.status_code == 422, put.text
        assert "PUT /api/terms" in str(put.json()["detail"])
        delete = client.delete(f"/api/settings/{key}", headers=_bearer(token))
        assert delete.status_code == 422, delete.text
        assert "PUT /api/terms" in str(delete.json()["detail"])
        assert terms_service.current_terms() == before
        assert db.get_setting_value("terms_text", None) == "## Custom"


# --------------------------------------------------------------------------- #
# The accept route consults the provider's door (W2)
# --------------------------------------------------------------------------- #

class _DoorProvider:
    """A stub provider: `answer` is what `decision` returns, or raises."""

    def __init__(self, answer):
        self.answer = answer

    def decision(self, user):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def door_for(self, user):
        return "inv_1"


class TestTheAcceptRouteConsultsTheDoor:
    """W2 on ent#1107: the OSS accept route stamped the provider's `door_for`
    on the row but never asked whether that door was OPEN, so a person whose
    invitation was revoked could accept through the OSS route and be admitted.
    `terms_gate.assert_door_open` is the seam: a provider decision naming a
    closed door (`invite_expired` / `invite_revoked`) refuses the accept with
    that code; a plain `terms_required` is what accepting answers."""

    @pytest.mark.parametrize("code", ["invite_revoked", "invite_expired"])
    def test_a_closed_door_refuses_the_accept_with_its_code(self, jwt_as, code):
        from database import db
        from services import terms_gate

        terms_gate.register_provider(_DoorProvider({"required": True, "version": "default-1", "reason": code}))
        client = _app()
        token = jwt_as(MEMBER)
        version = client.get("/api/terms/current", headers=_bearer(token)).json()["version"]
        r = client.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == code
        assert db.get_terms_acceptance(MEMBER["id"], version) is None

    def test_an_open_door_lets_the_accept_through_and_stamps_it(self, jwt_as):
        from database import db
        from services import terms_gate

        terms_gate.register_provider(_DoorProvider({"required": True, "version": "default-1", "reason": "terms_required"}))
        client = _app()
        token = jwt_as(MEMBER)
        version = client.get("/api/terms/current", headers=_bearer(token)).json()["version"]
        r = client.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))
        assert r.status_code == 200, r.text
        assert db.get_terms_acceptance(MEMBER["id"], version)["door"] == "inv_1"

    def test_a_broken_provider_refuses_a_workspace_only_persons_accept_and_not_an_admins(self, jwt_as):
        """The gate's own fault policy, applied to the accept: fail closed for
        the invitee the door is for, ignored for operator-and-above."""
        from services import terms_gate

        terms_gate.register_provider(_DoorProvider(RuntimeError("broken")))
        client = _app()
        member = jwt_as(MEMBER)
        version = client.get("/api/terms/current", headers=_bearer(member)).json()["version"]
        r = client.post("/api/terms/accept", json={"version": version}, headers=_bearer(member))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "terms_required"
        admin = jwt_as(ADMIN)
        r = client.post("/api/terms/accept", json={"version": version}, headers=_bearer(admin))
        assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The shipped default and the setting
# --------------------------------------------------------------------------- #

class TestTheDefaultAndTheSetting:
    def test_the_default_terms_are_served_when_nothing_is_configured(self, jwt_as):
        from services import terms_defaults

        r = _app().get("/api/terms/current", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["required"] is True
        assert body["is_default"] is True
        assert body["version"] == terms_defaults.DEFAULT_TERMS_VERSION
        assert body["title"] == terms_defaults.DEFAULT_TERMS_TITLE
        assert body["text"] == terms_defaults.DEFAULT_TERMS_TEXT
        assert body["accepted"] is False

    def test_saving_stamps_a_new_version_and_restore_brings_the_default_back(self, jwt_as):
        from services import terms_defaults

        c = _app()
        before = _accept(c, jwt_as(ADMIN))
        r = c.put("/api/terms", json={"title": "House rules", "text": "# Ours\n\nBe kind."},
                  headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        saved = r.json()
        assert saved["title"] == "House rules" and saved["text"] == "# Ours\n\nBe kind."
        assert saved["is_default"] is False
        assert saved["version"] != before and saved["version"] != terms_defaults.DEFAULT_TERMS_VERSION
        # The admin who wrote the version has accepted it by writing it — they are
        # not bounced out of Settings by their own save.
        assert saved["accepted"] is True
        assert c.get("/api/probe/plain", headers=_bearer(jwt_as(ADMIN))).status_code == 200

        r = c.post("/api/terms/restore-default", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        assert r.json()["is_default"] is True
        assert r.json()["version"] == terms_defaults.DEFAULT_TERMS_VERSION
        assert r.json()["text"] == terms_defaults.DEFAULT_TERMS_TEXT

    def test_empty_text_is_refused_by_name(self, jwt_as):
        c = _app()
        _accept(c, jwt_as(ADMIN))
        r = c.put("/api/terms", json={"title": "x", "text": "   \n  "}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "terms_text_empty"

    def test_oversized_text_is_refused_by_name(self, jwt_as):
        from services import terms_service

        big = "x" * (terms_service.TERMS_TEXT_MAX_BYTES + 1)
        c = _app()
        _accept(c, jwt_as(ADMIN))
        r = c.put("/api/terms", json={"title": "x", "text": big}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "terms_text_too_large"

    def test_the_switch_can_be_turned_off_and_then_nothing_is_gated(self, jwt_as):
        c = _app()
        _accept(c, jwt_as(ADMIN))
        r = c.put("/api/terms", json={"required": False}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        assert r.json()["required"] is False
        # Nobody is gated now — a member who never accepted included.
        r = c.get("/api/probe/workspace", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 200, r.text
        r = c.get("/api/terms/current", headers=_bearer(jwt_as(MEMBER)))
        assert r.json()["required"] is False

    def test_the_editor_is_admin_and_interactive_only(self, jwt_as, agent_key):
        c = _app()
        r = c.put("/api/terms", json={"text": "x"}, headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 403
        r = c.put("/api/terms", json={"text": "x"}, headers=_bearer(agent_key))
        assert r.status_code == 403

    def test_the_generic_settings_catch_all_refuses_the_terms_keys(self):
        """The validated route is the only writer (the #506 shape)."""
        from routers.settings import generic

        for key in ("terms_text", "terms_title", "terms_version", "terms_required"):
            assert key in generic.TERMS_SETTING_KEYS


# --------------------------------------------------------------------------- #
# The gate on every door
# --------------------------------------------------------------------------- #

class TestTheGateOnEveryDoor:
    @pytest.mark.parametrize("row", [ADMIN, MEMBER], ids=["admin", "member"])
    def test_a_person_with_no_acceptance_is_refused_on_an_ordinary_route(self, jwt_as, row):
        from services import terms_defaults

        r = _app().get("/api/probe/plain" if row is ADMIN else "/api/terms/current", headers=_bearer(jwt_as(row)))
        if row is ADMIN:
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["code"] == "terms_required"
            assert r.json()["detail"]["version"] == terms_defaults.DEFAULT_TERMS_VERSION
        else:
            assert r.status_code == 200  # the read itself is exempt

    def test_a_marked_route_answers_before_acceptance(self, jwt_as):
        r = _app().get("/api/probe/marked", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text

    def test_accepting_records_the_row_and_lifts_the_refusal(self, jwt_as):
        from database import db

        c = _app()
        version = c.get("/api/terms/current", headers=_bearer(jwt_as(ADMIN))).json()["version"]
        r = c.post("/api/terms/accept", json={"version": version}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True and r.json()["version"] == version

        rows = db.list_terms_acceptances(10)
        assert len(rows) == 1
        assert rows[0]["user_id"] == ADMIN["id"]
        assert rows[0]["terms_version"] == version
        assert rows[0]["accepted_at"]

        r = c.get("/api/probe/plain", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        assert c.get("/api/terms/current", headers=_bearer(jwt_as(ADMIN))).json()["accepted"] is True

        again = c.post("/api/terms/accept", json={"version": version}, headers=_bearer(jwt_as(ADMIN)))
        assert again.status_code == 200 and again.json()["created"] is False
        assert len(db.list_terms_acceptances(10)) == 1

    def test_a_new_version_asks_everyone_else_again(self, jwt_as):
        """A member accepted the default; the admin saves a new text. The member
        is refused at the next visit; the admin, who wrote it, is not."""
        c = _app()
        version = c.get("/api/terms/current", headers=_bearer(jwt_as(MEMBER))).json()["version"]
        c.post("/api/terms/accept", json={"version": version}, headers=_bearer(jwt_as(MEMBER)))
        assert c.get("/api/probe/workspace", headers=_bearer(jwt_as(MEMBER))).status_code == 200

        _accept(c, jwt_as(ADMIN))
        r = c.put("/api/terms", json={"text": "New rules."}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        new_version = r.json()["version"]
        assert new_version != version

        r = c.get("/api/probe/workspace", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "terms_required"
        assert r.json()["detail"]["version"] == new_version
        assert c.get("/api/probe/plain", headers=_bearer(jwt_as(ADMIN))).status_code == 200

    def test_a_stale_version_is_refused(self, jwt_as):
        r = _app().post("/api/terms/accept", json={"version": "not-the-one"}, headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "version_stale"

    def test_an_agents_key_is_never_gated(self, agent_key):
        """The owner has not accepted; the agent's own runtime keeps working."""
        import dependencies as deps
        from starlette.requests import Request

        req = Request({
            "type": "http", "method": "GET", "path": "/api/anything", "root_path": "",
            "query_string": b"", "scheme": "http", "server": ("backend", 8000),
            "headers": [(b"host", b"backend:8000")],
        })
        user = asyncio.run(deps.resolve_platform_user_unfloored(req, agent_key))
        assert user.agent_name == "atlas"

    def test_a_registered_provider_still_answers_the_door(self, jwt_as):
        """An optional module's provider adds the invitation door on top."""
        from services import terms_gate

        class Door:
            def decision(self, user):
                return {"required": True, "version": "v", "reason": "invite_revoked"}

        terms_gate.register_provider(Door())
        r = _app().get("/api/probe/plain", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "invite_revoked"

    def test_a_broken_provider_cannot_lock_an_admin_out(self, jwt_as):
        """The door layer is for invitees; a fault in it is ignored for the
        people who could fix it — they still face the OSS decision."""
        from services import terms_gate

        class Broken:
            def decision(self, user):
                raise RuntimeError("module broken")

        terms_gate.register_provider(Broken())
        c = _app()
        r = c.get("/api/probe/plain", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "terms_required"
        version = c.get("/api/terms/current", headers=_bearer(jwt_as(ADMIN))).json()["version"]
        c.post("/api/terms/accept", json={"version": version}, headers=_bearer(jwt_as(ADMIN)))
        assert c.get("/api/probe/plain", headers=_bearer(jwt_as(ADMIN))).status_code == 200
        # …while a Workspace-only person is still refused (fail closed for the invitees).
        r = c.get("/api/terms/current", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 200  # exempt read
        r = c.get("/api/probe/workspace", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "terms_required"


# --------------------------------------------------------------------------- #
# The admin record
# --------------------------------------------------------------------------- #

class TestTheAdminRecord:
    def test_the_list_is_admin_gated_and_refuses_an_agent_key(self, jwt_as, agent_key):
        c = _app()
        r = c.get("/api/terms/acceptances", headers=_bearer(jwt_as(MEMBER)))
        assert r.status_code == 403
        r = c.get("/api/terms/acceptances", headers=_bearer(agent_key))
        assert r.status_code == 403

    def test_the_list_shows_version_time_and_account(self, jwt_as):
        c = _app()
        version = c.get("/api/terms/current", headers=_bearer(jwt_as(ADMIN))).json()["version"]
        c.post("/api/terms/accept", json={"version": version}, headers=_bearer(jwt_as(ADMIN)))
        r = c.get("/api/terms/acceptances", headers=_bearer(jwt_as(ADMIN)))
        assert r.status_code == 200, r.text
        [row] = r.json()
        assert row["user_id"] == ADMIN["id"] and row["email"] == ADMIN["email"]
        assert row["terms_version"] == version and row["accepted_at"]


# --------------------------------------------------------------------------- #
# Setup precedes acceptance; the router is registered
# --------------------------------------------------------------------------- #

class TestWiring:
    def test_setup_resolves_no_principal(self):
        """The admin claim is unauthenticated by construction, so an install can
        never lock itself out behind terms nobody has accepted yet."""
        tree = ast.parse((BACKEND / "routers/setup.py").read_text(encoding="utf-8"))
        names = {
            getattr(n, "id", None) or getattr(n, "attr", None)
            for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute))
        }
        assert "get_current_user" not in names
        assert "resolve_platform_user_unfloored" not in names

    def test_main_registers_the_terms_router(self):
        tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
        included = {
            getattr(n.args[0], "id", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "include_router" and n.args
        }
        assert "terms_router" in included

    def test_the_terms_router_declares_its_mcp_surface(self):
        first = (BACKEND / "routers/terms.py").read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# mcp: none")


class TestAPortalPersonIsRecorded:
    """Ruling A(1): a Workspace portal session (the emailed-code sign-in) has no
    `users` row, so its acceptance is a row with `user_id` NULL keyed by
    (email, version); a platform account keeps (user_id, version)."""

    def test_a_session_row_has_no_user_id_and_is_one_per_email_and_version(self):
        from database import db

        first = db.insert_terms_acceptance(None, "Client@Example.com", "v1", "2026-11-04T00:00:00Z",
                                           actor_ip="10.0.0.9")
        assert first is not None
        # The second accept of the same version by the same address is a no-op
        # (UNIQUE(user_id, version) alone treats two NULLs as distinct).
        assert db.insert_terms_acceptance(None, "client@example.com", "v1", "2026-11-04T00:00:01Z") is None
        assert db.insert_terms_acceptance(None, "client@example.com", "v2", "2026-11-04T00:00:02Z") is not None

        row = db.get_terms_acceptance_by_email("CLIENT@example.com", "v1")
        assert row is not None
        assert row["user_id"] is None and row["email"] == "client@example.com"
        assert row["door"] is None and row["actor_ip"] == "10.0.0.9"
        assert db.get_terms_acceptance_by_email("client@example.com", "v3") is None

    def test_a_platform_row_still_keys_on_the_account(self):
        from database import db

        assert db.insert_terms_acceptance(ADMIN["id"], ADMIN["email"], "v1", "2026-11-04T00:00:00Z") is not None
        assert db.insert_terms_acceptance(ADMIN["id"], ADMIN["email"], "v1", "2026-11-04T00:00:01Z") is None
        # The account's row answers an email lookup too: the same person.
        assert db.get_terms_acceptance_by_email(ADMIN["email"], "v1")["user_id"] == ADMIN["id"]

    def test_every_schema_home_declares_user_id_nullable_and_the_session_key(self):
        """Both tracks are unshipped, so the DDL changed in place in all four homes."""
        homes = {
            "schema.py": (BACKEND / "db" / "schema.py").read_text(),
            "migrations.py": (BACKEND / "db" / "migrations.py").read_text(),
            "tables.py": (BACKEND / "db" / "tables.py").read_text(),
            "0118": (BACKEND / "migrations" / "versions" / "0118_terms_acceptances.py").read_text(),
        }
        for name, src in homes.items():
            body = src[src.index("terms_acceptances"):]
            assert "uq_terms_acceptances_email_version" in body, name
            assert "user_id IS NULL" in body, name
        schema_table = homes["schema.py"][homes["schema.py"].index('"terms_acceptances": """'):]
        assert "user_id INTEGER NOT NULL" not in schema_table[: schema_table.index('""",')]
        entry = homes["migrations.py"][homes["migrations.py"].index("def _migrate_terms_acceptances_table"):]
        assert "user_id INTEGER NOT NULL" not in entry[: entry.index("\ndef ")]
        assert 'Column("user_id", Integer, nullable=True)' in homes["tables.py"][homes["tables.py"].index("terms_acceptances = Table"):]
        assert 'sa.Column("user_id", sa.Integer(), nullable=True)' in homes["0118"]

    def test_a_development_database_on_the_not_null_shape_is_rebuilt_with_its_rows(self, tmp_path):
        import sqlite3
        from db.migrations import _migrate_terms_acceptances_table

        conn = sqlite3.connect(str(tmp_path / "old.db"))
        cur = conn.cursor()
        cur.execute("""CREATE TABLE terms_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, email TEXT,
            terms_version TEXT NOT NULL, accepted_at TEXT NOT NULL, door TEXT, actor_ip TEXT,
            UNIQUE(user_id, terms_version))""")
        cur.execute("INSERT INTO terms_acceptances (user_id, email, terms_version, accepted_at) "
                    "VALUES (41, 'admin@example.com', 'default-1', '2026-11-04T00:00:00Z')")
        conn.commit()
        _migrate_terms_acceptances_table(cur, conn)
        _migrate_terms_acceptances_table(cur, conn)  # idempotent on the new shape
        cur.execute("PRAGMA table_info(terms_acceptances)")
        assert {r[1]: r[3] for r in cur.fetchall()}["user_id"] == 0
        cur.execute("INSERT INTO terms_acceptances (user_id, email, terms_version, accepted_at) "
                    "VALUES (NULL, 'client@example.com', 'default-1', '2026-11-04T00:00:01Z')")
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO terms_acceptances (user_id, email, terms_version, accepted_at) "
                        "VALUES (NULL, 'client@example.com', 'default-1', '2026-11-04T00:00:02Z')")
        cur.execute("SELECT user_id, email FROM terms_acceptances ORDER BY id")
        assert cur.fetchall() == [(41, "admin@example.com"), (None, "client@example.com")]
        conn.close()


CLIENT = "client@example.com"


def _portal_request(path="/api/enterprise/client-portal/my-agents", endpoint=None):
    """An ASGI-scope request; `endpoint` (optional) is what `scope["route"]` names."""
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": path, "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", b"backend:8000")],
    }
    if endpoint is not None:
        scope["route"] = type("R", (), {"endpoint": endpoint})()
    return Request(scope)


@pytest.fixture
def portal_session(monkeypatch):
    """A real Workspace portal session token (the emailed-code sign-in), the
    client not blocked."""
    import client_portal.db as portal_db
    from dependencies import PORTAL_MINT_OTP, create_portal_session_token

    monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)

    def make(email=CLIENT, minted_by=PORTAL_MINT_OTP, interactive=True):
        # The Workspace SPA's sign-in mints `interactive: true` (ruling A); that
        # is the session the gate asks. A bare token is never asked.
        return create_portal_session_token(email, minted_by=minted_by, interactive=interactive)

    return make


def _portal_door(token, request=None):
    """`get_portal_principal` on `token`: the principal, or the HTTPException."""
    from client_portal.portal_auth import get_portal_principal
    from fastapi import HTTPException
    from starlette.responses import Response

    try:
        return asyncio.run(get_portal_principal(request or _portal_request(), Response(), token=token))
    except HTTPException as e:
        return e


class TestThePortalSessionIsGated:
    """Ruling A(1): a Workspace portal session (the emailed-code sign-in, no
    `users` row) is gated like every other visit — refused 403 `terms_required`
    until the address has an acceptance row for the current version, which the
    terms routes record on that same token."""

    def test_a_session_with_no_acceptance_is_refused_on_the_portal_door(self, portal_session):
        from services import terms_service

        refused = _portal_door(portal_session())
        assert getattr(refused, "status_code", None) == 403, refused
        assert refused.detail["code"] == "terms_required"
        assert refused.detail["version"] == terms_service.current_version()
        assert refused.detail["message"]

    def test_a_bare_or_delegate_minted_session_is_not_a_visit(self, portal_session):
        """Ruling A on the portal half: only a session the Workspace SPA
        declared (`"client": "web"` on the verify) carries the claim and is
        asked; a harness-minted or delegate-minted token is not."""
        from dependencies import PORTAL_MINT_DELEGATE

        assert _portal_door(portal_session(interactive=False)).email == CLIENT
        assert _portal_door(portal_session(minted_by=PORTAL_MINT_DELEGATE, interactive=False)).email == CLIENT

    def test_the_verify_route_mints_the_claim_only_for_the_spa(self, monkeypatch):
        import client_portal.service as service
        from client_portal.router import router as portal_router
        from dependencies import portal_session_is_interactive, portal_session_minted_by

        monkeypatch.setattr(service.login_policy_gate, "email_code_allowed_for", lambda *_a: True)
        import database
        monkeypatch.setattr(database.db, "verify_login_code", lambda *_a, **_k: True)
        monkeypatch.setattr(service, "platform_login_admits", lambda *_a: False)
        monkeypatch.setattr(service, "email_has_access", lambda *_a: True)
        app = FastAPI()
        app.include_router(portal_router)
        c = TestClient(app)
        web = c.post("/api/enterprise/client-portal/auth/verify",
                     json={"email": CLIENT, "code": "123456", "client": "web"})
        assert web.status_code == 200, web.text
        assert portal_session_is_interactive(web.json()["token"]) is True
        assert portal_session_minted_by(web.json()["token"]) == "otp"
        bare = c.post("/api/enterprise/client-portal/auth/verify", json={"email": CLIENT, "code": "123456"})
        assert bare.status_code == 200, bare.text
        assert portal_session_is_interactive(bare.json()["token"]) is False

    def test_rotation_carries_the_claim(self):
        """`renew_portal_session` needs Redis; the carry is pinned in the source."""
        src = (BACKEND / "dependencies.py").read_text()
        renew = src[src.index("def renew_portal_session"):src.index("def decode_token")]
        assert 'interactive=payload.get("interactive") is True' in renew
        store = pathlib.Path(__file__).resolve().parents[2] / "src" / "frontend" / "src" / "stores" / "clientPortal.js"
        assert "client: SPA_CLIENT" in store.read_text()[store.read_text().index("async verifyCode"):]

    def test_the_switch_off_admits_the_session(self, portal_session):
        from database import db

        db.set_setting("terms_required", "false")
        principal = _portal_door(portal_session())
        assert principal.email == CLIENT and principal.is_platform is False

    def test_a_marked_route_answers_before_acceptance(self, portal_session):
        import dependencies as deps

        @deps.terms_route("probe: reachable before acceptance")
        def marked():  # pragma: no cover — only its mark is read
            pass

        principal = _portal_door(portal_session(), _portal_request(endpoint=marked))
        assert principal.email == CLIENT

    def test_the_terms_routes_answer_the_session_and_record_the_email(self, portal_session, jwt_as):
        from database import db
        from services import terms_service

        c = _app()
        token = portal_session()
        r = c.get("/api/terms/current", headers=_bearer(token))
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is False and r.json()["text"]
        version = r.json()["version"]

        r = c.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))
        assert r.status_code == 200, r.text
        assert r.json() == {"accepted": True, "created": True, "version": version,
                            "accepted_at": r.json()["accepted_at"]}

        rows = db.list_terms_acceptances(10)
        assert len(rows) == 1
        row = rows[0]
        assert row["user_id"] is None and row["email"] == CLIENT
        assert row["terms_version"] == version and row["accepted_at"]
        assert row["door"] is None and row["actor_ip"] == "testclient"
        assert terms_service.decision(terms_service.PortalPerson(CLIENT))["required"] is False

        # The door opens, the read says so, and a second accept is a no-op.
        assert _portal_door(token).email == CLIENT
        assert c.get("/api/terms/current", headers=_bearer(token)).json()["accepted"] is True
        again = c.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))
        assert again.status_code == 200 and again.json()["created"] is False
        assert len(db.list_terms_acceptances(10)) == 1
        # The admin record lists the session row by email, with no account.
        admin = jwt_as(ADMIN)
        _accept(c, admin)
        listed = c.get("/api/terms/acceptances", headers=_bearer(admin))
        assert listed.status_code == 200, listed.text
        assert [(x["user_id"], x["email"]) for x in listed.json()
                if x["email"] == CLIENT] == [(None, CLIENT)]

    def test_the_acceptance_is_by_address_not_by_token(self, portal_session):
        """A second session of the same address is not asked again; another
        address is."""
        c = _app()
        version = c.get("/api/terms/current", headers=_bearer(portal_session())).json()["version"]
        assert c.post("/api/terms/accept", json={"version": version},
                      headers=_bearer(portal_session("Client@Example.com"))).status_code == 200
        assert _portal_door(portal_session()).email == CLIENT
        assert _portal_door(portal_session("other@example.com")).status_code == 403

    def test_a_stale_version_is_refused_for_the_session_too(self, portal_session):
        c = _app()
        r = c.post("/api/terms/accept", json={"version": "not-current"}, headers=_bearer(portal_session()))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "version_stale"

    def test_a_blocked_client_is_refused_before_the_terms(self, monkeypatch, portal_session):
        import client_portal.db as portal_db

        token = portal_session()
        monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: True)
        for path in ("/api/terms/current",):
            r = _app().get(path, headers=_bearer(token))
            assert r.status_code == 403 and "revoked" in str(r.json()["detail"]).lower()
        assert _portal_door(token).detail == "Access has been revoked."

    def test_a_providers_fault_is_fail_closed_and_its_door_passes_through(self, portal_session):
        from services import terms_gate

        class Broken:
            def decision(self, user):
                raise RuntimeError("boom")

        terms_gate.register_provider(Broken())
        assert _portal_door(portal_session()).detail["code"] == "terms_required"

        class Closed:
            def decision(self, user):
                assert user.email == CLIENT and user.id is None
                return {"required": True, "version": "v", "reason": "invite_revoked"}

        terms_gate.register_provider(Closed())
        assert _portal_door(portal_session()).detail["code"] == "invite_revoked"
        # The accept route asks the door first, on the session too (W2).
        r = _app().post("/api/terms/accept", json={"version": "v"}, headers=_bearer(portal_session()))
        assert r.status_code == 403 and r.json()["detail"]["code"] == "invite_revoked"

    def test_a_platform_person_through_the_terms_routes_is_unchanged(self, jwt_as, agent_key):
        c = _app()
        assert c.get("/api/terms/current", headers=_bearer(jwt_as(MEMBER))).status_code == 200
        # An agent's key is still no person here, and a bare bad token is 401.
        assert c.get("/api/terms/current", headers=_bearer(agent_key)).status_code == 403
        assert c.post("/api/terms/accept", json={"version": "v"}, headers=_bearer(agent_key)).status_code == 403

    def test_a_credential_that_is_neither_gets_the_resolvers_own_answer(self):
        c = _app()
        assert c.get("/api/terms/current", headers=_bearer("not-a-token")).status_code == 401
        assert c.post("/api/terms/accept", json={"version": "v"}, headers=_bearer("not-a-token")).status_code == 401

    def test_the_shared_canvas_viewer_and_the_delegate_key_are_not_gated(self):
        """`workspace_email_from_request` serves a route anonymous visitors may
        open and a delegate key is an automation: neither consults the gate."""
        src = (BACKEND / "client_portal" / "portal_auth.py").read_text()
        body = src[src.index("def workspace_email_from_request"):src.index("async def get_portal_principal")]
        assert "terms_gate" not in body
        door = src[src.index("async def get_portal_principal"):]
        assert door.index("_reject_if_blocked(email)") < door.index("terms_gate.enforce_portal_session(")
        assert door.index("terms_gate.enforce_portal_session(") < door.index("_maybe_rotate(token, response)")

