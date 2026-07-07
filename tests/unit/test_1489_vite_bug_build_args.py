"""
Issue #1489 — VITE_BUG_REPORTING_ENABLED / VITE_BUG_INTAKE_URL never plumbed as
prod build args (finding #4 of umbrella #1485).

HelpChatWidget (#1116) reads two build-time knobs that let operators disable
in-app bug reporting or repoint it off the hosted intake. Vite statically
inlines ``import.meta.env.*`` at ``npm run build`` — so the value MUST arrive as
a build arg. But the prod build path (``docker/frontend/Dockerfile.prod`` +
``docker-compose.prod.yml``) declared only ``VITE_API_URL``, so setting
``VITE_BUG_REPORTING_ENABLED=false`` in ``.env`` had no effect and every prod
build shipped with reporting on, pointed at ``intake.abilityai.dev``, with no
off switch.

These are static guards over the packaging surface — no Docker, no backend
import (see tests/unit/test_858_dockerfile_unbuffered.py for the precedent).
They assert every layer of the fix and, critically, that the three defaulting
layers agree (Dockerfile ARG default == compose ``${VAR:-default}`` ==
HelpChatWidget code default) so an unset ``.env`` byte-reproduces today's
behavior — the invariant that lets this stay a config-only fix.

Scope: this is the regression test for #1489 itself (the two VITE_BUG_* vars),
NOT the general "every import.meta.env.VITE_* consumer has a matching build arg"
CI parity guard — that broad guard is an umbrella #1485 follow-up (plan
decision #3), out of scope here.

Issue: https://github.com/Abilityai/trinity/issues/1489
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/unit/ lives two levels under the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DOCKERFILE = REPO_ROOT / "docker" / "frontend" / "Dockerfile.prod"
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"
HELP_WIDGET = REPO_ROOT / "src" / "frontend" / "src" / "components" / "HelpChatWidget.vue"

BUG_VARS = ("VITE_BUG_REPORTING_ENABLED", "VITE_BUG_INTAKE_URL")


# ---- helpers: parse each of the three defaulting layers ----


def _dockerfile_arg_default(var: str) -> str | None:
    """Value of a real ``ARG <var>=<default>`` instruction (ignores comments)."""
    text = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(rf"^\s*ARG\s+{re.escape(var)}=(?P<v>\S+)\s*$", text, re.MULTILINE)
    return m.group("v") if m else None


def _dockerfile_has_env_passthrough(var: str) -> bool:
    """True when a real ``ENV <var>=${<var>}`` instruction exists."""
    text = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    return bool(
        re.search(
            rf"^\s*ENV\s+{re.escape(var)}=\$\{{{re.escape(var)}\}}\s*$",
            text,
            re.MULTILINE,
        )
    )


def _compose_arg_default(var: str) -> str | None:
    """Default from a ``- <var>=${<var>:-<default>}`` line in build.args."""
    text = COMPOSE_PROD.read_text(encoding="utf-8")
    m = re.search(
        rf"-\s*{re.escape(var)}=\$\{{{re.escape(var)}:-(?P<v>[^}}]*)\}}",
        text,
    )
    return m.group("v") if m else None


def _code_default(var: str) -> str | None:
    """The source-of-truth default from HelpChatWidget.vue."""
    text = HELP_WIDGET.read_text(encoding="utf-8")
    if var == "VITE_BUG_INTAKE_URL":
        # const BUG_INTAKE_URL = import.meta.env.VITE_BUG_INTAKE_URL || '<default>'
        m = re.search(
            r"import\.meta\.env\.VITE_BUG_INTAKE_URL\s*\|\|\s*'(?P<v>[^']+)'", text
        )
        return m.group("v") if m else None
    # String(import.meta.env.VITE_BUG_REPORTING_ENABLED ?? '<default>')
    m = re.search(
        r"import\.meta\.env\.VITE_BUG_REPORTING_ENABLED\s*\?\?\s*'(?P<v>[^']+)'", text
    )
    return m.group("v") if m else None


# ---- Dockerfile.prod: ARG + ENV, before the build ----


@pytest.mark.unit
@pytest.mark.parametrize("var", BUG_VARS)
def test_dockerfile_declares_arg_and_env(var: str) -> None:
    """Dockerfile.prod must declare ``ARG <var>=<default>`` and export it as
    ``ENV <var>=${<var>}`` so Vite sees it at build time (#1489)."""
    assert FRONTEND_DOCKERFILE.is_file(), f"missing {FRONTEND_DOCKERFILE}"
    assert _dockerfile_arg_default(var) is not None, (
        f"docker/frontend/Dockerfile.prod is missing `ARG {var}=<default>` — "
        "the knob never reaches the Vite build (#1489)."
    )
    assert _dockerfile_has_env_passthrough(var), (
        f"docker/frontend/Dockerfile.prod is missing `ENV {var}=${{{var}}}` — an "
        "ARG alone is not visible to `npm run build` as import.meta.env (#1489)."
    )


@pytest.mark.unit
def test_dockerfile_env_before_npm_build() -> None:
    """Both ENV pass-throughs must precede ``RUN npm run build`` — Vite inlines
    import.meta.env at build time, so an ENV after the build is a no-op (#1489)."""
    text = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    build_m = re.search(r"^\s*RUN\s+npm\s+run\s+build\b", text, re.MULTILINE)
    assert build_m is not None, "Dockerfile.prod has no `RUN npm run build`"
    build_pos = build_m.start()
    for var in BUG_VARS:
        env_m = re.search(
            rf"^\s*ENV\s+{re.escape(var)}=\$\{{{re.escape(var)}\}}\s*$",
            text,
            re.MULTILINE,
        )
        assert env_m is not None and env_m.start() < build_pos, (
            f"`ENV {var}` must appear before `RUN npm run build` in "
            "Dockerfile.prod, else Vite bakes the default (#1489)."
        )


# ---- compose build.args ----


@pytest.mark.unit
@pytest.mark.parametrize("var", BUG_VARS)
def test_compose_passes_build_arg(var: str) -> None:
    """docker-compose.prod.yml frontend build.args must forward each knob with a
    ``${<var>:-<default>}`` fallback (#1489)."""
    assert _compose_arg_default(var) is not None, (
        f"docker-compose.prod.yml frontend build.args is missing "
        f"`{var}=${{{var}:-...}}` — the .env value never reaches the image build "
        "(#1489)."
    )


# ---- the load-bearing invariant: all three layers agree ----


@pytest.mark.unit
@pytest.mark.parametrize("var", BUG_VARS)
def test_defaults_agree_across_layers(var: str) -> None:
    """Dockerfile ARG default == compose ${VAR:-default} == HelpChatWidget code
    default. Keeping the three in lockstep is what makes an unset .env
    byte-reproduce today's behavior (#1489, plan decision #2)."""
    arg = _dockerfile_arg_default(var)
    compose = _compose_arg_default(var)
    code = _code_default(var)
    assert code is not None, (
        f"could not read the {var} default from HelpChatWidget.vue — the "
        "source-of-truth the build defaults must mirror (#1489)."
    )
    assert arg == code, (
        f"Dockerfile.prod ARG default for {var} ({arg!r}) drifted from the code "
        f"default ({code!r}); an unset .env would no longer reproduce today (#1489)."
    )
    assert compose == code, (
        f"compose build.arg default for {var} ({compose!r}) drifted from the code "
        f"default ({code!r}); an unset .env would no longer reproduce today (#1489)."
    )
