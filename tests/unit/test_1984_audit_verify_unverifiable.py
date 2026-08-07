"""#1984 — an unhashed audit chain must not report as verified.

`POST /api/audit-log/verify` answered `valid: true, checked: 0` for a log in
which no entry carried a hash. Found by probing a live instance: 1,162 real
audit entries, zero hashes, green tick. `valid: true` was returned for three
materially different states — chain verified, range empty, and *no integrity
data exists at all* — and the last is the DEFAULT for every install that never
enabled hashing.

Skipping an unhashed entry is correct on its own; a chain enabled midway
legitimately has an unhashed prefix. The defect was the aggregate verdict when
**every** entry was skipped.

`valid` is now tri-state. `None`, not `False`: `False` claims tampering, which
is an equally wrong and much louder lie, and a caller doing a truthiness test
degrades to "not verified" — the safe direction.

The frontend is covered here too, because a backend-only fix would have turned
a false green into a false *red*: the store did `data.valid ? 'valid' :
'invalid'`, so `null` would have rendered as "✗ Tamper detected".
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@pytest.fixture
def svc():
    try:
        from services.platform_audit_service import platform_audit_service
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return platform_audit_service


def _entries(svc_mod, monkeypatch, rows):
    import services.platform_audit_service as mod

    monkeypatch.setattr(mod.db, "get_audit_entries_range",
                        lambda s, e: rows, raising=False)


def _row(id_: int, **over) -> dict:
    """A complete audit row.

    `_compute_hash` reads event_id/event_type/event_action/timestamp directly
    (KeyError, not .get), so a partial fixture fails inside the hasher and looks
    like a code bug. Build the full shape once.
    """
    row = {
        "id": id_,
        "event_id": f"evt-{id_}",
        "event_type": "configuration",
        "event_action": "settings_change",
        "actor_id": "1",
        "target_id": None,
        "timestamp": f"2026-08-04T00:00:{id_:02d}Z",
        "details": None,
        "entry_hash": None,
        "previous_hash": None,
    }
    row.update(over)
    return row


def _hashed(svc_mod, entry: dict) -> dict:
    """Give an entry the hash the verifier will recompute for it."""
    out = dict(entry)
    out["entry_hash"] = svc_mod._compute_hash(out)
    return out


# ---------------------------------------------------------------------------
# THE bug.
# ---------------------------------------------------------------------------


def test_rows_with_no_hashes_are_unverifiable_not_valid(svc, monkeypatch):
    """The reported case, reproduced from the live shape: many rows, zero
    hashes. The old code answered `valid: True, checked: 0`."""
    _entries(svc, monkeypatch, [
        _row(i) for i in range(1, 51)
    ])

    out = asyncio.run(svc.verify_chain(1, 50))

    assert out["valid"] is None, (
        "an audit log with no integrity data reported a verdict about its "
        "integrity (#1984)"
    )
    assert out["status"] == "unverifiable"
    assert out["checked"] == 0
    assert out["skipped_unhashed"] == 50
    assert out["total_in_range"] == 50


def test_unverifiable_is_not_truthy(svc, monkeypatch):
    """A caller doing `if result["valid"]:` must NOT treat this as verified.
    That truthiness test is exactly what the frontend did."""
    _entries(svc, monkeypatch, [
        _row(1),
    ])
    assert not asyncio.run(svc.verify_chain(1, 1))["valid"]


def test_unverifiable_does_not_claim_tampering(svc, monkeypatch):
    """`False` would be a louder wrong answer than the original `True` — it
    would page someone about a breach that did not happen."""
    _entries(svc, monkeypatch, [
        _row(1),
    ])
    out = asyncio.run(svc.verify_chain(1, 1))
    assert out["valid"] is not False
    assert out["status"] != "tampered"
    assert out["first_invalid_id"] is None


# ---------------------------------------------------------------------------
# The states that must stay distinguishable (AC #2).
# ---------------------------------------------------------------------------


def test_empty_range_is_its_own_state(svc, monkeypatch):
    """"No such rows" is not a statement about integrity in either direction,
    and must not be conflated with "rows exist but none are hashed" — both used
    to be `checked: 0`."""
    _entries(svc, monkeypatch, [])
    out = asyncio.run(svc.verify_chain(1, 10))
    assert out["status"] == "empty_range"
    assert out["valid"] is None
    assert out["total_in_range"] == 0


def test_a_genuinely_intact_chain_still_verifies(svc, monkeypatch):
    """No regression: the whole point is that a real verification still passes,
    or the fix is just a different broken answer."""
    import services.platform_audit_service as mod

    e1 = _hashed(mod.platform_audit_service, _row(1))
    e2 = _hashed(mod.platform_audit_service,
                 _row(2, previous_hash=e1["entry_hash"]))
    _entries(svc, monkeypatch, [e1, e2])

    out = asyncio.run(svc.verify_chain(1, 2))
    assert out["valid"] is True
    assert out["status"] == "verified"
    assert out["checked"] == 2
    assert out["skipped_unhashed"] == 0


def test_tampering_is_still_detected(svc, monkeypatch):
    _entries(svc, monkeypatch, [
        _row(1, entry_hash="deadbeef"),
    ])
    out = asyncio.run(svc.verify_chain(1, 1))
    assert out["valid"] is False
    assert out["status"] == "tampered"
    assert out["first_invalid_id"] == 1


def test_a_partially_hashed_range_is_named_apart(svc, monkeypatch):
    """An install that enables hashing later carries a permanent unhashed
    prefix. Reporting that as a clean `verified` would silently average over
    the boundary — the reader cannot tell how much was actually covered."""
    import services.platform_audit_service as mod

    hashed = _hashed(mod.platform_audit_service, _row(3))
    _entries(svc, monkeypatch, [_row(1), _row(2), hashed])

    out = asyncio.run(svc.verify_chain(1, 3))
    assert out["valid"] is True, "the hashed portion did verify"
    assert out["status"] == "verified_partial"
    assert out["checked"] == 1
    assert out["skipped_unhashed"] == 2, "the uncovered prefix must be visible"


@pytest.mark.parametrize(
    ("rows", "expected_status"),
    [
        pytest.param([], "empty_range", id="empty"),
        pytest.param(
            [_row(1)], "unverifiable", id="no-hashes",
        ),
    ],
)
def test_the_two_zero_checked_states_are_distinguishable(svc, monkeypatch, rows,
                                                         expected_status):
    """Both report `checked: 0`. Before this change that was the ONLY signal,
    so they were indistinguishable — which is AC #2."""
    _entries(svc, monkeypatch, rows)
    out = asyncio.run(svc.verify_chain(1, 5))
    assert out["checked"] == 0
    assert out["status"] == expected_status


def test_response_reports_whether_hashing_is_even_on(svc, monkeypatch):
    """`monitoring/status` already does this — it returns `enabled: false`
    beside a stale summary so the reader can interpret it. Verify had no
    equivalent, which is why `checked: 0` was uninterpretable."""
    _entries(svc, monkeypatch, [])
    assert "hash_chain_enabled" in asyncio.run(svc.verify_chain(1, 1))


# ---------------------------------------------------------------------------
# The response model must be able to carry the third state.
# ---------------------------------------------------------------------------


def test_the_response_model_accepts_a_null_valid():
    """`valid: bool` would coerce `None` to a validation error at the router
    and turn the fix into a 500."""
    from models import AuditVerifyResponse

    m = AuditVerifyResponse(valid=None, status="unverifiable", checked=0,
                            skipped_unhashed=3, total_in_range=3,
                            hash_chain_enabled=False)
    assert m.valid is None
    assert m.model_dump()["status"] == "unverifiable"


def test_router_passes_the_whole_result_through():
    """A field the service computes but the router drops is invisible to every
    caller — the failure mode this endpoint already had."""
    src = (_BACKEND / "routers" / "audit_log.py").read_text(encoding="utf-8")
    assert "AuditVerifyResponse(**result)" in src


# ---------------------------------------------------------------------------
# Frontend — a backend-only fix turns a false green into a false red.
# ---------------------------------------------------------------------------


def _store_src() -> str:
    """Store source with `//` comment lines stripped.

    Deliberate, and the inverse of the trap `test_1941_nightly_merge_depth.py`
    records: there a comment quoting the old code made a naive search PASS
    vacuously. Here this fix's own comments quote the old
    `data.valid ? 'valid' : 'invalid'` while explaining why it went — and an
    unstripped search FAILS on the explanation. Either way, assert against what
    executes, not against prose about it.
    """
    raw = (_REPO / "src" / "frontend" / "src" / "stores" / "auditLog.js").read_text(
        encoding="utf-8"
    )
    return "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("//")
    )


def test_store_branches_on_the_tri_state_not_truthiness():
    """`data.valid ? 'valid' : 'invalid'` maps `null` to 'invalid' — a tamper
    alarm for a log that is merely unhashed."""
    src = _store_src()
    assert "data.valid === true" in src and "data.valid === false" in src
    assert not re.search(r"data\.valid\s*\?\s*'valid'\s*:\s*'invalid'", src), (
        "the store still collapses the tri-state into a boolean (#1984)"
    )


def test_store_declares_the_unverifiable_state():
    assert "'unverifiable'" in _store_src()


def test_store_no_longer_asserts_valid_for_an_empty_list():
    """It set `verifyState='valid', checked:0` without calling the API at all —
    the same vacuous affirmation, client-side."""
    src = _store_src()
    empty_branch = src[src.index("if (this.entries.length === 0)"):][:400]
    assert "'unverifiable'" in empty_branch
    assert "verifyState = 'valid'" not in empty_branch


def test_the_badge_renders_the_third_state():
    """Without this the new state falls through to the `v-else` "Verify failed"
    branch, which reads as a broken request rather than an honest answer."""
    vue = (
        _REPO / "src" / "frontend" / "src" / "views" / "enterprise" / "Audit.vue"
    ).read_text(encoding="utf-8")
    assert "store.verifyState === 'unverifiable'" in vue
    assert "Unverifiable" in vue
    # And it must not be styled as success or as tamper.
    badge = vue[vue.index("verifyState === 'unverifiable'"):][:400]
    assert "amber" in badge or "yellow" in badge
