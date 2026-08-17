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

Deliberately DEFERRED to the chokepoint-wiring change (the 8 sites are not wired
on this branch yet, so these would test nothing that exists):
  * the terminal-applier / idempotency-snapshot chokepoint scrubs;
  * ``tests/unit/test_ent279_scrub_parity.py`` — the drift guard that anchors on
    every function persisting agent text (its allowlist only makes sense once the
    wired set exists);
  * cross-worker propagation through a real sibling Redis (the store-is-global
    property is proven here single-client; two-client propagation needs :6390).

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
