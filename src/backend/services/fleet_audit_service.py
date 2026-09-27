"""
Fleet sync-audit aggregation (#390 / S6).

Builds a per-agent summary joining `agent_git_config` + `agent_sync_state`
(#389) with the duplicate-binding check from `find_duplicate_bindings`
(spec §P5 SQL). Returned shape matches issue #390's acceptance criteria.

Kept as a pure service function so the router is a thin wrapper.

trinity-enterprise#707: each entry also carries the #706 columns and the
policy's verdict (`services/sync_health_view.sync_block`), `dirty_tree` is real
(`dirty_files > 0`), and the summary adds `diverged` / `frozen` /
`auto_sync_off` / `red`. Every pre-#707 key keeps its meaning. Exposed over
MCP as `get_fleet_sync_audit`. No raw git error text: `reason` is the view's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from database import db
from services.sync_health_view import sync_block, summarize


async def build_fleet_sync_audit(agent_names: Optional[List[str]] = None) -> Dict:
    """Aggregate sync audit data for the given agents.

    Args:
        agent_names: restrict to these names (admins pass `None` to see all).

    Returns:
        {"agents": [...], "summary": {...}} per issue #390.
    """
    duplicates = db.find_duplicate_bindings()

    git_enabled = db.list_git_enabled_agents()
    sync_rows_by_name = {r["agent_name"]: r for r in db.list_sync_states()}

    name_filter = set(agent_names) if agent_names is not None else None
    now = datetime.now(timezone.utc)

    entries: List[Dict] = []
    blocks: List[Dict] = []
    for cfg in git_enabled:
        name = cfg.agent_name if hasattr(cfg, "agent_name") else cfg["agent_name"]
        if name_filter is not None and name not in name_filter:
            continue

        working_branch = _field(cfg, "working_branch")
        last_commit_sha = _field(cfg, "last_commit_sha")
        last_sync_at_raw = _field(cfg, "last_sync_at")
        state = sync_rows_by_name.get(name, {})

        # last_pushed_* are operator-readable aliases for git config fields.
        last_pushed_sha = last_commit_sha
        last_pushed_at = _iso(last_sync_at_raw)

        # unpushed_commits = local commits past the remote working branch
        # (#389 ahead_working). For agents we never polled, fall back to 0.
        unpushed = state.get("ahead_working") or 0
        # trinity-enterprise#707: the poller persists the porcelain count.
        dirty_tree = (state.get("dirty_files") or 0) > 0
        block = sync_block(state or None, cfg, now)
        blocks.append(block)

        entries.append({
            "name": name,
            "branch": working_branch,
            "last_pushed_sha": last_pushed_sha,
            "last_pushed_at": last_pushed_at,
            "local_head_sha": state.get("last_remote_sha_working") or last_pushed_sha,
            "unpushed_commits": unpushed,
            "dirty_tree": dirty_tree,
            "duplicate_binding": name in duplicates,
            "git_dir_bytes": state.get("git_dir_bytes"),  # #1596 workspace .git size
            # trinity-enterprise#707: the #706 columns and the verdict.
            "ahead": block["ahead"],
            "behind": block["behind"],
            "dirty_files": block["dirty_files"],
            "diverged_since": state.get("diverged_since"),
            "divergence_age_s": block["divergence_age_s"],
            "last_successful_push_at": block["last_successful_push_at"],
            "state": block["state"],
            "reason": block["reason"],
            "recommendation": block["recommendation"],
            "binding": block["binding"],
            "auto_sync_enabled": block["auto_sync_enabled"],
            "frozen": block["frozen"],
        })

    entries.sort(key=lambda e: e["name"])

    summary = {
        "total": len(entries),
        "in_sync": sum(1 for e in entries if e["unpushed_commits"] == 0
                                          and not e["dirty_tree"]),
        "ahead": sum(1 for e in entries if e["unpushed_commits"] > 0),
        "dirty": sum(1 for e in entries if e["dirty_tree"]),
        "duplicate_bindings": sum(1 for e in entries if e["duplicate_binding"]),
    }
    totals = summarize(blocks)
    for key in ("diverged", "frozen", "auto_sync_off", "red"):
        summary[key] = totals[key]

    return {"agents": entries, "summary": summary}


def _field(obj, name: str):
    if hasattr(obj, name):
        return getattr(obj, name)
    return obj[name] if name in obj else None


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
