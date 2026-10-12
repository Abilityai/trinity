"""/edge-cases 2026-10-06 — Hypothesis properties of the idempotency service.

Target: ``src/backend/services/idempotency_service.py`` (key derivation, scope
helpers, ``begin``) over the real ``db/idempotency.py`` storage. Companion to
``test_ec_idempotency_edges.py`` (the discrete boundary rows).

Properties:

* **Derivation is a pure, prefixed, fixed-width hash** — every ``derive_*`` is
  deterministic, carries its family prefix and a 64-hex digest, and never leaks
  its raw secret input (payment token) into the key.
* **Injectivity where the contract needs it** — for a FIXED webhook/payment
  token, distinct bodies give distinct keys; ``derive_effect_key`` is
  order-insensitive for dicts at EVERY nesting level (``sort_keys`` is
  recursive) yet sensitive to list order, identity, label and execution.
* **derive_payment_key None-iff-falsy** — the CRITICAL-B rule: no derivable key
  is ``None`` (dedup off), never a constant hash.
* **begin() is total** — for any text key (full unicode incl. lone surrogates,
  NUL, control chars) and any JSON-ish scalar, ``begin`` never raises (fail-open
  is the Invariant #18 contract), and for every *storable* key the state machine
  holds: new → (dup = in_flight) → complete → replay(snapshot) → fail(dup) inert.
* **Replay snapshot == JSON round-trip of what was completed** (oracle).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import types

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_DDL = """
CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, execution_id TEXT,
    status TEXT NOT NULL, response_snapshot TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (scope, idempotency_key))
"""

_HEX = set("0123456789abcdef")

# tests/lint_sys_modules.py pattern: every sys.modules entry this file installs
# is named here and restored after each test so nothing leaks across files.
_STUBBED_MODULE_NAMES = ['database']


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


def _load_service(fake_db):
    fake_database = types.ModuleType("database")
    fake_database.db = fake_db
    sys.modules["database"] = fake_database
    path = os.path.join(_BACKEND, "services", "idempotency_service.py")
    spec = importlib.util.spec_from_file_location("_ec_idem_service_props", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pure():
    """Service loaded with a db that must never be touched (pure functions)."""
    saved = sys.modules.get("database")
    try:
        yield _load_service(types.SimpleNamespace())
    finally:
        if saved is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = saved


@pytest.fixture
def stored(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ec_idem_props.db")
    c = sqlite3.connect(db_path)
    c.execute(_DDL)
    c.commit()
    c.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()
    monkeypatch.delitem(sys.modules, "db.idempotency", raising=False)
    from db.idempotency import IdempotencyOperations
    ops = IdempotencyOperations()
    fake_db = types.SimpleNamespace(
        idempotency_claim=ops.claim,
        idempotency_attach_execution=ops.attach_execution,
        idempotency_complete=ops.complete,
        idempotency_release=ops.release,
        idempotency_discard_completed=ops.discard_completed,
    )
    monkeypatch.setitem(sys.modules, "database", sys.modules.get("database", types.ModuleType("database")))
    svc = _load_service(fake_db)
    yield svc
    engine_mod.dispose_engines()


def _digest_ok(key: str, prefix: str) -> bool:
    if not key.startswith(prefix):
        return False
    tail = key[len(prefix):]
    return len(tail) == 64 and set(tail) <= _HEX


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

_tokens = st.text(min_size=1, max_size=80).filter(lambda s: "\x00" not in s)
_bodies = st.binary(min_size=1, max_size=300)
_utf8_text = st.text(max_size=80, alphabet=st.characters(blacklist_categories=("Cs",)))


@settings(max_examples=200, deadline=None)
@given(tok=_tokens.filter(lambda s: all(not ("\ud800" <= ch <= "\udfff") for ch in s)), b1=_bodies, b2=_bodies)
@example(tok="t", b1=b"a", b2=b"a\x00")
@example(tok="t", b1=b"{}", b2=b"{} ")
def test_payment_key_injective_over_body_for_fixed_token(pure, tok, b1, b2):
    k1, k2 = pure.derive_payment_key(tok, b1), pure.derive_payment_key(tok, b2)
    assert _digest_ok(k1, "paid:") and _digest_ok(k2, "paid:")
    assert (k1 == k2) == (b1 == b2)
    assert k1 == pure.derive_payment_key(tok, b1)  # deterministic
    if len(tok) >= 8:
        assert tok not in k1  # no bearer credential at rest


@settings(max_examples=200, deadline=None)
@given(tok=st.one_of(st.none(), st.just(""), _utf8_text), body=st.one_of(st.none(), st.just(b""), st.binary(max_size=20)))
def test_payment_key_none_iff_an_input_is_falsy(pure, tok, body):
    k = pure.derive_payment_key(tok, body)
    assert (k is None) == (not tok or not body)


@settings(max_examples=200, deadline=None)
@given(tok=_utf8_text, b1=st.binary(max_size=200), b2=st.binary(max_size=200))
@example(tok="t", b1=b"", b2=b"")
def test_webhook_key_injective_over_body_for_fixed_token(pure, tok, b1, b2):
    k1, k2 = pure.derive_webhook_key(tok, b1), pure.derive_webhook_key(tok, b2)
    assert _digest_ok(k1, "auto:")
    assert (k1 == k2) == (b1 == b2)
    # None body and b"" are the same naive-sender trigger
    assert pure.derive_webhook_key(tok, None) == pure.derive_webhook_key(tok, b"")


@settings(max_examples=200, deadline=None)
@given(agent=_utf8_text, m1=_utf8_text, m2=_utf8_text, delay=st.integers(0, 10**7))
def test_reminder_key_sensitive_to_message_for_fixed_spec(pure, agent, m1, m2, delay):
    spec = f"delay_seconds={delay}"
    k1 = pure.derive_reminder_key(agent, m1, spec)
    k2 = pure.derive_reminder_key(agent, m2, spec)
    assert _digest_ok(k1, "reminder:")
    assert (k1 == k2) == (m1 == m2)


_json_leaf = st.one_of(st.none(), st.booleans(), st.integers(-10**6, 10**6), _utf8_text)
_identity = st.recursive(
    st.dictionaries(st.text(max_size=8, alphabet="abcdefgh"), _json_leaf, max_size=4),
    lambda inner: st.dictionaries(st.text(max_size=8, alphabet="abcdefgh"), inner, max_size=3),
    max_leaves=10,
)


def _reverse_dicts(x):
    if isinstance(x, dict):
        return {k: _reverse_dicts(x[k]) for k in reversed(list(x))}
    if isinstance(x, list):
        return [_reverse_dicts(v) for v in x]
    return x


@settings(max_examples=200, deadline=None)
@given(eid=_utf8_text, ident=_identity, label=_utf8_text)
@example(eid="e", ident={"b": {"y": 1, "x": 2}, "a": 0}, label="")
def test_effect_key_insertion_order_insensitive_at_every_depth(pure, eid, ident, label):
    assert pure.derive_effect_key(eid, "message", ident, label) == pure.derive_effect_key(
        eid, "message", _reverse_dicts(ident), label
    )


@settings(max_examples=200, deadline=None)
@given(eid=_utf8_text, ident=_identity, label=_utf8_text, other=_utf8_text)
def test_effect_key_sensitive_to_each_dimension(pure, eid, ident, label, other):
    base = pure.derive_effect_key(eid, "message", ident, label)
    assert base.startswith("message:") and _digest_ok(base, "message:")
    if other != label:
        assert pure.derive_effect_key(eid, "message", ident, other) != base
    if other != eid:
        assert pure.derive_effect_key(other, "message", ident, label) != base
    # effect_type is hashed too, not just the prefix
    other_type = pure.derive_effect_key(eid, "voip_call", ident, label)
    assert other_type.split(":", 1)[1] != base.split(":", 1)[1]
    # list order IS identity (a sequence of recipients is not a set)
    assert pure.derive_effect_key(eid, "m", ["a", "b"], label) != pure.derive_effect_key(eid, "m", ["b", "a"], label)


@settings(max_examples=100, deadline=None)
@given(email=st.emails(), pad=st.sampled_from(["", " ", "\t", "  \n"]))
def test_inline_scope_is_case_and_space_insensitive(pure, email, pad):
    assert pure.make_inline_auth_scope("bot", pad + email.upper() + pad) == pure.make_inline_auth_scope("bot", email)


# ---------------------------------------------------------------------------
# begin() totality + state machine (DB-backed)
# ---------------------------------------------------------------------------

_any_key = st.one_of(
    st.text(max_size=60),                       # full unicode incl. surrogates, NUL
    st.integers(), st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(), st.none(), st.binary(max_size=20),
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=_any_key)
@example(key="\ud800")
@example(key="\x00")
@example(key=float("nan"))
@example(key=b"\xff")
def test_begin_never_raises(stored, key):
    d = stored.begin("agent:prop", key)
    assert isinstance(d.enabled, bool) and isinstance(d.replay, bool)
    if not key:
        assert d.enabled is False
    if d.enabled and not d.replay:
        stored.fail(d)  # leave the store clean for the next example


_storable = st.text(min_size=1, max_size=60, alphabet=st.characters(blacklist_categories=("Cs",)))
_snap = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(-10**9, 10**9), st.text(max_size=20,
              alphabet=st.characters(blacklist_categories=("Cs",)))),
    lambda inner: st.one_of(st.lists(inner, max_size=3),
                            st.dictionaries(st.text(max_size=6, alphabet="abcxyz"), inner, max_size=3)),
    max_leaves=8,
)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=_storable, snap=st.dictionaries(st.text(max_size=6, alphabet="abcxyz"), _snap, max_size=4),
       eid=st.text(min_size=1, max_size=20, alphabet="abcdef0123456789-"))
def test_state_machine_and_replay_oracle(stored, key, snap, eid):
    scope = "agent:sm"
    first = stored.begin(scope, key)
    if first.replay:  # key already used by an earlier example — skip, not a failure
        return
    assert first.enabled
    dup = stored.begin(scope, key)
    assert (dup.replay, dup.in_flight) == (True, True)
    stored.fail(dup)                                  # inert on a replay
    assert stored.begin(scope, key).in_flight is True
    stored.complete(first, eid, snap)
    r = stored.begin(scope, key)
    assert (r.replay, r.in_flight, r.execution_id) == (True, False, eid)
    assert r.snapshot == json.loads(json.dumps(snap, default=str))
    stored.complete(r, "imposter", {"x": 1})          # inert on a replay
    assert stored.begin(scope, key).snapshot == r.snapshot
