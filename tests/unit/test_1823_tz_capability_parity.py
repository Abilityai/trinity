"""Static guard: images that resolve timezones ship a complete tz database (#1823).

`docker/{backend,scheduler}/Dockerfile` are built ``FROM python:3.13-slim`` with
**no Debian codename pin**. Docker Hub repointed that tag from bookworm to
trixie, and trixie split the IANA *backward-compatibility links* out of `tzdata`
into a separate `tzdata-legacy` package. Nobody changed a line of Trinity; the
images simply stopped shipping `/usr/share/zoneinfo/Europe/Kiev`.

The failure is invisible to a source-only CI run, and worse than invisible — it
is inverted. `tests/requirements-test.txt` already declares `tzdata>=2024.1`
(#1771), so **the test environment had the capability the shipped images had
lost**: `tests/unit/test_1472_schedule_validation.py` was green on CI and red
inside the backend image, for months, until #1809's in-image suite run surfaced
it. That is the same "CI validates a runtime production does not have" class as
#1891, one layer down — system packages instead of the interpreter.

This is the durable half of the fix. Bumping the packaging fixed today's
divergence; it does nothing to stop the next image slim-down from dropping
`tzdata-legacy` and silently re-shipping the bug. Same shape as
``test_1891_python_version_parity.py`` / ``test_1560_agent_redis_key_parity.py``:
declarations that must agree, and a CI failure the moment they stop agreeing.

Rules enforced:
  1. Every image that resolves timezones installs `tzdata-legacy` (the system
     database, complete with the backward links).
  2. Every such image also declares the `tzdata` wheel — the portability floor
     that keeps the capability true on a non-Debian base and across another
     silent base-tag migration.
  3. `tests/requirements-test.txt` declares `tzdata` too, so CI cannot again be
     *more* capable than production (the #1891 Rule-3 shape).
  4. `docker/base-image/Dockerfile` is exempt only while the agent image
     registers no cron trigger. That justification is asserted, not assumed.

Plus one runtime assertion (``test_runtime_resolves_legacy_tz_aliases``) — the
static rules describe the declarations; only that one observes the environment
actually executing the suite, and it is the assertion that turns red *inside* a
mis-packaged image.

Lives in tests/unit/ as a near-pure static check — stdlib only, no YAML parser,
no backend/docker/database imports.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]

# Images that resolve IANA timezone keys at runtime, and therefore need the
# backward-compatibility links:
#   * backend  — hosts services/schedule_validation.py + the create/update routes
#   * scheduler — hosts _add_job, the actual CronTrigger registration
# docker/base-image/Dockerfile is deliberately absent; see
# `test_base_image_exemption_is_still_justified` for the assertion that keeps
# that exemption honest rather than inherited.
_TZ_RESOLVING_IMAGES = (
    "docker/backend/Dockerfile",
    "docker/scheduler/Dockerfile",
)

# Where each tz-resolving image installs its Python packages from. The backend
# inlines a pip list in its Dockerfile; the scheduler installs from a
# requirements FILE. Enumerating the SURFACE, not one file — #1891's `ea3c9d82`
# had to fix exactly this blindness ("scan every image dep source, not just the
# backend Dockerfile").
_IMAGE_DEP_SOURCES = (
    "docker/backend/Dockerfile",
    "docker/scheduler/requirements.txt",
)

_TEST_REQUIREMENTS = _ROOT / "tests" / "requirements-test.txt"

# The agent image. Exempt only for as long as it registers no cron trigger.
_BASE_IMAGE_TREE = _ROOT / "docker" / "base-image"

# A legacy alias that Debian moved into `tzdata-legacy`, and the canonical name
# it points at. `Europe/Kiev` is the exact key from the issue.
_LEGACY_ALIAS = "Europe/Kiev"
_CANONICAL = "Europe/Kyiv"


def _uncommented_lines(rel: str) -> list[str]:
    """Lines of a repo file with whole-line comments removed.

    Deliberately NOT a substring search over the raw text. #1891's guard shipped
    vacuous for exactly that reason: the explanatory comment above a pin
    contained the package name, so deleting the real declaration still matched.
    The comments this very file's fix added to both Dockerfiles say
    "tzdata-legacy" several times — a raw search would be green with the apt
    package removed.
    """
    path = _ROOT / rel
    assert path.is_file(), f"missing file: {rel}"
    return [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def _declared_package_names(rel: str) -> set[str]:
    """Package NAMES a pip declaration in `rel` installs.

    Handles both forms the repo uses: an inline `pip install --no-cache-dir a==1 \\`
    continuation list (backend Dockerfile) and a plain requirements file
    (scheduler). Version specifiers, extras, environment markers, quoting and the
    trailing backslash are all stripped.
    """
    names: set[str] = set()
    for line in _uncommented_lines(rel):
        token = line.strip().rstrip("\\").strip().strip('"').strip("'")
        if not token or token.startswith("#"):
            continue
        # Drop shell/Dockerfile leading words (RUN, pip, install, flags, ...)
        for word in token.split():
            word = word.strip('"').strip("'")
            if word.startswith("-") or word in {"RUN", "pip", "install"}:
                continue
            name = re.split(r"[;\[<>=!~ ]", word, maxsplit=1)[0].strip().lower()
            if name and re.fullmatch(r"[a-z0-9._-]+", name):
                names.add(name)
    return names


# ---------------------------------------------------------------------------
# Rule 1 — the system database, complete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", _TZ_RESOLVING_IMAGES)
def test_tz_resolving_image_installs_tzdata_legacy(rel: str) -> None:
    """Each tz-resolving image apt-installs `tzdata-legacy`.

    Dropping it is not a size optimisation, it is a silent re-ship of #1823:
    every schedule stored under a legacy alias stops registering, `next_run_at`
    freezes, and the create route 500s.
    """
    body = "\n".join(_uncommented_lines(rel))
    assert "tzdata-legacy" in body, (
        f"{rel} no longer installs `tzdata-legacy`, so its `zoneinfo` database "
        f"loses the IANA backward-compatibility links (#1823). A stored "
        f"'{_LEGACY_ALIAS}' schedule then fails to register — silently in the "
        f"scheduler, as a 500 on create. Re-add it, or move this image out of "
        f"_TZ_RESOLVING_IMAGES with the reason it no longer resolves timezones."
    )


# ---------------------------------------------------------------------------
# Rule 2 — the portability floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", _IMAGE_DEP_SOURCES)
def test_tz_resolving_image_declares_the_tzdata_wheel(rel: str) -> None:
    """Each tz-resolving image also declares the `tzdata` wheel.

    `zoneinfo` reads the SYSTEM database first and falls back to the wheel only
    for keys the system lacks, so with rule 1 satisfied the wheel is inert in
    production. It is here because the recurrence vector for #1823 is a base
    image moving underneath us: `python:3.13-slim` carries no codename pin, and
    an apt-only fix is Debian-shaped. The wheel makes the capability hold
    regardless of the base.
    """
    assert "tzdata" in _declared_package_names(rel), (
        f"{rel} no longer declares the `tzdata` wheel, so this image's tz "
        f"capability depends entirely on the (unpinned, floating) base image "
        f"shipping the backward links (#1823)."
    )


# ---------------------------------------------------------------------------
# Rule 3 — CI must not be more capable than production
# ---------------------------------------------------------------------------


def test_test_requirements_declare_tzdata() -> None:
    """CI installs the same tz capability the images ship.

    The inverted direction is what made #1823 survive so long: CI had `tzdata`
    (#1771) and the images did not, so the suite asserted a capability
    production had lost and stayed green. Removing it here would restore the
    divergence in the other direction — the suite would go red on CI while
    production is fine — which is at least loud, but the declaration is what
    makes "green on CI" mean anything about the image.
    """
    declared: set[str] = set()
    for raw in _TEST_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[;\[<>=!~ ]", line, maxsplit=1)[0].strip().lower()
        if name:
            declared.add(name)

    assert "tzdata" in declared, (
        "tests/requirements-test.txt no longer declares `tzdata`, so the suite's "
        "timezone behaviour depends on whatever the CI runner image happens to "
        "ship (#1771, #1823)."
    )


# ---------------------------------------------------------------------------
# Rule 4 — the base-image exemption, asserted rather than inherited
# ---------------------------------------------------------------------------


def test_base_image_exemption_is_still_justified() -> None:
    """The agent image is exempt only while it registers no cron trigger.

    Two independent reasons put `docker/base-image/Dockerfile` outside
    _TZ_RESOLVING_IMAGES: it pins `python:3.13-slim-bookworm` (where `tzdata`
    still carries the backward links — which is precisely why the agent image
    never regressed), and the agent container performs no schedule validation
    and constructs no `CronTrigger`.

    Only the second is asserted here. The codename pin is a fact about today's
    Debian, not an invariant anyone maintains, and testing it would false-fire
    on an unrelated base bump. "Nothing in the agent tree resolves IANA keys" is
    the reason that actually holds, so that is the reason kept honest.
    """
    if not _BASE_IMAGE_TREE.is_dir():
        pytest.skip("docker/base-image tree not present")

    offenders: list[str] = []
    for path in sorted(_BASE_IMAGE_TREE.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".txt", ".sh", ""}:
            continue
        if path.name == "Dockerfile" or path.suffix in {".py", ".txt", ".sh"}:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover - unreadable file
                continue
            for token in ("apscheduler", "APScheduler", "CronTrigger", "schedule_validation"):
                if token in text:
                    offenders.append(f"{path.relative_to(_ROOT)} mentions {token!r}")
                    break

    assert not offenders, (
        "the agent image now appears to resolve cron/timezone keys, so its "
        "exemption from _TZ_RESOLVING_IMAGES is no longer justified (#1823):\n  "
        + "\n  ".join(offenders)
        + "\n\nFix: add docker/base-image/Dockerfile to _TZ_RESOLVING_IMAGES and "
        "give it a tz-data source, or remove the cron consumer."
    )


# ---------------------------------------------------------------------------
# Stale-path guard — a guard's dangerous failure is watching less than it claims
# ---------------------------------------------------------------------------


def test_every_tz_capability_source_still_exists() -> None:
    """A renamed or deleted source must fail loudly, not shrink coverage."""
    missing = [
        rel for rel in set(_TZ_RESOLVING_IMAGES) | set(_IMAGE_DEP_SOURCES)
        if not (_ROOT / rel).is_file()
    ]
    assert not missing, (
        f"tz-capability sources point at files that no longer exist: {missing}. "
        "Repoint them — do not just delete the entry, or that image stops being "
        "checked."
    )
    assert _TEST_REQUIREMENTS.is_file(), (
        f"_TEST_REQUIREMENTS points at a file that no longer exists: "
        f"{_TEST_REQUIREMENTS}"
    )


# ---------------------------------------------------------------------------
# The one runtime assertion
# ---------------------------------------------------------------------------


def test_runtime_resolves_legacy_tz_aliases() -> None:
    """This interpreter can resolve a legacy IANA alias through `zoneinfo`.

    Every assertion above reads declarations. This one observes the environment
    actually running the suite, and it is the assertion that goes red *inside* a
    mis-packaged image — which is where #1823 was found and where AC 1 is
    provable. On CI it passes because `tests/requirements-test.txt` declares
    `tzdata`; in the image it passes because `tzdata-legacy` + the wheel do.

    `zoneinfo` — not `pytz` — because pytz bundles its own complete copy and
    therefore accepts the alias even in the broken image. `zoneinfo` is the
    resolver APScheduler's `astimezone()` actually binds through, so it is the
    only one whose verdict predicts whether the schedule registers.
    """
    import zoneinfo

    zoneinfo.ZoneInfo(_CANONICAL)  # canonical name — resolves everywhere
    try:
        zoneinfo.ZoneInfo(_LEGACY_ALIAS)
    except zoneinfo.ZoneInfoNotFoundError as exc:  # pragma: no cover - broken env
        pytest.fail(
            f"this runtime cannot resolve the legacy alias {_LEGACY_ALIAS!r} "
            f"({exc}). The IANA backward-compatibility links are missing: install "
            f"the `tzdata-legacy` system package or the `tzdata` wheel (#1823). "
            f"If this fired inside a Trinity image, that image regressed the "
            f"packaging fix."
        )
