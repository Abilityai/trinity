"""Unit regression test for #1486 — DO_NOT_TRACK truthiness.

Bug: `config.py` disabled the operator-intake POST only when DO_NOT_TRACK was
one of the exact strings `{"1", "true", "True"}`, so the very cross-tool
convention `.env.example` cites (consoledonottrack.com — "ANY non-zero value
disables tracking") was partly broken: `DO_NOT_TRACK=yes|on|2|TRUE` leaked the
intake POST despite an obvious opt-out intent.

Fix (src/backend/config.py) — convention-aligned "tracking-allowed" whitelist:
    OPERATOR_INTAKE_ENABLED = (
        os.getenv("OPERATOR_INTAKE_ENABLED", "true").lower() == "true"
        and os.getenv("DO_NOT_TRACK", "0").strip().lower() in ("0", "", "false")
    )

Now any value NOT in {"0", "", "false"} (case/space-insensitive) disables the
outbound submission; unset → "0" → tracking allowed (unchanged).

These tests exec the REAL config.py fresh under a controlled environment (same
pattern as tests/unit/test_1076_voice_model_config.py). No network, no Redis —
a dummy credentialed REDIS_URL only satisfies config.py's import-time guard
(Issue #589).

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`) collects
it — the regression guard must run where the bug would regress. A revert to the
`{1, true, True}` tuple fails the parametrized truthy cases loudly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _REPO_ROOT / "src" / "backend" / "config.py"


def _load_config_with_env(monkeypatch, env: dict[str, str | None]):
    """Exec src/backend/config.py fresh under `env` and return the module."""
    # config.py's only hard import-time requirement is a credentialed REDIS_URL.
    monkeypatch.setenv("REDIS_URL", "redis://backend:devpassword@localhost:6379/0")
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location(
        f"_config_under_test_{abs(hash(frozenset(env.items())))}", str(_CONFIG_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Values that MUST disable the intake POST. The pre-fix tuple only caught the
# first three — the rest are the regression the fix repairs.
_DISABLING_VALUES = ["1", "true", "True", "TRUE", "yes", "on", "2", " 1 "]

# Values that MUST leave intake on (tracking allowed), given OPERATOR_INTAKE_ENABLED
# defaults to "true". `None` = var entirely unset (the os.getenv-default path).
_ALLOWING_VALUES = ["0", "", "false", "False", "FALSE", " 0 ", None]


@pytest.mark.parametrize("dnt", _DISABLING_VALUES)
def test_do_not_track_disables_intake(monkeypatch, dnt):
    """Any non-{0,'',false} DO_NOT_TRACK value disables the intake POST (#1486)."""
    cfg = _load_config_with_env(monkeypatch, {"DO_NOT_TRACK": dnt})
    assert cfg.OPERATOR_INTAKE_ENABLED is False, (
        f"DO_NOT_TRACK={dnt!r} must disable operator intake — regression of "
        "#1486 (the exact-tuple {'1','true','True'} check leaked yes/on/2/TRUE)."
    )


@pytest.mark.parametrize("dnt", _ALLOWING_VALUES)
def test_do_not_track_allows_intake(monkeypatch, dnt):
    """0/empty/false/unset DO_NOT_TRACK keeps intake enabled (default true)."""
    cfg = _load_config_with_env(monkeypatch, {"DO_NOT_TRACK": dnt})
    assert cfg.OPERATOR_INTAKE_ENABLED is True, (
        f"DO_NOT_TRACK={dnt!r} must leave operator intake enabled "
        "(tracking-allowed), given OPERATOR_INTAKE_ENABLED defaults true."
    )


def test_operator_intake_enabled_false_still_wins(monkeypatch):
    """OPERATOR_INTAKE_ENABLED=false disables intake regardless of DO_NOT_TRACK."""
    cfg = _load_config_with_env(
        monkeypatch, {"OPERATOR_INTAKE_ENABLED": "false", "DO_NOT_TRACK": "0"}
    )
    assert cfg.OPERATOR_INTAKE_ENABLED is False
