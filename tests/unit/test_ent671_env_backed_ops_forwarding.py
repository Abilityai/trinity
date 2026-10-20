"""
The env-backed ops keys reach the backend container UNSET unless an operator
sets them (trinity-enterprise#671).

`config.ENV_BACKED_OPS_KEYS` resolve `system_settings` row -> environment ->
code default, and `GET /api/settings/retention` reports which tier answered.
The Settings → Retention rows for the two metric knobs are read-only when that
tier is `env` — an operator-pinned value — and say "unset the variable and
restart to edit it here".

That only means something if an unset variable arrives unset. It did not:
every compose file forwarded `${METRICS_DAILY_POINT_CAP:-100000}` (a non-empty
DEFAULT), and `.env.example` — which `start.sh` copies to `.env` on a fresh
install — set both metric variables outright. So on essentially every install
the source was `env`, the rows were locked, and the advice was false: removing
the line from `.env` just brought back the compose default. The same held for
`INTER_AGENT_MAX_CHAIN_DEPTH`.

Pure stdlib over the raw files (the unexpanded `${VAR:-default}` strings are
the subject), keyed off `config.ENV_BACKED_OPS_KEYS`, so a fourth env-backed
key is covered the day it is added.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import config

_ROOT = Path(__file__).resolve().parents[2]

# The compose files that launch the backend: each must forward every
# env-backed variable (or the .env lever is inert there — the #1039/#1056
# class), and forward it with an EMPTY default.
_BACKEND_COMPOSE = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml")

_VARS = sorted(config.ENV_BACKED_OPS_KEYS.values())


def _assignments(text: str, var: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(rf"^\s*-\s*{var}=", ln)]


def test_the_guard_sees_the_env_backed_keys():
    assert {"METRICS_RETENTION_DAYS", "METRICS_DAILY_POINT_CAP"} <= set(_VARS)


@pytest.mark.parametrize("compose", _BACKEND_COMPOSE)
@pytest.mark.parametrize("var", _VARS)
def test_backend_compose_forwards_the_variable_with_an_empty_default(compose, var):
    lines = _assignments((_ROOT / compose).read_text(encoding="utf-8"), var)
    assert len(lines) == 1, f"{compose}: expected exactly one `- {var}=...` line, found {lines}"
    assert re.match(rf"^\s*-\s*{var}=\$\{{{var}:-\}}(\s|$)", lines[0]), (
        f"{compose}: {lines[0].strip()!r} forwards a non-empty default. An unset "
        f"{var} must reach the container unset, or GET /api/settings/retention "
        f"reports `source: env` for a value nobody chose — use `${{{var}:-}}`."
    )


@pytest.mark.parametrize("var", _VARS)
def test_no_other_compose_file_supplies_a_default_either(var):
    for compose in sorted(_ROOT.glob("docker-compose*.yml")):
        for line in _assignments(compose.read_text(encoding="utf-8"), var):
            assert re.match(rf"^\s*-\s*{var}=\$\{{{var}:-\}}(\s|$)", line), (
                f"{compose.name}: {line.strip()!r} supplies a default for {var}"
            )


@pytest.mark.parametrize("var", _VARS)
def test_env_example_leaves_the_variable_commented(var):
    """`start.sh` copies .env.example to .env on a fresh install, so a line set
    here is a value every fresh install pins from the environment."""
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(rf"^\s*{var}=", text, re.M), (
        f".env.example sets {var}; leave it commented (`# {var}=...`)"
    )
    assert re.search(rf"^#\s*{var}=", text, re.M), (
        f".env.example no longer documents {var} at all — keep the commented line"
    )
