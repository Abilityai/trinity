"""Static guard: CI runs the Python version the images actually ship (#1891).

Every Trinity image is built ``FROM python:3.13``; every CI job that executes the
test suite used to pin ``python-version: '3.11'``. A suite validated on a runtime
two minor versions behind the shipped one cannot, by construction, catch the
stdlib-removal class of defect — and that class had already shipped twice:

  * ``crypt`` removed in 3.13 → host-side SSH password hashing was dead code
    (only surfaced as a named 400 in #1615),
  * ``audioop`` removed in 3.13 → the VoIP µ-law path needed the ``audioop-lts``
    pin.

A 3.11 runner still has both modules. The import succeeds, the test passes, and
the failure appears only inside the image. No amount of additional test-writing
closes that gap while the runner version differs from the image version.

Bumping the six literals fixed the drift that existed on 2026-07-31; it does
nothing to prevent the next one. The root cause is **two hand-maintained version
declarations with nothing tying them together**, so this guard is the durable
half of the fix — the same shape as ``test_1560_agent_redis_key_parity.py`` and
``test_1871_log_config_parity.py``: two lists that must agree, and a CI failure
the moment they stop agreeing.

Rules enforced:
  1. All three image Dockerfiles pin the SAME ``python:<major>.<minor>``.
  2. Every ``python-version:`` in ``.github/workflows/`` equals that pin, unless
     the workflow is allowlisted below with a stated reason.
  3. Every version-conditional dependency the image installs
     (``pkg; python_version >= X``) is also a test requirement — matching the
     interpreter is only half of "CI runs what we ship". See the block above
     ``test_version_conditional_deps_are_installed_in_ci`` for the 7 tests this
     rule was written after losing.

Rule 2 is deliberately a **scan of every workflow file**, not a check of six
known line numbers: a seventh workflow added next month with a stale pin has to
fail this test, or the guard only documents today's drift instead of closing the
class.

Lives in tests/unit/ as a pure static check — stdlib only, no YAML parser, no
backend/docker/database imports.
"""
from __future__ import annotations

import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"

# The images whose runtime the test suite is supposed to be validating. All
# three must agree with each other AND with CI.
_IMAGE_DOCKERFILES = (
    "docker/backend/Dockerfile",
    "docker/scheduler/Dockerfile",
    "docker/base-image/Dockerfile",
)

# Workflows deliberately NOT tied to the image pin, with the reason. Keep this
# list tiny — every entry is a job whose failures the image can no longer catch.
_WORKFLOW_ALLOWLIST = {
    # publish-cli.yml builds and publishes the PyPI CLI package. Its version is
    # governed by what CLI *consumers* run, not by what Trinity's own containers
    # run, and the CLI declares `requires-python = ">=3.10"`.
    "publish-cli.yml": "CLI packaging — governed by PyPI consumers, not the image runtime",
}

# `FROM python:3.13-slim`, `FROM python:3.13-slim-bookworm`, `FROM python:3.13`.
# Captures major.minor only: patch/variant drift between images is fine, a
# different *minor* is exactly the divergence this guard exists to catch.
_FROM_PYTHON_RE = re.compile(r"^FROM\s+python:(\d+\.\d+)", re.MULTILINE)

# `python-version: '3.13'` / `python-version: "3.13"` / `python-version: 3.13`.
# Values that are expressions (`${{ ... }}`) or matrix refs are skipped by the
# capture group simply not matching — see `_workflow_pins`.
_PYTHON_VERSION_RE = re.compile(
    r"""^\s*python-version:\s*['"]?(\d+\.\d+)['"]?\s*$""", re.MULTILINE
)


def _image_pins() -> dict[str, str]:
    """Map Dockerfile path -> the major.minor Python it is built FROM."""
    pins: dict[str, str] = {}
    for rel in _IMAGE_DOCKERFILES:
        path = _ROOT / rel
        assert path.is_file(), f"missing image Dockerfile: {rel}"
        found = _FROM_PYTHON_RE.findall(path.read_text(encoding="utf-8"))
        assert found, f"no `FROM python:<version>` found in {rel}"
        # A multi-stage image may repeat the base; they must not disagree.
        assert len(set(found)) == 1, f"{rel} pins conflicting Python versions: {found}"
        pins[rel] = found[0]
    return pins


def _workflow_pins() -> dict[str, list[tuple[int, str]]]:
    """Map workflow filename -> [(line number, pinned major.minor), ...]."""
    pins: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
        hits: list[tuple[int, str]] = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _PYTHON_VERSION_RE.match(line)
            if match:
                hits.append((lineno, match.group(1)))
        if hits:
            pins[path.name] = hits
    return pins


def test_all_images_pin_the_same_python() -> None:
    """Backend, scheduler and base image must ship one runtime, not three."""
    pins = _image_pins()
    assert len(set(pins.values())) == 1, (
        "Trinity images disagree on their Python runtime — CI can only be held to "
        f"one of them: {pins}"
    )


def test_ci_python_version_matches_the_shipped_image() -> None:
    """Every CI `python-version:` equals the image pin (or is allowlisted).

    This is the whole point of the guard: bumping the image without bumping CI
    (or the reverse) must turn CI red on the PR that does it, not months later
    inside a container.
    """
    image_version = next(iter(set(_image_pins().values())))
    workflow_pins = _workflow_pins()

    assert workflow_pins, (
        "no `python-version:` pins found in .github/workflows/ — the regex has "
        "drifted from the workflow syntax and this guard is now vacuous"
    )

    mismatches = [
        f"{name}:{lineno} pins {version} (images ship {image_version})"
        for name, hits in workflow_pins.items()
        if name not in _WORKFLOW_ALLOWLIST
        for lineno, version in hits
        if version != image_version
    ]

    assert not mismatches, (
        "CI would validate a Python version the images do not ship, which cannot "
        "catch the stdlib-removal class (#1891):\n  "
        + "\n  ".join(mismatches)
        + f"\n\nFix: bump these to '{image_version}', or add the workflow to "
        "_WORKFLOW_ALLOWLIST with the reason it is exempt."
    )


def test_allowlist_entries_still_exist() -> None:
    """A stale allowlist entry silently exempts nothing — delete it.

    Without this, renaming or deleting an exempted workflow leaves a dead entry
    that a future workflow could inherit by name and be exempted for free.
    """
    stale = [name for name in _WORKFLOW_ALLOWLIST if not (_WORKFLOWS / name).is_file()]
    assert not stale, (
        f"_WORKFLOW_ALLOWLIST references workflows that no longer exist: {stale}"
    )


# ---------------------------------------------------------------------------
# Version-conditional dependencies must reach CI too
# ---------------------------------------------------------------------------
#
# Matching the interpreter is only half of "CI runs what we ship". Python 3.13
# removed `audioop`, so the backend image pins a replacement:
#
#     docker/backend/Dockerfile:  "audioop-lts; python_version >= '3.13'"
#
# CI does NOT install the image — it installs tests/requirements-test.txt. When
# the bump landed, that file had no such pin, so on 3.13
# `tests/unit/test_voip_audio.py` skipped at collection and 7 codec tests
# silently stopped running. Nothing went red: the `regression diff` job compares
# FAILURES, not test counts, so a whole vanished module is invisible to it.
#
# That is the same "two declarations, nothing tying them together" shape as the
# version pin above — and the same bug class (`crypt`, `audioop`) this issue
# exists to close, one level along. A version-conditional dependency that the
# image needs and the test environment lacks means CI is again running a
# different environment than production, just via a package instead of a
# minor version.

# Matches a PEP 508 environment marker in BOTH forms the repo uses:
#   Dockerfile (shell, so the spec must be quoted):
#       "audioop-lts; python_version >= '3.13'"
#   requirements.txt (no shell, so no outer quotes and the version may be bare):
#       audioop-lts; python_version >= "3.13"
# The outer quotes are optional here on purpose — requiring them is what made an
# earlier draft blind to `docker/scheduler/requirements.txt` entirely.
_MARKER_RE = re.compile(
    r"""["']?([A-Za-z0-9._-]+)["']?\s*;\s*python_version\s*(?:>=?|==)\s*["']?(\d+\.\d+)["']?"""
)

_TEST_REQUIREMENTS = _ROOT / "tests" / "requirements-test.txt"

# EVERY place an image installs backend-side Python from. Enumerating the
# SURFACE, not one file: `learnings.md` 2026-07-29 (#1871) is the entry about a
# guard that watched one of two creation APIs while claiming to close the class,
# and rule 1 above already enumerates all three Dockerfiles — rule 3 scanning
# only the backend Dockerfile would be the same mistake inside the same file.
# The scheduler installs from a requirements FILE, not an inline pip line, which
# is also why the regex above cannot demand shell quoting.
_IMAGE_DEP_SOURCES = (
    "docker/backend/Dockerfile",
    "docker/scheduler/requirements.txt",
)

# docker/base-image/Dockerfile is deliberately NOT scanned: those packages are the
# AGENT container's runtime, and the backend unit suite never imports them. The
# agent-side modules it does import (`model_context`, `credential_paths`) are
# vendored byte-identically and stdlib-only by contract (Invariant #5), so they
# bring no third-party dependency into CI.

# Deps an image needs that the unit suite provably does not exercise. Add here
# ONLY with a reason — the default must be "CI installs what the image has".
_CI_EXEMPT_CONDITIONAL_DEPS: dict[str, str] = {}



def _declared_test_requirements() -> set[str]:
    """Requirement NAMES actually declared in tests/requirements-test.txt.

    Deliberately not a substring search over the file. The first version of this
    guard did exactly that and was vacuous on arrival: the explanatory comment
    directly above the pin contains the string "audioop-lts", so deleting the
    real requirement line still left a match and the guard stayed green. Comments
    are stripped here so only a genuine declaration counts.
    """
    names: set[str] = set()
    for raw in _TEST_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # Strip environment markers, extras and version specifiers:
        #   `audioop-lts; python_version >= '3.13'` -> `audioop-lts`
        #   `hypothesis[zoneinfo]>=6.0`             -> `hypothesis`
        name = re.split(r"[;\[<>=!~ ]", line, maxsplit=1)[0].strip().lower()
        if name:
            names.add(name)
    return names


def _conditional_deps() -> dict[str, tuple[str, str]]:
    """Map dep name -> (required python_version, source file) across every image."""
    found: dict[str, tuple[str, str]] = {}
    for rel in _IMAGE_DEP_SOURCES:
        path = _ROOT / rel
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in _MARKER_RE.finditer(line):
                found[m.group(1).lower()] = (m.group(2), rel)
    return found


def test_version_conditional_deps_are_installed_in_ci() -> None:
    """Every `pkg; python_version >= X` the image pins is also a test requirement.

    Without this, bumping CI onto the shipped interpreter *causes* the very
    class it was meant to close: the runtime gains a version, loses a module,
    and the tests covering that module quietly stop existing.
    """
    conditional = _conditional_deps()
    assert conditional, (
        f"no `pkg; python_version >= ...` markers found in any of {_IMAGE_DEP_SOURCES} — "
        "the regex has drifted from the files and this guard is now vacuous"
    )

    declared = _declared_test_requirements()
    missing = [
        f"{dep} ({src} pins it for python >= {ver})"
        for dep, (ver, src) in sorted(conditional.items())
        if dep not in _CI_EXEMPT_CONDITIONAL_DEPS and dep not in declared
    ]

    assert not missing, (
        "the backend image installs version-conditional dependencies that CI does "
        "not, so tests needing them skip at collection instead of failing loudly "
        "(#1891):\n  " + "\n  ".join(missing)
        + f"\n\nFix: add the same marker to {_TEST_REQUIREMENTS.relative_to(_ROOT)}, "
        "or exempt it in _CI_EXEMPT_CONDITIONAL_DEPS with a reason."
    )


def test_every_image_dep_source_still_exists() -> None:
    """A renamed or deleted dep source must fail loudly, not shrink coverage.

    Same reasoning as ``test_allowlist_entries_still_exist``: the dangerous
    failure for a guard is not going red, it is quietly watching less than it
    claims to.
    """
    missing = [rel for rel in _IMAGE_DEP_SOURCES if not (_ROOT / rel).is_file()]
    assert not missing, (
        f"_IMAGE_DEP_SOURCES points at files that no longer exist: {missing}. "
        "Repoint it — do not just delete the entry, or the deps installed there "
        "stop being checked."
    )


def test_conditional_dep_exemptions_are_still_real() -> None:
    """A stale exemption silently whitelists a dep nobody is installing."""
    conditional = _conditional_deps()
    stale = [dep for dep in _CI_EXEMPT_CONDITIONAL_DEPS if dep not in conditional]
    assert not stale, (
        f"_CI_EXEMPT_CONDITIONAL_DEPS exempts deps no image pins any more: {stale}"
    )
