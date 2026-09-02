"""Conflict classification — the 409 vocabulary the git routes and MCP tools share.

Carved out of the 2,322-line `services/git_service.py` (#1028). The package
`__init__` re-exports the public surface, so `from services.git_service
import …` and `git_service.<name>` callers are unchanged.

Cross-module calls go THROUGH the sibling module object
(`gitignore._detect_git_dir(...)`, never `from .gitignore import
_detect_git_dir`) so a test that patches the owning module reaches every
caller — a from-import freezes the binding and quietly detaches such a
patch.
"""
import asyncio
import os
import re
import shlex
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.exc import IntegrityError
from database import db, AgentGitConfig, GitSyncResult
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, execute_command_in_container
from utils.credential_sanitizer import scrub_secret_and_urls
from utils.safe_yaml import (  # ent#314
    AliasPolicy as _AliasPolicy,
    HardenedYamlError as _HardenedYamlError,
    load_hardened_yaml as _load_hardened_yaml,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Conflict classification (S5 — operator-readable diagnosis, issue #386)
# ----------------------------------------------------------------------------



logger = logging.getLogger(__name__)

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

NO_WRITE_CREDENTIALS_MESSAGE = (
    "This agent has no write credentials — it tracks a public template "
    "read-only. Use 'Bind to your own repo' in this agent's Git tab to point "
    "it at a GitHub repository you own (keeping everything it has learned), "
    "or add a GitHub token."
)

class ConflictClass(str, Enum):
    """Symbolic class of a git sync/push/pull failure, used by the UI to pick
    operator-readable copy.

    Members map one-to-one to the decision tree defined in the git-improvements
    proposal (§P4/§S5). The string value equals the member name so JSON
    serialization in ``conflict_class`` fields stays stable.
    """

    AHEAD_ONLY = "AHEAD_ONLY"
    BEHIND_ONLY = "BEHIND_ONLY"
    PARALLEL_HISTORY = "PARALLEL_HISTORY"
    UNCOMMITTED_LOCAL = "UNCOMMITTED_LOCAL"
    AUTH_FAILURE = "AUTH_FAILURE"
    WORKING_BRANCH_EXTERNAL_WRITE = "WORKING_BRANCH_EXTERNAL_WRITE"
    UNKNOWN = "UNKNOWN"


def classify_conflict(
    stderr: str,
    ahead: int,
    behind: int,
    common_ancestor_sha: Optional[str] = None,
) -> ConflictClass:
    """Classify a git sync/push/pull failure into an operator-readable class.

    Pure function: takes the raw stderr string plus the current ahead/behind
    counts (as reported by ``git rev-list --left-right --count``) and returns
    a :class:`ConflictClass` enum member. No IO, no DB access.

    The decision order is deliberate:

    1. Auth failures first — they mask everything downstream.
    2. Uncommitted-local before any ref-update checks, because git refuses to
       even try the update when the working tree is dirty.
    3. External-write on the working branch (``cannot lock ref`` /
       ``failed to update ref``) — this is the P5 silent-clobber signature.
    4. Parallel-history (rebase apply failed on a specific sha) — this is P2.
    5. Fall back to numeric state (``AHEAD_ONLY`` / ``BEHIND_ONLY``) when
       stderr is empty or unhelpful.
    6. ``UNKNOWN`` when we genuinely cannot tell.
    """
    # ``common_ancestor_sha`` is accepted for forward compatibility with the
    # parallel-history discriminator in #385; classification today does not
    # need it because the stderr patterns alone are specific enough.
    del common_ancestor_sha

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


