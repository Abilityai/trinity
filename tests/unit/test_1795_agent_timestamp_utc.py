"""Regression tests for #1795 — agent timezone / naive-timestamp bug.

Two guards:

* Part B — ``iso_z_from_mtime`` emits a canonical ISO-Z UTC string that is
  *independent of the container's local TZ*, so agent file/credential/dashboard
  ``modified`` fields can never again surface as naive local time (the bug the
  frontend rendered "4 hours ago" for a just-written file).
* Part A — the base image defaults ``TZ`` to ``Etc/UTC`` (static content guard
  so a future edit can't silently re-introduce ``America/New_York``).

``conftest.py`` registers ``docker/base-image/agent_server`` as a namespace
package, so ``from agent_server.utils.helpers import ...`` resolves directly.
"""
from __future__ import annotations

import importlib.util
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_server.utils.helpers import iso_z_from_mtime

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "docker" / "base-image" / "Dockerfile"


def test_iso_z_from_mtime_zero_epoch_is_canonical_z():
    # Deterministic anchor: epoch 0 is the Unix epoch in UTC.
    assert iso_z_from_mtime(0.0) == "1970-01-01T00:00:00Z"


def test_iso_z_from_mtime_marks_zone_and_preserves_instant():
    mtime = 1753603618.549706  # a real fractional mtime
    out = iso_z_from_mtime(mtime)

    # Canonical ISO-Z: zone-marked with a trailing Z, never a bare naive string
    # and never the "+00:00" form (Invariant #16 uses Z).
    assert out.endswith("Z")
    assert "+00:00" not in out
    assert out[10] == "T"  # date/time separator

    # Same instant as the source mtime, interpreted as UTC.
    parsed = datetime.fromisoformat(out.replace("Z", "+00:00"))
    assert parsed == datetime.fromtimestamp(mtime, tz=timezone.utc)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is Unix-only")
def test_iso_z_from_mtime_is_independent_of_container_tz():
    """The crux of #1795: output must not shift with the process TZ.

    ``datetime.fromtimestamp(mtime, tz=utc)`` ignores the local zone, so the
    formatted string is identical whether the container is UTC or US/Eastern —
    which is exactly why Part B is robust to any future TZ change.
    """
    mtime = 1753603618.549706
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        as_utc = iso_z_from_mtime(mtime)

        os.environ["TZ"] = "America/New_York"  # the value #1795 removed
        time.tzset()
        as_eastern = iso_z_from_mtime(mtime)

        assert as_utc == as_eastern
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_base_image_defaults_to_utc():
    """Part A static guard: the base image must default TZ to UTC."""
    content = _DOCKERFILE.read_text(encoding="utf-8")
    assert "ENV TZ=Etc/UTC" in content
    assert "America/New_York" not in content


def test_files_router_stays_standalone_importable():
    """#1795 regression: files.py must load by path with no package context.

    tests/unit/test_ent183_skill_packages.py loads files.py via
    spec_from_file_location to check its protected-path logic; a relative import
    (``from ..utils.helpers``) breaks that standalone load. Guard both the load
    and that the router's local ``_iso_z_from_mtime`` matches the shared helper.
    """
    spec = importlib.util.spec_from_file_location(
        "_test1795_agent_files",
        str(_REPO_ROOT / "docker/base-image/agent_server/routers/files.py"),
    )
    files_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(files_mod)  # must not raise (a relative import would)

    for mtime in (0.0, 1753603618.549706):
        assert files_mod._iso_z_from_mtime(mtime) == iso_z_from_mtime(mtime)
