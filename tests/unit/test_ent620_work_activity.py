"""
trinity-enterprise#620 — the Work card's live activity line: what the agent
is doing right now, from its heartbeat, folded onto the Work read.

Three layers, each pinned here:
  1. the heartbeat model bounds the agent-authored payload (a refused beat
     costs a card line, never the #307 liveness verdict);
  2. `heartbeat_service.read_execution_activity` keys the beat's entries by
     execution id and stamps the beat's receive time;
  3. the Work read folds a line ONLY onto a live, non-stale row of a rostered
     agent, through the SAME sanitiser as titles, masks an off-roster
     delegation target, and drops a line the agent stopped renewing —
     never a stale or invented line (AC #3/#4/#6). The cheap `/activity`
     sibling read carries the same gates.

Harness: the test_ent525_portal_work.py shape (every seam stubbed).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent620.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent620-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
AGENT = "scout"
OTHER = "sage"
HIDDEN = "vault"
SESSION = "sess-1"


def _run(coro):
    return asyncio.run(coro)


def _recent(seconds_ago: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(**over):
    base = dict(
        id="exec-1", agent_name=AGENT, status="running", started_at=_recent(),
        completed_at=None, duration_ms=None, message="Reconcile the invoices",
        triggered_by="public", source_user_email=EMAIL, source_agent_name=None,
        source_channel="portal", source_channel_chat_id=SESSION, loop_id=None,
        error_summary=None,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. The heartbeat model bounds the payload
# ---------------------------------------------------------------------------

def test_heartbeat_payload_accepts_the_activity_list_and_a_pre_620_beat():
    from models import HeartbeatPayload, HEARTBEAT_ACTIVITY_MAX_EXECUTIONS
    old = HeartbeatPayload(memory_mb=1.0, active_executions=1, uptime_s=2.0)
    assert old.executions is None
    new = HeartbeatPayload(memory_mb=1.0, executions=[
        {"execution_id": "exec-1", "tool": "Read", "summary": ".../routers/agents.py", "since": "2026-09-16T10:00:00"},
        {"execution_id": "exec-2", "tool": None, "summary": None, "since": None},
    ])
    assert [e.execution_id for e in new.executions] == ["exec-1", "exec-2"]
    assert new.model_dump(exclude_none=True)["executions"][1] == {"execution_id": "exec-2"}
    assert HEARTBEAT_ACTIVITY_MAX_EXECUTIONS == 20


@pytest.mark.parametrize("bad", [
    [{"execution_id": "x" * 129, "tool": "Read"}],                    # id too long
    [{"execution_id": "../etc", "tool": "Read"}],                      # id shape
    [{"execution_id": "e1", "tool": "T" * 65}],                        # tool too long
    [{"execution_id": "e1", "summary": "s" * 121}],                    # summary too long
    [{"execution_id": "e1", "tool": "Read", "input": {"raw": "no"}}],  # extra key: the raw input never rides
    [{"execution_id": f"e{i}"} for i in range(21)],                    # too many
])
def test_heartbeat_payload_refuses_what_the_agent_must_not_send(bad):
    from models import HeartbeatPayload
    with pytest.raises(ValidationError):
        HeartbeatPayload(memory_mb=1.0, executions=bad)


# ---------------------------------------------------------------------------
# 2. The read keys by execution id and carries the beat's receive time
# ---------------------------------------------------------------------------

def test_read_execution_activity_keys_entries_and_stamps_ts(monkeypatch):
    from services import heartbeat_service as hb
    monkeypatch.setattr(hb, "read_heartbeat", lambda name: {
        "memory_mb": 1, "ts": 1000.0,
        "executions": [
            {"execution_id": "e1", "tool": "Bash", "summary": "pytest tests/unit -q", "since": "s1"},
            {"execution_id": "e2"},
            {"execution_id": 7},            # malformed: dropped
            "garbage",                      # malformed: dropped
        ],
    })
    out = hb.read_execution_activity(AGENT)
    assert out == {
        "e1": {"tool": "Bash", "summary": "pytest tests/unit -q", "since": "s1", "ts": 1000.0},
        "e2": {"tool": None, "summary": None, "since": None, "ts": 1000.0},
    }


def test_read_execution_activity_is_empty_without_a_beat_or_on_an_old_image(monkeypatch):
    from services import heartbeat_service as hb
    monkeypatch.setattr(hb, "read_heartbeat", lambda name: None)
    assert hb.read_execution_activity(AGENT) == {}
    monkeypatch.setattr(hb, "read_heartbeat", lambda name: {"memory_mb": 1, "ts": 1.0})
    assert hb.read_execution_activity(AGENT) == {}


# ---------------------------------------------------------------------------
# 3. The Work read
# ---------------------------------------------------------------------------

@pytest.fixture
def svc():
    from client_portal.work import service as mod
    return mod


def test_clean_activity_sanitises_bounds_and_masks(svc):
    now = datetime.now(timezone.utc)
    roster = {AGENT, OTHER}
    fresh = now.timestamp() - 3
    # The same sanitiser as titles: a token in a command never reaches the card.
    a = svc.clean_activity({"tool": "Bash", "summary": "curl -H 'Authorization: Bearer sk-ant-api03-" + "B" * 40 + "'", "since": "s", "ts": fresh}, roster, now=now)
    assert a.tool == "Bash" and "sk-ant-api03-" not in a.summary and a.age_seconds == 3
    # Bounded to the title bound, with an ellipsis.
    b = svc.clean_activity({"tool": "Read", "summary": "x" * 500, "ts": fresh}, roster, now=now)
    assert len(b.summary) == svc.ACTIVITY_SUMMARY_MAX and b.summary.endswith("…")
    # A delegation to an off-roster agent names nobody; to a rostered one keeps the name.
    c = svc.clean_activity({"tool": "mcp:trinity", "summary": f"agent_name: {HIDDEN}", "ts": fresh}, roster, now=now)
    assert c.summary == "agent_name: another agent"
    d = svc.clean_activity({"tool": "mcp:trinity", "summary": f"agent_name: {OTHER}", "ts": fresh}, roster, now=now)
    assert d.summary == f"agent_name: {OTHER}"
    # Between tools: tool None, the card says "Thinking"; whitespace-only summary is None.
    e = svc.clean_activity({"tool": None, "summary": "   ", "ts": fresh}, roster, now=now)
    assert e.tool is None and e.summary is None
    # Nothing to fold.
    assert svc.clean_activity(None, roster, now=now) is None


def test_a_line_the_agent_stopped_renewing_is_dropped(svc):
    now = datetime.now(timezone.utc)
    old = now.timestamp() - (svc.ACTIVITY_MAX_AGE_S + 1)
    assert svc.clean_activity({"tool": "Read", "summary": "a/b", "ts": old}, {AGENT}, now=now) is None
    edge = now.timestamp() - svc.ACTIVITY_MAX_AGE_S
    assert svc.clean_activity({"tool": "Read", "summary": "a/b", "ts": edge}, {AGENT}, now=now) is not None


class _Ledger:
    def __init__(self, running=(), queued=(), recent=(), total=0, children=()):
        self.running, self.queued, self.recent = list(running), list(queued), list(recent)
        self.total, self.children = total, list(children)

    def get_fleet_executions(self, agent_names, *, status=None, hours=24, limit=50, **_):
        if status == "running":
            return self.running
        if status == "queued":
            return self.queued
        return self.recent

    def get_fleet_execution_stats(self, agent_names, hours=24):
        return {"total": self.total}

    def get_running_for_chat(self, chat_id):
        return self.children


@pytest.fixture
def wired(svc, monkeypatch):
    ledger = _Ledger()
    monkeypatch.setattr(svc, "core_db", ledger)
    monkeypatch.setattr(svc, "roster_agent_names", lambda email, include_owned: {AGENT, OTHER})
    monkeypatch.setattr(svc.portal_db, "get_portal_session",
                        lambda sid, agent, email: {"id": sid} if sid == SESSION and agent == AGENT else None)
    monkeypatch.setattr(svc, "_resolve_timeout", lambda agent: 3600)

    async def fake_steps(agent, started_at=None, roster=None):
        return svc.WorkSteps(state="unknown")
    monkeypatch.setattr(svc.pipeline_state, "read_pipeline_steps", fake_steps)

    beats = {}
    from services import heartbeat_service as hb
    monkeypatch.setattr(hb, "read_execution_activity", lambda name: beats.get(name, {}))
    return SimpleNamespace(ledger=ledger, beats=beats)


def _fresh_ts():
    return datetime.now(timezone.utc).timestamp() - 2


def test_the_line_is_folded_onto_live_rows_of_rostered_agents_only(svc, wired):
    wired.ledger.running = [
        _row(id="turn-1"),
        _row(id="sched-1", agent_name=OTHER, triggered_by="schedule", source_channel=None, source_channel_chat_id=None),
        _row(id="old-1", started_at=_recent(3 * 3600 * 2)),      # stale: × 1.5 the 3600 s bound
    ]
    wired.ledger.children = [
        _row(id="child-1", agent_name=HIDDEN, triggered_by="mcp", source_agent_name=AGENT),
    ]
    wired.beats[AGENT] = {
        "turn-1": {"tool": "Read", "summary": ".../routers/agents.py", "since": "s", "ts": _fresh_ts()},
        "old-1": {"tool": "Bash", "summary": "sleep 999", "since": "s", "ts": _fresh_ts()},
    }
    wired.beats[OTHER] = {"sched-1": {"tool": None, "summary": None, "since": "s", "ts": _fresh_ts()}}
    wired.beats[HIDDEN] = {"child-1": {"tool": "Bash", "summary": "rm -rf secrets", "since": "s", "ts": _fresh_ts()}}

    out = _run(svc.get_work(EMAIL, [AGENT, OTHER], chat_id=SESSION))
    by_id = {it.id: it for it in out.now}
    assert by_id["turn-1"].activity.tool == "Read"
    assert by_id["turn-1"].activity.summary == ".../routers/agents.py"
    assert by_id["sched-1"].activity.tool is None            # delegated/scheduled runs get the line too — "Thinking"
    assert by_id["old-1"].activity is None                   # stale: never a line on a row nobody is watching
    assert by_id["child-1"].activity is None                 # off-roster agent: nothing of its activity is the caller's


def test_no_beat_means_no_line_never_an_invented_one(svc, wired):
    wired.ledger.running = [_row(id="turn-1")]
    out = _run(svc.get_work(EMAIL, [AGENT], chat_id=SESSION))
    assert out.now[0].activity is None
    assert out.now[0].steps.state == "unknown"               # the existing three-state rule is untouched


def test_the_activity_read_carries_the_same_gates(svc, wired):
    wired.beats[AGENT] = {"turn-1": {"tool": "Grep", "summary": '"sync_health"', "since": "s", "ts": _fresh_ts()},
                          "gone-1": {"tool": "Read", "summary": "a/b", "since": "s", "ts": _fresh_ts() - 100}}
    wired.beats[HIDDEN] = {"h-1": {"tool": "Read", "summary": "a/b", "since": "s", "ts": _fresh_ts()}}
    out = _run(svc.get_work_activity(EMAIL, [AGENT, HIDDEN, "nope"]))
    assert out.agents == [AGENT]
    assert set(out.items) == {"turn-1"}                      # the expired line and the hidden agent are absent
    assert out.items["turn-1"].tool == "Grep" and out.items["turn-1"].summary == '"sync_health"'


def test_the_activity_route_is_platform_only_and_capped(monkeypatch):
    from client_portal.work import router as r
    from client_portal.work import service as svc
    from client_portal.portal_auth import PortalPrincipal
    from fastapi import HTTPException

    called = []

    async def spy(email, names):
        called.append((email, names))
        return svc.PortalWorkActivity(agents=names, items={})
    monkeypatch.setattr(svc, "get_work_activity", spy)
    monkeypatch.setattr(r, "service", svc)

    with pytest.raises(HTTPException) as exc:
        _run(r.get_work_activity(agents=AGENT, principal=PortalPrincipal("client@example.com", False)))
    assert exc.value.status_code == 404 and called == []

    too_many = ",".join(f"a{i}" for i in range(svc.MAX_AGENTS + 1))
    with pytest.raises(HTTPException) as exc:
        _run(r.get_work_activity(agents=too_many, principal=PortalPrincipal(EMAIL, True)))
    assert exc.value.status_code == 422 and called == []

    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    out = _run(r.get_work_activity(agents=f"{AGENT},{OTHER}", principal=PortalPrincipal(EMAIL, True)))
    assert called == [(EMAIL, [AGENT, OTHER])] and out.items == {}


def test_the_projection_still_excludes_the_log_and_tool_calls():
    """AC #6: nothing the Work payload excludes today becomes visible through
    the line. The model has exactly one new field, and it carries four facts."""
    from client_portal.work.models import WorkItem, WorkActivity
    assert "activity" in WorkItem.model_fields
    assert set(WorkActivity.model_fields) == {"tool", "summary", "since", "age_seconds"}
    for forbidden in ("execution_log", "tool_calls", "response", "input"):
        assert forbidden not in WorkItem.model_fields
        assert forbidden not in WorkActivity.model_fields
