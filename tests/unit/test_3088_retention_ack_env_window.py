"""
The retention approval binds to the window the sweep will run on (#3088).

`POST /api/settings/retention/acknowledge` rejects (409) an approval whose
`window_days` is not the window in force. It read that window with
`db.get_setting_value(key, OPS_SETTINGS_DEFAULTS[key])`, which skips the
environment tier, while the metric-points sweep
(`cleanup_service._read_retention_setting`) and `GET /api/settings/retention`
both resolve `metrics_retention_days` row -> env -> code default. On an install
booted with `METRICS_RETENTION_DAYS` set there is no row (the #2085 seeder
skips an env-supplied key), so approving the pending prune at the env window
answered 409 "currently 365 days" forever and the sweep stayed blocked.

Only the keys in `config.ENV_BACKED_OPS_KEYS` move to the resolver. Every other
key keeps its verbatim row read, pinned below: an empty-string row on a
soft-delete key makes its sweep read `0` (disabled), and the resolver would
report the default instead and record an approval no sweep ever consumes.

Every test drives the real handler. Both `db` bindings are patched — the
router's and the one `settings_service` reads — and every value is a
non-default, so a reader that falls through to the code default cannot pass.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import HTTPException  # noqa: E402

import services.cleanup_service as _CS  # noqa: E402
import services.retention_guard as _RG  # noqa: E402
import services.settings_service as _SS  # noqa: E402
from models import RetentionAcknowledge  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"


def _load_isolated(name: str, relpath: str):
    # Same isolation as test_ent671_retention_metric_rows.py: a direct file
    # load avoids the routers/__init__ chain a sibling test pollutes.
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RS = _load_isolated("retention_settings_3088", "routers/settings/retention.py")

METRICS = "metrics_retention_days"
AGENTS = "agent_soft_delete_retention_days"

# Deliberately none of these is a code default (365 for METRICS, 180 for AGENTS).
ENV_WINDOW = 45
ROW_WINDOW = 400


def _admin():
    u = MagicMock()
    u.role = "admin"
    u.username = "operator"
    u.connector_agent = None
    u.agent_name = None
    u.mcp_scope = None  # #2323: an interactive human, not a truthy MagicMock
    return u


def _request():
    r = MagicMock()
    r.client.host = "127.0.0.1"
    r.scope = {"path": "/api/settings/retention/acknowledge"}
    return r


def _ack(key, window_days, *, rows=None, env=None, sweep_window=None):
    """Drive the real acknowledge handler.

    Returns `(response, recorded)` where `recorded` is the list of
    `(key, window)` pairs handed to `record_acknowledgement` — the approval the
    guard will later honour. With `sweep_window` (a list), the window the
    sweep's own reader resolves under the same rows + env is appended to it.
    """
    rows = rows or {}
    db = MagicMock()
    db.get_setting_value.side_effect = lambda k, default=None: rows.get(k, default)
    recorded = []
    audit = MagicMock()
    audit.log = AsyncMock()

    with patch.object(_RS, "db", db), \
         patch.object(_SS, "db", db), \
         patch.object(_RS, "platform_audit_service", audit), \
         patch.object(_RG, "record_acknowledgement",
                      side_effect=lambda k, w: recorded.append((k, w))), \
         patch.dict("os.environ", env or {}, clear=False):
        if sweep_window is not None:
            sweep_window.append(_CS._read_retention_setting(key))
        try:
            res = asyncio.run(_RS.acknowledge_retention_prune(
                body=RetentionAcknowledge(key=key, window_days=window_days),
                request=_request(),
                current_user=_admin(),
            ))
        except HTTPException as exc:
            return exc, recorded
    return res, recorded


@pytest.fixture(autouse=True)
def _no_metric_env(monkeypatch):
    # A developer shell exporting the variable must not leak into a test.
    monkeypatch.delenv("METRICS_RETENTION_DAYS", raising=False)


def test_env_window_with_no_row_is_approvable():
    """The reported reproduction: env supplies the window, no row exists."""
    res, recorded = _ack(
        METRICS, ENV_WINDOW, env={"METRICS_RETENTION_DAYS": str(ENV_WINDOW)},
    )
    assert not isinstance(res, HTTPException), (
        f"approving the env-supplied window answered {res.status_code}: {res.detail}"
    )
    assert res == {"success": True, "key": METRICS, "window_days": ENV_WINDOW}
    assert recorded == [(METRICS, ENV_WINDOW)]


def test_the_approval_names_the_window_the_sweep_reads():
    """The binding exists so an approval authorizes exactly the prune that
    runs: the recorded window must equal what the sweep's reader resolves."""
    sweep = []
    res, recorded = _ack(
        METRICS, ENV_WINDOW,
        env={"METRICS_RETENTION_DAYS": str(ENV_WINDOW)}, sweep_window=sweep,
    )
    assert sweep == [ENV_WINDOW]
    assert recorded == [(METRICS, sweep[0])]


def test_a_stored_row_wins_over_env():
    env = {"METRICS_RETENTION_DAYS": str(ENV_WINDOW)}
    rows = {METRICS: str(ROW_WINDOW)}

    res, recorded = _ack(METRICS, ROW_WINDOW, rows=rows, env=env)
    assert res == {"success": True, "key": METRICS, "window_days": ROW_WINDOW}
    assert recorded == [(METRICS, ROW_WINDOW)]

    # ...and the env window, which is NOT in force, cannot be approved.
    res, recorded = _ack(METRICS, ENV_WINDOW, rows=rows, env=env)
    assert isinstance(res, HTTPException) and res.status_code == 409
    assert recorded == []


def test_env_does_not_loosen_the_binding():
    """With env in force, neither the code default nor any other window is
    approvable — the approval still has to name the window about to run."""
    res, recorded = _ack(
        METRICS, 365, env={"METRICS_RETENTION_DAYS": str(ENV_WINDOW)},
    )
    assert isinstance(res, HTTPException) and res.status_code == 409
    assert str(ENV_WINDOW) in res.detail
    assert recorded == []


def test_empty_row_on_a_soft_delete_key_is_still_422():
    """The narrow scope. The agent purge reads this row verbatim, so `""` is
    `0` = disabled; the resolver would call it absent and report 180. Approving
    180 must not be recorded — nothing would consume it, and it would later
    authorize a write of the default window without a fresh approval."""
    res, recorded = _ack(AGENTS, 180, rows={AGENTS: ""})
    assert isinstance(res, HTTPException) and res.status_code == 422
    assert recorded == []


def test_a_soft_delete_row_is_still_approvable_at_its_own_window():
    """The untouched reader still works for the keys it keeps."""
    res, recorded = _ack(AGENTS, 77, rows={AGENTS: "77"})
    assert res == {"success": True, "key": AGENTS, "window_days": 77}
    assert recorded == [(AGENTS, 77)]
