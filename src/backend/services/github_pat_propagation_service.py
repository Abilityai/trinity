"""
GitHub PAT propagation service (#211).

Pushes the global GitHub PAT to running agents when it is updated in Settings,
so agents pick up the new token without a restart.

Eligibility rules:
- Agent container must be running.
- Agent must NOT have a per-agent PAT (#347) configured — those override the global
  and are managed separately.
- Agent must either have a Trinity-managed git config (a `github_repo`) OR already
  carry a `GITHUB_PAT` line in its `.env`. Agents with neither never set up GitHub
  and are skipped, so a rotation does not spray the token into containers that
  have no use for it.

#1967 — WHAT ROTATION ACTUALLY REQUIRES
---------------------------------------
Two independent gaps each fully defeated a global rotation, and both existed
because this path and the per-agent path (`propagate_pat_to_single_agent`, #1264)
had drifted apart:

1. **The eligibility gate was `.env`-shaped.** It required an existing
   `GITHUB_PAT` line, so an agent provisioned from a GitHub template — which
   ships `.env.example` and no `.env` — skipped as `skipped_no_pat`. On a fleet
   of such agents, *every* agent skipped and the endpoint still answered
   `success: true`.

2. **`.env` is not where git authenticates from.** Clones are created as
   `https://oauth2:<PAT>@github.com/<org>/<repo>.git` and that URL is persisted
   in `.git/config` on the workspace volume. Rewriting `.env` changes nothing
   for the running `git` process. Only re-templating the remote restores
   fetch/push before a restart — which the per-agent path already did and this
   one did not.

So the `.env` write is the *next-start* fix and the remote rewrite is the *now*
fix, and a rotation needs both. `_apply_pat_to_agent` below is now the single
body both paths call, so the next divergence has to be deliberate.
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

# #1574: the same managed token also authenticates the `gh` CLI + REST API, which
# read GH_TOKEN/GITHUB_TOKEN. The no-restart propagation keeps all three in sync in
# the agent's .env so a next-start / .env-sourcing shell has them too (the live git
# remote-URL rewrite remains the load-bearing immediate fix — see the callers).
_TOKEN_ENV_KEYS = ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN")


def _format_pat_line(pat: str, key: str = "GITHUB_PAT") -> str:
    """Format a `KEY="value"` .env line matching the agent's own .env writer.

    The agent writes credentials as `KEY="value"` with embedded double quotes
    escaped (see docker/base-image/agent_server/routers/credentials.py).
    """
    escaped = pat.replace('"', '\\"')
    return f'{key}="{escaped}"'


def _patch_env_github_pat(env_content: str, new_pat: str) -> str:
    """Return env_content with GITHUB_PAT (and the #1574 gh mirrors) set to
    ``new_pat`` — EVERY occurrence replaced in place if present, else appended.

    Every occurrence, not the first (#2016). `count=1` left a second
    ``GITHUB_PAT=`` line untouched, and the agent's own ``.env`` reader is
    **last-wins** — so on a file carrying a duplicate the rotation wrote the new
    token to line 1, the revoked token survived below it, and the agent went on
    authenticating with the revoked one while the rotation reported the agent as
    ``updated``. That is the same silent-success failure #1967 exists to close,
    reached by a different route.

    The duplicate is not created here (this function appends only when the key
    is absent). It arrives from the paths that can also write the file: an agent
    editing its own ``.env`` (#1999), an operator appending over SSH or
    ``docker exec``, or a restored/hand-merged file.

    Duplicates are levelled, not de-duplicated. After this every copy carries
    the same value, so last-wins reads the right token whichever line it lands
    on, and the file keeps whatever structure the operator gave it — removing
    lines would be a second behaviour change for no correctness gain.
    """
    out = env_content
    for key in _TOKEN_ENV_KEYS:
        line_re = re.compile(rf'(?m)^[ \t]*{key}=.*$')
        new_line = _format_pat_line(new_pat, key)
        if line_re.search(out):
            out = line_re.sub(new_line, out)
        else:
            suffix = "" if (out == "" or out.endswith("\n")) else "\n"
            out = f"{out}{suffix}{new_line}\n"
    return out


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


async def _apply_pat_to_agent(
    client: httpx.AsyncClient,
    agent_name: str,
    pat: str,
    *,
    add_if_missing: bool,
) -> tuple[str, bool]:
    """Apply ``pat`` to one running agent: `.env` write **and** live remote rewrite.

    #1967: the single shared body for both propagation paths. Previously the
    global path wrote only `.env` and the per-agent path did both, so a global
    rotation left every clone authenticating with the revoked token until its
    container was restarted — for as long as it stayed up.

    Returns ``(env_status, remote_updated)`` where ``env_status`` is
    ``"updated"`` or ``"skipped_no_pat"``. The `.env` write raises httpx errors
    for the caller to classify; the remote rewrite is best-effort by contract
    (`git_service.update_remote_pat` returns False rather than raising), because
    a container that cannot be exec'd into must not fail the whole rotation for
    the agents that can.
    """
    from services import git_service

    env_status = await _apply_pat_to_env(
        client, agent_name, f"http://agent-{agent_name}:8000", pat,
        add_if_missing=add_if_missing,
    )

    # The load-bearing half. Only meaningful for an agent whose clone Trinity
    # templated in the first place — `update_remote_pat` no-ops without a repo.
    git_config = db.get_git_config(agent_name)
    github_repo = git_config.github_repo if git_config else None
    remote_updated = (
        await git_service.update_remote_pat(agent_name, pat, github_repo)
        if github_repo else False
    )
    return env_status, remote_updated


def _agent_has_git_config(agent_name: str) -> bool:
    """True when Trinity manages a GitHub repo for this agent.

    #1967: the eligibility signal that replaces "the `.env` already has a
    `GITHUB_PAT` line". A template-provisioned agent has a git config from the
    moment it is created but no `.env` until something writes one, which is
    exactly the population the old gate excluded.
    """
    try:
        git_config = db.get_git_config(agent_name)
        return bool(git_config and git_config.github_repo)
    except Exception:  # noqa: BLE001 — eligibility must not break the rotation
        logger.warning("could not read git config for %s", agent_name, exc_info=True)
        return False


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
    # #1264 review: query the single container instead of enumerating the fleet.
    # get_agent_container is a module-level import (above) so it's patchable as a
    # stable module global, not resolved from sys.modules at call time.
    container = get_agent_container(agent_name)
    if container is None or container.status != "running":
        return {"applied": False, "reason": "agent_not_running"}

    env_updated = False
    remote_updated = False
    async with httpx.AsyncClient(timeout=AGENT_HTTP_TIMEOUT_SECONDS) as client:
        try:
            # #1967: now the shared body. The remote rewrite used to sit outside
            # this try, so it still ran when the `.env` write failed — preserved
            # below by catching only the `.env` error and continuing.
            env_status, remote_updated = await _apply_pat_to_agent(
                client, agent_name, pat, add_if_missing=True
            )
            env_updated = env_status == "updated"
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning("single-agent PAT .env inject failed for %s: %s", agent_name, e)
            # The .env write is the half that failed; the remote rewrite is the
            # load-bearing one and is independent of it, so still attempt it.
            from services import git_service

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
    """Apply the new global PAT to one agent: `.env` + live remote (global path).

    #1967: `add_if_missing` is now derived per agent rather than hard-coded
    False. An agent Trinity manages a repo for GETS the line written even when
    its `.env` does not exist — that is the template-provisioned population the
    old gate silently excluded. An agent with no git config keeps the
    conservative behaviour (update an existing line, never create one), so a
    rotation still does not inject the token into containers that have no use
    for it.
    """
    has_git = _agent_has_git_config(agent_name)
    try:
        status, remote_updated = await _apply_pat_to_agent(
            client, agent_name, new_pat, add_if_missing=has_git
        )
        return AgentPropagationStatus(
            agent_name=agent_name, status=status, remote_updated=remote_updated
        )
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
    remotes_updated = 0

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
                if result.remote_updated:
                    remotes_updated += 1
            elif result.status == "failed":
                failed.append(result)
            else:
                skipped.append(result)

    # #1967: log the shape an operator would otherwise only see in the response
    # body. A rotation that updated nothing on a non-empty fleet is the exact
    # silent failure this issue reports, and it deserves a WARNING in the
    # platform log — not just a green line in a Settings panel nobody is looking
    # at during an incident.
    if running_agents and not updated:
        logger.warning(
            "GitHub PAT rotation reached 0 of %d running agents "
            "(skipped=%d, failed=%d) — agents keep authenticating with the "
            "previous token until restarted",
            len(running_agents), len(skipped), len(failed),
        )
    elif updated and remotes_updated < len(updated):
        logger.warning(
            "GitHub PAT rotation updated %d agents but re-templated only %d "
            "live git remotes — the remainder keep using the previous token "
            "for git until restarted",
            len(updated), remotes_updated,
        )

    return GithubPatPropagationResult(
        total_running=len(running_agents),
        updated=updated,
        skipped=skipped,
        failed=failed,
        remotes_updated=remotes_updated,
    )
