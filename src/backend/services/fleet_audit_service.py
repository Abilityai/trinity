"""
Fleet sync-audit aggregation (#390 / S6).

Builds a per-agent summary joining `agent_git_config` + `agent_sync_state`
(#389) with the duplicate-binding check from `find_duplicate_bindings`
(spec §P5 SQL). Returned shape matches issue #390's acceptance criteria.

Kept as a pure service function so the router is a thin wrapper.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from database import db


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

    entries: List[Dict] = []
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
        dirty_tree = False  # Requires live agent call — deferred; see PR note.

        entries.append({
            "name": name,
            "branch": working_branch,
            "last_pushed_sha": last_pushed_sha,
            "last_pushed_at": last_pushed_at,
            "local_head_sha": state.get("last_remote_sha_working") or last_pushed_sha,
            "unpushed_commits": unpushed,
            "dirty_tree": dirty_tree,
            "duplicate_binding": name in duplicates,
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
