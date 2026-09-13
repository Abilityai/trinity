"""ent#554 — a canvas leaves the Workspace: share links and PDF export.

The security decision this file exists to pin: **a share must never widen the
ent#438 audience by accident.** Two consequences, both tested here.

**A separate table, not a typed `agent_public_links` row.** That table has a
`type` column that looks purpose-built for this, and reusing it would have been
a real vulnerability: nothing in its read path filters on type —
`get_public_link_by_token`, `is_link_valid` and `routers/public.py::
_validate_public_link` all resolve a token whatever it is — so a canvas row
there would ALSO be a working public-CHAT token. Anyone sent a canvas would
have been able to talk to the agent. `type='site'` is the same trap already
sprung and unexploited only because nothing creates those rows today.

**The narrow scope is the default, everywhere it could be decided.** The column
default, the Pydantic default, the normaliser's fallback for an unrecognised
value, and the radio the dialog preselects. A link that reaches further than
the sharer understood is the failure this feature must not have, so every one
of those four places fails toward `authorized`.

The status vocabulary is the other half. `revoked` and `expired` are only ever
returned for a token that MATCHED a row — whoever holds such a link was already
told the canvas exists, so naming the state discloses nothing new (AC #2) —
while an unknown token and a canvas deleted out from under a link collapse into
one `not_found`, so a stranger guessing tokens learns nothing from the
difference.
"""
from __future__ import annotations

import pytest

from models import User


@pytest.fixture()
def share_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-share-554.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_canvases, agent_ownership, agent_sharing, users,
        schedule_executions,
    )
    m.create_all(get_engine(), tables=[
        agent_canvases, agent_ownership, agent_sharing, users, schedule_executions,
    ])

    # From the REAL DDL, not the metadata: `db/tables.py` is a shape
    # declaration and carries no server defaults (`pinned` and `scope` are both
    # bare `Column(...)` there), so a metadata-created table would not have the
    # `DEFAULT 'authorized'` this suite is partly here to prove ships.
    from conftest import ensure_schema_tables
    ensure_schema_tables("agent_canvas_shares", "agent_public_links",
                         "access_requests")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="owner", role="user",
                                          email="owner@example.com",
                                          created_at="t", updated_at="t"))
        conn.execute(insert(users).values(id=2, username="stranger", role="user",
                                          email="stranger@example.com",
                                          created_at="t", updated_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-a", owner_id=1, created_at="t"))
    yield str(db_file)


def _canvas(canvas_id="main"):
    from database import db
    return db.upsert_agent_canvas("agent-a", canvas_id, blocks=[], title="Board")


# --- the table choice -------------------------------------------------------

def test_a_canvas_token_is_not_a_public_chat_token(share_db):
    """The vulnerability this design avoids. A canvas share token must not
    resolve on the public-link surface at all."""
    from database import db
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")

    assert db.get_public_link_by_token(share["token"]) is None
    is_valid, _reason, _link = db.is_public_link_valid(share["token"])
    assert is_valid is False


def test_the_public_link_read_path_still_ignores_type():
    """Why the separate table is required rather than merely tidier.

    If this ever starts filtering on `type`, reusing `agent_public_links`
    becomes defensible — until then a second type in that table is a
    privilege-widening bug waiting for its first caller.
    """
    import inspect
    from db import public_links

    for fn in (public_links.PublicLinkOperations.get_public_link_by_token,
               public_links.PublicLinkOperations.is_link_valid):
        src = inspect.getsource(fn)
        assert "type" not in src.split("select(")[-1].split("where")[-1], (
            "the public-link read path now filters on type — revisit whether "
            "agent_canvas_shares should still be a separate table"
        )


# --- the narrow default -----------------------------------------------------

def test_every_place_a_scope_could_be_decided_defaults_narrow(share_db):
    """Four independent defaults, all failing toward `authorized`."""
    from database import db
    from db.canvas_shares import SCOPE_AUTHORIZED, normalize_scope
    from models import CanvasShareCreate
    _canvas()

    # 1. the normaliser's fallback for anything unrecognised
    assert normalize_scope("nonsense") == SCOPE_AUTHORIZED
    assert normalize_scope(None) == SCOPE_AUTHORIZED
    # 2. the request model's default
    assert CanvasShareCreate().scope == SCOPE_AUTHORIZED
    # 3. what actually lands in the row for a junk scope
    assert db.create_canvas_share("agent-a", "main", scope="admin")["scope"] == SCOPE_AUTHORIZED
    # 4. the column default, for a row written around the accessor
    from sqlalchemy import insert, select
    from db.engine import get_engine
    from db.tables import agent_canvas_shares
    with get_engine().begin() as conn:
        conn.execute(insert(agent_canvas_shares).values(
            id="raw", agent_name="agent-a", canvas_id="main", token="raw-token",
            created_at="t"))
        got = conn.execute(select(agent_canvas_shares.c.scope)
                           .where(agent_canvas_shares.c.id == "raw")).scalar()
    assert got == SCOPE_AUTHORIZED


def test_the_frontend_preselects_the_narrow_scope():
    """The dialog's default is the fifth place this could go wrong, and it is
    the one a person actually sees."""
    import pathlib, re
    repo = pathlib.Path(__file__).resolve().parents[2]
    src = (repo / "src/frontend/src/components/canvas/canvasShare.js").read_text()
    scopes = re.search(r"SHARE_SCOPES\s*=\s*\[([^\]]+)\]", src).group(1)
    assert scopes.split(",")[0].strip().strip("'\"") == "authorized"

    panel = (repo / "src/frontend/src/components/canvas/CanvasPanel.vue").read_text()
    assert "shareScope = ref('authorized')" in panel


# --- resolution -------------------------------------------------------------

def _resolve(token, user=None):
    from services import canvas_share_service as css
    return css.resolve(token, user)["status"]


def _owner():
    return User(id=1, username="owner", role="user")


def _stranger():
    return User(id=2, username="stranger", role="user")


def test_a_public_link_opens_for_a_stranger_with_no_account(share_db):
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    assert _resolve(share["token"], None) == ShareResolution.OK


def test_an_authorized_link_asks_an_anonymous_viewer_to_sign_in(share_db):
    """Not a 404: the page has to be able to offer a sign-in rather than a dead
    end, and this state is only reachable for a token that matched a live row."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="authorized")
    assert _resolve(share["token"], None) == ShareResolution.SIGN_IN_REQUIRED


def test_an_authorized_link_opens_for_someone_who_could_already_see_it(share_db):
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="authorized")
    assert _resolve(share["token"], _owner()) == ShareResolution.OK


def test_an_authorized_link_does_not_grant_access_by_itself(share_db):
    """THE property of the narrow scope. The link POINTS at a canvas; it does
    not confer the right to see it. A signed-in stranger is refused."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="authorized")
    assert _resolve(share["token"], _stranger()) == ShareResolution.NOT_AUTHORIZED


def test_sharing_with_an_agent_makes_an_authorized_link_open_for_them(share_db):
    """The other half of the same property: the audience IS the agent's, so
    granting access to the agent grants the link too, with no re-share."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    db.share_agent("agent-a", "owner", "stranger@example.com")
    share = db.create_canvas_share("agent-a", "main", scope="authorized")
    assert _resolve(share["token"], _stranger()) == ShareResolution.OK


def test_a_revoked_link_says_so_and_an_unknown_one_does_not(share_db):
    """Both halves of the disclosure contract in one test, because they are one
    decision: name the state for someone who already knew the canvas existed,
    and stay uniform for someone guessing."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    db.revoke_canvas_share("agent-a", share["id"])

    assert _resolve(share["token"], None) == ShareResolution.REVOKED
    assert _resolve("a-token-nobody-minted", None) == ShareResolution.NOT_FOUND


def test_an_expired_link_reads_as_expired_and_an_unreadable_expiry_fails_closed(share_db):
    """A link whose lifetime cannot be parsed is one we cannot promise is live,
    so it stops serving rather than keeping going."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    past = db.create_canvas_share("agent-a", "main", scope="public",
                                  expires_at="2020-01-01T00:00:00Z")
    junk = db.create_canvas_share("agent-a", "main", scope="public",
                                  expires_at="whenever")
    assert _resolve(past["token"], None) == ShareResolution.EXPIRED
    assert _resolve(junk["token"], None) == ShareResolution.EXPIRED


def test_a_link_to_a_deleted_canvas_reads_as_not_found(share_db):
    """Not a distinct "deleted" state: the link points at nothing and there is
    no version of it that would work again, so it joins the uniform bucket."""
    from database import db
    from services.canvas_share_service import ShareResolution
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    db.delete_agent_canvas("agent-a", "main")
    assert _resolve(share["token"], None) == ShareResolution.NOT_FOUND


def test_a_view_is_only_counted_when_the_canvas_actually_rendered(share_db):
    from database import db
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="authorized")
    _resolve(share["token"], None)        # sign-in required
    _resolve(share["token"], _stranger())  # refused
    assert db.get_canvas_share_by_token(share["token"])["view_count"] == 0
    _resolve(share["token"], _owner())     # rendered
    assert db.get_canvas_share_by_token(share["token"])["view_count"] == 1


def test_the_shared_payload_states_that_it_is_live(share_db):
    """AC #3 — never ambiguous. The operator ruling was LIVE, so the payload
    says so and the page renders that sentence."""
    from database import db
    from services import canvas_share_service as css
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    payload = css.public_view_payload(css.resolve(share["token"], None))
    assert payload["live"] is True
    assert payload["agent_name"] == "agent-a"


def test_a_shared_canvas_shows_current_content_not_a_copy(share_db):
    """The consequence of choosing live: what the recipient sees follows the
    agent's later writes."""
    from database import db
    from services import canvas_share_service as css
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    db.upsert_agent_canvas("agent-a", "main", blocks=[], title="Renamed")
    assert css.resolve(share["token"], None)["canvas"]["title"] == "Renamed"


# --- scoping ----------------------------------------------------------------

def test_a_share_cannot_be_revoked_from_another_agent(share_db):
    from database import db
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    assert db.revoke_canvas_share("other-agent", share["id"]) is False
    assert db.get_canvas_share_by_token(share["token"])["revoked_at"] is None


def test_revoking_twice_keeps_the_first_revocation_time(share_db):
    """When a link stopped working is a fact; a second call must not rewrite it."""
    from database import db
    _canvas()
    share = db.create_canvas_share("agent-a", "main", scope="public")
    assert db.revoke_canvas_share("agent-a", share["id"]) is True
    first = db.get_canvas_share_by_token(share["token"])["revoked_at"]
    assert db.revoke_canvas_share("agent-a", share["id"]) is False
    assert db.get_canvas_share_by_token(share["token"])["revoked_at"] == first


def test_tokens_are_unguessable_and_unique(share_db):
    from database import db
    _canvas()
    tokens = {db.create_canvas_share("agent-a", "main", scope="public")["token"]
              for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(t) >= 40 for t in tokens)


# --- wiring -----------------------------------------------------------------

def test_the_share_list_route_is_declared_above_the_canvas_id_routes():
    """Invariant #4 — "shares" is a valid canvas-id shape, so this route would
    otherwise be captured by `/{canvas_id}` and answer "not found" forever."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    src = (backend / "routers" / "canvas.py").read_text()
    assert src.index('"/{name}/canvas/shares"') < src.index('"/{name}/canvas/{canvas_id}"')


def test_the_share_table_ships_on_both_migration_tracks():
    """Invariant #9."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    sqlite_src = (backend / "db" / "migrations.py").read_text()
    assert "agent_canvas_shares_table" in sqlite_src
    alembic = backend / "migrations" / "versions" / "0060_agent_canvas_shares.py"
    assert alembic.exists()
    assert "agent_canvas_shares" in alembic.read_text()


def test_share_rows_follow_their_agent_through_rename_and_purge():
    """Registered in AGENT_REFS: a shared URL that dies because the agent was
    renamed is a broken promise to whoever holds it, and a purge must not leave
    a live token addressing a canvas that is gone."""
    from db.agent_cleanup import AGENT_REFS
    assert any(r.table == "agent_canvas_shares" for r in AGENT_REFS)


def test_the_audit_row_never_carries_the_token():
    """The token IS the capability, and the audit log is broadly readable
    (the G-04 rule)."""
    import inspect
    from routers import canvas as canvas_router
    src = inspect.getsource(canvas_router.create_canvas_share)
    details = src.split("details={")[-1].split("}")[0]
    assert "token" not in details
    assert "scope" in details


def test_the_public_scope_is_audited_distinctly():
    """"Who made this readable by anyone with the URL" should be answerable
    from the action alone, without reading every payload."""
    import inspect
    from routers import canvas as canvas_router
    src = inspect.getsource(canvas_router.create_canvas_share)
    assert "canvas_share_public" in src
