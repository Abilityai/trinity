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

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol

logger = logging.getLogger(__name__)

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
