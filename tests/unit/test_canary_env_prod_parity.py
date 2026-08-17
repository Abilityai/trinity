"""Regression guard: the canary env knobs must reach the backend in BOTH composes.

Bug: `CANARY_ENABLED` / `CANARY_SLACK_WEBHOOK_URL` were wired into
`docker-compose.yml` only. Every deploy path uses `docker-compose.prod.yml`
(`.github/workflows/deploy-dev.yml`, `scripts/deploy/gcp-deploy.sh`), and prod
compose launches **standalone** — no base-compose merge and no `env_file:` on any
service — so the explicit `environment:` list is the only path into the
container. A `CANARY_ENABLED=1` in the deployed `.env` was interpolated into
nothing and silently dropped, leaving the CANARY-001 harness (#411)
un-enableable on the very instance it exists to watch.

This is the #1039/#1056 packaging-gap class: a documented `.env` lever that
silently does nothing. It has now recurred five times (#1056 `VOIP_*`,
trinity-enterprise#31 `LOG_*`, #1039, #1871 `AGENT_LOG_*`, and this), so the
fix is pinned rather than trusted — deleting the compose lines must fail CI,
not pass silently.

Shape mirrors `test_1076_voice_model_config.py::test_compose_default_is_non_empty`,
the existing precedent for guarding a compose injection against a silent revert.

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`) collects
it — a guard must run where the bug would regress.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml"]

# (var, the OFF default the injection must carry)
_CANARY_VARS = [
    ("CANARY_ENABLED", "0"),
    ("CANARY_SLACK_WEBHOOK_URL", ""),
]


def _injection_lines(compose_file: str, var: str) -> list[str]:
    """Uncommented `- VAR=${VAR:-…}` lines for `var` in `compose_file`."""
    text = (_REPO_ROOT / compose_file).read_text(encoding="utf-8")
    return [
        ln
        for ln in text.splitlines()
        if f"{var}=${{{var}:-" in ln and not ln.strip().startswith("#")
    ]


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
@pytest.mark.parametrize("var,_default", _CANARY_VARS)
def test_canary_var_is_injected(compose_file, var, _default):
    """Both canary knobs must be injected in BOTH compose files.

    The prod file is the load-bearing one: it is what every deploy path uses,
    so an omission there makes the knob inert on every real instance while
    still working on a developer's laptop — which is exactly how this shipped
    unnoticed for ~2.5 months.
    """
    lines = _injection_lines(compose_file, var)
    assert lines, (
        f"{compose_file}: no `{var}=${{{var}:-…}}` injection under "
        f"backend.environment. Without it the .env lever is INERT on any deploy "
        f"using this file (the #1039/#1056 packaging-gap class)."
    )


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
@pytest.mark.parametrize("var,default", _CANARY_VARS)
def test_canary_default_stays_off(compose_file, var, default):
    """Presence must not mean enabled.

    `.env.example` promises "Production stays 0": wiring the var in is what
    makes the documented staging/dev workflow reachable, and the OFF default is
    what keeps production quiet. A future edit that ships `:-1` would silently
    start a 5-min watcher loop (and its synthetic fleet) on every install.
    """
    for ln in _injection_lines(compose_file, var):
        assert f"${{{var}:-{default}}}" in ln, (
            f"{compose_file}: `{var}` must default to {default!r} "
            f"(found: {ln.strip()!r}). Presence is required; enabled-by-default "
            f"is not — production must stay opt-in."
        )


def test_both_composes_agree():
    """dev and prod must not drift — the drift IS the bug this file guards."""
    for var, _ in _CANARY_VARS:
        per_file = {f: len(_injection_lines(f, var)) for f in _COMPOSE_FILES}
        assert len(set(per_file.values())) == 1 and 0 not in per_file.values(), (
            f"{var} is wired inconsistently across compose files: {per_file}. "
            f"A var present in dev but not prod is inert on every deploy."
        )


def test_guard_would_catch_a_revert():
    """Meta-test: prove the matcher actually fails on the pre-fix content.

    Without this, a matcher typo would make every assertion above vacuously
    true and the guard would rot into a no-op.
    """
    pre_fix = "    environment:\n      - SECRET_KEY=${SECRET_KEY}\n"
    assert not [
        ln
        for ln in pre_fix.splitlines()
        if "CANARY_ENABLED=${CANARY_ENABLED:-" in ln and not ln.strip().startswith("#")
    ], "matcher must find nothing in content that lacks the injection"

    post_fix = "      - CANARY_ENABLED=${CANARY_ENABLED:-0}\n"
    assert [
        ln
        for ln in post_fix.splitlines()
        if "CANARY_ENABLED=${CANARY_ENABLED:-" in ln and not ln.strip().startswith("#")
    ], "matcher must find the injection when present"

    commented = "      # - CANARY_ENABLED=${CANARY_ENABLED:-0}\n"
    assert not [
        ln
        for ln in commented.splitlines()
        if "CANARY_ENABLED=${CANARY_ENABLED:-" in ln and not ln.strip().startswith("#")
    ], "a commented-out injection must NOT count as wired"
