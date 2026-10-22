"""Static guard: no image or CI job declares an end-of-life Node.js line (#3886).

The agent base image installed Node 20 via ``setup_20.x``. Node 20 reached
end-of-life on 2026-04-30, so when the October 2026 Node security release
shipped fixes for a Brotli decoder OOB write and several TLS hostname-
verification bypasses, it shipped them for 22/24/26 only. Every agent container
— gemini-cli, codex, and every ``npx``-launched MCP server — kept running a
runtime that will never be patched again, and nothing in CI noticed.

The frontend and MCP server images did not have this problem only by accident:
they float on a major tag that happened to still be supported. A pinned major is
correct (an image rebuild should not silently jump majors), but a pinned major
has an expiry date, and nothing tied the pin to that date. This guard is that
tie — the same shape as ``test_1891_python_version_parity.py``: one table, every
declaration scanned, a CI failure the moment they stop agreeing.

Rules enforced:
  1. The agent base image installs exactly one nodesource ``setup_<major>.x``.
  2. Every Node declaration — ``FROM node:<major>`` in any Dockerfile, the
     base image's nodesource line, and every ``node-version:`` in
     ``.github/workflows/`` — names an even (LTS) major whose end-of-life date
     in ``_NODE_EOL`` has not passed, unless allowlisted below with a reason.
  3. A major missing from ``_NODE_EOL`` fails closed: add its date from the
     Node release schedule rather than letting it pass unchecked.

This test is deliberately date-dependent. Rule 2 compares against today, so the
day a pinned line reaches end-of-life every PR goes red — which is the point:
that is the day the pin became a security defect, not months later in a CVE
digest. The fix is a one-line major bump plus an image rebuild.

Lives in tests/unit/ as a pure static check — stdlib only, no YAML parser, no
backend/docker/database imports.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_BASE_IMAGE = "docker/base-image/Dockerfile"

# End-of-life dates per the Node.js release schedule
# (https://github.com/nodejs/Release). Even majors only: odd majors are
# never LTS and are rejected regardless of date.
_NODE_EOL = {
    20: dt.date(2026, 4, 30),
    22: dt.date(2027, 4, 30),
    24: dt.date(2028, 4, 30),
    26: dt.date(2029, 4, 30),
}

# Declarations deliberately exempt, keyed by repo-relative path, with the
# reason. Keep this tiny — every entry is a job running an unpatched runtime.
_ALLOWLIST = {
    ".github/workflows/frontend-build.yml": "#3598 — frontend CI on Node 20, bump tracked there",
    ".github/workflows/frontend-e2e.yml": "#3598 — frontend CI on Node 20, bump tracked there",
}

# `FROM node:22-alpine`, `FROM --platform=$BUILDPLATFORM node:22 AS b`,
# `FROM docker.io/library/node:22`. A floating tag (`node:lts`) has no major
# to check and is not matched.
_FROM_NODE_RE = re.compile(r"^FROM\s+(?:--\S+\s+)*(?:\S+/)?node:(\d+)", re.MULTILINE)
_NODESOURCE_RE = re.compile(r"deb\.nodesource\.com/setup_(\d+)\.x")
# `node-version: '22'` / `node-version: 22.x` / `node-version: [20, 22]`.
_NODE_VERSION_RE = re.compile(r"""^\s*node-version:\s*(.+)$""", re.MULTILINE)
_MAJOR_RE = re.compile(r"(?<![\w.])v?(\d+)(?:\.[\dx]+)*(?![\w])")

# Never scanned: vendored deps, VCS metadata, and the private submodules
# (their own repos, their own guards).
_SKIP_DIRS = {"node_modules", ".git", ".claude", "enterprise"}


def _dockerfiles() -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        found.extend(Path(dirpath) / f for f in filenames if f.startswith("Dockerfile"))
    return sorted(found)


def _workflow_majors(value: str) -> list[int]:
    """Majors in one `node-version:` value; `${{ ... }}` and `lts/*` yield none."""
    if "${{" in value:
        return []
    return [int(m) for m in _MAJOR_RE.findall(value.split("#", 1)[0])]


def _declarations() -> list[tuple[str, int]]:
    """[(repo-relative path, declared Node major), ...] across images and CI."""
    found: list[tuple[str, int]] = []
    for path in _dockerfiles():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_ROOT).as_posix()
        for major in _FROM_NODE_RE.findall(text) + _NODESOURCE_RE.findall(text):
            found.append((rel, int(major)))
    for path in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
        rel = path.relative_to(_ROOT).as_posix()
        for value in _NODE_VERSION_RE.findall(path.read_text(encoding="utf-8")):
            found.extend((rel, major) for major in _workflow_majors(value))
    return found


def _problem(major: int, today: dt.date) -> str | None:
    if major % 2:
        return f"Node {major} is an odd (non-LTS) line"
    eol = _NODE_EOL.get(major)
    if eol is None:
        return f"Node {major} has no end-of-life date in _NODE_EOL — add it"
    if eol <= today:
        return f"Node {major} reached end-of-life on {eol.isoformat()}"
    return None


def test_base_image_installs_exactly_one_node_line() -> None:
    """The base image's nodesource line is the agent runtime's single declaration."""
    text = (_ROOT / _BASE_IMAGE).read_text(encoding="utf-8")
    majors = _NODESOURCE_RE.findall(text)
    assert len(majors) == 1, (
        f"{_BASE_IMAGE} must install Node from exactly one nodesource "
        f"`setup_<major>.x` line, found {majors}"
    )


def test_no_declaration_names_an_end_of_life_node() -> None:
    """Every Node major an image or CI job declares is a supported LTS line."""
    declarations = _declarations()
    assert any(rel == _BASE_IMAGE for rel, _ in declarations), (
        "the base image's Node declaration was not found — the regex has drifted "
        "from the Dockerfile and this guard is now vacuous"
    )
    assert any(rel.startswith(".github/") for rel, _ in declarations), (
        "no `node-version:` pins found in .github/workflows/ — the regex has "
        "drifted from the workflow syntax and this guard is now vacuous"
    )

    today = dt.date.today()
    failures = [
        f"{rel}: {problem}"
        for rel, major in declarations
        if rel not in _ALLOWLIST and (problem := _problem(major, today))
    ]
    assert not failures, (
        "an image or CI job declares a Node line that receives no security "
        "fixes (#3886):\n  "
        + "\n  ".join(failures)
        + "\n\nFix: bump to a supported LTS major (and rebuild the image), or add "
        "the path to _ALLOWLIST with the issue tracking the bump."
    )


def test_allowlist_entries_are_still_needed() -> None:
    """An entry that no longer exempts an unsupported declaration must go.

    Otherwise the entry outlives its bump and silently exempts whatever that
    file declares next.
    """
    today = dt.date.today()
    declared = _declarations()
    stale = [
        rel
        for rel in _ALLOWLIST
        if not any(r == rel and _problem(m, today) for r, m in declared)
    ]
    assert not stale, f"_ALLOWLIST entries no longer exempt anything — delete them: {stale}"


def test_declaration_spellings_are_recognised() -> None:
    """The regexes see the spellings a future Dockerfile or workflow will use."""
    from_lines = (
        "FROM node:20-alpine\n"
        "FROM --platform=$BUILDPLATFORM node:20 AS build\n"
        "FROM docker.io/library/node:20-bookworm\n"
        "FROM node:lts-alpine\n"
        "FROM python:3.13-slim\n"
    )
    assert _FROM_NODE_RE.findall(from_lines) == ["20", "20", "20"]
    assert _workflow_majors("'20'") == [20]
    assert _workflow_majors("20.x") == [20]
    assert _workflow_majors("v20") == [20]
    assert _workflow_majors("'v20.19.5'") == [20]
    assert _workflow_majors("[20, 22]") == [20, 22]
    assert _workflow_majors("${{ matrix.node }}") == []
    assert _workflow_majors("lts/*") == []
    assert _workflow_majors("'24'  # pinned (#3886)") == [24]


def test_problem_rules() -> None:
    today = dt.date(2026, 10, 22)
    assert _problem(20, today) is not None  # past end-of-life
    assert _problem(23, today) is not None  # odd line
    assert _problem(28, today) is not None  # unknown: fails closed
    assert _problem(24, today) is None
    assert _problem(24, dt.date(2028, 4, 30)) is not None  # the EOL day itself
