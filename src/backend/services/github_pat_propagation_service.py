"""
GitHub PAT propagation service (#211).

Pushes the global GitHub PAT to running agents' .env files when it is updated
in Settings, so agents pick up the new token without a restart.

Eligibility rules:
- Agent container must be running.
- Agent must NOT have a per-agent PAT (#347) configured — those override the global
  and are managed separately.
- Agent's current .env must already contain a GITHUB_PAT key. Agents that never
  set up GitHub are skipped to avoid injecting unused credentials.
"""
import asyncio
import logging
import re
from typing import List

import httpx

from database import db
from models import AgentPropagationStatus, GithubPatPropagationResult
from services.agent_auth import build_agent_auth_headers
from services.docker_service import list_all_agents_fast, get_agent_container

logger = logging.getLogger(__name__)

AGENT_HTTP_TIMEOUT_SECONDS = 30.0

# Matches a GITHUB_PAT line in an agent's .env, ignoring leading whitespace.
# Captures everything up to (and including) the newline so we can replace cleanly.
_GITHUB_PAT_LINE_RE = re.compile(r'(?m)^[ \t]*GITHUB_PAT=.*$')


def _format_pat_line(pat: str) -> str:
    """Format a GITHUB_PAT line matching the agent's own .env writer.

    The agent writes credentials as `KEY="value"` with embedded double quotes
    escaped (see docker/base-image/agent_server/routers/credentials.py).
    """
    escaped = pat.replace('"', '\\"')
    return f'GITHUB_PAT="{escaped}"'


def _patch_env_github_pat(env_content: str, new_pat: str) -> str:
    """Return env_content with the GITHUB_PAT line replaced."""
    new_line = _format_pat_line(new_pat)
    if _GITHUB_PAT_LINE_RE.search(env_content):
        return _GITHUB_PAT_LINE_RE.sub(new_line, env_content, count=1)
    # Caller should have filtered this case, but keep the behavior explicit.
    suffix = "" if env_content.endswith("\n") else "\n"
    return f"{env_content}{suffix}{new_line}\n"


def _env_has_github_pat(env_content: str) -> bool:
    return bool(_GITHUB_PAT_LINE_RE.search(env_content))


async def _apply_pat_to_env(
    client: httpx.AsyncClient,
    agent_name: str,
    base_url: str,
    pat: str,
    *,
    add_if_missing: bool,
) -> str:
    """Read an agent's ``.env``, patch the ``GITHUB_PAT`` line, write it back.

    Shared by the global-PAT path (:func:`_propagate_to_agent`, ``add_if_missing
    =False`` — skip agents that never set up a token) and the per-agent path
    (:func:`propagate_pat_to_single_agent`, ``add_if_missing=True`` — the #1264
    case is a container with no ``GITHUB_PAT`` line yet). Returns ``"updated"`` or
    ``"skipped_no_pat"``; raises httpx errors for the caller to classify.
    """
    read_resp = await client.get(
        f"{base_url}/api/credentials/read",
        params={"paths": ".env"},
        timeout=AGENT_HTTP_TIMEOUT_SECONDS,
        headers=build_agent_auth_headers(agent_name),
    )
    read_resp.raise_for_status()
    env_content = read_resp.json().get("files", {}).get(".env")

    if env_content is None:
        if not add_if_missing:
            return "skipped_no_pat"
        env_content = ""
    elif not _env_has_github_pat(env_content) and not add_if_missing:
        return "skipped_no_pat"

    patched = _patch_env_github_pat(env_content, pat)
    inject_resp = await client.post(
        f"{base_url}/api/credentials/inject",
        json={"files": {".env": patched}},
        timeout=AGENT_HTTP_TIMEOUT_SECONDS,
        headers=build_agent_auth_headers(agent_name),
    )
    inject_resp.raise_for_status()
    return "updated"


async def propagate_pat_to_single_agent(agent_name: str, pat: str) -> dict:
    """Push a newly-set per-agent PAT into a running container with no restart (#1264).

    Adds the ``GITHUB_PAT`` line when absent (the #1264 case is a container
    created without any token) and re-templates the live git remote so the frozen
    empty-password remote is fixed immediately and fetch/push work. The live git
    process authenticates from the remote URL (not the env var), so
    ``remote_updated`` is the load-bearing part of the immediate fix; the ``.env``
    write only takes effect on the next restart.

    Best-effort and non-fatal: a stopped agent picks the PAT up on next start via
    the relaxed lifecycle injection + the startup.sh self-heal. Returns a small
    status dict for the set-PAT API response.
    """
    from services import git_service

    # #1264 review: query the single container instead of enumerating the fleet.
    # get_agent_container is a module-level import (above) so it's patchable as a
    # stable module global, not resolved from sys.modules at call time.
    container = get_agent_container(agent_name)
    if container is None or container.status != "running":
        return {"applied": False, "reason": "agent_not_running"}

    env_updated = False
    base_url = f"http://agent-{agent_name}:8000"
    async with httpx.AsyncClient(timeout=AGENT_HTTP_TIMEOUT_SECONDS) as client:
        try:
            await _apply_pat_to_env(client, agent_name, base_url, pat, add_if_missing=True)
            env_updated = True
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning("single-agent PAT .env inject failed for %s: %s", agent_name, e)

    # Re-template the live remote so an existing clone picks up the token now.
    git_config = db.get_git_config(agent_name)
    github_repo = git_config.github_repo if git_config else None
    remote_updated = (
        await git_service.update_remote_pat(agent_name, pat, github_repo)
        if github_repo else False
    )

    return {
        "applied": env_updated or remote_updated,
        "env_updated": env_updated,
        "remote_updated": remote_updated,
    }


async def _propagate_to_agent(
    agent_name: str,
    new_pat: str,
    client: httpx.AsyncClient,
) -> AgentPropagationStatus:
    """Read .env from one agent, patch GITHUB_PAT, write it back (global-PAT path)."""
    base_url = f"http://agent-{agent_name}:8000"
    try:
        status = await _apply_pat_to_env(client, agent_name, base_url, new_pat, add_if_missing=False)
        return AgentPropagationStatus(agent_name=agent_name, status=status)
    except httpx.HTTPStatusError as e:
        error = f"agent returned {e.response.status_code}: {e.response.text[:200]}"
        logger.warning("GITHUB_PAT propagation failed for %s: %s", agent_name, error)
        return AgentPropagationStatus(
            agent_name=agent_name, status="failed", error=error
        )
    except httpx.RequestError as e:
        error = f"connection error: {e}"
        logger.warning("GITHUB_PAT propagation failed for %s: %s", agent_name, error)
        return AgentPropagationStatus(
            agent_name=agent_name, status="failed", error=error
        )


async def propagate_github_pat(new_pat: str) -> GithubPatPropagationResult:
    """Propagate a new global GitHub PAT to all eligible running agents.

    Per-agent failures are captured in the result; they do not raise.
    """
    running_agents = [a for a in list_all_agents_fast() if a.status == "running"]

    targets: List[str] = []
    pre_skipped: List[AgentPropagationStatus] = []

    for agent in running_agents:
        if db.has_agent_github_pat(agent.name):
            pre_skipped.append(
                AgentPropagationStatus(
                    agent_name=agent.name, status="skipped_per_agent_pat"
                )
            )
            continue
        targets.append(agent.name)

    updated: List[str] = []
    skipped: List[AgentPropagationStatus] = list(pre_skipped)
    failed: List[AgentPropagationStatus] = []

    if targets:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(_propagate_to_agent(name, new_pat, client) for name in targets),
                return_exceptions=True,
            )

        for name, result in zip(targets, results):
            if isinstance(result, BaseException):
                logger.exception(
                    "Unexpected error propagating GITHUB_PAT to %s", name
                )
                failed.append(
                    AgentPropagationStatus(
                        agent_name=name, status="failed", error=str(result)
                    )
                )
                continue

            if result.status == "updated":
                updated.append(result.agent_name)
            elif result.status == "failed":
                failed.append(result)
            else:
                skipped.append(result)

    return GithubPatPropagationResult(
        total_running=len(running_agents),
        updated=updated,
        skipped=skipped,
        failed=failed,
    )
