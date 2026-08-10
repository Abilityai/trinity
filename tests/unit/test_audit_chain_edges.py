"""Edge-case analysis of the audit hash chain (`/edge-cases`, 2026-08-05).

Target: `services/platform_audit_service.PlatformAuditService.verify_chain`,
`_compute_hash` and `enable_hash_chain`, as merged on `dev` by #1985 (issue
#1984).

#1984's own suite covers the tri-state verdict thoroughly. This file covers the
dimensions it does not: what the chain check can and cannot *detect* (deletion,
reordering, a tampered field the hasher doesn't read), what `_compute_hash` does
with hostile-but-reachable field values, and whether the control that produces
hashes in the first place survives the lifecycle it lives in.

Cases marked `xfail(strict=True)` are REAL defects, not aspirational tests —
each one names the finding it pins. Per the skill's protocol, product code is
not changed here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture
def svc():
    try:
        from services.platform_audit_service import platform_audit_service
    except ImportError:  # pragma: no cover — backend venv required
        pytest.skip("backend venv required")
    return platform_audit_service


@pytest.fixture
def hasher(svc):
    """`_compute_hash` is a staticmethod on the service, not a module global."""
    return svc._compute_hash


@pytest.fixture
def mod():
    try:
        import services.platform_audit_service as m
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    return m


def _rows(mod, monkeypatch, rows):
    monkeypatch.setattr(mod.db, "get_audit_entries_range",
                        lambda s, e: rows, raising=False)


def _row(id_: int, **over) -> dict:
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


def _chain(hasher, n: int, **over) -> list:
    """`n` rows correctly hashed and linked, as `log()` would have written them."""
    out, prev = [], None
    for i in range(1, n + 1):
        row = _row(i, previous_hash=prev, **over)
        row["entry_hash"] = hasher(row)
        prev = row["entry_hash"]
        out.append(row)
    return out


def _verify(svc, start=1, end=100):
    """`asyncio.run`, not `get_event_loop().run_until_complete`.

    The sibling #1984 suite uses `asyncio.run`, which closes the loop and
    clears the current-loop slot — so a `get_event_loop()` here passes when
    this file runs alone and raises "no current event loop" the moment the two
    are collected together. Collection-order-dependent, i.e. green locally and
    red in CI.
    """
    return asyncio.run(svc.verify_chain(start, end))


# ---------------------------------------------------------------------------
# What the chain can detect — the reason it exists
# ---------------------------------------------------------------------------

class TestTamperDetection:

    def test_an_intact_chain_verifies(self, svc, mod, hasher, monkeypatch):
        _rows(mod, monkeypatch, _chain(hasher, 5))
        r = _verify(svc)
        assert (r["valid"], r["status"], r["checked"]) == (True, "verified", 5)

    def test_a_mutated_field_is_detected(self, svc, mod, hasher, monkeypatch):
        chain = _chain(hasher, 5)
        chain[2]["event_action"] = "settings_change_TAMPERED"
        _rows(mod, monkeypatch, chain)
        r = _verify(svc)
        assert r["valid"] is False and r["first_invalid_id"] == 3

    def test_a_deleted_middle_row_is_detected(self, svc, mod, hasher, monkeypatch):
        """Deletion is the attack the LINK check exists for: each surviving
        hash is still self-consistent, so only `previous_hash` catches it."""
        chain = _chain(hasher, 5)
        del chain[2]
        _rows(mod, monkeypatch, chain)
        r = _verify(svc)
        assert r["valid"] is False, "a deleted row left the chain reporting intact"

    def test_reordered_rows_are_detected(self, svc, mod, hasher, monkeypatch):
        chain = _chain(hasher, 5)
        chain[1], chain[2] = chain[2], chain[1]
        _rows(mod, monkeypatch, chain)
        r = _verify(svc)
        assert r["valid"] is False

    @pytest.mark.parametrize("field", ["source", "actor_email", "endpoint",
                                       "actor_ip", "mcp_key_id"])
    def test_fields_outside_the_hashed_subset_are_silently_mutable(
        self, svc, mod, hasher, monkeypatch, field
    ):
        """Documents the boundary rather than asserting a bug.

        `_compute_hash` covers event_id/type/action/actor_id/target_id/
        timestamp/details/previous_hash. Everything else on the row — including
        `actor_ip` and `actor_email`, which an incident responder would read as
        attributable evidence — can be edited with the chain still reporting
        `verified`. Worth knowing before citing a green tick as proof of who
        did something.
        """
        chain = _chain(hasher, 3)
        chain[1][field] = "rewritten-after-the-fact"
        _rows(mod, monkeypatch, chain)
        assert _verify(svc)["valid"] is True

    def test_truncation_at_the_tail_is_not_detectable(self, svc, mod, hasher, monkeypatch):
        """Also a boundary, not a bug: verification is over a caller-supplied
        range, so dropping the newest rows leaves a shorter intact chain. The
        DB trigger (`audit_log_no_delete`) is what defends this, not the hash."""
        _rows(mod, monkeypatch, _chain(hasher, 5)[:3])
        assert _verify(svc)["valid"] is True


# ---------------------------------------------------------------------------
# _compute_hash — inputs it will actually meet
# ---------------------------------------------------------------------------

class TestComputeHash:

    def test_details_str_and_dict_hash_identically(self, mod, hasher):
        """The write path stores `details` as a JSON string and the read path
        returns a dict; the hash has to be stable across that round-trip or
        every entry fails verification."""
        d = {"b": 1, "a": [1, 2, {"z": None}]}
        as_dict = _row(1, details=d)
        as_str = _row(1, details=json.dumps(d))
        assert hasher(as_dict) == hasher(as_str)

    def test_key_order_in_a_details_string_does_not_change_the_hash(self, mod, hasher):
        a = _row(1, details='{"x": 1, "y": 2}')
        b = _row(1, details='{"y": 2, "x": 1}')
        assert hasher(a) == hasher(b)

    def test_unparseable_details_still_hashes(self, mod, hasher):
        """The `except (TypeError, ValueError): pass` branch — the value stays
        a string and must not raise."""
        assert hasher(_row(1, details="{not json"))

    def test_non_ascii_details_hash_stably(self, mod, hasher):
        row = _row(1, details={"note": "café ✓ 日本語"})
        assert hasher(row) == hasher(dict(row))

    def test_a_missing_optional_field_is_not_a_crash(self, mod, hasher):
        row = _row(1)
        row.pop("actor_id")
        row.pop("target_id")
        assert hasher(row)

    @pytest.mark.parametrize("missing", ["event_id", "event_type",
                                         "event_action", "timestamp"])
    def test_required_fields_raise_rather_than_hash_a_hole(self, mod, hasher, missing):
        """These are read with `[]`, not `.get()`. A row missing one is a
        programming error and should surface as one, not hash to a value that
        silently differs from what was written."""
        row = _row(1)
        row.pop(missing)
        with pytest.raises(KeyError):
            hasher(row)

    def test_details_none_and_details_null_string_collide(self, mod, hasher):
        """`details=None` and `details="null"` both normalize to JSON `null`,
        so they hash identically. Harmless (both mean 'no details') and
        recorded so a future 'fix' doesn't treat it as a defect."""
        assert hasher(_row(1, details=None)) == \
               hasher(_row(1, details="null"))


# ---------------------------------------------------------------------------
# The control that produces the hashes
# ---------------------------------------------------------------------------

class TestHashChainLifecycle:

    def test_enabling_is_reflected_in_the_verdict(self, svc, mod, hasher, monkeypatch):
        # Forced through BOTH seams so this reads the same answer before and
        # after #2026: today the verdict is computed from the private
        # `_hash_chain_enabled`; once the flag moves to `system_settings` it is
        # computed from the `hash_chain_enabled` property, and a test that
        # forces only the private attribute fails on the merged tree while
        # testing nothing about the flag. Both are `raising=False`, so whichever
        # seam does not exist in the tree under test is simply unused.
        monkeypatch.setattr(svc, "_hash_chain_enabled", True, raising=False)
        monkeypatch.setattr(
            type(svc), "hash_chain_enabled", property(lambda self: True), raising=False
        )
        _rows(mod, monkeypatch, _chain(hasher, 2))
        assert _verify(svc)["hash_chain_enabled"] is True

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: enabling the audit hash chain is in-memory only — no "
               "persistence, no boot restore, so a backend restart silently "
               "turns the integrity control back off. See /edge-cases report "
               "2026-08-05, finding 1.",
    )
    def test_enabling_the_hash_chain_survives_a_restart(self, mod, hasher):
        """`enable_hash_chain(True)` sets `self._hash_chain_enabled` and writes
        nothing. A fresh process — every deploy, every config change, the
        documented post-restart re-login — starts `False`, and nothing tells the
        operator that hashing stopped.

        Modelled as "construct a second service instance", which is exactly what
        the next process does.
        """
        svc_a = mod.PlatformAuditService()
        svc_a.enable_hash_chain(True)

        svc_b = mod.PlatformAuditService()   # the process after a restart

        # The PUBLIC seam, not `_hash_chain_enabled`. The private attribute is
        # what the fix (#2026) deletes when the flag moves to `system_settings`,
        # and a `strict=True` xfail treats the resulting AttributeError as an
        # expected failure exactly like the assertion failure it replaces — so
        # the marker would go on reporting "BUG: ... in-memory only" against a
        # codebase where the bug is dead, which is the opposite of what strict
        # is for. Asserting the public seam makes it XPASS(strict) — loud — the
        # moment the flag genuinely persists.
        assert svc_b.hash_chain_enabled is True, (
            "hash chain silently reverted to disabled in a new process"
        )

    def test_the_enable_route_persists_nothing(self):
        """Pins the mechanism behind the xfail above, so the finding survives a
        refactor: the flag is set in memory and nothing writes it durably.

        Checks BOTH ends of the delegation. The first draft read only
        `routers/audit_log.py`, and #2026 puts the write in the *service*
        (`db.set_setting(...)`) while leaving the router a thin passthrough — so
        the router-only version passes with the fix in place, and this backstop
        died silently alongside the xfail it exists to protect.

        The service half is asserted over the AST, not the text: the fix's own
        docstring explains the persistence it adds, so a substring scan matches
        the prose and reports "still in-memory" while the write sits next to it.
        """
        import ast
        import inspect
        import textwrap

        src = (_REPO / "src" / "backend" / "routers" / "audit_log.py").read_text()
        block = src[src.index("async def enable_hash_chain"):]
        block = block[:block.index("\n@router") if "\n@router" in block else len(block)]
        assert "platform_audit_service.enable_hash_chain" in block
        assert "set_setting" not in block and "system_settings" not in block, (
            "the enable route now persists — update or remove the xfail above"
        )

        # ...and the setter the route delegates to, resolved through the import
        # rather than a fixed filename, so moving the service doesn't silence it.
        if _BACKEND_STR not in sys.path:
            sys.path.insert(0, _BACKEND_STR)
        from services.platform_audit_service import PlatformAuditService

        setter = ast.parse(
            textwrap.dedent(inspect.getsource(PlatformAuditService.enable_hash_chain))
        )
        persisted = [
            node for node in ast.walk(setter)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"set_setting", "set_system_setting"}
        ]
        assert not persisted, (
            "`enable_hash_chain` now persists the flag — the #2015 finding is "
            "fixed; update or remove the xfail above"
        )


# ---------------------------------------------------------------------------
# Range/argument boundaries
# ---------------------------------------------------------------------------

class TestRangeBoundaries:

    @pytest.mark.parametrize("start,end", [(0, 0), (5, 1), (-1, -1),
                                           (1, 10**12)])
    def test_odd_ranges_degrade_rather_than_raise(self, svc, mod, hasher, monkeypatch,
                                                  start, end):
        """The accessor decides what an inverted or absurd range returns; the
        verifier must not add a crash on top of an empty result."""
        _rows(mod, monkeypatch, [])
        r = _verify(svc, start, end)
        assert r["valid"] is None and r["status"] == "empty_range"

    def test_a_single_row_range_never_checks_a_link(self, svc, mod, hasher, monkeypatch):
        """`i > 0` means the first row in ANY range has its `previous_hash`
        unchecked. For a one-row range that is the whole verdict — self-hash
        only. Documented so a caller doesn't read `verified` on a 1-row range
        as 'linked to the row before it'."""
        chain = _chain(hasher, 3)
        _rows(mod, monkeypatch, [chain[2]])
        r = _verify(svc)
        assert (r["valid"], r["checked"]) == (True, 1)

    def test_counts_add_up(self, svc, mod, hasher, monkeypatch):
        chain = _chain(hasher, 3) + [_row(4), _row(5)]
        _rows(mod, monkeypatch, chain)
        r = _verify(svc)
        assert r["checked"] + r["skipped_unhashed"] == r["total_in_range"] == 5
        assert r["status"] == "verified_partial"
