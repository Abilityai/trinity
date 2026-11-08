"""`GET /api/ops/guardrail-stats` — fleet guardrail counts (trinity-enterprise#165, S4).

The app is built from the real router over a real database (`db_backend`) and a
fakeredis reporter store; events land through the real ingest. Only the
principal and the container runtime map are stubbed, each at the seam the
handler reads (asserted, so a stub that never lands cannot pass).

Pinned here:

- scope: an admin sees every live agent; anyone else sees the agents they OWN —
  never a sibling owner's, never one merely shared with them;
- a person only: agent, system, connector and ops keys are refused, even when
  the key's owner is an admin (Invariant #8 — an agent key carries its owner's
  role);
- per-kind and per-agent counts over the requested window, and only that window;
- every agent in scope is listed, events or not, with one `coverage` state —
  `no_hooks` / `never` / `stale` / `reporting` — so silence never reads as
  "no events".
"""

from __future__ import annotations

import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("fastapi", reason="backend venv required")
fakeredis = pytest.importorskip("fakeredis")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.guardrail_events as route_mod  # noqa: E402
from database import db  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402
from services import guardrail_event_service as svc  # noqa: E402
from utils.helpers import to_utc_iso, utc_now  # noqa: E402

OWNER = "stats-owner"
RIVAL = "stats-rival"
ADMIN = "stats-admin"

A_LOUD = "stats-loud"        # OWNER, claude, reporting, events
A_QUIET = "stats-quiet"      # OWNER, claude, reporting, no events
A_SILENT = "stats-silent"    # OWNER, never reported
A_STALE = "stats-stale"      # OWNER, last report an hour ago
A_CODEX = "stats-codex"      # OWNER, codex runtime, keepalives only
B_RIVAL = "stats-rival-agent"  # RIVAL, shared with OWNER, events

OWNED = {A_LOUD, A_QUIET, A_SILENT, A_STALE, A_CODEX}


def _ago(**kw) -> str:
    return to_utc_iso(utc_now() - timedelta(**kw))


def _ev(event_id, kind="deny", hook="bash_guardrail", code="destructive_rm", **kw):
    ev = {"event_id": event_id, "ts": _ago(minutes=5), "kind": kind, "hook": hook,
          "tool": "Bash", "code": code, "execution_id": "exec-1"}
    ev.update(kw)
    return ev


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from db_models import UserCreate

    db.create_user(UserCreate(username=OWNER, password="x", role="creator", email="owner@example.com"))
    db.create_user(UserCreate(username=RIVAL, password="x", role="creator", email="rival@example.com"))
    db.create_user(UserCreate(username=ADMIN, password="x", role="admin", email="admin@example.com"))
    for name in OWNED:
        db.register_agent_owner(name, OWNER)
    db.register_agent_owner(B_RIVAL, RIVAL)
    db.share_agent(B_RIVAL, RIVAL, "owner@example.com")

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(svc, "_get_redis", lambda: client)
    runtimes = {A_LOUD: "claude-code", A_QUIET: "claude-code", A_SILENT: "claude-code",
                A_STALE: "claude-code", A_CODEX: "codex", B_RIVAL: "claude-code"}
    calls = []

    def _runtimes():
        calls.append(1)
        return dict(runtimes)

    monkeypatch.setattr(svc, "_container_runtimes", _runtimes)

    # A_LOUD: three denies, one redaction, one old event outside a 2 h window.
    svc.ingest(A_LOUD, [
        _ev("l-1"), _ev("l-2"), _ev("l-3", hook="turn_tier_guard", code="shell_owner_only"),
        _ev("l-4", kind="credential_redacted", hook="output_scanner", code="sk"),
        _ev("l-old", ts=_ago(hours=3)),
    ], runtime="claude-code", dropped=0)
    svc.ingest(A_QUIET, [], runtime="claude-code", dropped=0)            # keepalive
    svc.ingest(A_CODEX, [], runtime="codex", dropped=0)                  # keepalive
    svc.ingest(A_STALE, [_ev("s-1")], runtime="claude-code", dropped=0)
    client.hset(svc._reporter_key(A_STALE), "last_report_at", _ago(hours=1))
    svc.ingest(B_RIVAL, [_ev("r-1"), _ev("r-2")], runtime="claude-code", dropped=0)
    yield {"redis": client, "runtimes": runtimes, "runtime_calls": calls}


def _user(username, role, **kw):
    row = db.get_user_by_username(username)
    return User(id=row["id"], username=username, email=row.get("email"), role=role, **kw)


def _get(principal, **params):
    app = FastAPI()
    app.include_router(route_mod.ops_router)
    app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app).get("/api/ops/guardrail-stats", params=params)


def _by_name(body):
    return {a["agent_name"]: a for a in body["agents"]}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestScope:
    def test_an_admin_sees_every_live_agent(self, world):
        r = _get(_user(ADMIN, "admin"))
        assert r.status_code == 200, r.text
        assert set(_by_name(r.json())) == OWNED | {B_RIVAL}

    def test_an_owner_sees_only_the_agents_they_own(self, world):
        """Owned, not shared: B_RIVAL is shared with OWNER and must not appear
        (the per-agent read is owner tier, ruling D8)."""
        r = _get(_user(OWNER, "creator"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(_by_name(body)) == OWNED
        # The rival's two events are not counted in OWNER's totals either.
        assert body["events"] == 6

    def test_a_second_owner_sees_only_theirs(self, world):
        r = _get(_user(RIVAL, "creator"))
        assert r.status_code == 200
        assert set(_by_name(r.json())) == {B_RIVAL}

    def test_a_person_who_owns_nothing_gets_an_empty_answer(self, world):
        from db_models import UserCreate
        db.create_user(UserCreate(username="stats-nobody", password="x", role="creator",
                                  email="nobody@example.com"))
        body = _get(_user("stats-nobody", "creator")).json()
        assert body["agents"] == []
        assert body["events"] == 0
        assert body["coverage"] == {"reporting": 0, "stale": 0, "never": 0, "no_hooks": 0}

    def test_a_soft_deleted_agent_is_not_listed(self, world):
        assert db.delete_agent_ownership(A_QUIET)
        assert A_QUIET not in _by_name(_get(_user(ADMIN, "admin")).json())
        assert A_QUIET not in _by_name(_get(_user(OWNER, "creator")).json())


# ---------------------------------------------------------------------------
# Who may call it
# ---------------------------------------------------------------------------

class TestPrincipals:
    def test_the_persons_own_user_key_is_a_person(self, world):
        r = _get(_user(ADMIN, "admin", mcp_scope="user"))
        assert r.status_code == 200, r.text
        assert set(_by_name(r.json())) == OWNED | {B_RIVAL}

    @pytest.mark.parametrize("kw", [
        {"mcp_scope": "agent", "agent_name": A_LOUD},          # an admin-owned agent's key
        {"mcp_scope": "system"},
        {"mcp_scope": "ops"},
        {"mcp_scope": "connector", "connector_agent": A_LOUD},
        {"mcp_scope": "portal_delegate", "portal_delegate": True},
    ])
    def test_a_machine_key_is_refused_even_on_an_admin_owner(self, world, kw):
        r = _get(_user(ADMIN, "admin", **kw))
        assert r.status_code == 403, r.text
        assert "agents" not in r.json()


# ---------------------------------------------------------------------------
# Counts and the window
# ---------------------------------------------------------------------------

class TestCounts:
    def test_per_kind_and_per_agent_counts_over_the_default_24_hours(self, world):
        body = _get(_user(OWNER, "creator")).json()
        assert body["hours"] == 24
        loud = _by_name(body)[A_LOUD]
        assert loud["events"] == 5
        assert loud["by_kind"] == {"deny": 4, "credential_redacted": 1}
        assert _by_name(body)[A_STALE]["by_kind"] == {"deny": 1}
        assert body["by_kind"] == {"deny": 5, "credential_redacted": 1}

    def test_totals_are_the_sum_of_the_rows(self, world):
        body = _get(_user(OWNER, "creator")).json()
        assert body["events"] == sum(a["events"] for a in body["agents"]) == 6
        assert sum(body["by_kind"].values()) == 6

    def test_the_window_is_the_requested_hours(self, world):
        """2 h drops the 3-hour-old event; 24 h keeps it. A non-default value,
        so a handler that ignores `hours` cannot pass."""
        two = _by_name(_get(_user(OWNER, "creator"), hours=2).json())[A_LOUD]
        day = _by_name(_get(_user(OWNER, "creator"), hours=24).json())[A_LOUD]
        assert two["events"] == 4
        assert day["events"] == 5

    def test_agents_are_listed_loudest_first(self, world):
        names = [a["agent_name"] for a in _get(_user(OWNER, "creator")).json()["agents"]]
        assert names[0] == A_LOUD
        assert names[1] == A_STALE
        assert names[2:] == sorted(names[2:])

    @pytest.mark.parametrize("hours", ["0", "-1", "2161", "abc"])
    def test_an_out_of_range_window_is_422(self, world, hours):
        assert _get(_user(ADMIN, "admin"), hours=hours).status_code == 422

    def test_the_longest_window_is_the_retention_window(self, world):
        assert _get(_user(ADMIN, "admin"), hours=2160).status_code == 200

    @pytest.mark.parametrize("hours", [0, 2161, True, "24"])
    def test_the_service_bounds_the_window_on_its_own(self, world, hours):
        """Not only the route: another caller of the seam gets the same bound."""
        with pytest.raises(svc.GuardrailEventQueryError, match="invalid_hours"):
            svc.fleet_stats(hours, [A_LOUD])


# ---------------------------------------------------------------------------
# Honest coverage: silence never reads as healthy
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_each_agent_carries_one_coverage_state(self, world):
        rows = _by_name(_get(_user(OWNER, "creator")).json())
        assert world["runtime_calls"], "the runtime seam was never reached"
        assert rows[A_LOUD]["coverage"] == "reporting"
        assert rows[A_QUIET]["coverage"] == "reporting"
        assert rows[A_QUIET]["events"] == 0
        assert rows[A_SILENT]["coverage"] == "never"
        assert rows[A_SILENT]["reporting"]["state"] == "never"
        assert rows[A_STALE]["coverage"] == "stale"
        assert rows[A_STALE]["reporting"]["last_report_at"] is not None
        # A Codex agent's keepalives make it "reporting", but no hook can fire.
        assert rows[A_CODEX]["reporting"]["state"] == "reporting"
        assert rows[A_CODEX]["coverage"] == "no_hooks"
        assert rows[A_CODEX]["runtime"] == "codex"

    def test_the_coverage_tally(self, world):
        body = _get(_user(OWNER, "creator")).json()
        assert body["coverage"] == {"reporting": 2, "stale": 1, "never": 1, "no_hooks": 1}

    def test_runtime_falls_back_to_the_forwarders_report(self, world, monkeypatch):
        """Docker unreadable: the forwarder's last report names the runtime."""
        monkeypatch.setattr(svc, "_container_runtimes", lambda: None)
        rows = _by_name(_get(_user(OWNER, "creator")).json())
        assert rows[A_CODEX]["coverage"] == "no_hooks"
        assert rows[A_CODEX]["runtime"] == "codex"
        # Never reported and no label: the platform default (Claude Code).
        assert rows[A_SILENT]["runtime"] is None
        assert rows[A_SILENT]["coverage"] == "never"

    def test_the_container_label_wins_over_the_report(self, world):
        world["runtimes"][A_QUIET] = "gemini"
        rows = _by_name(_get(_user(OWNER, "creator")).json())
        assert rows[A_QUIET]["runtime"] == "gemini"
        assert rows[A_QUIET]["coverage"] == "no_hooks"

    def test_with_redis_down_an_agent_with_rows_is_not_never(self, world, monkeypatch):
        monkeypatch.setattr(svc, "_get_redis", lambda: None)
        rows = _by_name(_get(_user(OWNER, "creator")).json())
        assert rows[A_LOUD]["reporting"]["state"] == "reporting"
        assert rows[A_QUIET]["reporting"]["state"] == "never"


# ---------------------------------------------------------------------------
# The batch reporting read agrees with the per-agent one
# ---------------------------------------------------------------------------

class TestReportingStatesParity:
    def test_batch_equals_one_at_a_time(self, world):
        names = sorted(OWNED | {B_RIVAL})
        now = utc_now()
        batch = svc.reporting_states(names, now=now)
        assert batch == {n: svc.reporting_state(n, now=now) for n in names}

    def test_batch_equals_one_at_a_time_on_the_db_fallback(self, world, monkeypatch):
        monkeypatch.setattr(svc, "_get_redis", lambda: None)
        names = sorted(OWNED | {B_RIVAL})
        now = utc_now()
        assert svc.reporting_states(names, now=now) == {
            n: svc.reporting_state(n, now=now) for n in names}

    def test_the_coverage_start_falls_back_to_the_oldest_row_in_both_reads(
            self, world, monkeypatch):
        """Redis lost: the batch read must take the OLDEST row as the coverage
        start, as the single read does — not the newest, which every
        one-batch agent in the world fixture would hide."""
        hour_ago = utc_now() - timedelta(hours=1)
        with monkeypatch.context() as m:
            m.setattr(svc, "utc_now", lambda: hour_ago)
            svc.ingest(A_LOUD, [_ev("l-early")], runtime="claude-code", dropped=0)
        monkeypatch.setattr(svc, "_get_redis", lambda: None)
        now = utc_now()
        batch = svc.reporting_states([A_LOUD], now=now)
        assert batch[A_LOUD]["first_report_at"] == to_utc_iso(hour_ago)
        assert batch[A_LOUD]["last_report_at"] > batch[A_LOUD]["first_report_at"]
        assert batch == {A_LOUD: svc.reporting_state(A_LOUD, now=now)}

    def test_no_names_reads_nothing(self, world, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no read expected")
        monkeypatch.setattr(db, "guardrail_event_received_bounds_by_agent", boom)
        assert svc.reporting_states([]) == {}


def test_hooked_runtimes_mirror_the_platforms_claude_names():
    """The coverage rule names the Claude runtimes; it must agree with the
    platform's own classification (`agent_service.helpers`)."""
    from services.agent_service.helpers import CLAUDE_RUNTIME_NAMES
    assert svc.HOOKED_RUNTIMES == CLAUDE_RUNTIME_NAMES
