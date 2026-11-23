"""Fleet base-image check (#4483): which running agents are on an old image.

A rebuilt or re-pulled `trinity-agent-base` reaches an agent only at its next
cold start (#1809 — image drift must never turn a start of a running agent
into a container kill). That rule is deliberate and unchanged here. What was
missing is the report: only `trinity-system` raised a stale-image alert
(#1816), so after an upgrade every other agent that nobody stopped kept the
old runtime, and agent-side fixes in the release were silently inert on it.

This module READS and REPORTS. It never stops, starts, restarts or recreates a
container.

* `compare_versions(...)` — pure: is the base image's version label older than
  the backend's release version.
* `run_once()` — one pass: read the fleet through the tri-state
  `agent_container_states()` (#2196), compare each running, non-ephemeral
  agent with `check_base_image_state` (the comparison the cold-start path
  uses — one comparison, one place), and file or end ONE platform alert,
  `fleet_base_image_stale:fleet`, on the system agent's queue.
* `FleetImageCheckService` — the loop: first pass shortly after boot, then
  every `CHECK_INTERVAL_SECONDS`, leader-leased.

Two conditions raise the alert:

1. agents whose container no longer runs the image its own tag resolves to
   (the image was rebuilt or re-pulled underneath them);
2. a base image whose `trinity.base-image-version` label is older than the
   backend. On an update path that did not rebuild the image no agent drifts,
   so (1) alone would stay silent while the whole fleet is on the old runtime.

**"Could not check" is its own answer.** Docker unreadable, an agent whose
comparison failed, a base image with no parseable version label: none of these
is reported as current (the alert is not ended) and none as stale (no alert is
filed for it). The system-agent probe (#1816) set that rule; an alert made by a
check that could not run is worse than no alert.

The system agent is left out: it has its own alert and its own restart route.
Ephemeral agents are left out as they are from the drift recreate
(trinity-enterprise#69) — they are never recreated, so the report could not be
acted on.

Stdlib-only at module level, like `queue_stall_service`: Docker, the database
and the alert seam are imported inside the functions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

ALERT_KIND = "fleet_base_image_stale"
ALERT_KEY = "fleet"
# The host of the one fleet row. Kept as a literal so this module stays
# import-free; equal to `db.agents.SYSTEM_AGENT_NAME`.
ALERT_AGENT_NAME = "trinity-system"

BASE_IMAGE_REF = "trinity-agent-base:latest"
VERSION_LABEL = "trinity.base-image-version"

BASE_CURRENT = "current"
BASE_OUTDATED = "outdated"
BASE_UNKNOWN = "unknown"

LEADER_KEY = "fleetimage:leader"
# A cold start is the only thing that changes an agent's reading, so the set
# moves on a human timescale. One container inspect and one image lookup per
# running agent, every ten minutes.
CHECK_INTERVAL_SECONDS = 600
# Past the other staggered boot loops (last at +13s) and the system agent's own
# deploy, so the first pass reads a settled fleet.
FIRST_CHECK_DELAY_SECONDS = 25

# How many names the alert text spells out; the context carries the full list
# (capped) for the snooze's "a new agent went stale" rule.
_TEXT_MAX_AGENTS = 25
_CONTEXT_MAX_AGENTS = 500

_VERSION_RX = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-rc\.?(\d+))?")
_RELEASE = float("inf")   # a release sorts after every candidate of itself


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------

def _parse_version(value: Any) -> Optional[Tuple[int, int, int, float]]:
    if not isinstance(value, str):
        return None
    match = _VERSION_RX.fullmatch(value.strip())
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), (_RELEASE if rc is None else float(rc))


def compare_versions(image_version: Any, backend_version: Any) -> str:
    """`BASE_OUTDATED` when the image's version is older than the backend's,
    `BASE_CURRENT` when it is the same or newer, `BASE_UNKNOWN` when either
    cannot be read as `X.Y.Z` or `X.Y.Z-rcN` (an absent label, the build
    script's `latest` fallback, an unresolved backend version)."""
    image, backend = _parse_version(image_version), _parse_version(backend_version)
    if image is None or backend is None:
        return BASE_UNKNOWN
    return BASE_OUTDATED if image < backend else BASE_CURRENT


def describe(stale: List[str], base_image: str, image_version: Any,
             backend_version: Any) -> Dict[str, str]:
    """The alert's title and text for a reading that has something to report."""
    parts: List[str] = []
    if stale:
        shown = ", ".join(stale[:_TEXT_MAX_AGENTS])
        more = len(stale) - _TEXT_MAX_AGENTS
        if more > 0:
            shown += f" and {more} more"
        noun = "agent is" if len(stale) == 1 else "agents are"
        parts.append(
            f"{len(stale)} running {noun} still on the agent base image they were "
            f"started from, and a newer one is installed: {shown}. Agent-side changes "
            f"in the release do not apply to them until they restart. Restart the fleet "
            f"(POST /api/ops/fleet/restart), or stop and start the named "
            f"agents. A restart replaces the container, so anything "
            f"installed outside /home/developer is lost."
        )
    if base_image == BASE_OUTDATED:
        parts.append(
            f"The installed agent base image was built for {image_version}, and the "
            f"platform is {backend_version}. Agents started from it get the "
            f"{image_version} runtime. Source install: run "
            f"./scripts/deploy/build-base-image.sh. Hosted install: run "
            f"./scripts/deploy/start.sh --hosted, which pulls the release's image. "
            f"Then restart the fleet."
        )
    elif base_image == BASE_UNKNOWN and stale:
        parts.append(
            "The installed base image's own version could not be checked (it carries "
            "no readable version label), so whether it matches this release is not known."
        )
    if stale:
        title = (f"{len(stale)} agent{'s' if len(stale) != 1 else ''} running an old "
                 f"agent base image")
    else:
        title = "The agent base image is older than the platform"
    return {"title": title, "question": " ".join(parts)}


# ---------------------------------------------------------------------------
# Reads — thin seams over Docker, the database and the release version
# ---------------------------------------------------------------------------

async def _container_states() -> Optional[Dict[str, str]]:
    from services.docker_utils import agent_container_states_async
    return await agent_container_states_async()


def _get_container(agent_name: str):
    from services.docker_service import get_agent_container
    return get_agent_container(agent_name)


async def _image_state(container, agent_name: str) -> str:
    from services.agent_service.helpers import check_base_image_state
    return await check_base_image_state(container, agent_name)


def _is_ephemeral(agent_name: str) -> bool:
    from database import db
    info = db.get_agent_ephemeral_info(agent_name)
    return bool(isinstance(info, dict) and info.get("is_ephemeral"))


async def _base_image_labels() -> Mapping[str, str]:
    from services.docker_utils import image_get
    return (await image_get(BASE_IMAGE_REF)).labels or {}


def _backend_version() -> str:
    from utils.app_version import resolve_release_version
    return resolve_release_version()


def _redis():
    from redis_breaker_util import get_breaker_redis
    return get_breaker_redis()


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

async def _read_agent(agent_name: str) -> Optional[str]:
    """`"match"` / `"drift"` / `"unknown"` for one running agent, or None when
    it is not part of the report (ephemeral). Never raises."""
    try:
        if await asyncio.to_thread(_is_ephemeral, agent_name):
            return None
        container = await asyncio.to_thread(_get_container, agent_name)
        if container is None:
            return "unknown"
        return await _image_state(container, agent_name)
    except Exception:  # noqa: BLE001 — one agent must not skip the rest
        logger.warning("[fleet-image] could not check %s", agent_name, exc_info=True)
        return "unknown"


async def _read_base_image() -> Tuple[str, Any, str]:
    """`(state, image_version, backend_version)`. Never raises."""
    backend_version = _backend_version()
    try:
        image_version = (await _base_image_labels()).get(VERSION_LABEL)
    except Exception as exc:  # noqa: BLE001 — an unreadable image is "unknown"
        logger.warning("[fleet-image] could not read %s (%s)", BASE_IMAGE_REF,
                       type(exc).__name__)
        return BASE_UNKNOWN, None, backend_version
    return compare_versions(image_version, backend_version), image_version, backend_version


async def run_once() -> Dict[str, Any]:
    """One pass over the fleet. Files, updates or ends the one fleet alert.

    Returns `{"status": "stale" | "current" | "could_not_check", ...}`.
    Never raises, and never changes a container."""
    try:
        return await _run_once()
    except Exception:  # noqa: BLE001 — a report must never break its loop
        logger.error("[fleet-image] check failed", exc_info=True)
        return {"status": "could_not_check", "reason": "error"}


async def _run_once() -> Dict[str, Any]:
    from services import platform_alerts

    states = await _container_states()
    if states is None:
        # #2196: "Docker could not be asked" is not "no agent is stale".
        logger.warning("[fleet-image] could not check: Docker is unreadable; "
                       "the alert is left as it is")
        return {"status": "could_not_check", "reason": "docker_unreadable"}

    stale: List[str] = []
    unknown: List[str] = []
    for name in sorted(states):
        if states[name] != "running" or name == ALERT_AGENT_NAME:
            continue
        state = await _read_agent(name)
        if state == "drift":
            stale.append(name)
        elif state == "unknown":
            unknown.append(name)

    base_image, image_version, backend_version = await _read_base_image()
    result: Dict[str, Any] = {"stale": stale, "unknown": unknown, "base_image": base_image}

    if stale or base_image == BASE_OUTDATED:
        text = describe(stale, base_image, image_version, backend_version)
        outcome = platform_alerts.observe(
            ALERT_AGENT_NAME, ALERT_KIND, ALERT_KEY,
            # An agent running without a release's fixes outranks an image that
            # only new starts would pick up.
            priority="high" if stale else "medium",
            title=text["title"],
            question=text["question"],
            context={
                "agents": stale[:_CONTEXT_MAX_AGENTS],
                "agents_stale": len(stale),
                "agents_unchecked": len(unknown),
                "base_image": base_image,
            },
        )
        logger.warning("[fleet-image] %d stale agent(s), base image %s: alert %s",
                       len(stale), base_image, outcome)
        return {"status": "stale", **result}

    if unknown or base_image == BASE_UNKNOWN:
        # Nothing proven stale, and not everything proven current: say so and
        # leave a pending alert alone — it may be about what could not be read.
        logger.warning(
            "[fleet-image] could not check: %d agent(s) unreadable, base image "
            "version %s; not reported as up to date", len(unknown), base_image)
        return {"status": "could_not_check", "reason": "partial", **result}

    platform_alerts.clear(ALERT_AGENT_NAME, ALERT_KIND, ALERT_KEY)
    return {"status": "current", **result}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class FleetImageCheckService:
    """Runs `run_once` shortly after boot and then on an interval.

    Starts in every uvicorn worker; only the holder of `LEADER_KEY` runs a
    cycle (SET NX, own-lease refresh, released on `stop()` — the
    `monitoring:leader` shape, #1464). **Fail-open:** with Redis down every
    worker runs it. Each write goes through the one-row-per-subject alert
    seam, so a duplicated cycle updates the same row; failing closed would
    silence the report exactly when the platform is degraded.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def start(self) -> None:
        """Schedule the loop. Never raises and never runs a pass inline, so
        the boot phase that calls it is not blocked or broken by it."""
        if self._running:
            return
        try:
            self._task = asyncio.create_task(self._run_loop())
            self._running = True
        except Exception:  # noqa: BLE001 — e.g. no running loop
            logger.error("[fleet-image] could not start the check", exc_info=True)

    def stop(self) -> None:
        """Cancel the loop and release the lease. Never raises."""
        self._running = False
        try:
            if self._task:
                self._task.cancel()
        except Exception:  # noqa: BLE001
            pass
        self._release_leadership()

    def _try_acquire_leadership(self) -> bool:
        ttl = CHECK_INTERVAL_SECONDS * 3
        try:
            r = _redis()
            if r is None:
                return True
            if r.set(LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            if r.get(LEADER_KEY) == self._worker_id:
                r.expire(LEADER_KEY, ttl)
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("[fleet-image] leader lock check failed open (%s)",
                           type(exc).__name__)
            return True

    def _release_leadership(self) -> None:
        try:
            r = _redis()
            if r is not None and r.get(LEADER_KEY) == self._worker_id:
                r.delete(LEADER_KEY)
        except Exception:  # noqa: BLE001
            pass

    async def run_cycle(self) -> Optional[Dict[str, Any]]:
        """One leader-gated pass; None when another worker holds the lease.

        The same lease and cadence also run the managed-hooks fleet check
        (trinity-enterprise#1176); its failure never breaks this one."""
        if not await asyncio.to_thread(self._try_acquire_leadership):
            return None
        result = await run_once()
        try:
            from services import managed_hooks_check_service
            await managed_hooks_check_service.run_once()
        except Exception:  # noqa: BLE001 — a second report must not break the first
            logger.error("[fleet-image] managed-hooks check failed", exc_info=True)
        return result

    async def _run_loop(self) -> None:
        delay = FIRST_CHECK_DELAY_SECONDS
        while True:
            await asyncio.sleep(delay)
            delay = CHECK_INTERVAL_SECONDS
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.error("[fleet-image] cycle failed", exc_info=True)


fleet_image_check_service = FleetImageCheckService()
