"""ent#615 — the credential helper install, the embedded-token sweep, and the
per-agent / fleet-wide scrub scheduling.

Carved out of `remotes.py` after the #2757 port pushed it past the 800-line
critical threshold (#1028). Sibling modules reach these through the module
object (`token_scrub.scrub_git_remote_tokens(...)`), never `from .token_scrub
import ...`, so a test that patches this module reaches every caller.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Dict, Optional

from database import db
from services import git_credential_helper
from services.docker_service import execute_command_in_container

from . import gitignore

logger = logging.getLogger(__name__)


# How long the in-container remediation sweep may run. Enforced TWICE, because
# the two bounds free different resources: an in-container `timeout N` prefix
# frees the `_docker_executor` pool thread (the fixed 6-thread pool the whole
# backend shares — `to_thread` draws from it too), and `asyncio.wait_for` frees
# the caller. `execute_command_in_container` accepts a `timeout` and forwards it
# nowhere, so without both a wedged exec pins a pool thread forever, fleet-wide,
# from a background pass.
SCRUB_TIMEOUT_S = 60

# One operator alarm per agent per day when the sweep REFUSED to strip: a
# stable id so a restart loop cannot flood the queue while an operator is
# already looking at it (the `archive_storage` precedent).
_SCRUB_ALARM_ID_PREFIX = "ent615-git-token-scrub-"

# Same daily-stable-id discipline for the "could not look" alarm. A SEPARATE
# family, not a variant of the refusal: the two need different operator action
# (set a token vs. re-run with full capabilities), and sharing one id would let
# whichever fired first suppress the other for the rest of the day.
_SCRUB_UNREADABLE_ALARM_ID_PREFIX = "ent615-git-token-scrub-unreadable-"

# Upserts `GITHUB_PAT` in the workspace `.env` — the FIRST rung of the
# credential helper's ladder, and therefore the one a rotation must reach.
#
# The value arrives in the exec's ENVIRONMENT, never on its argv: an exec's argv
# is visible in the container's process table, which is the exact leak this
# whole issue is about, and base64-ing a token onto argv would relocate it, not
# remove it. The exec runs as ROOT so `/proc/<pid>/environ` is unreadable by the
# agent (same uid could read it for the life of the exec); the file it writes
# keeps `.env`'s existing ownership and mode.
#
# This is the belt to `github_pat_propagation_service._apply_pat_to_env`, which
# writes the same line over `http://agent-<name>:8000/api/credentials/inject`
# and RAISES when the agent server is wedged, restarting or OOM. `docker exec`
# works in all three. The HTTP write stays primary because only it runs
# `sync_process_env()`.
_ENV_PAT_UPSERT_SCRIPT = r"""
set -u
ENV_FILE="${TRINITY_AGENT_HOME:-/home/developer}/.env"
[ -n "${TRINITY_NEW_GITHUB_PAT:-}" ] || exit 2
TMP="${ENV_FILE}.trinity-ent615.$$"
if [ -f "${ENV_FILE}" ]; then
    # `[[:space:]]`, not `[ \t]`: a POSIX bracket expression takes backslash
    # literally, so `[ \t]` is "space, backslash or t" and a TAB-indented line
    # would survive as a duplicate.
    grep -v '^[[:space:]]*GITHUB_PAT=' "${ENV_FILE}" > "${TMP}" 2>/dev/null || : > "${TMP}"
else
    : > "${TMP}" || exit 3
fi
printf 'GITHUB_PAT=%s\n' "${TRINITY_NEW_GITHUB_PAT}" >> "${TMP}" || exit 4
chmod --reference="${ENV_FILE}" "${TMP}" 2>/dev/null || chmod 600 "${TMP}" 2>/dev/null || true
chown --reference="${ENV_FILE}" "${TMP}" 2>/dev/null || chown developer:developer "${TMP}" 2>/dev/null || true
mv -f "${TMP}" "${ENV_FILE}" || exit 5
"""


async def write_container_github_pat(agent_name: str, github_pat: str) -> bool:
    """Write ``GITHUB_PAT`` into the container's ``.env`` over ``docker exec``.

    Returns True on success. Never raises — the caller decides what a failure
    means, and for every caller here it means "do not strip anything".
    """
    if not github_pat:
        return False
    try:
        result = await execute_command_in_container(
            container_name=f"agent-{agent_name}",
            command=f"bash -c {shlex.quote(_ENV_PAT_UPSERT_SCRIPT)}",
            user="root",
            environment={"TRINITY_NEW_GITHUB_PAT": github_pat},
        )
    except Exception as e:  # noqa: BLE001 — container may be down
        logger.warning("ent#615: .env PAT write raised for %s: %s", agent_name, e)
        return False
    ok = result.get("exit_code", 1) == 0
    if not ok:
        # NEVER the output: this exec's stdout/stderr could echo the file it was
        # editing. Only the exit code, which the script makes specific.
        logger.warning(
            "ent#615: .env PAT write failed for %s (exit %s)",
            agent_name, result.get("exit_code"),
        )
    return ok


def _alarm_git_token_scrub_refused(agent_name: str, report: Dict[str, Any]) -> None:
    """One operator alarm when the sweep left a token URL in place.

    Refusing is the CORRECT outcome — the sweep never strips a credential it
    could not replace, because the agent whose only credential lives in its
    `origin` URL (the `POST /{agent}/git/initialize` orphan class) is stranded
    permanently by a strip-first sweep. But a refusal is also invisible, so it
    is queued rather than logged and forgotten. Fail-soft.
    """
    try:
        from database import db
        from utils.helpers import utc_now_iso

        db.create_operator_queue_item(agent_name, {
            "id": f"{_SCRUB_ALARM_ID_PREFIX}{agent_name}-{utc_now_iso()[:10]}",
            "type": "alert",
            "priority": "medium",
            "title": "A git remote still carries an embedded credential",
            "question": (
                f"{agent_name} has {report.get('refused', 0)} git remote URL(s) "
                "with an embedded credential that Trinity deliberately did NOT "
                "remove: no replacement credential could be resolved, and "
                "removing it would have left the agent unable to fetch or push "
                "at all. Set a GitHub token for this agent (Git tab → add a "
                "GitHub token) and the next start will finish the job."
            ),
            "context": {k: v for k, v in report.items()},
            "created_at": utc_now_iso(),
        })
    except Exception:  # noqa: BLE001 — alarm plumbing must never break a sweep
        logger.exception("ent#615: could not file the scrub-refused alarm for %s", agent_name)


def _alarm_git_token_scrub_unreadable(agent_name: str) -> None:
    """One operator alarm when the sweep could not read the tree it swept.

    Distinct from a refusal: a refusal means the sweep LOOKED, found a token it
    could not replace, and correctly left it. This means it could not look —
    `find` enumerated nothing and the probe failed — so an all-zero report says
    nothing about whether a token is there. Nothing is destroyed; the
    remediation simply did not run. Fail-soft.
    """
    try:
        from database import db
        from utils.helpers import utc_now_iso

        db.create_operator_queue_item(agent_name, {
            "id": f"{_SCRUB_UNREADABLE_ALARM_ID_PREFIX}{agent_name}-{utc_now_iso()[:10]}",
            "type": "alert",
            "priority": "medium",
            "title": "Trinity could not check this agent's git remotes",
            "question": (
                f"The ent#615 credential sweep could not read {agent_name}'s "
                "workspace, so it cannot say whether a git remote still carries "
                "an embedded token — its all-clear is not evidence. This is the "
                "expected result when the agent runs with reduced Linux "
                "capabilities (Settings \u2192 agent full capabilities off), "
                "which withholds DAC_OVERRIDE from the platform's maintenance "
                "exec. Re-run with full capabilities, or check the agent's git "
                "remotes by hand."
            ),
            "context": {"reason": "root_readable=0"},
            "created_at": utc_now_iso(),
        })
    except Exception:  # noqa: BLE001 — alarm plumbing must never break a sweep
        logger.exception(
            "ent#615: could not file the scrub-unreadable alarm for %s", agent_name
        )


async def scrub_git_remote_tokens(
    agent_name: str,
    git_dir: Optional[str] = None,
    seed_pat: str = "",
) -> Dict[str, Any]:
    """Install the credential helper, then remove embedded credentials (ent#615).

    Idempotent. One root ``docker exec`` of a base64-injected script (the
    ``compatibility/collector.py`` precedent — it avoids the nested-quoting
    hazard entirely), which:

    1. installs and REGISTERS the helper, so a container on an older base image
       is covered without a recreate;
    2. proves the helper actually resolves a credential — **by exit code**,
       because ``git credential fill`` prints the credential on stdout and this
       exec's output reaches ``logger`` and ``GitInitResult.error``;
    3. harvests, only for an agent with no other source, into the helper's last
       rung (``/home/developer/.trinity/git-credential``, 0600) — deliberately
       NOT `.env`, whose ``GITHUB_PAT`` is exported as ``GH_TOKEN``/
       ``GITHUB_TOKEN`` and gates the ent#123 push blackhole, so writing that
       name would be a privilege GRANT (the ent#162 class), not a relocation;
    4. strips userinfo from every remote in every config under ``.git`` —
       including NESTED submodules, which a ``.git/modules/*/config`` glob
       misses — and from ``url.<base>.insteadOf`` subsections, scoped to the
       configured protocol+host so a foreign remote's credential is neither
       promoted nor stripped;
    5. REFUSES, and reports, rather than stripping what it could not replace.

    ``seed_pat`` is a credential the CALLER already holds — the path that stops
    new orphans being created. It is tried before the URL harvest and only when
    nothing else resolves, and it travels in the exec ENVIRONMENT, never argv.
    The exec runs as root so ``/proc/<pid>/environ`` is unreadable by the agent.

    Returns the parsed report (counts only — never a URL, host or value) plus
    ``success``. Never raises.
    """
    container_name = f"agent-{agent_name}"
    report: Dict[str, Any] = git_credential_helper.parse_scrub_report("")
    report["success"] = False
    try:
        if git_dir is None:
            git_dir = await gitignore._detect_git_dir(container_name)
        inner = git_credential_helper.scrub_command(git_dir)
        async with _scrub_semaphore:
            result = await asyncio.wait_for(
                execute_command_in_container(
                    container_name=container_name,
                    command=f"timeout {SCRUB_TIMEOUT_S} bash -c {shlex.quote(inner)}",
                    user="root",
                    environment=(
                        {git_credential_helper.SEED_ENV_VAR: seed_pat}
                        if seed_pat else None
                    ),
                ),
                timeout=SCRUB_TIMEOUT_S + 15,
            )
    except asyncio.TimeoutError:
        logger.warning("ent#615: remote-token sweep timed out for %s", agent_name)
        return report
    except Exception as e:  # noqa: BLE001 — best-effort, container may be down
        logger.warning("ent#615: remote-token sweep error for %s: %s", agent_name, e)
        return report

    output = result.get("output", "") or ""
    report.update(git_credential_helper.parse_scrub_report(output))
    report["success"] = result.get("exit_code", 1) == 0

    if report["refused"]:
        _alarm_git_token_scrub_refused(agent_name, report)
    elif report["success"] and not report["root_readable"]:
        # The sweep ran, exited 0, and reported all zeros — which is ALSO what a
        # healthy, already-clean agent reports. The discriminator is whether it
        # could see the tree at all. It could not on `agent_full_capabilities=
        # false`: RESTRICTED_CAPABILITIES omits DAC_OVERRIDE, so root in the
        # container cannot traverse the 0700 `developer`-owned home, `find`
        # enumerates nothing and the probe fails. Nothing is destroyed — the
        # token simply stays where it already was — but the remediation silently
        # did not happen, on exactly the hardened installs that will act on the
        # report. Alarmed, not logged: an INFO line indistinguishable from
        # success is how #1638 shipped.
        _alarm_git_token_scrub_unreadable(agent_name)
    if report["gitmodules_hits"]:
        # A token in the TRACKED `.gitmodules` is already committed and pushed.
        # The sweep cannot fix that; finding one turns "rotate the platform PAT
        # after adoption" from advice into a requirement.
        logger.error(
            "ent#615: %s has %s credential-bearing url(s) in its TRACKED "
            ".gitmodules — already committed and pushed; the platform token "
            "must be rotated",
            agent_name, report["gitmodules_hits"],
        )
    if report["competing_helpers"]:
        # Helper lists COMPOSE. A helper registered ahead of ours answers first
        # and masks it, which would also make the probe above report a
        # credential this helper never produced.
        logger.warning(
            "ent#615: %s has %s git credential helper(s) besides trinity",
            agent_name, report["competing_helpers"],
        )
    logger.info("ent#615: remote-token sweep for %s: %s", agent_name, report)
    return report


# Bounds EVERY caller of the sweep against the fixed `_docker_executor` pool the
# whole backend shares (`to_thread` draws from it too, #2433). One bound, at the
# sweep itself, rather than one per caller — because the caller that actually
# needed it is not the obvious one: `propagate_github_pat` gathers over the
# WHOLE FLEET with no bound of its own, and this change takes each agent from
# one exec to three. On a 50-agent rotation that is 150 execs against 6 threads,
# each holding its thread for up to the in-container `timeout`.
_SCRUB_CONCURRENCY = 3
_scrub_semaphore = asyncio.Semaphore(_SCRUB_CONCURRENCY)

# The one-shot's lease. TTL is generous: the whole point is that a second worker
# skips rather than duplicates, and an expiry mid-sweep just means a duplicate
# idempotent pass.
_FLEET_SCRUB_LOCK_KEY = "git:ent615-token-scrub:boot"
_FLEET_SCRUB_LOCK_TTL_S = 900


async def sweep_fleet_git_remote_tokens() -> Dict[str, int]:
    """One-shot, whole-fleet ent#615 remediation. Runs at boot, not on a loop.

    **Why a one-shot and not a recurring service.** After this change no code
    path produces a token-bearing remote URL, so there is no recurring producer
    for a recurring service to chase. A permanent loop plus a lease would be
    permanent surface bought for a transient problem. The three reachers are:
    this, the `start_agent_internal` hook (per agent, on every start), and
    `startup.sh`'s conditional per-restart rewrite. The one-shot is the one that
    covers a `restart: unless-stopped` container the Docker daemon brings back
    after a HOST REBOOT, which never passes through `start_agent_internal`.

    **Why a fail-open lease is right here**, unlike on most credential-mutating
    work: losing the lease costs a duplicate pass of an operation that is
    idempotent by construction and REFUSES rather than destroying when it
    cannot place a replacement. The failure mode of fail-open is "the sweep ran
    twice"; the failure mode of fail-closed would be "an install with no Redis
    never gets remediated at all".
    """
    from redis_breaker_util import SingleFlightLock, get_breaker_redis
    from services.docker_service import list_all_agents_fast

    lock = SingleFlightLock(
        _FLEET_SCRUB_LOCK_KEY, _FLEET_SCRUB_LOCK_TTL_S, client=get_breaker_redis()
    )
    totals = {"agents": 0, "remotes_scrubbed": 0, "harvested": 0, "refused": 0}
    if not lock.acquire():
        return totals

    # RELEASED when the pass ends, not left to expire. The lease exists to stop
    # N workers of the SAME boot sweeping N times — `acquire` never waits, so a
    # loser has already returned and releasing frees nobody prematurely. Letting
    # it run out the TTL instead would silently skip the sweep on a deliberate
    # restart inside the window, which is the one moment an operator is most
    # likely to want it.
    try:
        try:
            agents = [a.name for a in list_all_agents_fast() if a.status == "running"]
        except Exception as e:  # noqa: BLE001 — Docker may be unreadable at boot
            logger.warning("ent#615: fleet sweep could not enumerate agents: %s", e)
            return totals

        async def _one(name: str) -> None:
            # No bound here: `scrub_git_remote_tokens` carries the shared one, so
            # this pass cannot out-run a concurrent rotation or start hook.
            report = await scrub_git_remote_tokens(name)
            totals["agents"] += 1
            for key in ("remotes_scrubbed", "harvested", "refused"):
                totals[key] += report.get(key, 0)

        await asyncio.gather(*(_one(name) for name in agents), return_exceptions=True)
        logger.info("ent#615: fleet remote-token sweep complete: %s", totals)
        return totals
    finally:
        lock.release_if_owned()


# Staggered +20s, behind every other boot loop.
_FLEET_SCRUB_BOOT_DELAY_S = 20

# Strong ref. Unlike the staggered loops it sits beside, this task does REAL
# work after its sleep and runs exactly once per boot, so a GC-collected task is
# a remediation that silently never happened (the #1083 asyncio footgun, same
# remedy as `main._first_run_seed_task`).
_fleet_scrub_task: Optional["asyncio.Task"] = None


def schedule_fleet_git_remote_token_sweep() -> None:
    """Fire the boot one-shot after a stagger. Never raises."""

    async def _run() -> None:
        await asyncio.sleep(_FLEET_SCRUB_BOOT_DELAY_S)
        try:
            await sweep_fleet_git_remote_tokens()
        except Exception as e:  # noqa: BLE001 — a boot task must not kill boot
            logger.error("ent#615: fleet remote-token sweep failed: %s", e)

    global _fleet_scrub_task
    coro = _run()
    try:
        _fleet_scrub_task = asyncio.create_task(coro)
    except RuntimeError as e:
        coro.close()
        logger.debug("ent#615: fleet sweep not scheduled (no loop): %s", e)


def spawn_git_remote_token_scrub(agent_name: str) -> None:
    """Fire the per-agent sweep fire-and-forget from a start path (ent#615).

    Mirrors `spawn_gitignore_merge_after_clone` (#2069) — with ONE deliberate
    difference: that one is gated on `db.get_git_auto_sync_enabled`, and
    copying that gate here would silently skip every agent that does not
    auto-sync, which has nothing to do with whether its `.git/config` holds a
    credential.

    A strong ref defeats the asyncio `create_task` GC footgun. With no running
    loop the coro is closed and the spawn is skipped (logged), never raised —
    the boot one-shot and `startup.sh` are the backstops.
    """
    coro = scrub_git_remote_tokens(agent_name)
    try:
        task = asyncio.create_task(coro)
        _inflight_token_scrub_tasks.add(task)
        task.add_done_callback(_inflight_token_scrub_tasks.discard)
    except RuntimeError as e:
        coro.close()
        logger.debug("ent#615: spawn_git_remote_token_scrub skipped (no loop): %s", e)


_inflight_token_scrub_tasks: set = set()


