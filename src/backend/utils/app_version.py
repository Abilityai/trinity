"""Release version of this build (ent#437).

The telemetry aggregate must carry the RELEASE version — never the commit SHA.
Version-adoption timing is a join key: a per-commit value re-links the anonymous
share stream to the identified presence/intake streams on any fleet small enough
that two installs rarely share a commit. So the payload gets the semver base.

Resolved the way `/api/version` does (`main.py`): the `VERSION` env var wins
(with any `+g<sha>` build suffix stripped), then `/app/VERSION` (mounted in dev,
COPY'd in prod), then the repo-root `VERSION` file for a source checkout. The
`main.py` copy stays inline for now — adopting this helper there is registered as
`debt:2026-09-03-main-version-resolver-adopt-util`.

Cross-cutting helpers live in `utils/` rather than `services/` because `services.*`
is stubbed wholesale by isolation harnesses and a MagicMock version string would
flow straight into the payload validator (learnings 2026-08-03).
"""
from __future__ import annotations

import os
from pathlib import Path

UNKNOWN_VERSION = "unknown"



def _candidate_paths() -> list:
    """Where a VERSION file may live, in preference order. In the container the
    module is `/app/utils/app_version.py`, so a fixed `parents[3]` does not
    exist and would raise at IMPORT time — taking the whole backend down with
    it (the packaging-differs-from-checkout class). The repo-root candidate is
    therefore added only when the path is deep enough to have one."""
    paths = [Path("/app/VERSION")]                              # container
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:
        paths.append(parents[3] / "VERSION")                   # source checkout
    return paths


def resolve_release_version() -> str:
    """Return the release version string, or ``"unknown"``. Never raises."""
    env_version = (os.getenv("VERSION") or "").strip()
    if env_version and env_version != UNKNOWN_VERSION:
        # `0.9.5-rc2+g1a2b3c4` → `0.9.5-rc2`: the build suffix is the SHA.
        return env_version.split("+", 1)[0]
    for path in _candidate_paths():
        try:
            if path.exists():
                value = path.read_text().strip()
                if value:
                    return value
        except OSError:
            # An unreadable VERSION file must never break a payload build.
            continue
    return UNKNOWN_VERSION
