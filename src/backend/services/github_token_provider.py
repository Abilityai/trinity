"""Repo-scoped GitHub token seam (trinity-enterprise#1277).

A registered provider can answer "a short-lived token scoped to this one
repository". `settings_service.resolve_github_token` consults it between the
per-user PAT and the platform PAT. With no provider registered this module is
inert and resolution is PAT-only.

Protocol:

    await provider.token_for_repo(repo, permissions=None) -> Optional[RepoToken]

* ``None``: the provider does not cover this repo. Resolution falls through to
  the platform PAT.
* raises :class:`GithubTokenUnavailable`: the provider covers this repo and
  could not produce a token. Resolution stops. It does not fall back to the
  platform PAT, which has wider reach than the token that was asked for.

The token expires (``RepoToken.expires_at``, ISO-8601 UTC). Callers fetch one
per operation and never persist it or bake it into a container environment.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol

logger = logging.getLogger(__name__)

# Container env flag: "this agent fetches its git credential from the backend
# at use time" (trinity-enterprise#1278). A container carrying it holds no
# baked GITHUB_PAT.
BROKER_ENV = "TRINITY_GIT_TOKEN_BROKER"

# Ceiling on the coverage lookup made while an agent starts.
_COVERAGE_TIMEOUT = 5

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GithubTokenUnavailable(RuntimeError):
    """The provider covers the repo and could not produce a token."""


@dataclass(frozen=True)
class RepoToken:
    token: str = field(repr=False)
    expires_at: str


class GithubTokenProvider(Protocol):
    async def token_for_repo(
        self, repo: str, permissions: Optional[Dict[str, str]] = None
    ) -> Optional[RepoToken]:
        ...


_provider: Optional[GithubTokenProvider] = None


def register_provider(provider: GithubTokenProvider) -> None:
    """Register the repo-token provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[github_token_provider] provider registered: %s", type(provider).__name__)


def clear_provider() -> None:
    """Drop the provider. Used by tests to restore the PAT-only path."""
    global _provider
    _provider = None


def normalize_repo(repo: str) -> Optional[str]:
    """``owner/repo`` with a trailing ``.git`` removed, or None for anything else.

    The value is interpolated into an API path by the provider, so only the
    plain two-segment form is accepted. URLs and ``.``/``..`` segments are refused.
    """
    repo = (repo or "").removesuffix(".git")
    if not _REPO.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        return None
    return repo


async def token_for_repo(
    repo: str, permissions: Optional[Dict[str, str]] = None
) -> Optional[RepoToken]:
    """The provider's token for ``repo``, or None when uncovered or no provider."""
    if _provider is None:
        return None
    normalized = normalize_repo(repo)
    if normalized is None:
        return None
    try:
        return await _provider.token_for_repo(normalized, permissions)
    except GithubTokenUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        # Type only: the message can carry a token or key material.
        logger.error("[github_token_provider] provider failed for %s: %s",
                     normalized, type(e).__name__)
        raise GithubTokenUnavailable(
            f"GitHub token provider failed for {normalized}") from None


async def coverage(repo: str) -> Optional[bool]:
    """Does the provider cover ``repo``? True, False, or None for unknown.

    None means the provider could not answer (GitHub unreachable, a failed
    mint, a slow lookup). Callers keep whatever mode a container already has.
    """
    if _provider is None:
        return False
    try:
        return await asyncio.wait_for(token_for_repo(repo), _COVERAGE_TIMEOUT) is not None
    except (GithubTokenUnavailable, asyncio.TimeoutError):
        return None


def wants_broker(
    *,
    coverage: Optional[bool],
    carried_flag: bool,
    would_bake_pat: bool,
    has_own_pat: bool,
) -> bool:
    """Should this agent's container carry :data:`BROKER_ENV`?

    The single decision shared by agent creation, the recreate matcher and the
    recreate env writer, so they cannot disagree.

    * A per-agent PAT outranks the provider.
    * Unknown coverage keeps the mode the container already has.
    * A covered repo uses the broker only where a platform PAT would have been
      baked (``would_bake_pat``) or the broker is already in use. A container
      that was deliberately given no credential stays that way.
    """
    if has_own_pat:
        return False
    if coverage is None:
        return carried_flag
    return bool(coverage) and (carried_flag or would_bake_pat)
