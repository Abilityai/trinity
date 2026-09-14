"""The Workspace roster keeps containerless agents and says why (#2196).

The reported bug: the roster lists agents whose container no longer exists, so a
client sees a row `/api/agents` does not have and clicking it fails opaquely.

The decision this file pins is the one the issue exists to make — **the
`agent_ownership` row is authoritative for membership; container state is
projected onto the card, never used as a filter.** Two tests carry that weight:

  * `test_a_containerless_agent_stays_on_the_roster_and_says_why` — the
    anti-#1747 regression guard. If a future change starts filtering, it fails.
  * `test_an_unreadable_docker_never_empties_the_roster` — the guard on the
    failure mode that decided the design. Every Docker read in the platform
    returns a falsy value for BOTH "no container" and "could not ask", so a
    filter would tell every paying customer they have no agents the moment a
    daemon restarts or a `DOCKER_GID` changes.

PATCHING RULE (ent#356, and the reason #2196 built named seams at all):
`client_portal.service` reaches its dependencies through function-local
`from services.x import y`, which resolves `sys.modules["services.x"]` at call
time. So:

  * patch `client_portal.service._availability_map` / `._agent_availability`
    directly — the preferred route, and what the seams are for;
  * when the leaf must be patched, take the module object out of `sys.modules`
    (`_services_module` below) or use `unittest.mock.patch("services.x.y")`;
  * NEVER `monkeypatch.setattr("services.x.y", ...)` and never an
    `import services.x as m` alias — both walk the package attribute, which
    conftest's #762 restore can leave pointing at a different object.

A missed patch does not error here. It lets the real read run, which on a
Docker-less machine answers `None` → `"unknown"` — the value several of these
tests assert. So every classification test below is written to prove it can
produce a DIFFERENT value, and the seam-stubbing is asserted explicitly by
`test_the_seam_is_stubbed_not_ambient`.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _services_module(name: str):
    """The module object the product code will ACTUALLY resolve (see the
    module docstring — `from services.x import y` reads `sys.modules`, every
    other route walks the package attribute)."""
    import importlib
    import sys

    importlib.import_module(f"services.{name}")
    return sys.modules[f"services.{name}"]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A real sqlite roster (the ent#357 fixture shape — no mocks over the queries)
# ---------------------------------------------------------------------------

@pytest.fixture()
def roster_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-2196.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata, agent_ownership, agent_sharing, system_settings, users,
    )
    oss_metadata.create_all(
        get_engine(), tables=[users, agent_ownership, agent_sharing, system_settings]
    )

    with get_engine().begin() as conn:
        now = "2026-08-14T00:00:00Z"
        conn.execute(users.insert().values(
            id=1, username="alice", email="alice@example.com", role="user",
            created_at=now, updated_at=now,
        ))
        for name in ("scout", "sage", "ghosted"):
            conn.execute(agent_ownership.insert().values(
                agent_name=name, owner_id=1, created_at=now, is_system=0, deleted_at=None,
            ))
            conn.execute(agent_sharing.insert().values(
                agent_name=name, shared_with_email="bob@example.com",
                shared_by_id=1, created_at=now,
            ))
    yield get_engine()


@pytest.fixture(autouse=True)
def _quiet_briefing(monkeypatch):
    """The briefing is #138's concern, not this file's — and left live it would
    make real HTTP attempts at every roster load."""
    from client_portal import service as svc

    real = svc._agent_briefing

    async def _briefing(name, availability="ready"):
        return svc.AgentBriefing()   # #2213: the real return shape

    monkeypatch.setattr(svc, "_agent_briefing", _briefing)
    monkeypatch.setattr(_services_module("tts_service"), "is_available", lambda: False)
    # Handed back so the one test that IS about the briefing can reach the real
    # function rather than asserting against this stub.
    return real


def _pin_states(monkeypatch, states):
    """Pin the LEAF `docker_service.agent_container_states`, so the real
    `_availability_map` — narrowing, enum guard, absence rule — is what runs.

    Patched on the `sys.modules` entry: that is the object `docker_utils`'
    function-local `from services.docker_service import ...` resolves.
    """
    monkeypatch.setattr(
        _services_module("docker_service"), "agent_container_states", lambda: states
    )


def _pin_state(monkeypatch, state):
    """Same, for the single-agent read."""
    monkeypatch.setattr(
        _services_module("docker_service"), "agent_container_state", lambda name: state
    )


# ---------------------------------------------------------------------------
# The two that carry the decision
# ---------------------------------------------------------------------------

def test_a_containerless_agent_stays_on_the_roster_and_says_why(roster_db, monkeypatch):
    """THE anti-#1747 guard.

    A live `agent_ownership` row whose container is gone is a routine state —
    #834 Phase 1c recovery reaches it by design, as does a `docker system
    prune` or a crash mid-create. On the one surface a client has, hiding the
    row makes "not shared with me" indistinguishable from "shared but
    containerless", so the state becomes undiagnosable rather than merely
    unusable. It stays listed, and it says what is wrong.
    """
    from client_portal import service

    _pin_states(monkeypatch, {"scout": "running", "sage": "exited"})   # `ghosted` absent

    cards = {c.name: c for c in _run(service.get_roster("bob@example.com")).agents}

    assert set(cards) == {"scout", "sage", "ghosted"}, "a row was FILTERED OUT"
    assert cards["ghosted"].availability == "unavailable"


def test_an_unreadable_docker_never_empties_the_roster(roster_db, monkeypatch):
    """THE guard on the failure mode that decided the design.

    `list_all_agents_fast` returns `[]` and `get_agent_container` returns `None`
    on ANY Docker fault, so a filter built on either would empty a paying
    customer's Workspace on one daemon restart. The tri-state read answers
    `None` instead, every card reads `unknown`, and the roster renders exactly
    as it does today: every row, no chip, nothing blocked.
    """
    from client_portal import service

    _pin_states(monkeypatch, None)          # "Docker could not be asked"

    cards = _run(service.get_roster("bob@example.com")).agents

    assert {c.name for c in cards} == {"scout", "sage", "ghosted"}
    assert {c.availability for c in cards} == {"unknown"}


# ---------------------------------------------------------------------------
# Classification — each proves it can produce a DIFFERENT value
# ---------------------------------------------------------------------------

def test_the_three_states_are_distinguished_in_one_roster(roster_db, monkeypatch):
    from client_portal import service

    _pin_states(monkeypatch, {"scout": "running", "sage": "stopped"})

    cards = {c.name: c.availability for c in _run(service.get_roster("bob@example.com")).agents}

    assert cards == {"scout": "ready", "sage": "stopped", "ghosted": "unavailable"}


def test_the_seam_is_stubbed_not_ambient(roster_db, monkeypatch):
    """Proves these assertions read the stub, not the developer's Docker.

    `services/docker_service.py` runs `docker.from_env()` at import and
    `tests/unit/conftest.py` re-imports it after every test, so on a machine
    with Docker up the real seam answers with a real map — in which a fixture
    agent does not appear, so it reads `unavailable`, not `unknown`. The same
    call under two different stubs must therefore yield two different answers;
    if it did not, every value below would be whatever the daemon happened to
    say.
    """
    from client_portal import service

    _pin_states(monkeypatch, None)
    unreadable = {c.availability for c in _run(service.get_roster("bob@example.com")).agents}

    _pin_states(monkeypatch, {})
    empty_fleet = {c.availability for c in _run(service.get_roster("bob@example.com")).agents}

    assert unreadable == {"unknown"}
    assert empty_fleet == {"unavailable"}
    assert unreadable != empty_fleet


@pytest.mark.parametrize("docker_state,expected", [
    ("running", "ready"),
    ("stopped", "stopped"),
    ("missing", "unavailable"),
    (None, "unknown"),
    ("paused", "unknown"),        # a status the classifier does not name
    ("removing", "unknown"),
    (object(), "unknown"),        # not even a string
])
def test_the_vocabulary_mapping(docker_state, expected):
    """Docker's words and the card's words are different, and passing one
    through as the other puts `"running"` onto a `Literal["ready", ...]`. ONE
    translation table, guarded per VALUE — an unrecognised state degrades to
    `unknown` rather than failing the card's validation."""
    from client_portal import service

    assert service._to_availability(docker_state) == expected


def test_a_paused_or_removing_container_does_not_break_the_roster(roster_db, monkeypatch):
    """`list_all_agents_fast` falls through with the RAW docker status for
    `paused`/`restarting`/`removing`. `removing` happens routinely during any
    agent delete, and copying that fall-through here would push it into the
    card's `Literal` and 500 the entire roster for every client of the
    instance. Classification is explicit for exactly this reason."""
    import types
    from client_portal import service

    class _Sparse:
        def __init__(self, name, status):
            self.attrs = {"Names": [f"/agent-{name}"], "State": status}

        @property
        def status(self):
            return self.attrs["State"]

    # The REAL classifier, driven by the raw statuses Docker actually reports
    # mid-delete and mid-pause — pinning the seam here instead would test the
    # stub and prove nothing.
    ds = _services_module("docker_service")
    monkeypatch.setattr(ds, "docker_client", types.SimpleNamespace(
        containers=types.SimpleNamespace(list=lambda **kw: [
            _Sparse("scout", "paused"), _Sparse("sage", "removing"),
            _Sparse("ghosted", "restarting"),
        ])
    ))

    roster = _run(service.get_roster("bob@example.com"))     # must not raise
    cards = {c.name: c.availability for c in roster.agents}

    assert cards == {"scout": "stopped", "sage": "stopped", "ghosted": "stopped"}


# ---------------------------------------------------------------------------
# The seams' own guards
# ---------------------------------------------------------------------------

def test_a_magicmock_docker_module_reads_unknown_not_unavailable(roster_db, monkeypatch):
    """A dozen test modules install a MagicMock at
    `sys.modules["services.docker_service"]`. Its `agent_container_states()`
    returns a truthy MagicMock — neither a dict nor None — which unguarded would
    silently invert the fail-open default INSIDE the suite meant to prove it.

    Same shape as `a2a_outbound`'s `isinstance(ResolvedEndpoint)` check, with
    the direction flipped: that one fails closed because it decides where a
    credential is sent; this one fails open because it decides whether to deny a
    working agent.
    """
    from unittest.mock import MagicMock
    from client_portal import service

    monkeypatch.setattr(
        _services_module("docker_service"), "agent_container_states", MagicMock()
    )

    cards = _run(service.get_roster("bob@example.com")).agents

    assert {c.name for c in cards} == {"scout", "sage", "ghosted"}
    assert {c.availability for c in cards} == {"unknown"}


def test_a_raising_leaf_reads_unknown_through_both_seams(monkeypatch):
    """The seams' OWN except branches, driven by a leaf that raises.

    The real leaves swallow their errors and return `None`, so the seams'
    `except` arms are otherwise only reachable through a broken stub or an
    import failure — exactly the two silent shapes the #2114 learning names for
    a lazy cross-package import inside a fail-open handler. A raise must degrade
    to `unknown` (render as today), never propagate into a roster 500 and never
    read as `unavailable`.
    """
    from client_portal import service

    def _boom_batch():
        raise RuntimeError("docker exploded")

    def _boom_single(name):
        raise RuntimeError("docker exploded")

    ds = _services_module("docker_service")
    monkeypatch.setattr(ds, "agent_container_states", _boom_batch)
    monkeypatch.setattr(ds, "agent_container_state", _boom_single)

    assert _run(service._availability_map(["scout"])) == {"scout": "unknown"}
    assert _run(service._agent_availability("scout")) == "unknown"


def test_the_availability_map_never_returns_another_tenants_agents(monkeypatch):
    """The underlying call sees EVERY agent container on the host — other
    clients' agents, and agents outside this caller's roster entirely. That map
    must never be returned, logged, or attached to a response."""
    from client_portal import service

    _pin_states(monkeypatch, {
        "scout": "running", "someone-elses-agent": "running", "internal-thing": "stopped",
    })

    out = _run(service._availability_map(["scout"]))

    assert out == {"scout": "ready"}


def test_a_one_agent_roster_body_names_no_other_agent(roster_db, monkeypatch):
    """The same property end-to-end: nothing Docker-derived escapes onto the
    payload beyond one of four server-chosen constants."""
    from client_portal import service

    _pin_states(monkeypatch, {
        "scout": "running", "sage": "running", "ghosted": "running",
        "another-tenants-agent": "running",
    })

    body = _run(service.get_roster("nobody@example.com")).model_dump_json()

    assert "another-tenants-agent" not in body


def test_an_empty_roster_asks_docker_nothing(monkeypatch):
    """Zero rows ⇒ zero Docker calls: an external client with no shares must
    not make the platform inspect its fleet."""
    calls = []

    def _boom():
        calls.append(1)
        return {}

    monkeypatch.setattr(_services_module("docker_service"), "agent_container_states", _boom)

    from client_portal import service
    assert _run(service._availability_map([])) == {}
    assert calls == []


def test_a_renamed_agent_is_not_reported_unavailable(monkeypatch):
    """The batch map is keyed by CONTAINER NAME, never by the
    `trinity.agent-name` label.

    Rename moves the container name (`docker_utils.container_rename`) and leaves
    the label at whatever it was written as at create time. Key by the label and
    every renamed agent reads `unavailable` — a silent regression on a working
    agent, which is the exact direction this whole change exists to avoid.
    """
    import types
    from client_portal import service

    class _Sparse:
        """The shape `containers.list(sparse=True)` actually returns."""

        def __init__(self, attrs):
            self.attrs = attrs

        @property
        def status(self):
            state = self.attrs["State"]
            return state["Status"] if isinstance(state, dict) else state

    renamed = _Sparse({
        "Names": ["/agent-new-name"],
        "State": "running",
        "Labels": {"trinity.platform": "agent", "trinity.agent-name": "old-name"},
    })

    fake = types.SimpleNamespace(
        containers=types.SimpleNamespace(list=lambda **kw: [renamed])
    )
    ds = _services_module("docker_service")
    monkeypatch.setattr(ds, "docker_client", fake)

    states = ds.agent_container_states()

    assert states == {"new-name": "running"}
    assert _run(service._availability_map(["new-name"]))["new-name"] == "ready"
    assert _run(service._availability_map(["old-name"]))["old-name"] == "unavailable"


def test_the_batch_read_uses_the_sparse_attrs_shape(monkeypatch):
    """The single test that catches the most likely TOTAL failure.

    `containers.list()` defaults to `sparse=False` and full-inspects every
    container, so `sparse=True` is required for the cost claim to hold at all.
    But under sparse the summary carries `Names` (a list) rather than `Name`,
    which makes docker-py's `.name` property return **None**, and `.labels`
    **raise**. Both fail in the SAFE direction — every card would read
    `"unknown"` forever while the suite stayed green and the feature simply
    never worked in production.

    A test against a hand-built object with a `.name` attribute proves nothing:
    that is precisely the shape `sparse=True` does not produce. So the fixture
    below exposes ONLY what a sparse container exposes.
    """
    import types
    import docker.errors
    from docker.models.containers import Container

    ds = _services_module("docker_service")
    captured = {}

    # A REAL docker-py Container seeded with a real /containers/json summary —
    # so `.name` and `.labels` behave exactly as they do against a live daemon.
    sparse = Container(attrs={
        "Id": "abc123",
        "Names": ["/agent-scout"],
        "State": "running",
        "Image": "trinity-agent-base:latest",
    })
    assert sparse.name is None, "fixture is not sparse-shaped"
    with pytest.raises(docker.errors.DockerException):
        _ = sparse.labels

    def _list(**kwargs):
        captured.update(kwargs)
        return [sparse]

    monkeypatch.setattr(
        ds, "docker_client", types.SimpleNamespace(containers=types.SimpleNamespace(list=_list))
    )

    assert ds.agent_container_states() == {"scout": "running"}
    assert captured.get("sparse") is True, "the batch call must pass sparse=True"
    assert captured.get("filters") == {"label": "trinity.platform=agent"}


# ---------------------------------------------------------------------------
# docker_service's own contract
# ---------------------------------------------------------------------------

def test_the_batch_read_distinguishes_empty_from_unreadable(monkeypatch):
    import types

    ds = _services_module("docker_service")

    monkeypatch.setattr(ds, "docker_client", None)
    assert ds.agent_container_states() is None            # could not be asked

    def _boom(**kwargs):
        raise RuntimeError("permission denied while trying to connect to the docker daemon")

    monkeypatch.setattr(
        ds, "docker_client", types.SimpleNamespace(containers=types.SimpleNamespace(list=_boom))
    )
    assert ds.agent_container_states() is None            # still: could not be asked

    monkeypatch.setattr(
        ds, "docker_client",
        types.SimpleNamespace(containers=types.SimpleNamespace(list=lambda **kw: [])),
    )
    assert ds.agent_container_states() == {}              # Docker ANSWERED: none


def test_the_single_read_reports_missing_only_when_docker_answered(monkeypatch):
    import types
    import docker.errors

    ds = _services_module("docker_service")

    def _get_notfound(name):
        raise docker.errors.NotFound("no such container")

    monkeypatch.setattr(
        ds, "docker_client", types.SimpleNamespace(containers=types.SimpleNamespace(get=_get_notfound))
    )
    assert ds.agent_container_state("scout") == "missing"

    def _get_boom(name):
        raise RuntimeError("docker socket down")

    monkeypatch.setattr(
        ds, "docker_client", types.SimpleNamespace(containers=types.SimpleNamespace(get=_get_boom))
    )
    assert ds.agent_container_state("scout") is None

    monkeypatch.setattr(ds, "docker_client", None)
    assert ds.agent_container_state("scout") is None


def test_the_two_forms_agree_about_an_unlabelled_container(monkeypatch):
    """The batch call filters on `trinity.platform=agent` server-side, so a
    container merely NAMED `agent-x` never appears in it. Without the same check
    on the single form, one agent would read `unavailable` on the roster and
    `ready` on its own page."""
    import types

    ds = _services_module("docker_service")

    impostor = types.SimpleNamespace(
        labels={"some.other": "thing"}, status="running", name="agent-scout",
    )
    monkeypatch.setattr(
        ds, "docker_client",
        types.SimpleNamespace(containers=types.SimpleNamespace(get=lambda n: impostor)),
    )

    assert ds.agent_container_state("scout") == "missing"


def test_list_all_agents_fast_still_returns_empty_on_a_docker_fault(monkeypatch):
    """Its []-on-fault contract is depended on by ~60 stub sites and many
    callers, and #2196 deliberately did NOT change it — the new distinction was
    added alongside rather than folded in."""
    import types

    ds = _services_module("docker_service")

    def _boom(**kwargs):
        raise RuntimeError("docker socket down")

    monkeypatch.setattr(
        ds, "docker_client", types.SimpleNamespace(containers=types.SimpleNamespace(list=_boom))
    )
    assert ds.list_all_agents_fast() == []

    monkeypatch.setattr(ds, "docker_client", None)
    assert ds.list_all_agents_fast() == []


# ---------------------------------------------------------------------------
# The page, the card default, and the turn gates
# ---------------------------------------------------------------------------

def test_the_page_and_the_roster_agree_about_the_same_agent(roster_db, monkeypatch):
    """#2160's no-disagreement property, extended to the new field. The two
    reads are deliberately DIFFERENT calls (batch vs single) so one agent's page
    never pays a fleet-scale read — which is exactly the setup in which two
    surfaces drift."""
    from client_portal import service

    _pin_states(monkeypatch, {"scout": "running"})       # `sage` has no container
    _pin_state(monkeypatch, "missing")

    roster = {c.name: c.availability for c in _run(service.get_roster("bob@example.com")).agents}
    card = _run(service.get_agent_card("bob@example.com", "sage"))

    assert roster["sage"] == "unavailable"
    assert card.availability == "unavailable"


def test_the_page_header_carries_the_cards_availability(roster_db, monkeypatch):
    from client_portal import agent_page, service
    from client_portal.models import PortalAgentPage

    _pin_state(monkeypatch, "missing")
    card = _run(service.get_agent_card("bob@example.com", "sage"))

    page = agent_page.build_page("bob@example.com", "sage", card.model_dump())

    assert page["header"]["availability"] == "unavailable"
    PortalAgentPage(**page)      # the Literal must accept it


def test_a_page_built_from_no_card_still_validates():
    """`get_agent_card` returning None is documented-reachable (the agent
    vanished between the roster read and this one), and `card = card or {}`
    turns that into an explicit `None` from a bare `.get` — which a
    `Literal`-with-default REJECTS, 500ing the one page ent#360 built to always
    render. Hence `or "unknown"`."""
    from client_portal import agent_page
    from client_portal.models import PortalAgentPage

    page = agent_page.build_page("bob@example.com", "sage", None)

    assert page["header"]["availability"] == "unknown"
    PortalAgentPage(**page)


def test_the_card_default_is_fail_open():
    """Deliberately the OPPOSITE of `voice_available` /
    `multi_agent_chat_available`, which default False. Those fail closed because
    their bug is promising an affordance that cannot work; this one's bug is
    denying a working agent — and, since one unreadable socket marks every card
    at once, emptying a customer's roster over an infrastructure fault."""
    from client_portal.models import PortalAgentCard, PortalAgentHeader

    assert PortalAgentCard(name="scout").availability == "unknown"
    assert PortalAgentHeader(name="scout").availability == "unknown"
    assert PortalAgentCard(name="scout").voice_available is False


def test_the_briefing_is_skipped_only_for_states_that_cannot_answer(_quiet_briefing, monkeypatch):
    """`unknown` is ATTEMPTED, not skipped — and the asymmetry with the turn
    gate is the point rather than an oversight. The briefing reaches the agent at
    `http://agent-{name}:8000` **by DNS over the agent network**, so a backend
    Docker-socket fault says nothing about whether it answers HTTP. Skipping
    would turn one unreadable socket into "no briefings fleet-wide" when every
    briefing would in fact have worked.
    """
    briefing = _quiet_briefing        # the REAL function, not this file's stub
    attempted = []

    # Deliberately SYNCHRONOUS: `agent_httpx_client(...)` is called to obtain an
    # async context manager, so a coroutine stub would record nothing until it
    # was awaited — and it never is, because entering it fails first.
    def _fake_client(name, timeout=None):
        attempted.append(name)
        raise RuntimeError("no agent here")

    monkeypatch.setattr(_services_module("agent_auth"), "agent_httpx_client", _fake_client)

    for availability, should_attempt in (
        ("ready", True), ("unknown", True), ("stopped", False), ("unavailable", False),
    ):
        attempted.clear()
        # #2213: the briefing is an AgentBriefing NamedTuple (4 fields) —
        # the skip still costs nothing and still yields nothing.
        result = _run(briefing("scout", availability))
        assert result.description is None and not result.playbooks
        assert not result.searchable_playbooks and result.playbooks_total == 0
        assert bool(attempted) is should_attempt, availability


def test_a_roster_load_makes_no_per_agent_container_lookup(roster_db, monkeypatch):
    """`_agent_briefing` used to call `get_agent_container()` per card and throw
    the answer away. N inspects became one list call, and the answer is now
    used — which is why this change is cheaper than what it replaces, not an
    added Docker dependency."""
    from client_portal import service

    calls = []
    monkeypatch.setattr(
        _services_module("docker_service"), "get_agent_container",
        lambda name: calls.append(name),
    )
    _pin_states(monkeypatch, {"scout": "running", "sage": "running", "ghosted": "running"})

    _run(service.get_roster("bob@example.com"))

    assert calls == []


# ---------------------------------------------------------------------------
# AC #3 — refusing a turn honestly
# ---------------------------------------------------------------------------

@pytest.fixture()
def turn(monkeypatch):
    """`portal_chat` with everything but the availability gate stubbed."""
    from client_portal import service as svc
    from client_portal import db as portal_db

    written = []

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: "sess-1")
    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(
        portal_db, "add_portal_message",
        lambda *a, **kw: written.append(a),
    )
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    return svc, written


def test_portal_chat_refuses_before_it_persists_the_user_turn(turn, monkeypatch):
    """The real AC #3 hole, and the worse of the two paths.

    `portal_chat` had NO liveness gate — its 502 lives at the far end, AFTER
    `_persist_user_turn`. So a containerless agent left the client's thread
    holding a durable user message with no reply, plus an execution row, plus a
    "Please try again" that could never work. And this is not a dead path:
    `/chat` is the documented headless integration surface (ent#83) and the
    browser's fallback when streaming fails.
    """
    svc, written = turn
    _pin_state(monkeypatch, "missing")

    with pytest.raises(svc.ClientPortalError) as ei:
        _run(svc.portal_chat("ghosted", "hello?", "bob@example.com"))

    assert ei.value.status_code == 502
    assert written == [], "a user message was persisted for a turn that was refused"


def test_the_refusal_names_the_state_and_the_next_action(turn, monkeypatch):
    """Two states, two messages, one next action — and no "try again", because
    for both of these retrying cannot work. `POST /api/agents/{name}/start`
    recreates a missing container (#1559), so "its owner needs to start it" is
    correct for both. No infrastructure jargon: the viewer may be an external
    client with no Trinity account.
    """
    svc, _ = turn

    stopped = svc._refusal_detail("stopped")
    gone = svc._refusal_detail("unavailable")

    assert stopped != gone
    for detail in (stopped, gone):
        assert "try again" not in detail.lower()
        assert "owner" in detail.lower() and "start it" in detail.lower()
        for jargon in ("container", "docker", "502", "socket"):
            assert jargon not in detail.lower()


def test_an_unreadable_docker_does_not_refuse_the_turn(turn, monkeypatch):
    """Fail OPEN at the gate. A Docker API fault — a daemon restart, a socket
    permission change, a wrong `group_add` GID — leaves agent containers running
    and serving HTTP, so "cannot ask Docker" is not "the agent is down".
    Refusing here would deny every Workspace turn on the instance from one
    unreadable socket, which is the bug `_agent_is_running` shipped with."""
    svc, _ = turn
    _pin_state(monkeypatch, None)

    # Gets past the gate and fails later, on the stubbed execution stack — the
    # point is that it is NOT the 502 refusal.
    with pytest.raises(Exception) as ei:
        _run(svc.portal_chat("scout", "hello?", "bob@example.com"))

    assert svc._refusal_detail("unavailable") not in str(getattr(ei.value, "detail", ""))


def test_the_terminal_failure_copy_only_says_offline_when_we_could_not_look():
    """The far-end 502 is a different message from the pre-flight refusal: the
    turn RAN, so retrying may genuinely help and the instruction stays. But
    "it may be offline" is only honest when the agent's state could not be read
    at dispatch."""
    from client_portal import service

    assert "offline" in service._turn_failed_detail("unknown").lower()
    assert "offline" not in service._turn_failed_detail("ready").lower()
    assert "try again" in service._turn_failed_detail("ready").lower()


def test_the_roster_gate_still_runs_before_the_availability_gate():
    """A state-dependent refusal reached BEFORE `agent_on_roster` would be an
    existence oracle for a caller who holds no share — the Invariant #8 class.
    Both turn entry points must answer the uniform 404 first."""
    import inspect
    from client_portal import service

    for fn in (service.portal_chat, service.start_portal_turn):
        src = inspect.getsource(fn)
        assert src.index("agent_on_roster") < src.index("_availability_allows_turn"), fn.__name__


def test_membership_is_resolved_by_pure_sql(monkeypatch):
    """`_roster_rows` must stay pure SQL: #2198 resolves the roster through it
    for a batch-sessions gate, and availability attached there would be
    inherited by that endpoint. It is also the structural guarantee that
    container state is a PROJECTION and never a membership filter."""
    import inspect
    from client_portal import service

    src = inspect.getsource(service._roster_rows)
    for forbidden in ("_availability_map", "_agent_availability", "docker"):
        assert forbidden not in src, f"_roster_rows reaches for {forbidden}"


def test_availability_is_not_computed_inside_the_briefing():
    """#2163 is free to defer, bound or cache `_agent_briefing`. Anything
    computed inside it breaks the moment that lands, so availability is resolved
    by the caller and passed IN."""
    import inspect
    from client_portal import service

    src = inspect.getsource(service._agent_briefing)
    assert "_availability_map" not in src
    assert "_agent_availability" not in src
    assert "availability" in inspect.signature(service._agent_briefing).parameters
