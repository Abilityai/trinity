"""Ref-resolving `ls-remote` probes for template reads (#3725).

`provisioning.probe_anonymous_repo_access` answers only "is this repo
clonable without credentials?". The template-metadata fallback needs more from
the same single call: proof that the repo is public AND the commit the
requested ref points at, so the credential-free file read can be pinned to a
SHA (no CDN staleness, and a 404 there is unambiguous absence). The 403
re-check in the create path needs the same question asked WITH a token.

A sibling module rather than more of `provisioning.py`, which sits at the
#1028 size threshold. The stderr classification is provisioning's own
(`provisioning._ANON_PROBE_DEFINITIVE_PATTERNS`, read through the module
object so there is one list), which keeps the outcome vocabulary identical:

  - ``"ok"``          — the remote answered and the ref exists; ``sha`` is set
  - ``"missing_ref"`` — the remote answered (repo readable) but has no such ref
  - ``"unavailable"`` — auth challenge / not found: private or nonexistent for
                        this credential (git cannot tell the two apart)
  - ``"transient"``   — GitHub unreachable or an unrecognised failure; says
                        nothing about the repo
"""
import asyncio
import logging
import os
import re
from typing import Optional, Tuple

from . import provisioning

logger = logging.getLogger(__name__)

# A full object id: SHA-1 (40) or SHA-256 (64). Anything else in the first
# column is not something a raw URL may be built from.
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _ref_arg(ref: Optional[str]) -> str:
    """``None``/``"HEAD"`` → the default branch; a branch name → its full ref,
    so a tag or a same-named ref elsewhere can never answer for it."""
    if not ref or ref == "HEAD":
        return "HEAD"
    return f"refs/heads/{ref}"


async def _ls_remote_ref(
    github_repo: str, ref: Optional[str], env: dict, caller: str
) -> Tuple[str, str]:
    base = os.getenv("TRINITY_GIT_BASE_URL", "https://github.com").rstrip("/")
    remote_url = f"{base}/{github_repo}.git"
    wanted = _ref_arg(ref)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", remote_url, wanted,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("%s: ls-remote timed out for %s", caller, github_repo)
            return "transient", ""
    except FileNotFoundError:
        logger.warning("%s: git not installed on backend host", caller)
        return "transient", ""
    except Exception as exc:  # noqa: BLE001 — defensive
        # The type only: a tokened child's exception text is not logged.
        logger.warning("%s: ls-remote failed for %s: %s",
                       caller, github_repo, type(exc).__name__)
        return "transient", ""

    if proc.returncode == 0:
        for line in (stdout or b"").decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1] == wanted:
                sha = parts[0].strip().lower()
                if _SHA_RE.match(sha):
                    return "ok", sha
                logger.warning("%s: ls-remote for %s returned a malformed object id",
                               caller, github_repo)
                return "transient", ""
        return "missing_ref", ""

    stderr_text = (stderr or b"").decode("utf-8", errors="replace").lower()
    if any(p in stderr_text for p in provisioning._ANON_PROBE_DEFINITIVE_PATTERNS):
        return "unavailable", ""
    logger.warning(
        "%s: ls-remote for %s exited %s with unrecognized error — treating as transient",
        caller, github_repo, proc.returncode,
    )
    return "transient", ""


async def probe_anonymous_ref(
    github_repo: str, ref: Optional[str] = None
) -> Tuple[str, str]:
    """Credential-less ``git ls-remote <url> <ref|HEAD>`` → ``(outcome, sha)``.

    Same transport and env as ``probe_anonymous_repo_access`` (no credential,
    ``GIT_TERMINAL_PROMPT=0`` so an auth challenge fails fast instead of
    hanging), so "ok" here means the anonymous clone would succeed — and the
    SHA is the commit it would check out.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return await _ls_remote_ref(github_repo, ref, env, "probe_anonymous_ref")


async def probe_tokened_repo_access(
    github_repo: str, github_pat: str, ref: Optional[str] = None
) -> Tuple[str, str]:
    """READ access over the git transport WITH ``github_pat`` → ``(outcome, sha)``.

    For a token the REST API answered 403 (a rate limit, an SSO or org policy
    on the API) — the git transport is a separate budget, and it is the
    transport the clone uses. The token rides ``git_auth_env`` (config via env),
    never argv, the ``probe_push_access`` pattern. Pass the branch the agent
    will clone as ``ref``: "ok" then also proves that branch exists.
    """
    from services.git_credential_helper import git_auth_env

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **git_auth_env(github_pat)}
    return await _ls_remote_ref(github_repo, ref, env, "probe_tokened_repo_access")
