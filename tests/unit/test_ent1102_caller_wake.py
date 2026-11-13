"""ent#1102 — the caller is woken when a run it delegated ends.

The retro this closes measured the cost of the opposite: an agent that cannot
be told "your delegated run ended" polls for it instead. So the wake is a
fourth sibling in the one terminal fan-out every CAS-won writer already calls,
and these tests pin the properties that keep a spend-causing auto-dispatch
safe:

* only the row's principal-derived ``source_agent_name`` is woken — never a
  name from a header, a body or an event payload (P10);
* a platform turn's end is not a delegation's end: the four reserved triggers
  (including the wake's own) never wake anyone, so no wake chain can be
  self-sustaining;
* the wake carries the delegator's standing, ``max(callee − 1, MAX(running
  caller), 0)`` — not one hop more (#4357; it was ``callee + 1``, which added
  two per delegate→wake round). The rounds themselves are driven through the
  real guard in ``test_4357_wake_chain_depth.py``;
* one wake per caller per 15 s bucket, so a cleanup sweep that terminates 200
  stale rows cannot become 200 billed turns;
* a stopped caller is audited, not dispatched at.

Everything runs through the real ``maybe_wake_caller`` with the dispatch and
the policy stubbed at their own module seams, so the branch order, the frame
and the kwargs handed to the dispatch are all executed.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _row(**kw):
    """A terminal execution row, as ``db.get_execution`` returns one."""
    base = dict(
        id="exec-callee",
        agent_name="callee",
        status="completed",
        triggered_by="chat",
        source_agent_name="caller",
        fan_out_id=None,
        chain_depth=2,
        response="the answer",
        error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def wake(monkeypatch):
    """``caller_wake_service`` with every outbound edge stubbed and recorded."""
    pytest.importorskip("database")
    from services import caller_wake_service as mod

    calls = {"dispatch": [], "audit": [], "idem": []}

    async def _dispatch(**kwargs):
        calls["dispatch"].append(kwargs)
        return SimpleNamespace(execution_id="exec-wake", status="completed", error=None)

    async def _state(agent_name):
        return calls.get("container_state", "running")

    monkeypatch.setattr(mod, "_dispatch_wake", _dispatch)
    monkeypatch.setattr(mod, "_container_state", _state)
    async def _audit(**kw):
        calls["audit"].append(kw)

    monkeypatch.setattr(mod, "_audit", _audit)
    monkeypatch.setattr(
        mod, "_begin_idempotency",
        lambda scope, key: calls["idem"].append((scope, key)) or SimpleNamespace(
            replay=calls.get("replay", False), enabled=True, in_flight=False
        ),
    )
    monkeypatch.setattr(mod, "_fail_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_pending_ask_ids", lambda execution_id: calls.get("ask_ids", []))
    monkeypatch.setattr(mod, "_caller_subscribes_to", lambda caller, callee, status: False)
    monkeypatch.setattr(mod, "_running_depth", lambda agent_name: calls.get("running_depth", 0))

    def _policy(agent_name):
        from services.wake_policy import WakeVerdict
        return calls.get("verdict") or WakeVerdict(enabled=True, source="instance")

    monkeypatch.setattr(mod, "_resolve_policy", _policy)
    monkeypatch.setattr(mod, "_read_row", lambda execution_id: calls.get("row", _row()))

    mod._calls = calls  # noqa: SLF001 — test handle
    return mod


def _run(coro):
    return asyncio.run(coro)


class TestWhoGetsWoken:
    def test_the_caller_is_woken_once(self, wake):
        assert _run(wake.maybe_wake_caller("exec-callee")) == "exec-wake"
        assert len(wake._calls["dispatch"]) == 1
        sent = wake._calls["dispatch"][0]
        assert sent["agent_name"] == "caller"
        assert sent["triggered_by"] == wake.TRIGGERED_BY_CALLER_WAKE

    def test_a_row_with_no_caller_wakes_nobody(self, wake):
        """A human-triggered run has no delegating agent to tell."""
        wake._calls["row"] = _row(source_agent_name=None)
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_a_self_task_does_not_wake_itself(self, wake):
        wake._calls["row"] = _row(agent_name="caller")
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_a_fan_out_leg_wakes_nobody(self, wake):
        """The #2524 join is the batch's own reaction; a leg must not double it."""
        wake._calls["row"] = _row(fan_out_id="batch-1")
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    @pytest.mark.parametrize(
        "trigger", ["caller_wake", "event", "operator_response", "operator_ending"]
    )
    def test_a_platform_turns_end_is_not_a_delegations_end(self, wake, trigger):
        """Including the wake's own trigger: no wake is ever woken for."""
        wake._calls["row"] = _row(triggered_by=trigger)
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_an_event_loopback_row_cannot_bill_a_third_agent(self, wake):
        """Premise P10. An ent#614 loopback-vouched row carries a vouched
        ``source_agent_name``; were it honoured here, a subscriber could make
        the platform spend a turn on an agent it merely named. It cannot: an
        ``event``-triggered row is in the break set before the name is read at
        all, so the vouching path has no reach into the wake."""
        wake._calls["row"] = _row(triggered_by="event", source_agent_name="innocent-third")
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_an_unreadable_row_fails_open(self, wake, monkeypatch):
        monkeypatch.setattr(wake, "_read_row", lambda execution_id: None)
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []


class TestPolicy:
    def test_policy_off_spends_nothing(self, wake):
        from services.wake_policy import WakeVerdict

        wake._calls["verdict"] = WakeVerdict(enabled=False, source="agent")
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_an_existing_subscriber_gets_exactly_one_turn(self, wake, monkeypatch):
        """AC 6: a caller already subscribed to the callee's ``agent.task.*``
        is dispatched at by the emit, so the wake stands down."""
        monkeypatch.setattr(wake, "_caller_subscribes_to", lambda c, e, s: True)
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []


class TestSafety:
    def test_a_stopped_caller_is_audited_not_dispatched_at(self, wake):
        """Nothing auto-starts an agent, so a wake for a stopped one would be a
        FAILED execution and nothing else. The ending stays readable."""
        wake._calls["container_state"] = "stopped"
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []
        assert [a["status"] for a in wake._calls["audit"]] == ["skipped_not_running"]

    def test_docker_unreadable_still_attempts_the_wake(self, wake):
        """#2196 tri-state: "could not ask Docker" is not "not running"."""
        wake._calls["container_state"] = None
        assert _run(wake.maybe_wake_caller("exec-callee")) == "exec-wake"

    def test_a_replayed_claim_produces_no_second_wake(self, wake):
        wake._calls["replay"] = True
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert wake._calls["dispatch"] == []

    def test_one_wake_per_caller_per_bucket(self, wake):
        """The storm bound: the key is the CALLER and a 15 s bucket, not the
        callee's id — so 200 rows swept at once claim one key, not 200."""
        _run(wake.maybe_wake_caller("exec-callee"))
        scope, key = wake._calls["idem"][0]
        assert "caller" in scope
        assert key.startswith("caller_wake:caller:")
        bucket = key.rsplit(":", 1)[-1]
        assert bucket.isdigit()
        # The callee's id is deliberately NOT in the key: keying on it is what
        # would turn a 200-row sweep into 200 turns.
        assert "exec-callee" not in key

    def test_the_bucket_is_stable_within_a_window_and_moves_between_them(self, wake):
        width = wake.WAKE_BUCKET_SECONDS
        start = 1_000_000 - (1_000_000 % width)   # a real window boundary
        # Three probes, not two at the one point where both bounds coincide:
        # the start, a mid-window instant, and the last instant still inside.
        assert wake._bucket_for(start) == wake._bucket_for(start + width / 2)
        assert wake._bucket_for(start) == wake._bucket_for(start + width - 0.001)
        assert wake._bucket_for(start) != wake._bucket_for(start + width)
        assert wake._bucket_for(start) != wake._bucket_for(start - 0.001)

    def test_a_dispatch_that_raises_is_released_and_audited(self, wake, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("capacity exploded")

        monkeypatch.setattr(wake, "_dispatch_wake", _boom)
        assert _run(wake.maybe_wake_caller("exec-callee")) is None
        assert [a["status"] for a in wake._calls["audit"]] == ["dispatch_error"]


class TestTheFrame:
    def test_chain_depth_is_the_delegators_standing(self, wake):
        """#4357: the callee was stamped ``1 + MAX(running caller)``, so the
        delegator stood at ``callee − 1`` — the wake continues from there."""
        _run(wake.maybe_wake_caller("exec-callee"))
        assert wake._calls["dispatch"][0]["chain_depth"] == 1

    def test_a_missing_chain_depth_is_a_root(self, wake):
        wake._calls["row"] = _row(chain_depth=None)
        _run(wake.maybe_wake_caller("exec-callee"))
        assert wake._calls["dispatch"][0]["chain_depth"] == 0

    def test_a_callee_at_the_max_leaves_the_wake_room_for_one_hop(self, wake):
        wake._calls["row"] = _row(chain_depth=8)
        _run(wake.maybe_wake_caller("exec-callee"))
        assert wake._calls["dispatch"][0]["chain_depth"] == 7

    def test_a_deeper_running_turn_of_the_caller_lifts_the_wake(self, wake):
        """A collapsed window can read the shallower ending; the wake must not
        stand below a deep turn of the caller that is still running."""
        wake._calls["running_depth"] = 5
        _run(wake.maybe_wake_caller("exec-callee"))
        assert wake._calls["dispatch"][0]["chain_depth"] == 5

    def test_an_unreadable_running_depth_still_wakes_at_the_callees_standing(self, wake, monkeypatch):
        def _boom(agent_name):
            raise RuntimeError("db gone")

        monkeypatch.setattr(wake, "_running_depth", _boom)
        wake._calls["row"] = _row(chain_depth=4)
        assert _run(wake.maybe_wake_caller("exec-callee")) == "exec-wake"
        assert wake._calls["dispatch"][0]["chain_depth"] == 3

    def test_a_non_integer_chain_depth_falls_back_to_the_running_depth(self, wake):
        wake._calls["row"] = _row(chain_depth="garbage")
        wake._calls["running_depth"] = 2
        _run(wake.maybe_wake_caller("exec-callee"))
        assert wake._calls["dispatch"][0]["chain_depth"] == 2

    def test_the_audit_names_the_callees_depth_beside_the_wakes(self, wake):
        wake._calls["row"] = _row(chain_depth=6)
        _run(wake.maybe_wake_caller("exec-callee"))
        (audit,) = wake._calls["audit"]
        assert (audit["chain_depth"], audit["callee_chain_depth"]) == (5, 6)

    def test_the_frame_names_the_run_the_callee_and_the_ending(self, wake):
        _run(wake.maybe_wake_caller("exec-callee"))
        message = wake._calls["dispatch"][0]["message"]
        assert "exec-callee" in message
        assert "callee" in message
        assert "get_execution_result" in message

    def test_parked_asks_are_named_so_the_caller_knows_it_stopped_at_one(self, wake):
        wake._calls["ask_ids"] = ["approval-abc", "question-def"]
        _run(wake.maybe_wake_caller("exec-callee"))
        message = wake._calls["dispatch"][0]["message"]
        assert "approval-abc" in message
        assert "question-def" in message

    def test_the_frame_carries_no_person_text(self, wake):
        """ent#751/ent#886: the skill gate reads person text only, and no
        person is on this prompt — the platform wrote it."""
        _run(wake.maybe_wake_caller("exec-callee"))
        sent = wake._calls["dispatch"][0]
        assert sent.get("request_text") == ""
        assert "source_user_email" not in sent

    def test_the_summary_is_sanitized_and_capped(self, wake):
        secret = "sk-ant-" + "A" * 40
        wake._calls["row"] = _row(response=secret + " x" * 4000)
        _run(wake.maybe_wake_caller("exec-callee"))
        message = wake._calls["dispatch"][0]["message"]
        assert secret not in message
        assert len(message) < 12_000

    def test_a_cut_summary_says_so_with_both_lengths(self, wake):
        """ent#1216: the notice cut the callee's words mid-sentence and did not
        say so, which read as the run's own answer stopping there."""
        words = "word " * 1000                       # 5,000 characters
        wake._calls["row"] = _row(response=words)
        _run(wake.maybe_wake_caller("exec-callee"))
        message = wake._calls["dispatch"][0]["message"]
        assert f"[cut here: the first {wake.SUMMARY_MAX:,} of 5,000 characters]" in message
        shown = message.split("Its own words on how it ended:\n\n", 1)[1]
        assert shown.startswith(words[: wake.SUMMARY_MAX])

    def test_a_summary_that_fits_carries_no_cut_marker(self, wake):
        wake._calls["row"] = _row(response="x" * wake.SUMMARY_MAX)
        _run(wake.maybe_wake_caller("exec-callee"))
        assert "[cut here" not in wake._calls["dispatch"][0]["message"]

    def test_the_cut_is_measured_after_redaction(self, wake):
        """A raw text over the cap whose redacted form fits was not cut."""
        secret = "sk-ant-" + "A" * 40
        raw = (secret + " ") * 60                    # 2,880 raw characters
        from utils.credential_sanitizer import sanitize_text
        assert len(raw) > wake.SUMMARY_MAX >= len(sanitize_text(raw))
        wake._calls["row"] = _row(response=raw)
        _run(wake.maybe_wake_caller("exec-callee"))
        message = wake._calls["dispatch"][0]["message"]
        assert secret not in message
        assert "[cut here" not in message


class TestWiring:
    """The hook must be a SIBLING in the terminal fan-out, not nested in the emit.

    Nested inside ``emit_task_terminal_event`` it would run only when an event
    subscription matches — the rare case — which is exactly the bug the fan-out
    comment in that module warns about. Deleting the call line turns this red.
    """

    def _fan_out_fn(self):
        source = (_BACKEND / "services" / "event_dispatch_service.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_terminal_side_effects":
                return node
        pytest.fail("_terminal_side_effects not found in event_dispatch_service")

    def test_the_wake_is_called_from_the_terminal_fan_out(self):
        fn = self._fan_out_fn()
        names = {
            n.func.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        } | {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "maybe_wake_caller" in names

    def test_each_sibling_is_guarded_separately(self):
        """A fault in one consumer must not skip the others — the fan-out runs
        on the path of an already-billed terminal."""
        fn = self._fan_out_fn()
        guarded = 0
        for node in fn.body:
            if isinstance(node, ast.Try):
                calls = {
                    n.func.attr
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                } | {
                    n.func.id for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                if "maybe_wake_caller" in calls:
                    guarded += 1
        assert guarded == 1, "the wake must sit in its own try, like its siblings"
