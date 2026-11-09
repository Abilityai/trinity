"""trinity-enterprise#165 S2 — the agent server's guardrail event forwarder.

The hooks append to `/logs/guardrails.jsonl`; nothing used to read it, so no
guardrail decision ever left the container. `agent_server/guardrail_events.py`
tails that spool and posts the events to the platform under the agent's own
key. These tests drive the real module with a fake clock and a fake backend:

- what leaves the container is a projection through an allowlist, and a
  credential a command carried never appears in a POST body;
- delivery is within 2 s of the first unsent append, not on a sweep;
- 413 splits, 422 drops and counts, 404 (a backend without the ingest route)
  keeps the spool and retries hourly, 5xx backs off;
- the offset survives an agent-server restart, rotation loses nothing a hook
  appended during the rename, and a spool the platform never drains is bounded
  with the loss counted in `dropped`;
- the loop starts with the app only when the agent has its key.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Import-time `agent_server` namespace shim (the heartbeat test's pattern):
# monkeypatch cannot reach import time, so the bare writes are declared here.
_STUBBED_MODULE_NAMES = [
    "agent_server",
    "agent_server.guardrail_events",
    "agent_server.state",
    "agent_server.utils",
    "agent_server.utils.credential_sanitizer",
    "agent_server.models",
    "agent_server.model_context",
    "agent_server.services",
    "agent_server.services.execution_env",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [_real_path]  # type: ignore[attr-defined]
    _stub.__package__ = "agent_server"
    sys.modules["agent_server"] = _stub

from agent_server import guardrail_events as ge  # noqa: E402

FAKE_KEY = "sk-ant-api03-" + "Z" * 93


# ---- fixtures ----------------------------------------------------------------------------------


class Backend:
    """A fake ingest. `answers` is consumed one per POST; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(200, None)]
        self.bodies = []

    async def post(self, body):
        # What would go on the wire: httpx's own encoder (strict UTF-8), not our dict.
        self.bodies.append(
            json.loads(httpx.Request("POST", "http://backend:8000", json=body).content)
        )
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def event_ids(self):
        return [e["event_id"] for b in self.bodies for e in b["events"]]


@pytest.fixture
def spool(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "guardrails.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(ge, "SPOOL_PATH", str(path))
    monkeypatch.setattr(
        ge, "STATE_PATH", str(tmp_path / "logs" / ".guardrails-forwarder.json")
    )
    return path


def record(**over):
    """A deny record as lib.log_event writes it after S2."""
    rec = {
        "ts": 1762560000.25,
        "event": "deny",
        "reason": "a long human sentence",
        "tool": "Bash",
        "pattern": "rm -rf",
        "command_prefix": "rm -rf / " + FAKE_KEY,
        "v": 1,
        "event_id": str(uuid.uuid4()),
        "kind": "deny",
        "hook": "bash_guardrail",
        "code": "bash_deny_pattern",
        "execution_id": "exec-165",
        "session_id": "sess-1",
        "tool_use_id": "toolu_1",
        "rule": "rm -rf",
    }
    rec.update(over)
    return rec


def append(path, *records, raw=None):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        if raw is not None:
            f.write(raw)


def make(backend, **kw):
    return ge.Forwarder(
        backend.post, runtime=kw.pop("runtime", "claude-code"), rand=lambda: 1.0, **kw
    )


async def drive(fwd, start, until, *, stop=None):
    """Run the loop on a fake clock: tick, then advance by what tick asked for."""
    now = start
    while now < until:
        delay = await fwd.tick(now)
        if stop is not None and stop():
            return now
        now += max(delay, 0.01)
    return now


def run(coro):
    return asyncio.run(coro)


# ---- the projection: what may leave the container -------------------------------------------


def test_the_projection_is_an_allowlist():
    out = ge.project(
        record(
            error="Traceback ... " + FAKE_KEY,
            traceback="tb",
            verb="exec",
            tier="share",
            turn_kind="visitor",
            patterns=["openai_api_key"],
            path="/home/developer/x",
        )
    )
    assert set(out) == {
        "event_id",
        "ts",
        "kind",
        "hook",
        "tool",
        "code",
        "execution_id",
        "session_id",
        "tool_use_id",
        "verb",
        "tier",
        "turn_kind",
        "patterns",
        "rule",
        "path",
    }
    assert out["ts"] == "2025-11-08T00:00:00.250Z"
    for never in (
        "command_prefix",
        "traceback",
        "error",
        "reason",
        "pattern",
        "event",
        "v",
    ):
        assert never not in out


def test_a_key_the_command_carried_never_reaches_a_post_body(spool):
    append(spool, record(rule="curl -H " + FAKE_KEY, path="/tmp/" + FAKE_KEY))
    backend = Backend()
    run(drive(make(backend), 0.0, 5.0))
    wire = json.dumps(backend.bodies)
    assert backend.event_ids(), "the event was delivered"
    assert FAKE_KEY not in wire and FAKE_KEY[:30] not in wire


def test_long_strings_are_bounded_and_overlong_ids_dropped():
    out = ge.project(record(rule="r" * 999, path="p" * 999, execution_id="e" * 129))
    assert len(out["rule"]) == 200 and len(out["path"]) == 512
    assert out["execution_id"] is None


@pytest.mark.parametrize(
    "rec",
    [
        {"ts": 1.0, "event": "deny", "reason": "x"},  # no `v`: older hook
        record(kind=None),  # allow-type record
        record(hook=None),  # not one of the hooks
        {k: v for k, v in record().items() if k != "v"},  # every field but the version
    ],
)
def test_records_that_are_not_forwarded(spool, rec):
    append(spool, rec)
    backend = Backend()
    run(drive(make(backend), 0.0, 5.0))
    assert backend.event_ids() == []
    assert all(b["dropped"] == 0 for b in backend.bodies)


def test_a_line_that_does_not_parse_is_counted_as_dropped(spool):
    append(spool, record(), raw="{not json\n")
    backend = Backend()
    run(drive(make(backend), 0.0, 5.0))
    assert len(backend.event_ids()) == 1
    assert sum(b["dropped"] for b in backend.bodies) == 1


# ---- delivery latency -------------------------------------------------------------------------


def test_a_lone_surrogate_in_a_field_never_stalls_the_spool(spool):
    """A hook input can carry an escaped lone surrogate (a model-written
    `file_path`). It survives the spool's json round trip, but httpx encodes the
    body as strict UTF-8, so the POST raised before it was sent, on every
    attempt: that record and every one after it were never delivered, and the
    keepalives stopped with them. Text gets U+FFFD and an id holding one is
    dropped, the platform's own rule."""
    poison = record(
        hook="file_guardrail",
        code="protected_path",
        tool="Write",
        path="/home/developer/\udcff.env",
        session_id="sess-\udcff",
    )
    good = record()
    append(spool, poison, good)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "duplicates": 0, "rejected": 0})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            async def post(body):
                response = await client.post("http://backend:8000/ingest", json=body)
                return response.status_code, response.headers.get("Retry-After")

            await drive(ge.Forwarder(post, runtime="claude-code", rand=lambda: 1.0), 0.0, 5.0)

    run(go())
    events = [e for b in sent for e in b["events"]]
    assert [e["event_id"] for e in events] == [poison["event_id"], good["event_id"]]
    assert events[0]["path"] == "/home/developer/\ufffd.env"
    assert events[0]["session_id"] is None


def test_a_timestamp_out_of_float_range_is_counted_not_retried_forever(spool):
    """A JSON integer too large for a float raised OverflowError in the
    projection, outside every guard, so the step failed on the same line forever."""
    bad = record(ts=10**400)
    good = record()
    append(spool, bad, good)
    backend = Backend()
    run(drive(make(backend), 0.0, 5.0))
    assert backend.event_ids() == [good["event_id"]]
    assert sum(b["dropped"] for b in backend.bodies) == 1


def test_an_append_is_delivered_within_two_seconds(spool):
    backend = Backend()
    fwd = make(backend)
    now = run(
        drive(fwd, 0.0, 30.0, stop=lambda: len(backend.bodies) >= 1)
    )  # startup keepalive
    appended_at = now + 0.3
    append(spool, record())

    async def until_delivered():
        t = appended_at
        while not backend.event_ids():
            t += max(await fwd.tick(t), 0.01)
            assert t - appended_at < 10, "never delivered"
        return t

    delivered_at = run(until_delivered())
    assert delivered_at - appended_at <= 2.0


def test_a_burst_goes_in_one_post(spool):
    backend = Backend()
    fwd = make(backend)
    run(drive(fwd, 0.0, 1.0, stop=lambda: len(backend.bodies) >= 1))
    append(spool, *[record() for _ in range(20)])
    run(drive(fwd, 10.0, 15.0))
    assert [len(b["events"]) for b in backend.bodies if b["events"]] == [20]


def test_a_batch_is_bounded_by_count_and_bytes(spool, monkeypatch):
    monkeypatch.setattr(ge, "MAX_BATCH_EVENTS", 7)
    append(spool, *[record() for _ in range(20)])
    backend = Backend()
    run(drive(make(backend), 0.0, 10.0))
    sizes = [len(b["events"]) for b in backend.bodies if b["events"]]
    assert sizes == [7, 7, 6]
    monkeypatch.setattr(ge, "MAX_BATCH_EVENTS", 500)
    monkeypatch.setattr(ge, "MAX_BATCH_BYTES", 4096)
    append(spool, *[record(path="p" * 400) for _ in range(30)])
    backend2 = Backend()
    run(drive(make(backend2), 20.0, 40.0))
    assert all(len(json.dumps(b)) <= 4096 for b in backend2.bodies)
    assert len(backend2.event_ids()) == 30


# ---- the envelope and keepalive --------------------------------------------------------------


def test_the_envelope(spool):
    append(spool, record())
    backend = Backend()
    run(drive(make(backend, runtime="codex"), 0.0, 5.0))
    body = backend.bodies[0]
    assert set(body) == {"schema", "runtime", "events", "dropped"}
    assert body["schema"] == 1 and body["runtime"] == "codex"


def test_an_idle_agent_sends_a_keepalive_every_five_minutes(spool):
    backend = Backend()
    run(drive(make(backend, runtime="gemini"), 0.0, 901.0))
    assert len(backend.bodies) == 4  # t=0, 300, 600, 900
    assert all(b["events"] == [] and b["runtime"] == "gemini" for b in backend.bodies)


# ---- responses ---------------------------------------------------------------------------------


def test_413_splits_the_batch_and_delivers_everything(spool):
    append(spool, *[record() for _ in range(9)])

    class Small(Backend):
        async def post(self, body):
            self.bodies.append(json.loads(json.dumps(body)))
            return (413, None) if len(body["events"]) > 2 else (200, None)

    backend = Small()
    run(drive(make(backend), 0.0, 30.0))
    accepted = [
        e["event_id"]
        for b in backend.bodies
        if len(b["events"]) <= 2
        for e in b["events"]
    ]
    assert len(accepted) == 9 and len(set(accepted)) == 9


def test_413_on_a_single_event_drops_it_and_counts_it(spool):
    append(spool, record(path="p" * 500), record())

    class NoBig(Backend):
        async def post(self, body):
            self.bodies.append(json.loads(json.dumps(body)))
            big = any(len(e.get("path", "")) > 100 for e in body["events"])
            return (413, None) if big else (200, None)

    backend = NoBig()
    run(drive(make(backend), 0.0, 30.0))
    delivered = [
        b
        for b in backend.bodies
        if b["events"] and not any(len(e.get("path", "")) > 100 for e in b["events"])
    ]
    assert len(delivered[0]["events"]) == 1
    assert sum(b["dropped"] for b in delivered) == 1


def test_422_drops_the_batch_and_counts_it_in_the_next_post(spool):
    append(spool, record(), record())
    backend = Backend((422, None), (200, None))
    fwd = make(backend)
    run(drive(fwd, 0.0, 5.0))
    refused, accepted = backend.bodies[0], backend.bodies[1]
    assert len(refused["events"]) == 2
    assert accepted["dropped"] == 2, "the next accepted post reports the loss"
    append(spool, record())
    run(drive(fwd, 10.0, 15.0))
    last = backend.bodies[-1]
    assert len(last["events"]) == 1 and last["dropped"] == 0, "never resent, never counted twice"


def test_a_backend_without_the_route_keeps_the_spool_and_retries_hourly(spool, caplog):
    append(spool, record(), record())
    backend = Backend((404, None))
    fwd = make(backend)
    caplog.set_level("DEBUG", logger=ge.logger.name)
    run(drive(fwd, 0.0, 3599.0))
    assert len(backend.bodies) == 1, "one attempt, then quiet for an hour"
    assert (
        sum("guardrail" in r.getMessage() for r in caplog.records if r.levelno >= 20)
        == 1
    )
    backend.answers = [(200, None)]
    run(drive(fwd, 3601.0, 3610.0))
    assert len(backend.event_ids()) == 4  # the 404'd pair, then the same pair delivered
    assert backend.bodies[-1]["events"] and len(set(backend.event_ids())) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_keeps_the_spool_and_retries_hourly(spool, caplog, status):
    """The platform answers 403 to a key that is not the agent's own, and also
    when the key is revoked while the body uploads. A key will not fix itself
    in seconds: keep the records, ask again in an hour, log once."""
    append(spool, record(), record())
    backend = Backend((status, None))
    fwd = make(backend)
    caplog.set_level("DEBUG", logger=ge.logger.name)
    run(drive(fwd, 0.0, 3599.0))
    assert len(backend.bodies) == 1, "one attempt, then quiet for an hour"
    assert (
        sum("guardrail" in r.getMessage() for r in caplog.records if r.levelno >= 20)
        == 1
    )
    backend.answers = [(200, None)]
    run(drive(fwd, 3601.0, 3610.0))
    assert len(set(backend.event_ids())) == 2 and backend.bodies[-1]["dropped"] == 0


def test_a_resend_carries_the_same_event_ids(spool):
    append(spool, record(), record())
    backend = Backend((503, None), ConnectionError("down"), (200, None))
    run(drive(make(backend), 0.0, 60.0))
    first, second, third = (
        sorted(e["event_id"] for e in b["events"]) for b in backend.bodies[:3]
    )
    assert first == second == third and len(first) == 2


def test_5xx_backs_off_exponentially_and_is_capped(spool):
    append(spool, record())
    backend = Backend((503, None))
    fwd = make(backend)
    times = []

    async def go():
        t = 0.0
        while t < 2000:
            n = len(backend.bodies)
            delay = await fwd.tick(t)
            if len(backend.bodies) > n:
                times.append(t)
            t += max(delay, 0.01)

    run(go())
    gaps = [round(b - a) for a, b in zip(times, times[1:])]
    assert gaps[:5] == [2, 4, 8, 16, 32]
    assert max(gaps) <= ge.BACKOFF_MAX_SECONDS + 1


def test_429_waits_for_retry_after(spool):
    append(spool, record())
    backend = Backend((429, "120"), (200, None))
    fwd = make(backend)
    run(drive(fwd, 0.0, 119.0))
    assert len(backend.bodies) == 1
    run(drive(fwd, 121.0, 125.0))
    assert backend.event_ids()


def test_a_post_error_never_escapes_the_loop(spool, monkeypatch):
    """The loop is fail-open: a fault inside tick is logged and the loop goes on."""
    calls = []

    async def boom(now):
        calls.append(now)
        raise RuntimeError("forwarder bug")

    class StopLoop(Exception):
        pass

    async def fake_sleep(seconds):
        if len(calls) >= 3:
            raise StopLoop

    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    monkeypatch.setattr(ge.Forwarder, "tick", lambda self, now: boom(now))
    monkeypatch.setattr(ge.asyncio, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        run(ge.run_guardrail_forwarder())
    assert len(calls) == 3


# ---- the spool's position ------------------------------------------------------------------------


def test_an_agent_server_restart_resumes_at_the_offset(spool):
    append(spool, record(), record())
    backend = Backend()
    run(drive(make(backend), 0.0, 5.0))
    assert len(backend.event_ids()) == 2
    append(spool, record())
    again = Backend()
    run(drive(make(again), 100.0, 105.0))  # a new process: state from disk
    assert len(again.event_ids()) == 1


def test_a_partial_last_line_waits_for_its_newline(spool):
    rec = record()
    line = json.dumps(rec)
    append(spool, raw=line[:40])
    backend = Backend()
    fwd = make(backend)
    run(drive(fwd, 0.0, 5.0))
    assert backend.event_ids() == []
    append(spool, raw=line[40:] + "\n")
    run(drive(fwd, 10.0, 15.0))
    assert backend.event_ids() == [rec["event_id"]]


def test_rotation_loses_nothing_a_hook_appended_during_the_rename(spool, monkeypatch):
    first = [record() for _ in range(10)]
    append(spool, *first)
    backend = Backend()
    fwd = make(backend)
    run(drive(fwd, 0.0, 5.0))
    assert len(backend.event_ids()) == 10
    monkeypatch.setattr(ge, "ROTATE_BYTES", 2048)  # the drained spool is now over the limit
    rotated_at = 5.0
    run(fwd.tick(rotated_at))  # the next tick rotates the full, drained spool
    rotated = Path(str(spool) + ".1")
    assert rotated.exists() and not spool.exists()
    after = record()
    append(spool, after)  # the next hook creates a fresh spool
    # Still inside the grace window: the forwarder waits on `.1` without spinning.
    ticks = []

    async def within_grace():
        t = rotated_at + 0.1
        while t < rotated_at + 1.5:
            delay = await fwd.tick(t)
            ticks.append(delay)
            t += max(delay, 0.01)
    run(within_grace())
    assert min(ticks) >= ge.POLL_SECONDS, "no busy loop while waiting out the grace"
    late = record()
    append(rotated, late)  # a hook that opened the path just before the rename
    run(drive(fwd, rotated_at + 1.5, rotated_at + 20.0))
    ids = backend.event_ids()
    assert late["event_id"] in ids and after["event_id"] in ids
    assert len(ids) == len(set(ids)) == 12


def test_a_cut_short_line_at_the_end_of_the_rotated_spool_never_stalls_delivery(
    spool, monkeypatch
):
    """A hook write cut short (a full disk, a kill mid-write) leaves a line with
    no newline. In the live spool the next append completes it into one line;
    in `.1` nothing ever will, so the forwarder waited there for good while its
    keepalives went on saying all was well. Past the grace it is counted and
    the forwarder moves on to the live spool."""
    first = [record() for _ in range(15)]
    append(spool, *first, raw=json.dumps(record())[:50])
    backend = Backend()
    fwd = make(backend)
    run(drive(fwd, 0.0, 5.0))
    monkeypatch.setattr(ge, "ROTATE_BYTES", 2048)
    run(fwd.tick(5.0))  # the spool, cut-short tail and all, becomes `.1`
    assert Path(str(spool) + ".1").exists() and not spool.exists()
    later = [record() for _ in range(3)]
    append(spool, *later)
    run(drive(fwd, 5.1, 30.0))
    ids = backend.event_ids()
    assert all(r["event_id"] in ids for r in first + later)
    assert sum(b["dropped"] for b in backend.bodies) == 1


def test_a_second_rotation_waits_out_the_first_ones_grace(spool, monkeypatch):
    """A flood that fills the fresh spool inside the grace window must not
    replace `.1` while a hook may still be appending to it: that write would land
    in an unlinked file, neither delivered nor counted."""
    append(spool, *[record() for _ in range(25)])
    backend = Backend()
    fwd = make(backend)
    run(drive(fwd, 0.0, 5.0))
    monkeypatch.setattr(ge, "ROTATE_BYTES", 2048)
    with open(spool, "a") as held:  # a hook that opened the path just before the rename
        run(fwd.tick(5.0))  # rotation 1
        append(spool, *[record() for _ in range(25)])  # the new spool is full at once
        run(fwd.tick(5.6))  # still inside rotation 1's grace
        held.write(json.dumps(record()) + "\n")
    run(drive(fwd, 6.0, 60.0))
    delivered = backend.event_ids()
    dropped = sum(b["dropped"] for b in backend.bodies)
    assert len(delivered) == len(set(delivered))
    assert len(delivered) + dropped == 51, "every record is delivered or counted"


def test_a_spool_the_platform_never_drains_is_bounded_and_the_loss_counted(
    spool, monkeypatch
):
    monkeypatch.setattr(ge, "ROTATE_BYTES", 4096)
    backend = Backend((503, None))
    fwd = make(backend)
    total = 0
    t = 0.0
    for _ in range(6):
        append(spool, *[record() for _ in range(15)])
        total += 15
        t = run(drive(fwd, t, t + 30.0))
    files = [p for p in spool.parent.iterdir() if p.name.startswith("guardrails.jsonl")]
    assert sum(p.stat().st_size for p in files) <= 3 * 4096
    first_ok = len(backend.bodies)
    backend.answers = [(200, None)]
    run(drive(fwd, t + 400.0, t + 500.0))
    accepted = backend.bodies[first_ok:]
    delivered = [e["event_id"] for b in accepted for e in b["events"]]
    dropped = sum(b["dropped"] for b in accepted)
    assert dropped > 0, "the oldest records went, and the platform is told how many"
    assert len(delivered) == len(set(delivered))
    assert len(delivered) + dropped == total


# ---- wiring ------------------------------------------------------------------------------------------


class _FakeApp:
    def __init__(self):
        self.startup = []
        self.shutdown = []

    def on_event(self, name):
        def deco(fn):
            (self.startup if name == "startup" else self.shutdown).append(fn)
            return fn

        return deco


def test_no_key_means_no_forwarder(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    app = _FakeApp()
    ge.schedule_guardrail_forwarder(app)
    assert app.startup == []


def test_no_backend_url_means_no_forwarder(monkeypatch):
    monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    app = _FakeApp()
    ge.schedule_guardrail_forwarder(app)
    assert app.startup == []


def test_the_forwarder_starts_with_the_app_when_the_key_is_present(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    started = []

    async def fake_loop():
        started.append(True)

    monkeypatch.setattr(ge, "run_guardrail_forwarder", fake_loop)
    app = _FakeApp()
    ge.schedule_guardrail_forwarder(app)
    assert len(app.startup) == 1 and len(app.shutdown) == 1

    async def lifespan():
        await app.startup[0]()
        await asyncio.sleep(0)
        await app.shutdown[0]()

    run(lifespan())
    assert started == [True]


def test_the_agent_server_wires_the_forwarder():
    tree = ast.parse((_BASE_IMAGE / "agent_server" / "main.py").read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "schedule_guardrail_forwarder"
    ]
    assert len(calls) == 1 and [a.id for a in calls[0].args] == ["app"]


def test_the_post_uses_the_agents_own_key_and_its_own_route(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, headers=headers, body=json)
            raise asyncio.CancelledError

    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000/")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_own_key")
    monkeypatch.setattr(ge.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ge, "_agent_name", lambda: "agent a")

    sleeps = []

    async def no_sleep(_):
        sleeps.append(_)
        assert len(sleeps) < 50, "the forwarder never posted"

    monkeypatch.setattr(ge.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(ge.run_guardrail_forwarder())
    assert seen["url"] == "http://backend:8000/api/agents/agent%20a/guardrail-events"
    assert seen["headers"] == {"Authorization": "Bearer trinity_mcp_own_key"}
    assert seen["body"]["events"] == []


def test_a_reachable_agent_never_reads_stale():
    """The platform marks an agent `stale` after 15 minutes of silence
    (ent#165 S1, `STALE_AFTER_SECONDS`). A keepalive plus the longest 5xx
    backoff must stay inside that, or a healthy agent behind a flaky backend
    would read as not reporting."""
    platform_stale_after = 15 * 60
    assert ge.KEEPALIVE_SECONDS + ge.BACKOFF_MAX_SECONDS < platform_stale_after
