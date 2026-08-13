"""#925 — the client-side cron validator must mirror the backend grammar exactly.

The schedule form pre-validates cron expressions in the browser
(``src/frontend/src/utils/cronValidation.js``), a hand-rolled mirror of THIS
backend's acceptance grammar: ``services/schedule_validation.py``'s strict
5-field split + ``_dow_to_apscheduler`` translation + APScheduler 3.11's
``CronTrigger`` field expressions. The contract between the two is the shared
fixture ``tests/fixtures/cron-grammar-cases.json`` — verdicts PROBED against the
live validator at generation time, never hand-typed — asserted row-for-row by
both this pytest (against the real ``validate_cron_expression``) and the vitest
spec ``src/frontend/tests/unit/cronValidation.spec.js`` (against the mirror).

This test is the DRIFT ALARM: it runs in the backend CI environment with the
pinned APScheduler, so an upgrade that changes the cron grammar fails HERE
loudly instead of silently desynchronizing the client mirror (the damaging
direction being client-invalid/server-valid false warnings in the schedules UI).

If this test goes red after an APScheduler (or validator) change:
  1. Re-probe every fixture row against the new grammar and rewrite the verdicts
     (never hand-edit a verdict). From the repo root::

         python3 - <<'EOF'
         import importlib.util, json, pathlib
         spec = importlib.util.spec_from_file_location(
             "sv", "src/backend/services/schedule_validation.py")
         sv = importlib.util.module_from_spec(spec); spec.loader.exec_module(sv)
         p = pathlib.Path("tests/fixtures/cron-grammar-cases.json")
         rows = json.loads(p.read_text())
         for r in rows:
             try:
                 sv.validate_cron_expression(r["expr"], "UTC"); r["valid"] = True
             except sv.ScheduleValidationError:
                 r["valid"] = False
         p.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
         EOF

  2. Port the grammar change into ``cronValidation.js`` and re-run the vitest
     spec until it agrees with the regenerated fixture.

Quirk rows (prefix-matched names ``MON/999``/``jan/0``/``lastx``, the
comma-translation asymmetry ``0-6,1`` vs bare ``0-6``, the falsy-zero
``0-0/2``) are pinned deliberately — parity outranks tidiness; do not "fix"
them on either side. Rows carrying ``divergence: "client-stricter"`` record the
SERVER verdict here; the vitest spec asserts the client's deliberate stricter
verdict for those (documented: Python ``\\d``/``int()`` accept Unicode digits,
the client is ASCII-only).

Backend RUNTIME is untouched by #925 — this file is test-tree only.
"""

import json
from pathlib import Path

import pytest

from services.schedule_validation import (
    ScheduleValidationError,
    validate_cron_expression,
)

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "cron-grammar-cases.json"


def _load_rows():
    rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows, "fixture must be a non-empty array"
    return rows


_ROWS = _load_rows()


def _row_id(row):
    expr = row["expr"]
    return "<None>" if expr is None else repr(expr)


@pytest.mark.parametrize("row", _ROWS, ids=[_row_id(r) for r in _ROWS])
def test_fixture_row_matches_live_validator(row):
    """Every fixture verdict re-proven against the REAL backend validator."""
    expr = row["expr"]
    try:
        validate_cron_expression(expr, "UTC")
        actual = True
    except ScheduleValidationError:
        actual = False
    assert actual == row["valid"], (
        f"Grammar drift: {row['expr']!r} ({row['note']}) — fixture says "
        f"valid={row['valid']}, live validate_cron_expression says {actual}. "
        f"Re-probe the fixture and update the client mirror (see module docstring)."
    )


def test_fixture_covers_both_verdicts_and_the_quirks():
    """Structural floor: the fixture must keep exercising both verdicts and the
    named quirk rows whose absence would let the client mirror drift silently."""
    by_expr = {r["expr"]: r["valid"] for r in _ROWS}
    # Both verdict classes present in force.
    assert sum(1 for r in _ROWS if r["valid"]) >= 40
    assert sum(1 for r in _ROWS if not r["valid"]) >= 40
    # The load-bearing quirk rows (probed upstream behaviours, not typos).
    assert by_expr["0-0/2 * * * *"] is True      # falsy-zero last → span = max
    assert by_expr["5-5/1 * * * *"] is False     # truthy last → span 0
    assert by_expr["0 9 * * MON/999"] is True    # name prefix match drops the step
    assert by_expr["0 9 * * 0-6,1"] is True      # comma branch keeps ranges raw…
    assert by_expr["0 9 * * 0-6"] is False       # …bare range maps to sun-sat (inverted)
    assert by_expr["0 9 lastx * *"] is True      # 'last' is a prefix match
    # The four shipped presets stay valid.
    for preset in ("0 9 * * *", "0 9 * * 1", "0 */6 * * *", "*/30 * * * *"):
        assert by_expr[preset] is True, f"preset {preset!r} must stay fixture-valid"


def test_divergence_rows_are_server_valid_client_stricter_only():
    """A divergence row records the SERVER verdict; the only sanctioned direction
    is client-stricter (server-valid input the client rejects). A server-INVALID
    divergence row would mean the client fails open on something the server
    rejects — that is just a mirror bug, not a documented divergence."""
    for row in _ROWS:
        if row.get("divergence"):
            assert row["divergence"] == "client-stricter"
            assert row["valid"] is True, (
                f"divergence row {row['expr']!r} must be server-valid"
            )
