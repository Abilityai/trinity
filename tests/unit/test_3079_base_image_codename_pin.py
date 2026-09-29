"""Static guard: every image Dockerfile pins its Debian codename (#3079).

`docker/{backend,scheduler}/Dockerfile` were built ``FROM python:3.13-slim``, a
tag Docker Hub repoints when Debian releases. A rebuild on any host therefore
followed bookworm→trixie without a line of Trinity changing, and the drift
shipped twice in one release cycle: the `tzdata` backward links (#1823) and
libmagic 5.46's buffer-detection regression (#3046, #3080).

This checks only that a codename is NAMED, never which one. Moving to a new
Debian release stays a one-line reviewed diff (test_1823 explains why asserting
a specific codename would false-fire on an unrelated base bump); what fails is
a tag that can change underneath the image.

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

_FROM_PYTHON_RE = re.compile(r"^FROM\s+python:(\S+)", re.MULTILINE)

# `-slim-bookworm`, `-trixie`, ... A digest pin (`@sha256:`) also fixes the base.
_CODENAME_PINNED_RE = re.compile(r"-(?:slim-)?[a-z]+(?:@sha256:[0-9a-f]{64})?$|@sha256:[0-9a-f]{64}$")
_UNPINNED_VARIANTS = {"slim", "alpine"}


def _python_tags(rel: str) -> list[str]:
    path = _ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not present")
    tags = _FROM_PYTHON_RE.findall(path.read_text(encoding="utf-8"))
    assert tags, f"no `FROM python:<tag>` found in {rel}"
    return tags


def _is_codename_pinned(tag: str) -> bool:
    if not _CODENAME_PINNED_RE.search(tag):
        return False
    suffix = tag.split("@", 1)[0].rsplit("-", 1)[-1]
    return "@sha256:" in tag or suffix not in _UNPINNED_VARIANTS


@pytest.mark.parametrize("rel", _IMAGE_DOCKERFILES)
def test_image_pins_debian_codename(rel: str) -> None:
    unpinned = [t for t in _python_tags(rel) if not _is_codename_pinned(t)]
    assert not unpinned, (
        f"{rel} builds FROM python:{', python:'.join(unpinned)}, which names no Debian "
        f"codename, so Docker Hub can move it to a new Debian release under an "
        f"unrelated rebuild (#1823, #3046). Pin it, e.g. `python:3.13-slim-trixie`."
    )


@pytest.mark.parametrize(
    ("tag", "pinned"),
    [
        ("3.13-slim-trixie", True),
        ("3.13-slim-bookworm", True),
        ("3.13-trixie", True),
        ("3.13-slim@sha256:" + "a" * 64, True),
        ("3.13-slim", False),
        ("3.13", False),
        ("3.13-alpine", False),
    ],
)
def test_codename_detection(tag: str, pinned: bool) -> None:
    assert _is_codename_pinned(tag) is pinned
