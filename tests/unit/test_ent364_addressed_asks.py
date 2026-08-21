"""`operator_queue.addressed_to_email` and its roster validation (ent#364).

The OSS half of workspace asks: an edition-agnostic primitive (the column and the
rule that fills it), with the read/answer surface gated separately — the same split
as `users.suspended_at` in #995, where OSS owns the column AND its enforcement.

This field is the one piece of an agent-authored queue item that is an
AUTHORIZATION decision: it decides who may answer the ask and whose Workspace
sidebar it appears in. Which is exactly why it is not a key inside `context` —
`context` is free-form agent JSON whose hygiene clamp bounds size and type only, so
an addressee living there would be an agent choosing its own audience.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def roster(monkeypatch):
    state = {"members": {("scout", "client@example.com")}, "calls": [], "raises": False}

    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        state["calls"].append((agent_name, email, include_owned))
        if state["raises"]:
            raise RuntimeError("roster down")
        return (agent_name, (email or "").lower()) in state["members"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _clamp(raw_email, agent="scout"):
    from services.operator_queue_service import _clamp_ingested_item

    out = _clamp_ingested_item(
        {"id": "req-1", "title": "t", "question": "q", "addressed_to_email": raw_email},
        agent,
    )
    return out["addressed_to_email"]


def test_an_on_roster_address_is_kept_and_normalised(roster):
    assert _clamp("Client@Example.com  ") == "client@example.com"


def test_an_off_roster_address_becomes_an_operator_ask(roster):
    """The whole point: an agent may not decide who answers for it. Dropped to
    NULL — which is a perfectly valid item, just one the operator answers."""
    assert _clamp("stranger@example.com") is None


def test_an_address_for_a_different_agent_does_not_carry_over(roster):
    """Roster membership is per agent. `sage` cannot address `scout`'s client."""
    assert _clamp("client@example.com", agent="sage") is None


@pytest.mark.parametrize("bad", [None, 123, {"email": "x@y.z"}, [], "", "   ", "not-an-email"])
def test_malformed_values_are_dropped_without_a_roster_call(roster, bad):
    """Refused before the DB read: a non-string or address-shaped-nothing can never
    match a roster row, and a lookup per garbage value is a free amplification."""
    assert _clamp(bad) is None
    assert roster["calls"] == []


def test_an_absurdly_long_value_is_refused_before_the_lookup(roster):
    from services.operator_queue_service import OPERATOR_QUEUE_EMAIL_MAX

    assert _clamp("a" * (OPERATOR_QUEUE_EMAIL_MAX + 1) + "@example.com") is None
    assert roster["calls"] == []


def test_a_roster_failure_fails_closed(roster):
    """Addressing an ask we could not validate is worse than not addressing it."""
    roster["raises"] = True
    assert _clamp("client@example.com") is None


def test_the_lookup_never_unlocks_the_owned_agents_branch(roster):
    """`include_owned=True` would hand a client agents nobody shared with them —
    see `agent_on_roster`'s own docstring. It is the rule here, not a default."""
    _clamp("client@example.com")
    assert roster["calls"] == [("scout", "client@example.com", False)]


def test_the_addressee_is_never_read_from_context(roster):
    """A `context.addressed_to_email` must not become the addressee: `context` is
    agent-authored and only size/type-clamped, so honouring it would reintroduce
    exactly the hole the column exists to close."""
    from services.operator_queue_service import _clamp_ingested_item

    out = _clamp_ingested_item(
        {
            "id": "req-2", "title": "t", "question": "q",
            "context": {"addressed_to_email": "client@example.com"},
        },
        "scout",
    )
    assert out["addressed_to_email"] is None


# --- persistence ------------------------------------------------------------

@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    import sqlite3

    db_file = tmp_path / "trinity-asks-oss.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    yield str(db_file)


def test_the_column_round_trips(queue_db, roster):
    from database import db

    item_id = db.create_operator_queue_item("scout", {
        "id": "req-round-trip", "title": "t", "question": "q",
        "addressed_to_email": "client@example.com",
    })

    assert db.get_operator_queue_item(item_id)["addressed_to_email"] == "client@example.com"


def test_an_unaddressed_item_reads_back_as_none(queue_db, roster):
    """Every pre-ent#364 row is this row: NULL means the operator answers."""
    from database import db

    item_id = db.create_operator_queue_item("scout", {
        "id": "req-operator", "title": "t", "question": "q",
    })

    assert db.get_operator_queue_item(item_id)["addressed_to_email"] is None


# --- the read path (ent#428) --------------------------------------------------
#
# The addressee has to be a SQL condition, not something the caller filters out
# of the result. `list_items` orders by status, then priority, then age, and
# applies `limit` before the caller sees a row — so a post-hoc filter really
# means "the newest N pending items in the FLEET, of which some are yours", and
# one person's ask silently drops out of their sidebar as soon as the fleet is
# busy enough. It is still pending in the queue; it just stops being visible to
# the only person who may answer it.

def _seed(db, agent, req_id, email=None, priority="medium"):
    item = {"id": req_id, "title": "t", "question": "q", "priority": priority}
    if email is not None:
        item["addressed_to_email"] = email
    return db.create_operator_queue_item(agent, item)


def test_the_read_path_narrows_to_the_addressee(queue_db, roster):
    from database import db

    mine = _seed(db, "scout", "req-mine", "client@example.com")
    _seed(db, "scout", "req-operator")                      # NULL — operator's
    _seed(db, "scout", "req-someone-else", "other@example.com")

    got = db.list_operator_queue_items(
        status="pending", addressed_to_email="client@example.com"
    )

    assert [i["id"] for i in got] == [mine]


def test_the_addressee_filter_is_case_insensitive(queue_db, roster):
    """The ingestion boundary lowercases, but `create_item` is a public writer
    and the read must not depend on every caller having remembered to."""
    from database import db

    item_id = db.create_operator_queue_item("scout", {
        "id": "req-mixed", "title": "t", "question": "q",
        "addressed_to_email": "Client@Example.COM",
    })

    got = db.list_operator_queue_items(
        status="pending", addressed_to_email="client@example.com"
    )

    assert [i["id"] for i in got] == [item_id]


def test_an_ask_is_not_crowded_out_of_its_own_window(queue_db, roster):
    """The regression this filter exists for.

    One low-priority ask addressed to a client, behind a wall of higher-priority
    operator items. Filtering after the fact loses it; filtering in SQL does not.
    The `limit` here stands in for the caller's page size — the point is that the
    cap is spent on the fleet's items rather than on the client's.
    """
    from database import db

    for n in range(5):
        _seed(db, "scout", f"req-noise-{n}", priority="critical")
    mine = _seed(db, "scout", "req-quiet", "client@example.com", priority="low")

    unfiltered = db.list_operator_queue_items(status="pending", limit=5)
    assert mine not in [i["id"] for i in unfiltered], (
        "fixture is not exercising the crowd-out; raise the noise count"
    )

    filtered = db.list_operator_queue_items(
        status="pending", limit=5, addressed_to_email="client@example.com"
    )
    assert [i["id"] for i in filtered] == [mine]


def test_no_addressee_argument_leaves_the_operator_listing_alone(queue_db, roster):
    """AC #4: a platform caller's view of the queue does not move."""
    from database import db

    ids = {
        _seed(db, "scout", "req-a"),
        _seed(db, "scout", "req-b", "client@example.com"),
    }

    got = db.list_operator_queue_items(status="pending")

    assert {i["id"] for i in got} == ids


def test_an_empty_addressee_matches_nothing_rather_than_everyone(queue_db, roster):
    """The falsy value fails CLOSED.

    Every other filter on `list_items` uses truthiness, so an empty value means
    "don't filter". This one decides who may see a row, so the same convention
    would turn a caller that lost its email into a caller that sees everyone's
    asks. `None` still means "no filter" — that is the operator listing.
    """
    from database import db

    _seed(db, "scout", "req-mine", "client@example.com")
    _seed(db, "scout", "req-operator")

    assert db.list_operator_queue_items(status="pending", addressed_to_email="") == []
    assert len(db.list_operator_queue_items(status="pending", addressed_to_email=None)) == 2


# --- the reader that predated the column (ent#428) -----------------------------

def test_the_agent_page_does_not_show_one_client_another_clients_ask(queue_db, roster):
    """One agent, two clients, one ask addressed to the first.

    `client_portal/agent_page.py` renders the client-facing "waiting on you"
    block, and it was scoped by `agent_name` alone — so the second client read
    the first's ask, `title` and `question` verbatim. `context` was already
    withheld there as a known leak surface, but the agent-authored title and
    question are exactly where an ask meant for someone else says something not
    meant for this reader.
    """
    from client_portal import agent_page
    from database import db
    from services.operator_queue_service import _clamp_ingested_item

    roster["members"] = {("scout", "alice@example.com"), ("scout", "bob@example.com")}
    # The row id is platform-minted (#1631); the agent's own string is
    # `request_id`, so keep what create returns rather than assuming either.
    item_id = db.create_operator_queue_item("scout", _clamp_ingested_item({
        "id": "req-for-alice", "type": "approval",
        "title": "Invoice Acme at the agreed discount?",
        "question": "Alice, confirm the Q3 discount before I send it.",
        "options": ["yes", "no"],
        "addressed_to_email": "alice@example.com",
    }, "scout"))

    assert [a["id"] for a in agent_page._asks("scout", "alice@example.com")] == [item_id]
    assert agent_page._asks("scout", "bob@example.com") == []


def test_the_agent_page_does_not_show_a_client_an_operator_ask(queue_db, roster):
    """The narrowing is intended, not a side effect.

    An unaddressed ask is agent-authored text written FOR THE OPERATOR, and a
    client cannot act on it from this page anyway — the only affordance is
    "reply in chat". Operators keep the full queue in Operations.
    """
    from client_portal import agent_page
    from database import db

    _seed(db, "scout", "req-operator")

    assert agent_page._asks("scout", "client@example.com") == []
