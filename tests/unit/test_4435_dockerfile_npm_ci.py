"""
Issue #4435 — Dockerfiles must install project npm dependencies from the lockfile.

`docker/frontend/Dockerfile` ran `rm -f package-lock.json && npm install`, so
every dev-stack build resolved each semver range to whatever was newest on the
registry: no review, no soak time, and a dependency tree that differed from the
shipped bundle. This guard fails when any tracked Dockerfile

  * installs project dependencies with `npm install` / `npm i` / `npm add`
    instead of `npm ci`, or
  * deletes a lockfile.

Global tool installs (`npm install -g <pkg>@<version>`) are out of scope.

True unit test — no Docker, no backend.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LOCKFILES = r"(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml)"
_RM_LOCKFILE = re.compile(r"\brm\b[^;&|]*" + LOCKFILES)
# Command position only, so `echo "... npm install ..."` prose does not match.
_NPM_INSTALL = re.compile(
    r"(?:^|[;&|(]|\b(?:then|do|else))\s*npm\s+(?:install|i|add)\b([^;&|]*)"
)
_GLOBAL = re.compile(r"(?:^|\s)(?:-g|--global)(?:\s|$)")


def _run_commands(text: str) -> list[str]:
    """Return each RUN instruction with continuations joined and comments dropped."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    joined = re.sub(r"\\\s*\n", " ", "\n".join(lines))
    return [
        m.group(1)
        for m in re.finditer(r"^\s*RUN\s+(.*)$", joined, re.MULTILINE | re.IGNORECASE)
    ]


def violations(text: str) -> list[str]:
    found = []
    for cmd in _run_commands(text):
        if _RM_LOCKFILE.search(cmd):
            found.append(f"deletes a lockfile: {cmd.strip()}")
        for m in _NPM_INSTALL.finditer(cmd):
            if not _GLOBAL.search(m.group(1) + " "):
                found.append(f"npm install instead of npm ci: {cmd.strip()}")
    return found


@pytest.mark.parametrize(
    "dockerfile",
    [
        "RUN rm -f package-lock.json && npm install",
        "RUN npm install",
        "RUN npm i --omit=dev",
        "RUN apk add git \\\n    && npm install --no-audit",
        "RUN rm package-lock.json",
        "RUN if true; then npm install; fi",
    ],
)
def test_matcher_flags_floating_installs(dockerfile: str) -> None:
    assert violations(dockerfile)


@pytest.mark.parametrize(
    "dockerfile",
    [
        "RUN npm ci",
        "RUN npm ci --omit=dev",
        "RUN npm install -g @openai/codex@0.147.0",
        "RUN npm install -g --include=optional \\\n    \"@anthropic-ai/claude-code@${V}\"",
        "# RUN npm install\nRUN npm ci",
        "COPY package.json package-lock.json ./",
        'RUN echo "native dep missing after npm install: retrying"',
    ],
)
def test_matcher_allows_lockfile_installs(dockerfile: str) -> None:
    assert violations(dockerfile) == []


def _tracked_dockerfiles() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*Dockerfile*"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [REPO_ROOT / p for p in out]


def test_dockerfiles_are_discovered() -> None:
    # An empty list would make the next test pass vacuously.
    assert REPO_ROOT / "docker" / "frontend" / "Dockerfile" in _tracked_dockerfiles()


def test_no_dockerfile_floats_npm_dependencies() -> None:
    bad = {
        str(p.relative_to(REPO_ROOT)): v
        for p in _tracked_dockerfiles()
        if p.is_file() and (v := violations(p.read_text()))
    }
    assert bad == {}, f"Install npm dependencies with `npm ci` from the lockfile: {bad}"
