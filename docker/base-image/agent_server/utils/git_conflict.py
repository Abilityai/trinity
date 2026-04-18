"""
Git conflict classification (S5 — operator-readable diagnosis, issue #386).

Mirror of ``src/backend/services/git_service.py::classify_conflict``. The
agent server runs inside the container image and does NOT import from the
backend, so the classifier is duplicated here — intentionally small, pure,
regex-only. Keep the two copies in sync; the backend copy is the canonical
source of truth.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class ConflictClass(str, Enum):
    """Symbolic class of a git sync/push/pull failure."""

    AHEAD_ONLY = "AHEAD_ONLY"
    BEHIND_ONLY = "BEHIND_ONLY"
    PARALLEL_HISTORY = "PARALLEL_HISTORY"
    UNCOMMITTED_LOCAL = "UNCOMMITTED_LOCAL"
    AUTH_FAILURE = "AUTH_FAILURE"
    WORKING_BRANCH_EXTERNAL_WRITE = "WORKING_BRANCH_EXTERNAL_WRITE"
    UNKNOWN = "UNKNOWN"


_AUTH_PATTERNS = (
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"could not read username", re.IGNORECASE),
    re.compile(r"could not read password", re.IGNORECASE),
    re.compile(r"invalid username or password", re.IGNORECASE),
    re.compile(r"permission denied \(publickey\)", re.IGNORECASE),
)

_UNCOMMITTED_PATTERNS = (
    re.compile(r"your local changes to the following files would be overwritten", re.IGNORECASE),
    re.compile(r"please commit your changes or stash them", re.IGNORECASE),
)

_EXTERNAL_WRITE_PATTERNS = (
    re.compile(r"cannot lock ref", re.IGNORECASE),
    re.compile(r"failed to update ref", re.IGNORECASE),
)

_PARALLEL_HISTORY_PATTERNS = (
    re.compile(r"could not apply [0-9a-f]{7,40}", re.IGNORECASE),
    re.compile(r"conflict \(add/add\):", re.IGNORECASE),
)


def classify_conflict(
    stderr: str,
    ahead: int,
    behind: int,
    common_ancestor_sha: Optional[str] = None,
) -> ConflictClass:
    """Classify a git sync/push/pull failure. Pure function; see backend docstring."""
    del common_ancestor_sha  # reserved for future discriminators

    text = stderr or ""

    for pat in _AUTH_PATTERNS:
        if pat.search(text):
            return ConflictClass.AUTH_FAILURE

    for pat in _UNCOMMITTED_PATTERNS:
        if pat.search(text):
            return ConflictClass.UNCOMMITTED_LOCAL

    for pat in _EXTERNAL_WRITE_PATTERNS:
        if pat.search(text):
            return ConflictClass.WORKING_BRANCH_EXTERNAL_WRITE

    for pat in _PARALLEL_HISTORY_PATTERNS:
        if pat.search(text):
            return ConflictClass.PARALLEL_HISTORY

    if not text.strip():
        if ahead > 0 and behind == 0:
            return ConflictClass.AHEAD_ONLY
        if behind > 0 and ahead == 0:
            return ConflictClass.BEHIND_ONLY

    return ConflictClass.UNKNOWN
