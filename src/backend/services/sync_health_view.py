"""One call, one answer: an agent's sync-health view (trinity-enterprise#706).

Every backend surface that shows sync health goes through :func:`sync_view`,
which feeds the persisted `agent_sync_state` row and the `agent_git_config`
flags to `sync_freeze_policy.classify`. The policy module is a stdlib leaf
(the scheduler vendors it), so the two backend-only inputs are resolved here:

- `push_denied` — `git_service.is_push_denied(last_error_summary)` (#2107),
  which turns a refused push into the `credential is read-only` recommendation;
- the git-config model — read into the plain dict `classify` takes.

trinity-enterprise#707: `reason` / `freeze_reason` never carry the agent-written
git error (`last_error_summary` is raw git stderr). The policy excerpts it for
the scheduler's skip row; every backend surface gets the reason computed as if
there were no error text, while the recommendation still reads the error. The
fleet surfaces (`fleet_sync`, `sync_block`) are built on the same call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from database import db
from services import git_service
from services.sync_freeze_policy import classify

# The computed fields every surface adds next to the row it already returns.
VIEW_FIELDS = (
    "state",
    "reason",
    "recommendation",
    "binding",
    "work_agent",
    "divergence_age_s",
    "dirty_age_s",
    "sync_failing",
    "freeze",
    "freeze_cause",
    "freeze_reason",
    "stale_observation",
)

_CONFIG_FIELDS = (
    "source_mode",
    "auto_sync_enabled",
    "freeze_schedules_if_sync_failing",
    "created_at",
)


def _config_dict(config: Any) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return {key: config.get(key) for key in _CONFIG_FIELDS}
    return {key: getattr(config, key, None) for key in _CONFIG_FIELDS}


def sync_view(
    row: Optional[Dict[str, Any]],
    config: Any,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The policy verdict for one agent, restricted to `VIEW_FIELDS`.

    `row` is its `agent_sync_state` row or None; `config` its
    `AgentGitConfig` (or a dict with the same fields) or None when the agent
    has no git binding.
    """
    error = (row or {}).get("last_error_summary") or ""
    cfg = _config_dict(config)
    now = now or datetime.now(timezone.utc)
    push_denied = git_service.is_push_denied(error)
    verdict = classify(row, cfg, now, push_denied=push_denied)
    if error:
        # #707: the same verdict with the error text withheld supplies the
        # operator-facing strings; state, freeze and the recommendation (which
        # reads the error — a refusal, a denied push) stay from the full row.
        redacted = classify(
            {**row, "last_error_summary": None}, cfg, now, push_denied=push_denied
        )
        verdict["reason"] = redacted["reason"]
        verdict["freeze_reason"] = redacted["freeze_reason"]
    return {key: verdict[key] for key in VIEW_FIELDS}


def sync_block(
    row: Optional[Dict[str, Any]],
    config: Any,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The fleet surfaces' per-agent sync block (trinity-enterprise#707).

    The counts are None until the poller has written a row. `ahead` / `behind`
    are the working tuple — origin on the agent's own branch (#2105).
    """
    view = sync_view(row, config, now)
    observed = row or {}
    return {
        "binding": view["binding"],
        "auto_sync_enabled": bool(_config_dict(config).get("auto_sync_enabled")),
        "ahead": observed.get("ahead_working"),
        "behind": observed.get("behind_working"),
        "dirty_files": observed.get("dirty_files"),
        "last_successful_push_at": observed.get("last_successful_push_at"),
        "divergence_age_s": view["divergence_age_s"],
        "state": view["state"],
        "reason": view["reason"],
        "recommendation": view["recommendation"],
        "frozen": bool(view["freeze"]),
    }


def summarize(blocks: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Fleet totals over sync blocks (trinity-enterprise#707)."""
    totals = {
        "git_bound": 0, "diverged": 0, "frozen": 0, "auto_sync_off": 0,
        "dirty": 0, "red": 0, "yellow": 0,
    }
    for block in blocks:
        totals["git_bound"] += 1
        totals["diverged"] += block["divergence_age_s"] is not None
        totals["frozen"] += bool(block["frozen"])
        totals["auto_sync_off"] += not block["auto_sync_enabled"]
        totals["dirty"] += (block["dirty_files"] or 0) > 0
        totals["red"] += block["state"] == "red"
        totals["yellow"] += block["state"] == "yellow"
    return totals


def fleet_sync(
    agent_names: Optional[Iterable[str]],
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Sync blocks for the git-bound agents among `agent_names`, and their totals.

    One query (`db.list_sync_health_rows`). An agent with no git binding is
    absent from the blocks — the caller reports it as `sync: null`.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.list_sync_health_rows(agent_names)
    blocks = {
        name: sync_block(bound["state"], bound["config"], now)
        for name, bound in rows.items()
    }
    return blocks, summarize(blocks.values())


def sync_issue(block: Dict[str, Any]) -> Optional[str]:
    """The fleet-health `issues[]` entry for a red agent, else None."""
    if block.get("state") != "red":
        return None
    if block.get("recommendation"):
        return f"sync: {block['reason']} — {block['recommendation']}"
    return f"sync: {block['reason']}"
