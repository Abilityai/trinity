"""/edge-cases 2026-10-06 — boundaries of the idempotency service (Invariant #18).

Target: ``src/backend/services/idempotency_service.py`` (``begin`` / ``complete`` /
``fail`` / ``attach_execution`` / ``upgrade_snapshot`` / ``discard_stale_replay``,
the scope helpers, ``derive_payment_key``) plus the storage contract it relies on
in ``src/backend/db/idempotency.py`` (the 24h window, the replay snapshot shape).

Companion to ``test_idempotency.py`` (happy-path lifecycle), ``test_1018_*``
(``derive_payment_key`` None-safety), ``test_2392_*`` (effect-guard fail-closed)
and ``test_ent665_*`` (intent keys). Those suites prove replay WORKS; this file
works what they never assert:

* the guards that keep a DUPLICATE from touching the FIRST request's row —
  ``fail()`` / ``complete()`` on a ``replay`` decision must be no-ops, otherwise a
  409'd duplicate releases (or overwrites) the claim of the request still running;
* every fail-open arm (claim raises, unknown state, complete/release/attach/
  upgrade/discard raise) — each is "never block a real execution on the dedup
  layer" and none was executed by the existing suite;
* the 24h window at its own boundary (``created_at < cutoff`` is strict), and the
  fact that the default path expires an ``in_flight`` row too;
* the snapshot shape a replay actually returns (JSON round-trip, not identity),
  including a corrupt stored snapshot and an unserializable one;
* hostile keys: empty, unicode, a lone surrogate (reachable from a JSON body such
  as A2A ``messageId``), NUL, non-str, 100 KB.

Runs against an isolated temp SQLite DB (engine seam pointed at it), with the
service loaded standalone over a fake ``database.db`` that delegates to the REAL
``IdempotencyOperations`` — the same harness shape as ``test_idempotency.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_DDL = """
CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    execution_id TEXT,
    status TEXT NOT NULL,
    response_snapshot TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
)
"""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def ops(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ec_idem.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()
    monkeypatch.delitem(sys.modules, "db.idempotency", raising=False)
    import db.idempotency as idem_db_mod
    o = idem_db_mod.IdempotencyOperations()
    o._path = db_path
    o._mod = idem_db_mod
    yield o
    engine_mod.dispose_engines()


class _CountingDb(types.SimpleNamespace):
    pass


@pytest.fixture
def svc(ops, monkeypatch):
    calls: list = []

    def _wrap(name, fn):
        def inner(*a, **k):
            calls.append(name)
            return fn(*a, **k)
        return inner

    fake_db = _CountingDb(
        idempotency_claim=_wrap("claim", ops.claim),
        idempotency_attach_execution=_wrap("attach", ops.attach_execution),
        idempotency_complete=_wrap("complete", ops.complete),
        idempotency_release=_wrap("release", ops.release),
        idempotency_discard_completed=_wrap("discard", ops.discard_completed),
        idempotency_purge_expired=ops.purge_expired,
        get_execution=lambda eid: None,
    )
    fake_database = types.ModuleType("database")
    fake_database.db = fake_db
    monkeypatch.setitem(sys.modules, "database", fake_database)
    path = os.path.join(_BACKEND, "services", "idempotency_service.py")
    spec = importlib.util.spec_from_file_location("_ec_idem_service_edges", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._calls = calls
    module._fake_db = fake_db
    return module


def _row(ops, scope, key):
    c = sqlite3.connect(ops._path)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute(
            "SELECT * FROM idempotency_keys WHERE scope=? AND idempotency_key=?", (scope, key)
        ).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _seed(ops, scope, key, status, created_at, snapshot=None, execution_id=None):
    c = sqlite3.connect(ops._path)
    try:
        c.execute(
            "INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?,?)",
            (scope, key, execution_id, status, snapshot, created_at, created_at),
        )
        c.commit()
    finally:
        c.close()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _boom(*_a, **_k):
    raise RuntimeError("store down")


# ---------------------------------------------------------------------------
# begin() — key domain + fail-open arms
# ---------------------------------------------------------------------------

class TestBeginKeyDomain:
    @pytest.mark.parametrize("key", [None, ""], ids=["r1-none", "r2-empty"])
    def test_falsy_key_disables_without_touching_store(self, svc, key):
        d = svc.begin("agent:a", key)
        assert (d.enabled, d.replay, d.in_flight) == (False, False, False)
        assert svc._calls == []

    @pytest.mark.parametrize(
        "key",
        [
            "0",                               # r3 looks-falsy string is a real key
            " ",                               # r3 whitespace-only is truthy
            "ключ-🔑-مفتاح",                   # r10 unicode / RTL / emoji
            "é",                         # r10 combining mark
            "a\x00b",                          # r13 embedded NUL
            "k" * 100_000,                     # r12 100 KB (SQLite has no cap)
        ],
        ids=["r3-zero-str", "r3-space", "r10-unicode", "r10-combining", "r13-nul", "r12-huge"],
    )
    def test_truthy_key_round_trips_new_then_replay(self, svc, key):
        d1 = svc.begin("agent:a", key)
        assert d1.enabled and not d1.replay
        svc.complete(d1, "exec-1", {"r": 1})
        d2 = svc.begin("agent:a", key)
        assert d2.replay and not d2.in_flight
        assert d2.snapshot == {"r": 1} and d2.execution_id == "exec-1"

    def test_r10_unicode_normalization_forms_are_distinct_keys(self, svc):
        # NFC "é" vs NFD "é" are different byte strings → different rows.
        svc.begin("agent:a", "é")
        assert svc.begin("agent:a", "é").replay is False

    def test_r11_lone_surrogate_key_fails_open(self, svc):
        # json.loads('"\\ud800"') yields this — reachable via a JSON-body key
        # (A2A messageId, routers/a2a.py:1067-1068). The store cannot encode it;
        # begin() must degrade to no-dedup, never raise.
        d = svc.begin("agent:a", "\ud800")
        assert d.enabled is False and d.replay is False

    @pytest.mark.parametrize("key", [123, 1.5], ids=["r14-int", "r14-float"])
    def test_r14_non_str_key_never_raises(self, svc, key):
        # A2A passes message.get("messageId") straight through — a peer can
        # send a number. Whatever the store does, begin() must not raise.
        d = svc.begin("agent:a", key)
        assert isinstance(d.enabled, bool)

    def test_r14_dict_key_fails_open(self, svc):
        d = svc.begin("agent:a", {"id": 1})
        assert d.enabled is False

    def test_r4_claim_raises_fails_open(self, svc):
        svc._fake_db.idempotency_claim = _boom
        d = svc.begin("agent:a", "k")
        assert (d.enabled, d.replay, d.in_flight) == (False, False, False)
        # and the lifecycle calls on that decision are inert
        svc._fake_db.idempotency_complete = _boom
        svc._fake_db.idempotency_release = _boom
        svc.complete(d, "e", {"x": 1})
        svc.fail(d)

    @pytest.mark.parametrize("state", [None, "weird", "COMPLETED"], ids=["r5-none", "r5-weird", "r5-case"])
    def test_r5_unknown_claim_state_fails_open(self, svc, state):
        svc._fake_db.idempotency_claim = lambda *a, **k: {"state": state}
        d = svc.begin("agent:a", "k")
        assert d.enabled is False and d.replay is False

    def test_r7_in_flight_replay_carries_attached_execution_id_no_snapshot(self, svc):
        d1 = svc.begin("agent:a", "k")
        svc.attach_execution(d1, "exec-attached")
        d2 = svc.begin("agent:a", "k")
        assert (d2.replay, d2.in_flight) == (True, True)
        assert d2.execution_id == "exec-attached"
        assert d2.snapshot is None
        assert (d2.scope, d2.key) == ("agent:a", "k")

    def test_r6_new_decision_echoes_scope_and_key(self, svc):
        d = svc.begin("webhook:t", "k")
        assert (d.scope, d.key, d.execution_id, d.snapshot) == ("webhook:t", "k", None, None)


# ---------------------------------------------------------------------------
# A duplicate must never touch the first request's row
# ---------------------------------------------------------------------------

class TestDuplicateCannotDisturbOriginal:
    def test_r20_fail_on_in_flight_replay_does_not_release_original(self, svc, ops):
        first = svc.begin("agent:a", "k")
        dup = svc.begin("agent:a", "k")
        assert dup.in_flight
        svc.fail(dup)  # the 409 path must not free the running request's claim
        assert _row(ops, "agent:a", "k")["status"] == "in_flight"
        assert svc.begin("agent:a", "k").in_flight is True
        assert "release" not in svc._calls
        svc.complete(first, "e1", {"ok": 1})

    def test_r19_complete_on_completed_replay_does_not_overwrite(self, svc, ops):
        first = svc.begin("agent:a", "k")
        svc.complete(first, "e1", {"v": "original"})
        dup = svc.begin("agent:a", "k")
        svc.complete(dup, "e2", {"v": "imposter"})
        again = svc.begin("agent:a", "k")
        assert again.snapshot == {"v": "original"} and again.execution_id == "e1"

    def test_r19b_complete_on_in_flight_replay_does_not_complete_original(self, svc, ops):
        svc.begin("agent:a", "k")
        dup = svc.begin("agent:a", "k")
        svc.complete(dup, "e2", {"v": "imposter"})
        assert _row(ops, "agent:a", "k")["status"] == "in_flight"

    def test_r21_fail_on_completed_replay_is_noop(self, svc, ops):
        first = svc.begin("agent:a", "k")
        svc.complete(first, "e1", {"v": 1})
        svc.fail(svc.begin("agent:a", "k"))
        assert svc.begin("agent:a", "k").snapshot == {"v": 1}

    @pytest.mark.parametrize("which", ["disabled", "replay", "no-exec"])
    def test_r24_attach_execution_guards(self, svc, ops, which):
        first = svc.begin("agent:a", "k")
        if which == "disabled":
            d = svc.begin("agent:a", None)
            svc.attach_execution(d, "e-x")
        elif which == "replay":
            d = svc.begin("agent:a", "k")
            svc.attach_execution(d, "e-x")
        else:
            svc.attach_execution(first, None)
            svc.attach_execution(first, "")
        assert "attach" not in svc._calls
        assert _row(ops, "agent:a", "k")["execution_id"] is None


# ---------------------------------------------------------------------------
# Lifecycle fail-open arms (none executed by the existing suite)
# ---------------------------------------------------------------------------

class TestLifecycleFailOpen:
    @pytest.mark.parametrize(
        "attr,call",
        [
            ("idempotency_complete", lambda s, d: s.complete(d, "e", {"x": 1})),
            ("idempotency_release", lambda s, d: s.fail(d)),
            ("idempotency_attach_execution", lambda s, d: s.attach_execution(d, "e")),
            ("idempotency_complete", lambda s, d: s.upgrade_snapshot(d.scope, d.key, {"x": 1})),
            ("idempotency_discard_completed", lambda s, d: s.discard_stale_replay(d.scope, d.key)),
        ],
        ids=["r22-complete", "r23-fail", "r25-attach", "r35-upgrade", "r36d-discard"],
    )
    def test_store_error_is_swallowed(self, svc, attr, call, caplog):
        d = svc.begin("agent:a", "k")
        setattr(svc._fake_db, attr, _boom)
        with caplog.at_level("WARNING"):
            call(svc, d)  # must not raise
        assert any("store down" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# complete() — snapshot shape a replay returns
# ---------------------------------------------------------------------------

class TestSnapshotShape:
    def test_r27_complete_without_execution_keeps_attached_id(self, svc):
        d = svc.begin("agent:a", "k")
        svc.attach_execution(d, "exec-att")
        svc.complete(d, None, {"x": 1})
        assert svc.begin("agent:a", "k").execution_id == "exec-att"

    def test_r28_snapshot_is_json_round_tripped_not_identity(self, svc):
        when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        snap = {"t": (1, 2), 7: "int-key", "when": when, "nested": {"z": [None, True]}}
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e", snap)
        got = svc.begin("agent:a", "k").snapshot
        assert got == {"t": [1, 2], "7": "int-key", "when": str(when), "nested": {"z": [None, True]}}

    def test_r29_unserializable_snapshot_still_completes_with_none(self, svc, ops):
        circ: dict = {}
        circ["self"] = circ
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e", circ)
        r = svc.begin("agent:a", "k")
        # The duplicate is still suppressed (no double execution); it just
        # replays without a body.
        assert (r.replay, r.in_flight, r.snapshot, r.execution_id) == (True, False, None, "e")

    @pytest.mark.parametrize(
        "snap,expected",
        [(None, None), ({}, {}), ([], []), (0, 0), ("", ""), (False, False)],
        ids=["r30-none", "r31-empty-dict", "r31-empty-list", "r31-zero", "r31-empty-str", "r31-false"],
    )
    def test_r30_r31_falsy_snapshots(self, svc, snap, expected):
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e", snap)
        assert svc.begin("agent:a", "k").snapshot == expected

    def test_r9_corrupt_stored_snapshot_replays_none(self, svc, ops):
        now = _fmt(datetime.now(timezone.utc))
        _seed(ops, "agent:a", "k", "completed", now, snapshot="{not json", execution_id="e")
        r = svc.begin("agent:a", "k")
        assert (r.replay, r.in_flight, r.snapshot, r.execution_id) == (True, False, None, "e")

    def test_r26_second_complete_on_same_fresh_decision_last_write_wins(self, svc):
        # UNSPECIFIED in the contract; characterised so a change is deliberate.
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e1", {"v": 1})
        svc.complete(d, "e2", {"v": 2})
        r = svc.begin("agent:a", "k")
        assert r.snapshot == {"v": 2} and r.execution_id == "e2"


# ---------------------------------------------------------------------------
# upgrade_snapshot / discard_stale_replay
# ---------------------------------------------------------------------------

class TestUpgradeAndDiscard:
    @pytest.mark.parametrize("scope,key", [(None, "k"), ("agent:a", None), ("", "k"), ("agent:a", "")],
                             ids=["r32-scope-none", "r32-key-none", "r32-scope-empty", "r32-key-empty"])
    def test_r32_falsy_scope_or_key_is_noop(self, svc, scope, key):
        svc.upgrade_snapshot(scope, key, {"x": 1})
        svc.discard_stale_replay(scope, key)
        assert svc._calls == []

    def test_r33_upgrade_overwrites_completed_and_keeps_execution(self, svc):
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e1", {"settled": False})
        svc.upgrade_snapshot("agent:a", "k", {"settled": True})
        r = svc.begin("agent:a", "k")
        assert r.snapshot == {"settled": True} and r.execution_id == "e1"

    def test_r34_upgrade_finalizes_an_in_flight_claim(self, svc):
        svc.begin("agent:a", "k")
        svc.upgrade_snapshot("agent:a", "k", {"settled": True})
        r = svc.begin("agent:a", "k")
        assert (r.in_flight, r.snapshot) == (False, {"settled": True})

    def test_r34b_upgrade_on_missing_row_creates_nothing(self, svc):
        svc.upgrade_snapshot("agent:a", "ghost", {"x": 1})
        assert svc.begin("agent:a", "ghost").replay is False

    def test_r36_discard_removes_completed_so_next_begin_is_fresh(self, svc):
        d = svc.begin("agent:a", "k")
        svc.complete(d, "e1", {"x": 1})
        svc.discard_stale_replay("agent:a", "k")
        assert svc.begin("agent:a", "k").replay is False

    def test_r36b_discard_never_removes_in_flight(self, svc):
        svc.begin("agent:a", "k")
        svc.discard_stale_replay("agent:a", "k")
        assert svc.begin("agent:a", "k").in_flight is True


# ---------------------------------------------------------------------------
# The 24h window — at its own boundary
# ---------------------------------------------------------------------------

class TestWindowBoundary:
    @pytest.fixture
    def frozen(self, ops, monkeypatch):
        now = datetime(2026, 10, 6, 12, 0, 0, 500000, tzinfo=timezone.utc)
        mod = ops._mod
        monkeypatch.setattr(mod, "utc_now_iso", lambda: _fmt(now))
        monkeypatch.setattr(
            mod, "iso_cutoff",
            lambda hours=0, *, minutes=0, seconds=0: _fmt(now - timedelta(hours=hours, minutes=minutes, seconds=seconds)),
        )
        return now

    @pytest.mark.parametrize(
        "age,expect_replay",
        [
            (timedelta(hours=24), True),                              # r37 exactly at cutoff: NOT expired
            (timedelta(hours=24, microseconds=1), False),             # r38 1µs past: expired
            (timedelta(hours=24) - timedelta(microseconds=1), True),  # r37b 1µs inside
            (timedelta(0), True),
        ],
        ids=["r37-equal", "r38-past-by-1us", "r37b-inside-by-1us", "now"],
    )
    def test_completed_row_at_boundary(self, svc, ops, frozen, age, expect_replay):
        _seed(ops, "agent:a", "k", "completed", _fmt(frozen - age), snapshot='{"x":1}', execution_id="e")
        d = svc.begin("agent:a", "k")
        assert d.replay is expect_replay
        if not expect_replay:
            # expired row is replaced by a fresh in_flight claim, stamped now
            assert _row(ops, "agent:a", "k")["created_at"] == _fmt(frozen)

    def test_r39_default_path_expires_an_in_flight_row_too(self, svc, ops, frozen):
        _seed(ops, "agent:a", "k", "in_flight", _fmt(frozen - timedelta(hours=25)))
        d = svc.begin("agent:a", "k")
        assert d.enabled and not d.replay

    def test_r39b_expiry_is_per_key_not_a_sweep(self, svc, ops, frozen):
        old = _fmt(frozen - timedelta(hours=30))
        _seed(ops, "agent:a", "other", "completed", old)
        svc.begin("agent:a", "k")
        assert _row(ops, "agent:a", "other") is not None

    def test_r40_purge_boundary_matches_claim_boundary(self, ops, frozen):
        _seed(ops, "s", "equal", "completed", _fmt(frozen - timedelta(hours=24)))
        _seed(ops, "s", "past", "completed", _fmt(frozen - timedelta(hours=24, microseconds=1)))
        assert ops.purge_expired(ttl_hours=24) == 1
        assert _row(ops, "s", "equal") is not None and _row(ops, "s", "past") is None

    @pytest.mark.parametrize(
        "stored,expect_replay",
        [
            # r41 millisecond-precision legacy shape (test_idempotency._iso)
            ("2026-10-05T11:59:59.999Z", False),
            ("2026-10-05T12:00:01.000Z", True),
        ],
        ids=["r41-ms-older", "r41-ms-newer"],
    )
    def test_r41_ms_precision_rows_compare_correctly(self, svc, ops, frozen, stored, expect_replay):
        _seed(ops, "agent:a", "k", "completed", stored, snapshot="{}")
        assert svc.begin("agent:a", "k").replay is expect_replay


# ---------------------------------------------------------------------------
# Scope helpers — isolation
# ---------------------------------------------------------------------------

class TestScopes:
    @pytest.mark.parametrize(
        "email",
        ["A@Example.com", "  a@example.com  ", "a@EXAMPLE.COM\t"],
        ids=["r16-case", "r16-space", "r16-tab"],
    )
    def test_r16_inline_auth_scope_normalizes_email(self, svc, email):
        assert svc.make_inline_auth_scope("bot", email) == svc.make_inline_auth_scope("bot", "a@example.com")

    def test_r16b_inline_scope_distinct_users_do_not_share_a_replay(self, svc):
        s1 = svc.make_inline_auth_scope("bot", "a@example.com")
        s2 = svc.make_inline_auth_scope("bot", "b@example.com")
        d = svc.begin(s1, "same-key")
        svc.complete(d, "e1", {"answer": "for a"})
        assert svc.begin(s2, "same-key").replay is False

    def test_r15_scope_families_never_coincide(self, svc):
        name = "bot"
        scopes = {
            svc.make_agent_scope(name),
            svc.make_inline_auth_scope(name, "a@example.com"),
            svc.make_webhook_scope(name),
            svc.make_effect_scope(name),
            svc.make_payment_scope(name),
            svc.make_intent_scope(name),
        }
        assert len(scopes) == 6

    def test_r17_inline_scope_none_email_is_empty_suffix(self, svc):
        # Unreachable through mcp_auth (email is verified before begin), pinned
        # so a None never becomes the literal "None".
        assert svc.make_inline_auth_scope("bot", None) == "agent:bot:mcp-inline:"


# ---------------------------------------------------------------------------
# derive_payment_key — no credential at rest, shape
# ---------------------------------------------------------------------------

class TestPaymentKey:
    def test_r44_key_shape_and_no_token_at_rest(self, svc):
        tok = "secret-access-token-xyz"
        k = svc.derive_payment_key(tok, b"hello")
        assert k.startswith("paid:") and len(k) == len("paid:") + 64
        assert tok not in k
        int(k[5:], 16)

    def test_r45_unicode_token_and_body(self, svc):
        k = svc.derive_payment_key("токен-🔑", "привет".encode())
        assert k.startswith("paid:")

    def test_r46_nul_boundary_ambiguity_is_header_unreachable(self, svc):
        # (token "t\\0", body "x") and (token "t", body "\\0x") hash the same
        # bytes. Accepted: the token arrives in an HTTP header, which cannot
        # carry NUL (h11 rejects it with 400). Pinned so the reasoning is visible.
        assert svc.derive_payment_key("t\x00", b"x") == svc.derive_payment_key("t", b"\x00x")

    @pytest.mark.parametrize("tok,body", [(" ", b"x"), ("t", b" ")], ids=["r42-space-token", "r42-space-body"])
    def test_r42_whitespace_is_not_falsy(self, svc, tok, body):
        assert svc.derive_payment_key(tok, body) is not None


# ---------------------------------------------------------------------------
# effect_guard / intent_guard fail-open arms the suite never ran
# ---------------------------------------------------------------------------

class TestGuardFailOpenArms:
    @pytest.mark.asyncio
    async def test_r52_effect_guard_payment_claim_hiccup_runs_body_unguarded(self, svc):
        svc._fake_db.idempotency_claim = _boom
        ran = []
        async with svc.effect_guard("nevermined_settle", {"p": 1}, payment_request_id="req-1") as g:
            ran.append(1)
            assert (g.replay, g.dedup_enabled) == (False, False)
        assert ran == [1]

    @pytest.mark.asyncio
    async def test_r53_intent_guard_release_failure_still_reraises_send_error(self, svc):
        svc._fake_db.idempotency_release = _boom
        with pytest.raises(ValueError, match="send failed"):
            async with svc.intent_guard(
                "message", agent_name="bot", target="a@example.com",
                idempotency_key="k1", ttl_seconds=3600, execution_id=None,
            ):
                raise ValueError("send failed")

    @pytest.mark.asyncio
    async def test_r54_intent_guard_complete_failure_is_swallowed(self, svc):
        svc._fake_db.idempotency_complete = _boom
        async with svc.intent_guard(
            "message", agent_name="bot", target="a@example.com",
            idempotency_key="k1", ttl_seconds=3600, execution_id=None,
        ) as g:
            assert g.result_fields() == {"sent": True}

    @pytest.mark.asyncio
    async def test_r55_intent_guard_zero_ttl_falls_back_to_ceiling(self, svc, monkeypatch):
        # ttl 0 is refused by IntentKeyFields (ge=60) before it gets here; the
        # service's `or` makes it the 24h ceiling rather than "expire now".
        seen = {}

        def claim(scope, key, **kw):
            seen.update(kw)
            return {"state": "new"}

        svc._fake_db.idempotency_claim = claim
        async with svc.intent_guard(
            "message", agent_name="bot", target="x", idempotency_key="k",
            ttl_seconds=0, execution_id=None,
        ):
            pass
        assert seen["ttl_seconds"] == svc.INTENT_TTL_MAX_SECONDS


# ---------------------------------------------------------------------------
# Boundary consumer: rooms (Invariant #18 "in-flight duplicate → 409")
# ---------------------------------------------------------------------------

class TestRoomsInFlightBoundary:
    """``POST /api/rooms/{id}/messages`` — ``begin()`` reports an in-flight
    claim as ``replay=True, in_flight=True``, so the ``in_flight`` check must
    run BEFORE the generic ``replay`` return or it never runs (#3320). Every
    other boundary nests ``if in_flight`` inside ``if replay``."""

    def _drive(self, monkeypatch, decision, posted):
        # ``posted`` is passed in so a test can read it after the handler raises.
        import asyncio
        import services.idempotency_service as real_idem
        import shared_sessions.router as rooms_router

        async def _post(*a, **k):
            posted.append(a)
            return {"seq": 1}

        monkeypatch.setattr(real_idem, "begin", lambda scope, key: decision)
        monkeypatch.setattr(rooms_router.service, "post_message", _post)
        from shared_sessions.models import RoomMessageCreate
        out = asyncio.run(rooms_router.post_message(
            "room-1", RoomMessageCreate(content="hello"),
            idempotency_key="room-1:opt-1700000000000", current_user=object(),
        ))
        return out

    def test_completed_replay_returns_snapshot_without_posting(self, monkeypatch):
        import services.idempotency_service as real_idem
        d = real_idem.IdempotencyDecision(enabled=True, replay=True, in_flight=False,
                                          scope="room:room-1", key="k", snapshot={"seq": 7})
        posted = []
        out = self._drive(monkeypatch, d, posted)
        assert out == {"seq": 7, "replayed": True} and posted == []

    def test_in_flight_duplicate_is_409_not_a_silent_success(self, monkeypatch):
        from fastapi import HTTPException
        import services.idempotency_service as real_idem
        d = real_idem.IdempotencyDecision(enabled=True, replay=True, in_flight=True,
                                          scope="room:room-1", key="k")
        posted = []
        with pytest.raises(HTTPException) as exc:
            self._drive(monkeypatch, d, posted)
        assert exc.value.status_code == 409
        assert posted == []  # the duplicate dispatched nothing
        # Same detail shape as /chat, fan-out and the portal (#3320).
        assert exc.value.detail == {
            "error": "request_in_progress",
            "message": "A post with this Idempotency-Key is still being processed.",
            "execution_id": None,
        }


# ---------------------------------------------------------------------------
# derive_effect_key — identity canonicalization branches
# ---------------------------------------------------------------------------

class TestEffectKeyCanonical:
    def test_r49_none_and_empty_string_identity_are_the_same_effect(self, svc):
        assert svc.derive_effect_key("e", "m", None) == svc.derive_effect_key("e", "m", "")

    def test_r49b_str_identity_is_hashed_raw_not_json_quoted(self, svc):
        # A str is used verbatim; json.dumps("x") would be '"x"'. Pinned because
        # flipping it re-keys every str-identity sink across a deploy (a
        # re-delivery straddling the deploy would double-send).
        import hashlib
        h = hashlib.sha256(b"e\x00m\x00x\x00").hexdigest()
        assert svc.derive_effect_key("e", "m", "x") == f"m:{h}"

    def test_r50_none_execution_id_hashes_as_empty(self, svc):
        assert svc.derive_effect_key(None, "m", {"a": 1}) == svc.derive_effect_key("", "m", {"a": 1})

    def test_r51_non_json_values_use_str_default(self, svc):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert svc.derive_effect_key("e", "m", {"t": when}) == svc.derive_effect_key("e", "m", {"t": str(when)})


# ---------------------------------------------------------------------------
# Golden digests — a derivation change re-keys every live row across a deploy
# ---------------------------------------------------------------------------

class TestGoldenDigests:
    """A request retried across a deploy that changed the derivation lands on a
    different (scope, key) and executes twice (for x402: a second LLM run +
    settle). The NUL separator and the empty-body encoding are part of the
    contract; pin the exact bytes hashed. (Killed mutants M12/M22.)"""

    def test_payment_key_hashes_token_nul_body(self, svc):
        import hashlib
        assert svc.derive_payment_key("tok", b"body") == "paid:" + hashlib.sha256(b"tok\x00body").hexdigest()

    @pytest.mark.parametrize("body", [None, b""], ids=["none", "empty"])
    def test_webhook_key_hashes_token_nul_and_empty_body(self, svc, body):
        import hashlib
        assert svc.derive_webhook_key("tok", body) == "auto:" + hashlib.sha256(b"tok\x00").hexdigest()
