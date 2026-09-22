"""Declared metric registry — reconcile service (trinity-enterprise#477).

Sits between the tolerant reader (`services/template_metrics.py`) and the store
(`db/metric_definitions.py`), and owns the one thing neither of them can: how a
`template.yaml` is obtained at each of the six trigger points.

    create (github / local / snapshot-import)  → the template dict the creation
        resolver already parsed                → `reconcile_metric_definitions`

    git pull / reset / sync-pull_first / container start / explicit refresh
        → the agent's live `template.yaml`     → `refresh_from_running_agent`

**Why a live read and not an agent-server endpoint (E4).** `/api/template-info`
does not return `metrics:` at all, and `/api/metrics` is the path ent#478
supersedes — neither is a door for this. The transport is
`docker_service.execute_command_in_container` (`cat`, fixed argv, output
capped), the same door the compatibility collector already uses, and the parse
is the backend's own hardened loader — so the template read on a pull is the
SAME parse as the one at creation, and the two cannot disagree about one file.

**Never retire on absence of evidence (#2196).** A failed exec, an unparseable
file and a missing container are all *unreadable*, not *"the author removed the
block"*. `refresh_from_running_agent` raises `RefreshUnavailable` in every one
of those cases and the registry is left exactly as it was. Only a template that
PARSED and genuinely carries no `metrics:` retires its rows.

Import edges (no cycles): `crud.py` / `routers/git.py` / `routers/agent_files.py`
/ `lifecycle.py` → here → `{template_metrics, database, docker_service,
utils.safe_yaml}`. This module never imports `services.agent_service` (#1991).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from database import db
from services import template_metrics
from utils.safe_yaml import load_template_yaml

logger = logging.getLogger(__name__)

# The agent's template lives at a fixed path in a fixed image layout — there is
# no user input anywhere in this argv.
TEMPLATE_PATH = "/home/developer/template.yaml"

# A `template.yaml` is a hand-authored manifest; the largest bundled one is a
# few KB. Refusing above this bounds the parse BEFORE the loader runs, which is
# the half that matters (a hardened loader still has to read what it is given).
MAX_TEMPLATE_BYTES = 256 * 1024

# Bounds the exec itself — `execute_command_in_container` does not forward its
# `timeout`, so the bound has to be in the container (`timeout N`) and around
# the await (`asyncio.wait_for`). The in-container prefix is the one that frees
# the Docker pool thread.
_EXEC_TIMEOUT = 15

# Every trigger that may write the registry. Recorded on the row so an operator
# can tell "this came in with the agent" from "this arrived on a pull".
SOURCES = ("create", "import", "pull", "reset", "sync", "start", "refresh")


class RefreshUnavailable(Exception):
    """The agent's `template.yaml` could not be READ or PARSED.

    Distinct from "the template declares no metrics", which is a legitimate
    reconcile that retires rows. Carries a stable `reason` code so the route
    can name it (`agent_not_running`, `template_unreadable`) instead of
    surfacing a 500.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class ReconcileSummary:
    """What one reconcile did, in the operator's terms."""

    agent_name: str
    source: str
    declared: int = 0
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    revived: List[str] = field(default_factory=list)
    retired: List[str] = field(default_factory=list)
    unchanged: int = 0
    type_change_refused: List[Dict[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.updated or self.revived or self.retired
            or self.type_change_refused
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "source": self.source,
            "declared": self.declared,
            "created": self.created,
            "updated": self.updated,
            "revived": self.revived,
            "retired": self.retired,
            "unchanged": self.unchanged,
            "type_change_refused": self.type_change_refused,
            "errors": self.errors,
        }


def reconcile_metric_definitions(
    agent_name: str,
    template_data: Any,
    *,
    source: str,
) -> ReconcileSummary:
    """Reconcile an agent's registry against an ALREADY-PARSED template dict.

    The creation path's entry point: `crud.py` has the template in hand (from
    the GitHub API, an uploaded `local:` template, or a staged snapshot tree),
    so re-reading it would be a second source that can disagree with the first.

    A `template_data` that is not a mapping is treated as "declares nothing" —
    the reader is total and the creation resolvers already degrade to `{}`;
    there is no shape here that can make this raise.
    """
    block = template_data.get("metrics") if isinstance(template_data, dict) else None
    declared, errors = (
        template_metrics.normalize_declared_metrics(block),
        template_metrics.metric_shape_errors(block),
    )
    return _reconcile(agent_name, declared, errors, source=source)


async def refresh_from_running_agent(
    agent_name: str, *, source: str
) -> ReconcileSummary:
    """Re-read the agent's live `template.yaml` and reconcile from it.

    Raises `RefreshUnavailable` when the file could not be read or parsed — the
    registry is left untouched in that case (see the module docstring).
    """
    raw = await _read_template_from_container(agent_name)
    try:
        template_data = load_template_yaml(raw)
    except Exception as e:  # noqa: BLE001 — the hardened loader raises its own
        # Type name only: the message can embed untrusted template content, and
        # this reason reaches an API response and the logs.
        logger.warning(
            "[ent#477] template.yaml for %s did not parse (%s) — registry "
            "left untouched", agent_name, type(e).__name__,
        )
        raise RefreshUnavailable(
            "template_unreadable",
            f"template.yaml could not be parsed ({type(e).__name__}) — see T-001",
        ) from e

    if not isinstance(template_data, dict):
        raise RefreshUnavailable(
            "template_unreadable",
            "template.yaml is not a mapping — see T-001",
        )

    return reconcile_metric_definitions(agent_name, template_data, source=source)


def spawn_refresh_from_running_agent(agent_name: str, *, source: str) -> None:
    """Fire `refresh_from_running_agent` fire-and-forget (T1).

    The container-start hook. Mirrors `git_service.spawn_gitignore_merge_after_
    clone`: zero added start latency, a strong task ref so the asyncio
    `create_task` GC footgun cannot drop it, and a closed coro + skip (never a
    raise) when there is no running loop. Every failure mode is a WARNING and
    nothing else — the next pull, start or explicit refresh converges.
    """
    coro = _refresh_quietly(agent_name, source=source)
    try:
        task = asyncio.create_task(coro)
        _inflight_refresh_tasks.add(task)
        task.add_done_callback(_inflight_refresh_tasks.discard)
    except RuntimeError as e:
        coro.close()
        logger.debug(
            "[ent#477] spawn_refresh_from_running_agent skipped (no loop): %s", e
        )


_inflight_refresh_tasks: Set["asyncio.Task"] = set()


async def _refresh_quietly(agent_name: str, *, source: str) -> None:
    """`refresh_from_running_agent` with every outcome logged and none raised."""
    try:
        summary = await refresh_from_running_agent(agent_name, source=source)
    except RefreshUnavailable as e:
        logger.info(
            "[ent#477] metric registry refresh skipped for %s (%s): %s",
            agent_name, source, e.reason,
        )
    except Exception as e:  # noqa: BLE001 — a background hook never escalates
        logger.warning(
            "[ent#477] metric registry refresh failed for %s (%s): %s",
            agent_name, source, e,
        )
    else:
        if summary.changed:
            logger.info(
                "[ent#477] metric registry refreshed for %s (%s): %s",
                agent_name, source, summary.to_dict(),
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _reconcile(
    agent_name: str,
    declared: List[Dict[str, Any]],
    errors: List[str],
    *,
    source: str,
) -> ReconcileSummary:
    """Hash each normalized entry, hand the set to the store, shape the summary."""
    if source not in SOURCES:
        raise ValueError(
            f"unknown metric registry source {source!r}; expected one of {SOURCES}")
    for entry in declared:
        entry["definition_hash"] = template_metrics.definition_hash(entry)

    result = db.reconcile_metric_definitions(agent_name, declared, source)
    summary = ReconcileSummary(
        agent_name=agent_name,
        source=source,
        declared=result.get("declared", 0),
        created=result.get("created", []),
        updated=result.get("updated", []),
        revived=result.get("revived", []),
        retired=result.get("retired", []),
        unchanged=result.get("unchanged", 0),
        type_change_refused=result.get("type_change_refused", []),
        errors=errors[:25],
    )
    for refused in summary.type_change_refused:
        # WARNING, not INFO: the author believes they changed a metric's shape
        # and the platform did not do it. Names only — a metric name is the
        # author's own identifier and is charset-bounded by the reader.
        logger.warning(
            "[ent#477] %s: refused a type change on metric '%s' (%s -> %s) — "
            "points are stored by name; rename the metric instead",
            agent_name, refused["name"], refused["from"], refused["to"],
        )
    return summary


async def _read_template_from_container(agent_name: str) -> str:
    """`cat` the agent's template.yaml through the compatibility collector's door.

    Running container only — the stopped-agent path (`lifecycle.
    _read_template_yaml_from_volume`) spawns a THROWAWAY CONTAINER, and no
    user-triggered route in Trinity may create a container as a side effect of
    a read. A stopped agent gets a named 409 instead.
    """
    from services.docker_service import execute_command_in_container, get_agent_container

    container = get_agent_container(agent_name)
    if container is None or getattr(container, "status", None) != "running":
        raise RefreshUnavailable(
            "agent_not_running",
            "the agent must be running to re-read its template.yaml",
        )

    # Fixed argv, no interpolation of anything a caller supplies. `head -c`
    # caps the output INSIDE the container so an oversized file never crosses
    # the socket, and the in-container `timeout` frees the Docker pool thread
    # (the `timeout=` kwarg is accepted-and-not-forwarded, by its own docstring).
    command = (
        f"timeout {_EXEC_TIMEOUT} head -c {MAX_TEMPLATE_BYTES + 1} {TEMPLATE_PATH}"
    )
    try:
        result = await asyncio.wait_for(
            execute_command_in_container(
                container_name=f"agent-{agent_name}",
                command=command,
                timeout=_EXEC_TIMEOUT,
            ),
            timeout=_EXEC_TIMEOUT + 5,
        )
    except asyncio.TimeoutError as e:
        raise RefreshUnavailable(
            "template_unreadable", "reading template.yaml timed out"
        ) from e

    if result.get("exit_code") != 0:
        raise RefreshUnavailable(
            "template_unreadable",
            "template.yaml could not be read from the agent",
        )
    raw = result.get("output") or ""
    if not raw.strip():
        # An empty read is indistinguishable from a truncated exec, and both
        # are "no evidence" — never "the author emptied the file".
        raise RefreshUnavailable(
            "template_unreadable", "template.yaml is empty or unreadable"
        )
    if len(raw.encode("utf-8", "ignore")) > MAX_TEMPLATE_BYTES:
        raise RefreshUnavailable(
            "template_unreadable",
            f"template.yaml exceeds the {MAX_TEMPLATE_BYTES}-byte read limit",
        )
    return raw


def list_metric_definitions(
    agent_name: str, *, include_retired: bool = False
) -> List[Dict[str, Any]]:
    """The definitions read, one indexed query. Thin by design — the router
    shapes the response, this keeps the DB call out of it (Invariant #1)."""
    return db.list_metric_definitions(agent_name, include_retired=include_retired)


def declared_metrics_from_template(template_data: Any) -> List[Dict[str, Any]]:
    """Normalized `metrics:` for a template dict the caller already parsed.

    The creation resolvers' entry point, beside `normalize_declared_schedules`
    and `normalize_declared_plugins`. A thin alias so `crud.py` imports ONE
    registry module rather than reaching past the service into the leaf.
    """
    block = template_data.get("metrics") if isinstance(template_data, dict) else None
    return template_metrics.normalize_declared_metrics(block)


def reconcile_declared_metrics(
    agent_name: str, declared: List[Dict[str, Any]], *, source: str
) -> Optional[ReconcileSummary]:
    """Reconcile an ALREADY-NORMALIZED declaration set (the creation path).

    `crud.py` normalizes at resolve time (so the declaration travels with the
    rest of the resolution, exactly like `declared_schedules`) and materializes
    later, inside the rollback fence — this is the second half.
    """
    return _reconcile(agent_name, declared, [], source=source)
