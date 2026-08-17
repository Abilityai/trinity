"""Runtime secret-scrub seam (ent#279 PR-2) — seam-direct unit coverage.

Scope: the generic OSS mechanism in ``services/runtime_secret_scrub.py`` —
staging (fail CLOSED), reading (fail OPEN), and the identity-scrub replacement
(renditions / longest-first / literal / falsy-passthrough / recursive obj).

Covered here — the store + scrub logic, in isolation from crypto and from the
chokepoints:
  * stage/read round-trip + exact dedup (one HASH field per distinct value);
  * the <8-char floor is SKIPPED, never staged, never an error;
  * per-member 24h expiry is pruned at stage time (member-scoped, not key TTL);
  * the hard cap raises ``StagingUnavailable`` for a NEW value, but an
    already-staged value at the cap does not;
  * stage side FAILS CLOSED (Redis down → raises);
  * read side FAILS OPEN (Redis down / hgetall error / a corrupt member → []
    or skip-the-member, never raises);
  * ``scrub_text`` redacts the raw / JSON-escaped / base64 renditions, is
    LONGEST-FIRST, is a LITERAL replace (no regex interpretation), and passes a
    falsy input through UNCHANGED (never None→"");
  * ``scrub_obj`` walks dict/list string leaves, leaves non-strings alone, and
    returns a NEW structure (input never mutated).

Now WIRED (added below in this same chokepoint-wiring change):
  * the terminal-applier / idempotency-snapshot chokepoint scrubs — every wired
    site, driven with mocked db + activity/capacity and the REAL scrub functions
    (incl. the escaping-evasion test and the transitive routers/sessions.py check);
  * cross-worker propagation through a real sibling Redis :6390 (the
    store-is-global property, which the single-client tests above cannot show;
    skips when :6390 is absent).

The DRIFT GUARD lives in its OWN file — ``tests/unit/test_ent279_scrub_parity.py``
— because it is a pure-AST scan with no heavy imports, and its allowlist is only
meaningful against the now-wired set.

Loaded standalone via importlib against a fake Redis + fake reversible enc
service, mirroring tests/unit/test_1085_correlated_pause.py — real AES is
already covered by test_267_credential_key_rotation.py, so the seam test isolates
its own store/expiry/cap/fail-mode/scrub logic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Fake reversible "encryption" — the seam's only contract with the enc service
# is encrypt(dict)->str / decrypt(str)->dict, and decrypt raises on garbage.
# A JSON envelope with a sentinel prefix reproduces that contract without a key
# and lets a corrupt member be injected trivially (any non-"ENV:" string).
# ---------------------------------------------------------------------------
class _FakeEnc:
    def encrypt(self, d: dict) -> str:
        return "ENV:" + json.dumps(d)

    def decrypt(self, s) -> dict:
        if not isinstance(s, str) or not s.startswith("ENV:"):
            raise ValueError("bad envelope")
        return json.loads(s[len("ENV:") :])


@pytest.fixture
def scrub(monkeypatch):
    """Load services/runtime_secret_scrub.py standalone with a fake Redis + fake
    enc service. Function-scoped, so each test re-executes the module and gets a
    pristine ``_redis_down_until`` / ``_last_error_log_ts``."""
    spec = importlib.util.spec_from_file_location(
        "_runtime_secret_scrub_under_test",
        str(_BACKEND / "services" / "runtime_secret_scrub.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(module, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(module, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(module, "_redis_down_until", 0.0, raising=False)
    return SimpleNamespace(m=module, fake=fake)


# ---------------------------------------------------------------------------
# Stage / read round-trip + dedup + floor
# ---------------------------------------------------------------------------
class TestStageRead:
    def test_round_trip(self, scrub):
        m = scrub.m
        m.stage_secret("agent-a", "sk-supersecret-value")
        assert m.get_staged_values() == ["sk-supersecret-value"]

    def test_exact_dedup_one_field_per_value(self, scrub):
        m, fake = scrub.m, scrub.fake
        for _ in range(5):
            m.stage_secret("agent-a", "repeat-me-please")
        # AES-GCM's random nonce would defeat a value-SET dedup; the HASH keyed
        # on sha256(value) collapses the repeats to one field / one value.
        assert fake.hlen(m._STAGED_HASH) == 1
        assert m.get_staged_values() == ["repeat-me-please"]

    def test_distinct_values_accumulate(self, scrub):
        m = scrub.m
        m.stage_secret("a", "value-one-xxxx")
        m.stage_secret("a", "value-two-yyyy")
        assert set(m.get_staged_values()) == {"value-one-xxxx", "value-two-yyyy"}

    def test_below_floor_is_skipped_not_staged_not_error(self, scrub):
        m, fake = scrub.m, scrub.fake
        m.stage_secret("a", "short")  # 5 < 8, no raise
        m.stage_secret("a", "")  # empty, no raise
        assert fake.hlen(m._STAGED_HASH) == 0
        assert m.get_staged_values() == []

    def test_empty_store_read_is_empty_fast_path(self, scrub):
        assert scrub.m.get_staged_values() == []


# ---------------------------------------------------------------------------
# Per-member expiry + hard cap
# ---------------------------------------------------------------------------
class TestExpiryAndCap:
    def test_expired_member_pruned_at_next_stage(self, scrub):
        m, fake = scrub.m, scrub.fake
        m.stage_secret("a", "old-secret-aaaa")
        field_old = m._field("old-secret-aaaa")
        # Rewrite the old member's stage-time to before the TTL window.
        fake.zadd(m._STAGED_ZSET, {field_old: time.time() - m._TTL_SECONDS - 10})
        m.stage_secret("a", "new-secret-bbbb")  # prunes the old one first
        assert m.get_staged_values() == ["new-secret-bbbb"]
        assert fake.hexists(m._STAGED_HASH, field_old) is False
        assert fake.zscore(m._STAGED_ZSET, field_old) is None

    def test_new_value_at_hard_cap_fails_closed(self, scrub, monkeypatch):
        m = scrub.m
        monkeypatch.setattr(m, "_HARD_CAP_FIELDS", 2)
        monkeypatch.setattr(m, "_SOFT_WARN_FIELDS", 1)
        m.stage_secret("a", "cap-value-0000")
        m.stage_secret("a", "cap-value-1111")
        with pytest.raises(m.StagingUnavailable):
            m.stage_secret("a", "cap-value-2222")  # a NEW value over the cap

    def test_already_staged_value_at_cap_does_not_raise(self, scrub, monkeypatch):
        m = scrub.m
        monkeypatch.setattr(m, "_HARD_CAP_FIELDS", 2)
        m.stage_secret("a", "cap-value-0000")
        m.stage_secret("a", "cap-value-1111")
        # Re-staging an EXISTING value at the cap is a no-growth no-op, allowed.
        m.stage_secret("a", "cap-value-0000")
        assert set(m.get_staged_values()) == {"cap-value-0000", "cap-value-1111"}


# ---------------------------------------------------------------------------
# Failure asymmetry: stage fails CLOSED, read fails OPEN
# ---------------------------------------------------------------------------
class TestFailureAsymmetry:
    def test_stage_fails_closed_when_redis_down(self, scrub, monkeypatch):
        m = scrub.m
        monkeypatch.setattr(m, "get_breaker_redis", lambda: None)
        with pytest.raises(m.StagingUnavailable):
            m.stage_secret("a", "cannot-stage-this")

    def test_stage_fails_closed_when_store_op_raises(self, scrub, monkeypatch):
        m = scrub.m
        boom = MagicMock()
        boom.zrangebyscore.side_effect = RuntimeError("redis boom")
        monkeypatch.setattr(m, "get_breaker_redis", lambda: boom)
        with pytest.raises(m.StagingUnavailable):
            m.stage_secret("a", "cannot-stage-this")

    def test_read_fails_open_when_redis_down(self, scrub, monkeypatch):
        m = scrub.m
        monkeypatch.setattr(m, "get_breaker_redis", lambda: None)
        assert m.get_staged_values() == []  # no raise

    def test_read_fails_open_when_hgetall_raises(self, scrub, monkeypatch):
        m = scrub.m
        boom = MagicMock()
        boom.hgetall.side_effect = RuntimeError("redis boom")
        monkeypatch.setattr(m, "get_breaker_redis", lambda: boom)
        assert m.get_staged_values() == []  # throttled ERROR, empty set

    def test_corrupt_member_is_skipped_others_survive(self, scrub):
        m, fake = scrub.m, scrub.fake
        m.stage_secret("a", "good-secret-aaaa")
        # Inject a member whose envelope will not decrypt.
        fake.hset(m._STAGED_HASH, "deadbeef" * 8, "NOT-AN-ENVELOPE")
        assert m.get_staged_values() == ["good-secret-aaaa"]  # good one still scrubs

    def test_redis_down_is_memoized(self, scrub, monkeypatch):
        m = scrub.m
        calls = {"n": 0}

        def _down():
            calls["n"] += 1
            return None

        monkeypatch.setattr(m, "get_breaker_redis", _down)
        assert m.get_staged_values() == []
        assert m.get_staged_values() == []
        # Second call inside the memo window short-circuits before the accessor.
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# scrub_text: renditions, longest-first, literal, falsy passthrough
# ---------------------------------------------------------------------------
class TestScrubText:
    MARK = "***REDACTED***"

    def test_marker_is_the_sanitizer_placeholder(self, scrub):
        from utils.credential_sanitizer import REDACTION_PLACEHOLDER

        assert scrub.m.REDACTION_PLACEHOLDER == REDACTION_PLACEHOLDER == self.MARK

    def test_raw_rendition_redacted(self, scrub):
        out = scrub.m.scrub_text(["hunter2-token"], "the value is hunter2-token here")
        assert "hunter2-token" not in out
        assert self.MARK in out

    def test_json_escaped_rendition_redacted(self, scrub):
        # An agent value embedded inside an already-JSON-dumped transcript appears
        # only in its escaped form; the raw rendition would miss it.
        value = 'p@ss"w\\orld✓'
        embedded = json.dumps({"log": f"token is {value}"})
        assert value not in embedded  # proves it is only present escaped
        out = scrub.m.scrub_text([value], embedded)
        assert self.MARK in out
        # The escaped form must be gone.
        assert json.dumps(value)[1:-1] not in out

    def test_base64_rendition_redacted(self, scrub):
        import base64

        value = "raw-secret-value"
        b64 = base64.b64encode(value.encode()).decode()
        out = scrub.m.scrub_text([value], f"encoded: {b64}")
        assert b64 not in out
        assert self.MARK in out

    def test_longest_first_no_partial_shred(self, scrub):
        # Both staged; the shorter is a substring of the longer. Longest-first
        # means the longer is fully redacted, not "<mark>-token-longer".
        out = scrub.m.scrub_text(
            ["secret", "secret-token-longer"], "here is secret-token-longer!"
        )
        assert self.MARK in out
        assert "-token-longer" not in out
        assert "secret" not in out

    def test_replace_is_literal_not_regex(self, scrub):
        value = "a.*b+c?"  # regex metachars
        # The literal appears → redacted.
        assert self.MARK in scrub.m.scrub_text([value], f"x {value} y")
        # A string a regex 'a.*b+c?' WOULD match is left untouched → proves
        # str.replace, not re.sub.
        assert scrub.m.scrub_text([value], "axxxbbc") == "axxxbbc"

    def test_falsy_passthrough_preserves_none_and_empty(self, scrub):
        vals = ["some-secret-val"]
        assert scrub.m.scrub_text(vals, None) is None  # never None -> ""
        assert scrub.m.scrub_text(vals, "") == ""

    def test_no_values_returns_text_unchanged(self, scrub):
        assert (
            scrub.m.scrub_text([], "keep secret-token here") == "keep secret-token here"
        )


# ---------------------------------------------------------------------------
# scrub_obj: recursive, new structure, non-strings pass through
# ---------------------------------------------------------------------------
class TestScrubObj:
    MARK = "***REDACTED***"

    def test_nested_string_leaves_scrubbed_non_strings_intact(self, scrub):
        v = "topsecret-value"
        obj = {"a": v, "b": [v, 123, None, True], "c": {"d": v}, "n": 5}
        out = scrub.m.scrub_obj([v], obj)
        assert out["a"] == self.MARK
        assert out["b"][0] == self.MARK
        assert out["b"][1:] == [123, None, True]  # non-strings untouched
        assert out["c"]["d"] == self.MARK
        assert out["n"] == 5

    def test_returns_new_structure_input_not_mutated(self, scrub):
        v = "topsecret-value"
        obj = {"a": v, "b": [v]}
        out = scrub.m.scrub_obj([v], obj)
        assert obj == {"a": v, "b": [v]}  # original untouched
        assert out is not obj

    def test_no_values_returns_input_unchanged(self, scrub):
        obj = {"a": "secret-token"}
        assert scrub.m.scrub_obj([], obj) is obj


# ---------------------------------------------------------------------------
# clear_staged test hook
# ---------------------------------------------------------------------------
def test_clear_staged_wipes_both_keys(scrub):
    m, fake = scrub.m, scrub.fake
    m.stage_secret("a", "wipe-me-please")
    assert fake.hlen(m._STAGED_HASH) == 1
    m.clear_staged()
    assert fake.hlen(m._STAGED_HASH) == 0
    assert fake.zcard(m._STAGED_ZSET) == 0
# ===========================================================================
# Chokepoint scrubs (ent#279 PR-2) — the wired-set behaviour, end to end.
#
# These exercise the REAL scrub functions (services.runtime_secret_scrub's
# scrub_text/scrub_obj) but patch each chokepoint module's `get_staged_values`
# to a fixed staged set, so no Redis/crypto is involved — the applier is driven
# with mocked db/activity/capacity and we assert what reaches each durable sink.
#
# Heavy modules are imported lazily INSIDE the tests (mirrors
# test_1083_apply_result.py) and patched IN PLACE (never reimported), so the
# importlib-standalone `scrub` fixture above and these coexist without polluting
# `services.runtime_secret_scrub`.
# ===========================================================================

MARK = "***REDACTED***"

# A staged secret with a JSON quote, a backslash, and a non-ASCII glyph — the
# escaping-evasion payload. >= 8 chars so the stage-side floor never skips it.
EVIL = 'p@ss"w\\orld✓-vault-9f3a'
# A plain prefixless secret the pattern-based sanitizer would MISS (no sk-/ghp-).
PLAIN = "customer-db-Passw0rd-2f8e"


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _dumps(obj):
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# apply_result — the single terminal applier (success + failure branches)
# ---------------------------------------------------------------------------
class TestApplyResultChokepoint:
    def _run(self, envelope, staged):
        from services.task_execution_service import TaskExecutionService
        from unittest.mock import AsyncMock, MagicMock, patch

        mdb = MagicMock()
        mdb.update_execution_status.return_value = True
        mdb.get_execution.return_value = MagicMock(status="cancelled")

        with (
            patch("services.task_execution_service.db", mdb),
            patch(
                "services.task_execution_service.get_capacity_manager",
                return_value=MagicMock(release=AsyncMock()),
            ),
            patch(
                "services.task_execution_service.activity_service",
                MagicMock(complete_activity=AsyncMock()),
            ),
            patch(
                "services.task_execution_service._record_dispatch_terminal",
                AsyncMock(),
            ),
            patch(
                "services.task_execution_service.event_dispatch_service", MagicMock()
            ),
            patch(
                "services.task_execution_service.channel_completion_report", MagicMock()
            ),
            patch("services.task_execution_service._spawn_bg", lambda *_a, **_k: None),
            patch(
                "services.task_execution_service._maybe_discard_exhausted_ephemeral",
                MagicMock(return_value=None),
            ),
            patch("services.task_execution_service.get_staged_values", lambda: staged),
        ):
            svc = TaskExecutionService()
            result = _await(
                svc.apply_result(
                    "agent-x", envelope, activity_id="a", release_slot=False
                )
            )
        return result, mdb

    def _success_envelope(self, **over):
        from services.task_execution_service import (
            TerminalEnvelope,
            TaskExecutionStatus,
        )

        base = dict(
            execution_id="exec-279",
            status=TaskExecutionStatus.SUCCESS,
            response=f"connected using {PLAIN}",
            execution_log=[
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_result", "content": f"psql {PLAIN} ok"},
            ],
            metadata={"cost_usd": 0.01, "context_window": 200000},
            session_id="11111111-1111-1111-1111-111111111111",
            execution_time_ms=10,
            raw_response={
                "response": f"connected using {PLAIN}",
                "execution_log_simplified": f"ran psql {PLAIN}",
                "cost": 0.01,
            },
        )
        base.update(over)
        return TerminalEnvelope(**base)

    def test_success_scrubs_response_execution_log_tool_calls_and_raw_response(self):
        result, mdb = self._run(self._success_envelope(), [PLAIN])
        kw = mdb.update_execution_status.call_args.kwargs
        # The three agent-free-text columns on schedule_executions.
        assert PLAIN not in (kw["response"] or "")
        assert PLAIN not in (kw["execution_log"] or "")
        assert PLAIN not in (kw["tool_calls"] or "")
        assert MARK in kw["response"]
        # The returned result — what routers/sessions.py & the /task snapshot read.
        assert PLAIN not in (result.response or "")
        assert PLAIN not in (result.execution_log or "")
        assert PLAIN not in _dumps(result.raw_response)
        assert MARK in _dumps(result.raw_response)

    def test_failure_scrubs_error_column_and_returned_error(self):
        from services.task_execution_service import (
            TerminalEnvelope,
            TaskExecutionStatus,
        )

        env = TerminalEnvelope(
            execution_id="exec-279",
            status=TaskExecutionStatus.FAILED,
            error=f"agent aborted after leaking {PLAIN}",
            error_code=None,
            metadata={"cost_usd": 0.0},
        )
        result, mdb = self._run(env, [PLAIN])
        assert PLAIN not in mdb.update_execution_status.call_args.kwargs["error"]
        assert PLAIN not in (result.error or "")
        assert MARK in result.error

    def test_no_staged_values_is_a_noop(self):
        # Behaviour-neutral for OSS: nothing staged -> the secret text is untouched.
        result, mdb = self._run(self._success_envelope(), [])
        assert result.response == f"connected using {PLAIN}"
        assert PLAIN in mdb.update_execution_status.call_args.kwargs["response"]

    def test_escaping_evasion_json_escaped_value_is_redacted(self):
        # THE feature's headline test: a quote/backslash/non-ASCII secret in a
        # tool result appears in the persisted execution_log ONLY json-escaped.
        # Because scrub_obj redacts the raw list element BEFORE json.dumps, the
        # persisted JSON must contain neither the raw nor the escaped rendition.
        env = self._success_envelope(
            response=f"done {EVIL}",
            execution_log=[{"type": "tool_result", "content": f"secret is {EVIL}"}],
            raw_response={
                "response": f"done {EVIL}",
                "execution_log_simplified": f"secret is {EVIL}",
            },
        )
        result, mdb = self._run(env, [EVIL])
        kw = mdb.update_execution_status.call_args.kwargs
        escaped = json.dumps(EVIL)[1:-1]
        for blob in (
            kw["response"],
            kw["execution_log"],
            kw["tool_calls"],
            _dumps(result.raw_response),
        ):
            assert EVIL not in (blob or "")
            assert escaped not in (blob or "")

    def test_task_sync_idempotency_snapshot_scrubbed_transitively(self):
        # The /task sync path persists `result.raw_response` into
        # idempotency_keys.response_snapshot (chat_execution_service :1445/:1510),
        # replayed to duplicate-key callers for 24h. It is scrubbed transitively
        # because apply_result scrubs envelope.raw_response — assert exactly the
        # value that becomes that snapshot carries no secret.
        result, _ = self._run(self._success_envelope(), [PLAIN])
        snapshot = dict(result.raw_response)  # what _dispatch_sync_immediate passes
        snapshot["task_execution_id"] = "exec-279"
        assert PLAIN not in _dumps(snapshot)


# ---------------------------------------------------------------------------
# _write_terminal_and_gate — the timeout/capacity/breaker/shutdown terminal
# ---------------------------------------------------------------------------
class TestWriteTerminalAndGate:
    def test_scrubs_error_before_write(self):
        from services.task_execution_service import (
            _write_terminal_and_gate,
            TaskExecutionStatus,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        mdb = MagicMock()
        mdb.update_execution_status.return_value = True
        with (
            patch("services.task_execution_service.db", mdb),
            patch(
                "services.task_execution_service.activity_service",
                MagicMock(close_execution_activity=AsyncMock()),
            ),
            patch(
                "services.task_execution_service.event_dispatch_service", MagicMock()
            ),
            patch(
                "services.task_execution_service.channel_completion_report", MagicMock()
            ),
            patch("services.task_execution_service.get_staged_values", lambda: [PLAIN]),
        ):
            _await(
                _write_terminal_and_gate(
                    "exec-279",
                    "act-1",
                    status=TaskExecutionStatus.FAILED,
                    error=f"unexpected {PLAIN}",
                    agent_name="agent-x",
                )
            )
        assert PLAIN not in mdb.update_execution_status.call_args.kwargs["error"]


# ---------------------------------------------------------------------------
# chat_execution_service — the three chat terminals + the inline idempotency
# snapshot (#425)
# ---------------------------------------------------------------------------
class TestChatExecutionChokepoints:
    def test_finalize_chat_success_scrubs_writes_and_snapshot(self):
        from services import chat_execution_service as ces
        from unittest.mock import AsyncMock, MagicMock, patch
        from datetime import datetime

        response_data = {
            "response": f"here you go {PLAIN}",
            "execution_log": [{"type": "tool_result", "content": f"db {PLAIN}"}],
            "execution_log_simplified": [
                {"type": "tool", "tool": "Bash", "input": f"psql {PLAIN}"}
            ],
            "metadata": {"cost_usd": 0.02, "output_tokens": 5},
            "session": {"context_tokens": 100, "context_window": 200000},
            "session_id": "22222222-2222-2222-2222-222222222222",
        }
        resp = MagicMock()
        resp.json.return_value = response_data

        mdb = MagicMock()
        mdb.add_chat_message.return_value = MagicMock(id="msg-1")
        captured = {}

        def _complete(idem, exec_id, snapshot):
            captured["snapshot"] = snapshot

        with (
            patch.object(ces, "db", mdb),
            patch.object(ces, "get_staged_values", lambda: [PLAIN]),
            patch.object(
                ces, "activity_service", MagicMock(complete_activity=AsyncMock())
            ),
            patch.object(ces.idempotency_service, "complete", side_effect=_complete),
        ):
            _await(
                ces._finalize_chat_success(
                    name="agent-x",
                    response=resp,
                    start_time=datetime.utcnow(),
                    session=MagicMock(id="sess-1"),
                    current_user=MagicMock(id=1, email="u@x.io", username="u"),
                    chat_activity_id="ca",
                    collaboration_activity_id=None,
                    task_execution_id="exec-279",
                    _chat_subscription_id=None,
                    execution=MagicMock(id="q-1"),
                    queue_result="done",
                    is_queued=False,
                    idem="idem-1",
                )
            )
        # assistant message content scrubbed
        assert PLAIN not in mdb.add_chat_message.call_args.kwargs["content"]
        # terminal row response/execution_log/tool_calls scrubbed
        kw = mdb.update_execution_status.call_args.kwargs
        assert PLAIN not in (kw["response"] or "")
        assert PLAIN not in (kw["execution_log"] or "")
        assert PLAIN not in (kw["tool_calls"] or "")
        # idempotency snapshot #1 (the inline /chat replay blob) scrubbed
        assert PLAIN not in _dumps(captured["snapshot"])
        assert MARK in _dumps(captured["snapshot"])

    def test_finalize_budget_exhausted_scrubs_error(self):
        from services import chat_execution_service as ces
        from services.chat_signals import ChatDispatchError
        from unittest.mock import AsyncMock, MagicMock, patch

        mdb = MagicMock()
        mdb.get_execution.return_value = None
        with (
            patch.object(ces, "db", mdb),
            patch.object(ces, "get_staged_values", lambda: [PLAIN]),
            patch.object(
                ces, "activity_service", MagicMock(complete_activity=AsyncMock())
            ),
            pytest.raises(ChatDispatchError) as exc,
        ):
            _await(
                ces._finalize_budget_exhausted(
                    budget_exc=Exception(f"budget blown near {PLAIN}"),
                    task_execution_id="exec-279",
                    chat_activity_id="ca",
                    collaboration_activity_id=None,
                )
            )
        assert PLAIN not in mdb.update_execution_status.call_args.kwargs["error"]
        assert PLAIN not in str(exc.value.detail)

    def test_parse_agent_http_error_scrubs_before_log_and_persist(self):
        from services import chat_execution_service as ces
        from unittest.mock import MagicMock, patch

        e = MagicMock()
        e.response = MagicMock()
        e.response.status_code = 503
        e.response.json.return_value = {"detail": {"message": f"auth failed: {PLAIN}"}}
        with patch.object(ces, "get_staged_values", lambda: [PLAIN]):
            error_msg, status_code, _meta = ces._parse_agent_http_error(e, "agent-x")
        assert PLAIN not in error_msg
        assert MARK in error_msg
        assert status_code == 503


# ---------------------------------------------------------------------------
# pull_coordination_service.apply_task_result — the (dark) pull sink, sync
# ---------------------------------------------------------------------------
class TestPullSinkChokepoint:
    def _run(self, *, status, content, execution_log, error_code=None):
        from services import pull_coordination_service as pcs
        from unittest.mock import MagicMock, patch

        mdb = MagicMock()
        mdb.get_execution.return_value = MagicMock(
            status="running", agent_name="agent-x"
        )
        mdb.update_execution_status.return_value = True
        with (
            patch.object(pcs, "db", mdb),
            patch.object(pcs, "get_staged_values", lambda: [PLAIN]),
            patch.object(pcs, "event_dispatch_service", MagicMock()),
            patch.object(pcs, "activity_service", MagicMock()),
        ):
            pcs.apply_task_result(
                "exec-279",
                "tok",
                status=status,
                content=content,
                error_code=error_code,
                execution_log=execution_log,
            )
        return mdb

    def test_success_scrubs_content_and_log(self):
        mdb = self._run(
            status="success",
            content=f"result: {PLAIN}",
            execution_log=[{"type": "tool_result", "content": f"psql {PLAIN}"}],
        )
        kw = mdb.update_execution_status.call_args.kwargs
        assert PLAIN not in (kw["response"] or "")
        assert PLAIN not in (kw["execution_log"] or "")

    def test_failure_scrubs_folded_error_text(self):
        mdb = self._run(
            status="failed",
            content=f"blew up on {PLAIN}",
            execution_log=None,
            error_code="agent_error",
        )
        kw = mdb.update_execution_status.call_args.kwargs
        assert PLAIN not in (kw["error"] or "")
        assert PLAIN not in (kw["response"] or "")


# ---------------------------------------------------------------------------
# proactive_message_service.send_message — scrub BEFORE delivery AND persist
# ---------------------------------------------------------------------------
class TestProactiveChokepoint:
    def test_send_message_scrubs_before_inner_delivery(self):
        from services import proactive_message_service as pms
        from unittest.mock import patch

        class _Guard:
            replay = False
            snapshot = None

        class _CM:
            async def __aenter__(self):
                return _Guard()

            async def __aexit__(self, *a):
                return False

        captured = {}

        async def _inner(self, agent_name, recipient, text, channel, reply):
            captured["text"] = text
            return pms.DeliveryResult(success=True, channel="web", message_id="m1")

        svc = pms.ProactiveMessageService()
        with (
            patch.object(pms, "get_staged_values", lambda: [PLAIN]),
            patch.object(
                pms.idempotency_service, "effect_guard", lambda *a, **k: _CM()
            ),
            patch.object(pms.ProactiveMessageService, "_send_message_inner", _inner),
        ):
            _await(
                svc.send_message(
                    agent_name="agent-x",
                    recipient_email="U@X.io",
                    text=f"psst the key is {PLAIN}",
                    channel="web",
                )
            )
        assert PLAIN not in captured["text"]
        assert MARK in captured["text"]


# ---------------------------------------------------------------------------
# channel_history.persist_outbound_group_message — group broadcast history
# ---------------------------------------------------------------------------
class TestChannelHistoryChokepoint:
    def test_persist_scrubs_broadcast_body(self):
        from services import channel_history as ch
        from unittest.mock import MagicMock, patch

        mdb = MagicMock()
        mdb.get_or_create_public_chat_session.return_value = MagicMock(id="sess-1")
        with (
            patch.object(ch, "db", mdb),
            patch.object(ch, "get_staged_values", lambda: [PLAIN]),
        ):
            ch.persist_outbound_group_message(
                "agent-x", "slack", "team:chan:ts", f"broadcast: {PLAIN}"
            )
        # add_public_chat_message(session_id, "assistant", text, ...) — text is 3rd positional
        args = mdb.add_public_chat_message.call_args.args
        assert PLAIN not in args[2]
        assert MARK in args[2]


# ---------------------------------------------------------------------------
# routers/voice._save_transcript — voice transcript into chat_messages
# ---------------------------------------------------------------------------
class TestVoiceTranscriptChokepoint:
    def test_save_transcript_scrubs_each_entry(self):
        from routers import voice
        from unittest.mock import MagicMock, patch

        session = MagicMock()
        session.chat_session_id = "cs"
        session.agent_name = "agent-x"
        session.user_id = 1
        session.user_email = "u@x.io"
        session.session_id = "vs"
        session.transcript = [
            MagicMock(role="assistant", text=f"the token is {PLAIN}"),
            MagicMock(role="user", text="what token"),
        ]
        mdb = MagicMock()
        with (
            patch.object(voice, "db", mdb),
            patch.object(voice, "get_staged_values", lambda: [PLAIN]),
        ):
            saved = voice._save_transcript(session)
        assert saved == 2
        contents = [c.kwargs["content"] for c in mdb.add_chat_message.call_args_list]
        assert all(PLAIN not in c for c in contents)
        assert any(MARK in c for c in contents)


# ---------------------------------------------------------------------------
# routers/sessions.py — transitively covered (NO direct wiring): the assistant
# write persists result.response/result.execution_log, which ARE the applier's
# scrubbed outputs. Prove the value it would write carries no secret.
# ---------------------------------------------------------------------------
class TestSessionsTransitiveCoverage:
    def test_session_assistant_write_reads_scrubbed_applier_output(self):
        # sessions.py:439 writes content=result.response, tool_calls=
        # result.execution_log. Both come from apply_result, which scrubs. Drive
        # apply_result and confirm those exact fields are already clean.
        from services.task_execution_service import (
            TaskExecutionService,
            TerminalEnvelope,
            TaskExecutionStatus,
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        env = TerminalEnvelope(
            execution_id="exec-279",
            status=TaskExecutionStatus.SUCCESS,
            response=f"answer {PLAIN}",
            execution_log=[{"type": "tool_result", "content": f"x {PLAIN}"}],
            metadata={"context_window": 200000},
            raw_response={"response": f"answer {PLAIN}", "metadata": {}},
        )
        mdb = MagicMock()
        mdb.update_execution_status.return_value = True
        with (
            patch("services.task_execution_service.db", mdb),
            patch(
                "services.task_execution_service.get_capacity_manager",
                return_value=MagicMock(release=AsyncMock()),
            ),
            patch(
                "services.task_execution_service.activity_service",
                MagicMock(complete_activity=AsyncMock()),
            ),
            patch(
                "services.task_execution_service._record_dispatch_terminal", AsyncMock()
            ),
            patch(
                "services.task_execution_service.event_dispatch_service", MagicMock()
            ),
            patch(
                "services.task_execution_service.channel_completion_report", MagicMock()
            ),
            patch("services.task_execution_service._spawn_bg", lambda *_a, **_k: None),
            patch(
                "services.task_execution_service._maybe_discard_exhausted_ephemeral",
                MagicMock(return_value=None),
            ),
            patch("services.task_execution_service.get_staged_values", lambda: [PLAIN]),
        ):
            result = _await(
                TaskExecutionService().apply_result("agent-x", env, activity_id="a")
            )
        assert PLAIN not in (result.response or "")  # sessions.py content=
        assert PLAIN not in (result.execution_log or "")  # sessions.py tool_calls=


# ---------------------------------------------------------------------------
# Cross-worker propagation — the store is GLOBAL: a value staged by "worker A"
# (one Redis client) is scrubbed by "worker B" (a separate client on the SAME
# sibling redis :6390). Proves the store-is-global property across processes,
# which the single-client seam tests above cannot. Skips when :6390 is absent.
# ---------------------------------------------------------------------------
def _sibling_redis():
    """A real redis client on the sibling test instance (:6390), or None."""
    try:
        import redis

        client = redis.StrictRedis(
            host="localhost",
            port=6390,
            db=0,
            decode_responses=True,
            socket_connect_timeout=1,
        )
        client.ping()
        return client
    except Exception:
        return None


def _load_seam_bound_to(redis_client, monkeypatch, tag):
    """Fresh seam module instance whose Redis + enc are bound to the given real
    client + the reversible fake enc (isolating the store-propagation property
    from real crypto). Distinct module identity per call = a distinct 'worker'.
    Registered via monkeypatch.setitem so it is auto-removed on teardown (the
    sys.modules lint requires this, not a bare assignment/del)."""
    spec = importlib.util.spec_from_file_location(
        f"_scrub_worker_{tag}",
        str(_BACKEND / "services" / "runtime_secret_scrub.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_breaker_redis", lambda: redis_client)
    monkeypatch.setattr(module, "_enc_svc", lambda: _FakeEnc())
    return module


class TestCrossWorkerPropagation:
    def test_value_staged_by_one_worker_is_scrubbed_by_another(self, monkeypatch):
        client_a = _sibling_redis()
        client_b = _sibling_redis()
        if client_a is None or client_b is None:
            pytest.skip("sibling redis :6390 unavailable")
        worker_a = _load_seam_bound_to(client_a, monkeypatch, "a")
        worker_b = _load_seam_bound_to(client_b, monkeypatch, "b")
        secret = "cross-worker-Passw0rd-7d21"
        try:
            worker_a.clear_staged()
            # Worker A stages; worker B (separate client, separate module) reads.
            worker_a.stage_secret("agent-a", secret)
            staged_seen_by_b = worker_b.get_staged_values()
            assert secret in staged_seen_by_b
            out = worker_b.scrub_text(
                staged_seen_by_b, f"B persisting turn that echoes {secret}!"
            )
            assert secret not in out
            assert MARK in out
        finally:
            worker_a.clear_staged()
