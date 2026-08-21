"""Workspace agent-initiated asks (ent#364).

The feature is one sentence — an ask is one `operator_queue` row with an addressee
— so the tests are about the two things that sentence makes load-bearing:

  * **who may see and answer it.** The addressee match is the authorisation, and it
    is re-checked against the roster at READ time: membership when the ask was
    raised is not a standing grant, and a revoked share must stop showing it.
  * **what a client is told.** The projection is explicit, so `context` — which is
    agent-authored and may hold anything — never reaches a client, and a
    not-yours ask is indistinguishable from a missing one (Invariant #8: a 403
    would be an id oracle).

Plus the audit shape: a workspace client has no `users` row, so `responded_by_id`
must stay NULL rather than carry a fabricated id.

Ported to OSS core by ent#428 — this was the entitled `workspace_asks` module,
and the frontend that drives it shipped in every build and self-disabled, so a
community install rendered the ask affordance and then 404'd it. Everything
about the Workspace lives in OSS. The suite is unchanged beyond its import
paths, one path calculation, and the two cases below that pin the new edition.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def asks_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-asks.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    # Build the schema the way the platform does, not from `tables.py` metadata
    # alone: the `(agent_name, request_id)` UNIQUE index (#1631) is created by
    # `db/schema.py`, and `create_item`'s ON CONFLICT targets exactly that index —
    # a metadata-only fixture raises "ON CONFLICT clause does not match any
    # PRIMARY KEY or UNIQUE constraint", which is the fixture diverging from
    # production rather than a bug in the code under test.
    import sqlite3

    from db.schema import init_schema

    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    yield str(db_file)


@pytest.fixture()
def client_email():
    """A unique addressee per test.

    `db.engine` caches the engine at first use, so the per-test `tmp_path` database
    is only honoured for the FIRST test in the file — every later one reads and
    writes the same file. Rather than fight that (and depend on import order for
    correctness, which is the ent#379 class), each test addresses its own email and
    raises its own request ids, so cross-test rows cannot collide.
    """
    import uuid
    return f"client-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Roster membership, controllable. Default: everyone is on every roster."""
    state = {"on": True, "calls": []}

    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        state["calls"].append((agent_name, email, include_owned))
        return state["on"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _raise_ask(agent="agent-a", addressed=None, req_id=None,
               kind="question", expires_at=None, context=None):
    """Create a row the way ingestion does — through the clamp, so the addressee
    goes through the validation this feature depends on."""
    import uuid

    from database import db
    from services.operator_queue_service import _clamp_ingested_item

    item = _clamp_ingested_item({
        "id": req_id or f"req-{uuid.uuid4().hex[:12]}",
        "type": kind,
        "title": "Need a decision",
        "question": "Ship it?",
        "options": ["yes", "no"],
        "addressed_to_email": addressed,
        "expires_at": expires_at,
        "context": context if context is not None else {"secret": "do-not-leak"},
    }, agent)
    return db.create_operator_queue_item(agent, item)


# --- who may see it ---------------------------------------------------------

def test_an_addressed_ask_reaches_its_addressee(asks_db, client_email):
    from client_portal.asks import service
    _raise_ask(addressed=client_email)

    asks = service.list_asks(client_email, is_platform=False)

    assert [a.kind for a in asks] == ["question"]
    assert asks[0].options == ["yes", "no"]
    assert asks[0].status == "pending"


def test_another_users_ask_is_invisible(asks_db, client_email):
    from client_portal.asks import service
    _raise_ask(addressed="someone-else@example.com")

    assert service.list_asks(client_email, is_platform=False) == []


def test_a_busy_fleet_does_not_crowd_out_one_client_ask(asks_db, client_email):
    """ent#428 — the narrowing happens in SQL, and this is what that buys.

    `list_items` orders by status, then priority, then age, and applies its
    `limit` before this module sees a row. Filtering the addressee out of the
    RESULT would therefore mean "the newest 200 pending items in the fleet, of
    which some are yours" — and one client's low-priority ask would vanish from
    their sidebar the moment the fleet got busy, while still sitting pending in
    the queue with nobody else permitted to answer it.

    The noise is operator asks (`addressed=None`), the majority case on any real
    instance, and it is raised AFTER the client's ask: within one priority the
    ordering is newest-first, so every noise row sorts ahead of it and the whole
    `limit` is spent before the client's row is reached.
    """
    from database import db
    from client_portal.asks import service

    mine = _raise_ask(addressed=client_email, req_id="req-quiet")
    for n in range(220):
        _raise_ask(req_id=f"req-noise-{n}", kind="approval")

    # The fixture has to actually reproduce the crowd-out, or this test passes
    # for the wrong reason — filtering the result of a window the ask is still
    # inside proves nothing.
    unfiltered = db.list_operator_queue_items(status="pending", limit=200)
    assert mine not in [i["id"] for i in unfiltered], (
        "fixture is not exercising the crowd-out; raise the noise count"
    )

    asks = service.list_asks(client_email, is_platform=False)

    assert [a.id for a in asks] == [mine]


def test_an_operator_ask_is_not_a_workspace_ask(asks_db, client_email, roster):
    """`addressed_to_email` NULL is every pre-ent#364 row and every ordinary
    operator item. Those must not start appearing in a client's sidebar."""
    from client_portal.asks import service
    _raise_ask(addressed=None)

    assert service.list_asks(client_email, is_platform=False) == []


def test_a_revoked_share_stops_showing_an_already_raised_ask(asks_db, client_email, roster):
    from client_portal.asks import service
    _raise_ask(addressed=client_email)
    assert len(service.list_asks(client_email, is_platform=False)) == 1

    roster["on"] = False          # the share is withdrawn afterwards
    assert service.list_asks(client_email, is_platform=False) == []


def test_an_unreadable_roster_hides_rather_than_shows(asks_db, client_email, monkeypatch):
    """Fail closed: showing an ask we cannot justify is worse than hiding one."""
    import client_portal.service as portal_service
    from client_portal.asks import service
    _raise_ask(addressed=client_email)

    def boom(*_a, **_kw):
        raise RuntimeError("roster down")

    monkeypatch.setattr(portal_service, "agent_on_roster", boom)
    assert service.list_asks(client_email, is_platform=False) == []


def test_a_client_read_never_unlocks_the_owned_agents_branch(asks_db, client_email, roster):
    """`include_owned` mirrors the principal kind. Flipping it on for a portal
    session would hand a client agents nobody shared with them."""
    from client_portal.asks import service
    _raise_ask(addressed=client_email)

    service.list_asks(client_email, is_platform=False)
    assert all(call[2] is False for call in roster["calls"])

    roster["calls"].clear()
    service.list_asks("op@example.com", is_platform=True)
    assert all(call[2] is True for call in roster["calls"])


# --- what a client is told --------------------------------------------------

def test_the_projection_never_forwards_agent_authored_context(asks_db, client_email):
    from client_portal.asks import service
    _raise_ask(addressed=client_email, context={"secret": "do-not-leak", "execution_id": "exec-1"})

    ask = service.list_asks(client_email, is_platform=False)[0]
    dumped = ask.model_dump()

    assert "context" not in dumped
    assert "do-not-leak" not in str(dumped)


def test_the_chat_id_comes_from_platform_written_context_only(asks_db, client_email):
    from client_portal.asks import service
    _raise_ask(addressed=client_email, context={"workspace_session_id": "sess-123"})

    ask = service.list_asks(client_email, is_platform=False)[0]
    assert ask.chat_id == "sess-123"


def test_an_expired_ask_is_shown_as_expired_not_hidden(asks_db, client_email):
    """#1142 DELETES terminal rows, so expiry must be visible while it is there —
    an absent ask reads as 'answered' to the person who did not answer it."""
    from client_portal.asks import service
    _raise_ask(addressed=client_email, expires_at="2000-01-01T00:00:00Z")

    asks = service.list_asks(client_email, is_platform=False)
    assert [a.status for a in asks] == ["expired"]


# --- answering --------------------------------------------------------------

def test_answering_records_the_client_without_a_fabricated_user_id(asks_db, client_email):
    from database import db
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email)

    service.answer_ask(item_id, client_email, False, "yes", "ship it")

    row = db.get_operator_queue_item(item_id)
    assert row["status"] == "responded"
    assert row["response"] == "yes"
    assert row["responded_by_email"] == client_email
    assert row["responded_by_id"] is None, (
        "a workspace client has no users row; an id here would be a lie in the audit trail"
    )
    assert row["responded_at"]


def test_answering_clears_it_everywhere_because_there_is_one_row(asks_db, client_email):
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email)

    service.answer_ask(item_id, client_email, False, "no", None)

    # Every surface reads this one list; no sync step exists to get wrong.
    assert service.list_asks(client_email, is_platform=False) == []


def test_an_empty_answer_is_refused(asks_db, client_email):
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email)

    with pytest.raises(service.AskError) as ei:
        service.answer_ask(item_id, client_email, False, None, None)
    assert (ei.value.status_code, ei.value.code) == (422, "empty_answer")


def test_someone_elses_ask_is_a_uniform_404(asks_db, client_email):
    """Not 403. A distinguishable refusal would let any client enumerate ask ids."""
    from client_portal.asks import service
    item_id = _raise_ask(addressed="someone-else@example.com")

    with pytest.raises(service.AskError) as ei:
        service.answer_ask(item_id, client_email, False, "yes", None)
    assert ei.value.status_code == 404

    with pytest.raises(service.AskError) as missing:
        service.answer_ask("no-such-id", client_email, False, "yes", None)
    assert missing.value.status_code == 404
    assert missing.value.code == ei.value.code, "the two must be indistinguishable"


def test_answering_twice_is_refused_exactly_as_the_operator_path_refuses_it(
    asks_db, client_email
):
    """ent#428 AC #6 — the SAME 400 `POST /api/operator-queue/{id}/respond`
    returns for a non-pending item, not a code of this surface's own choosing.

    The operator path spends 400 and 409 on two different facts: 400 for "it was
    already resolved when you looked", 409 for "someone resolved it between your
    read and your write". A client answering the same row should get the same
    answer as an operator answering it.
    """
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email)
    service.answer_ask(item_id, client_email, False, "yes", None)

    with pytest.raises(service.AskError) as ei:
        service.answer_ask(item_id, client_email, False, "no", None)
    assert (ei.value.status_code, ei.value.code) == (400, "already_resolved")


def test_an_expired_ask_cannot_be_answered(asks_db, client_email):
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email, expires_at="2000-01-01T00:00:00Z")

    with pytest.raises(service.AskError) as ei:
        service.answer_ask(item_id, client_email, False, "yes", None)
    assert (ei.value.status_code, ei.value.code) == (409, "expired")


def test_a_revoked_share_cannot_answer(asks_db, client_email, roster):
    from client_portal.asks import service
    item_id = _raise_ask(addressed=client_email)
    roster["on"] = False

    with pytest.raises(service.AskError) as ei:
        service.answer_ask(item_id, client_email, False, "yes", None)
    assert ei.value.status_code == 404


# --- the route surface ------------------------------------------------------

def test_the_router_cannot_be_shadowed_by_the_oss_portal_routes():
    """`/asks` lives under the OSS portal prefix, so it shares a namespace with a
    router registered EARLIER (Invariant #4). Safe only while no OSS portal route
    is a single-segment path parameter that would capture it."""
    import re
    from pathlib import Path

    # tests/unit/ → tests/ → repo root → src/backend/client_portal.
    oss_router = (
        Path(__file__).resolve().parents[2]
        / "src" / "backend" / "client_portal" / "router.py"
    ).read_text()
    single_segment_catchalls = re.findall(r'@router\.\w+\("/\{[^/}]+\}"\)', oss_router)
    assert not single_segment_catchalls, (
        f"an OSS portal catch-all would shadow /asks: {single_segment_catchalls}"
    )


def test_the_answer_route_is_not_admin_gated():
    """The audience is the addressee. A role gate here would lock out the exact
    person the ask was raised for."""
    from client_portal.asks import router as router_mod

    source = __import__("inspect").getsource(router_mod)
    assert "require_admin" not in source
    assert "get_portal_principal" in source


# ---------------------------------------------------------------------------
# The edition itself (ent#428)
# ---------------------------------------------------------------------------

def test_the_asks_routes_answer_on_an_oss_build():
    """The point of the move. `main.py` mounts this router unconditionally, so
    the routes exist with no submodule and no entitlement."""
    from client_portal.asks.router import router

    paths = {r.path for r in router.routes}
    assert "/api/enterprise/client-portal/asks" in paths
    assert "/api/enterprise/client-portal/asks/{item_id}/answer" in paths


def test_nothing_here_is_entitlement_gated_or_registers_a_feature_id():
    """Parsed, not grepped: a docstring is allowed to SAY it used to be gated —
    recording why the gate went away is the point. What must not exist is a call.

    A `register_module` call would also be wrong in the other direction: it would
    make an OSS build advertise itself as carrying an enterprise module on
    `/api/version` and `/api/settings/feature-flags`.
    """
    import ast
    import inspect

    from client_portal import asks
    from client_portal.asks import router as router_mod
    from client_portal.asks import service as service_mod

    banned = {"requires_entitlement", "register_module", "is_entitled"}
    for module in (asks, router_mod, service_mod):
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert not (called & banned), f"{module.__name__} still calls {called & banned}"


def test_the_oss_asks_router_is_mounted_before_the_enterprise_seam():
    """The transition rule (ent#443's, reused).

    While an install's submodule still registers the old gated `workspace_asks`,
    BOTH routers mount on the same prefix and FastAPI serves the first match. So
    the OSS include has to come first, or a stale submodule silently takes the
    paths back and 403s community builds — with every other test here still
    green, because they import the module directly.

    Matched as CODE, not as a substring: `main.py` mentions
    `register_enterprise(app)` in prose above the call, and a bare
    `str.index` finds the comment — which is both a false pass (a comment
    anywhere below the mount would satisfy it) and, as written first, a false
    FAIL. Anchor on a line whose first non-space character starts the call.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"
    ).read_text()

    def _call_line(pattern: str) -> int:
        m = re.search(rf"^\s*{re.escape(pattern)}", source, re.MULTILINE)
        assert m, f"{pattern} is not called in main.py at all"
        return m.start()

    assert _call_line("app.include_router(portal_asks_router)") < _call_line(
        "register_enterprise(app)"
    )
