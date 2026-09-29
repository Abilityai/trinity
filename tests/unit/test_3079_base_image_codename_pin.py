"""Static guard: every image Dockerfile pins its Debian codename (#3079).

`docker/{backend,scheduler}/Dockerfile` were built ``FROM python:3.13-slim``, a
tag Docker Hub repoints when Debian releases. A rebuild on any host therefore
followed bookworm→trixie without a line of Trinity changing, and the drift
shipped twice in one release cycle: the `tzdata` backward links (#1823) and
libmagic 5.46's buffer-detection regression (#3046, #3080).

Rules enforced:
  1. Every ``FROM python:`` in the three image Dockerfiles names a Debian
     codename (or pins a digest). Which codename is NOT asserted: moving to a new
     Debian release stays a one-line reviewed diff, and test_1823 explains why a
     specific codename would false-fire on an unrelated base bump.
  2. backend and scheduler name the SAME codename. Both resolve IANA keys and
     build a `CronTrigger` from the same schedule (test_1823 rule 5 pins the same
     APScheduler for that reason), so their system tz database must come from
     one Debian release. base-image is exempt, as in test_1823 rule 4.

Stdlib only, no backend imports.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

# Same set as test_1891_python_version_parity._IMAGE_DOCKERFILES.
_IMAGE_DOCKERFILES = (
    "docker/backend/Dockerfile",
    "docker/scheduler/Dockerfile",
    "docker/base-image/Dockerfile",
)
_SAME_CODENAME = ("docker/backend/Dockerfile", "docker/scheduler/Dockerfile")

# `FROM [--platform=...] python:<tag> [AS stage]`
_FROM_PYTHON_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?python:(\S+)", re.MULTILINE | re.IGNORECASE)
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_UNPINNED_VARIANTS = {"slim", "alpine"}


def _codename(tag: str) -> str | None:
    """The Debian codename (or alpineX.Y) a python tag pins, else None.

    `3.13-slim-trixie` -> `trixie`, `3.13-bookworm` -> `bookworm`,
    `3.13-alpine3.20` -> `alpine3.20`, `3.13-slim` / `3.13` / `3.13-alpine` -> None.
    """
    base = _DIGEST_RE.sub("", tag)
    parts = base.split("-")[1:]  # drop the version
    if not parts:
        return None
    last = parts[-1].lower()
    if last in _UNPINNED_VARIANTS:
        return None
    if re.fullmatch(r"alpine\d+(\.\d+)*", last) or re.fullmatch(r"[a-z]+", last):
        return last
    return None


def _is_pinned(tag: str) -> bool:
    return _codename(tag) is not None or bool(_DIGEST_RE.search(tag))


def _python_tags(rel: str) -> list[str]:
    path = _ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not present")
    tags = _FROM_PYTHON_RE.findall(path.read_text(encoding="utf-8"))
    assert tags, f"no `FROM python:<tag>` found in {rel}"
    return tags


@pytest.mark.parametrize("rel", _IMAGE_DOCKERFILES)
def test_image_pins_debian_codename(rel: str) -> None:
    unpinned = [t for t in _python_tags(rel) if not _is_pinned(t)]
    assert not unpinned, (
        f"{rel} builds FROM python:{', python:'.join(unpinned)}, which names no Debian "
        f"codename, so Docker Hub can move it to a new Debian release under an "
        f"unrelated rebuild (#1823, #3046). Pin it, e.g. `python:3.13-slim-trixie`."
    )


def test_backend_and_scheduler_share_codename() -> None:
    codenames = {rel: {_codename(t) for t in _python_tags(rel)} for rel in _SAME_CODENAME}
    backend, scheduler = (codenames[rel] for rel in _SAME_CODENAME)
    assert backend == scheduler and len(backend) == 1, (
        f"backend and scheduler must build from the same Debian release so the "
        f"schedule validator and executor share one tz database (#1472, #3079): "
        f"{codenames}"
    )


@pytest.mark.parametrize(
    ("tag", "codename", "pinned"),
    [
        ("3.13-slim-trixie", "trixie", True),
        ("3.13-slim-bookworm", "bookworm", True),
        ("3.13-trixie", "trixie", True),
        ("3.13-alpine3.20", "alpine3.20", True),
        ("3.13-slim-trixie@sha256:" + "a" * 64, "trixie", True),
        ("3.13-slim@sha256:" + "a" * 64, None, True),
        ("3.13-slim", None, False),
        ("3.13", None, False),
        ("3.13-alpine", None, False),
    ],
)
def test_codename_detection(tag: str, codename: str | None, pinned: bool) -> None:
    assert _codename(tag) == codename
    assert _is_pinned(tag) is pinned


@pytest.mark.parametrize(
    ("line", "tag"),
    [
        ("FROM python:3.13-slim-trixie", "3.13-slim-trixie"),
        ("FROM python:3.13-slim-trixie AS builder", "3.13-slim-trixie"),
        ("FROM --platform=$BUILDPLATFORM python:3.13-slim", "3.13-slim"),
        ("  from python:3.13-slim-bookworm", "3.13-slim-bookworm"),
    ],
)
def test_from_line_parsing(line: str, tag: str) -> None:
    assert _FROM_PYTHON_RE.findall(line) == [tag]
