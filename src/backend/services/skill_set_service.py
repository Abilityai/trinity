"""Skill sets — resolution, per-agent status and the write paths (trinity-enterprise#530).

The library side reads each enabled source's ``catalog.yaml`` ``sets:`` (see
``services.skill_sets``) and resolves set names with the SAME precedence as
skills: the first source in resolution order (custom before default) owns a
name, later ones are recorded as shadowing. A member resolves like any skill —
by the library's precedence — and a member whose winning copy comes from a
different source than the set is flagged (``shadowed_source``), never silently
swapped.

Every write hands the DB layer a map ``set → present members | None`` for every
set the agent holds. ``None`` means the set cannot be resolved right now (source
disabled or removed, catalog unreadable, set dropped upstream), and the DB layer
then removes nothing — the fail-closed rule. Governance (who may call these) is
the router's ``get_skill_managed_agent_by_name`` (ent#596).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.skill_sets import SET_PREFIX

logger = logging.getLogger(__name__)

_cache: Tuple[Optional[str], Dict[str, dict]] = (None, {})


class SetError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **extra}


def _skill_service():
    from services.skill_service import skill_service
    return skill_service


def library_sets(*, refresh: bool = False) -> Dict[str, dict]:
    """Every resolvable set in the library, keyed by name. Never raises."""
    global _cache
    svc = _skill_service()
    try:
        clones = svc._clones(enabled_only=True)
        fingerprint = svc._library_fingerprint(clones)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#530] could not list skill sources: %s", e)
        return {}
    if not refresh and fingerprint is not None and _cache[0] == fingerprint:
        return _cache[1]
    try:
        names = svc._source_names()
        skills = svc._resolution()
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#530] could not resolve the skills library: %s", e)
        return {}
    shas: Dict[str, Dict[str, str]] = {}
    out: Dict[str, dict] = {}
    for clone in clones:
        try:
            declared = clone.declared_sets()
        except Exception:  # noqa: BLE001
            declared = []
        for sd in declared:
            label = names.get(clone.source_id, clone.source_id)
            if sd.name in out:
                out[sd.name]["shadowed_by"].append({"source_id": clone.source_id, "source_name": label})
                continue
            members = []
            for m in sd.members:
                win = skills.get(m)
                win_source = win["source_id"] if win else None
                if win_source and win_source not in shas:
                    try:
                        shas[win_source] = win["clone"].tree_shas()
                    except Exception:  # noqa: BLE001
                        shas[win_source] = {}
                members.append({
                    "name": m,
                    "present": m not in sd.missing,
                    "version": shas.get(win_source, {}).get(m) if win_source else None,
                    "shadowed_source": win_source if (win_source and win_source != clone.source_id) else None,
                })
            out[sd.name] = {
                "name": sd.name,
                "source_id": clone.source_id,
                "source_name": label,
                "shadowed_by": [],
                "members": members,
                "status": sd.status,
                "problems": list(sd.problems),
                "requires": {"env": list(sd.requires_env)},
                "schedules": list(sd.schedules),
            }
    if fingerprint is not None:
        _cache = (fingerprint, out)
    return out


def held_sets(agent_name: str) -> Dict[str, Optional[str]]:
    """``set → source_id recorded at assignment`` for the sets an agent holds."""
    from database import db
    return {r["set_name"]: r.get("source_id") for r in db.list_agent_skill_sets(agent_name)}


def unresolved_reason(name: str, held_source: Optional[str], lib: Dict[str, dict]) -> Optional[str]:
    """Why a held set cannot drive a prune right now, or None when it resolves.

    Fail-closed on every doubt (review findings 1-3): the set is gone, its
    definition is broken, its source no longer ships a member it names (which is
    also what an empty or half-checked-out skills root looks like), or the name
    now resolves to a DIFFERENT source than the one it was assigned from — a
    disabled custom source must not silently swap in another source's family.
    """
    entry = lib.get(name)
    if entry is None:
        return "not_found"
    if entry["status"] == "invalid":
        return "invalid"
    if entry["status"] != "ok":
        return "partial_upstream"
    if held_source and entry["source_id"] != held_source:
        return "source_changed"
    return None


def resolved_members(held, lib: Optional[Dict[str, dict]] = None) -> Dict[str, Optional[List[str]]]:
    """``set → members`` for the DB layer; ``None`` for a set that must not drive
    a prune (see ``unresolved_reason``). ``held`` is ``{set: source_id}`` or a
    list of names (no source check). Nothing held → no library read at all."""
    if not held:
        return {}
    pairs = held.items() if isinstance(held, dict) else ((n, None) for n in held)
    lib = library_sets() if lib is None else lib
    out: Dict[str, Optional[List[str]]] = {}
    for name, source in pairs:
        if unresolved_reason(name, source, lib) is not None:
            out[name] = None
        else:
            out[name] = [m["name"] for m in lib[name]["members"]]
    return out


def set_named_from(resolved: Dict[str, Optional[List[str]]]) -> Optional[set]:
    """Every skill the held sets name, or None when any is unresolved."""
    if any(v is None for v in resolved.values()):
        return None
    return {m for members in resolved.values() for m in (members or [])}


def set_resolver(lib: Optional[Dict[str, dict]] = None):
    """A pure ``held → set_named | None`` for ``db.set_agent_skills`` to call
    INSIDE its transaction, over a library snapshot taken once up front (the
    I/O stays outside the transaction; the held sets are read inside it)."""
    snapshot = {"lib": lib}

    def resolve(held: Dict[str, Optional[str]]) -> Optional[set]:
        if not held:
            return set()
        if snapshot["lib"] is None:
            snapshot["lib"] = library_sets()
        return set_named_from(resolved_members(held, snapshot["lib"]))

    return resolve


def via_sets_map(agent_name: str, lib: Optional[Dict[str, dict]] = None) -> Dict[str, List[str]]:
    """``skill → [assigned sets that name it]`` from the current catalogs."""
    held = held_sets(agent_name)
    if not held:
        return {}
    out: Dict[str, List[str]] = {}
    for set_name, members in resolved_members(held, lib).items():
        for m in members or []:
            out.setdefault(m, []).append(set_name)
    return out


def any_unresolved(agent_name: str, lib: Optional[Dict[str, dict]] = None) -> bool:
    held = held_sets(agent_name)
    return bool(held) and any(v is None for v in resolved_members(held, lib).values())


def reconcile_agent(agent_name: str, lib: Optional[Dict[str, dict]] = None) -> Tuple[List[str], List[str]]:
    """Step 1 of every inject path: bring set-derived rows in line with the
    catalogs, DB only, BEFORE the names are read. Never raises."""
    from database import db

    try:
        held = held_sets(agent_name)
        if not held:
            return [], []
        return db.reconcile_skill_sets(agent_name, resolved_members(held, lib))
    except Exception as e:  # noqa: BLE001 — a set problem never blocks injection
        logger.warning("[ent#530] set reconcile failed for %s: %s", agent_name, e)
        return [], []


async def agent_set_status(agent_name: str, *, probe: bool = False) -> List[dict]:
    """The honest status of every set the agent holds (#342)."""
    from database import db

    import asyncio
    lib = await asyncio.to_thread(library_sets)
    skill_env = await asyncio.to_thread(_skill_env_map) if db.agent_skill_set_names(agent_name) else {}
    rows = {s.skill_name: s for s in db.get_agent_skills(agent_name)}
    out = []
    running = None
    for held in db.list_agent_skill_sets(agent_name):
        name = held["set_name"]
        entry = lib.get(name)
        reason = unresolved_reason(name, held.get("source_id"), lib)
        if entry is None or reason in ("invalid", "source_changed"):
            out.append({"name": name, "status": "unresolved", "reason": reason,
                        "source_id": held.get("source_id"),
                        "drift": reason == "source_changed", "members": [],
                        "prerequisites": {"state": "unknown", "missing_env": []},
                        "suggested_schedules": [], "assigned_by": held.get("assigned_by"),
                        "assigned_by_agent": held.get("assigned_by_agent"), "assigned_at": held.get("assigned_at")})
            continue
        members = []
        partial = entry["status"] != "ok"
        for m in entry["members"]:
            row = rows.get(m["name"])
            state = "missing_upstream" if not m["present"] else (
                "not_assigned" if row is None else ("conflict" if row.delivery_status == "conflict" else "assigned"))
            if state != "assigned":
                partial = True
            members.append({"name": m["name"], "state": state, "version": m["version"],
                            "shadowed_source": m["shadowed_source"]})
        prerequisites = {"state": "unknown", "missing_env": []}
        env = sorted(set(entry["requires"]["env"]) | _member_env(entry, skill_env))
        if not env:
            prerequisites = {"state": "none", "missing_env": []}
        elif probe:
            if running is None:
                running = await _is_running(agent_name)
            if running:
                found = await _probe_env(agent_name, env)
                if found is not None:
                    missing = [k for k in env if not found.get(k)]
                    prerequisites = {"state": "missing" if missing else "ok", "missing_env": missing}
        out.append({
            "name": name, "status": "partial" if partial else "ok", "reason": reason,
            "source_id": entry["source_id"], "drift": False,
            "members": members, "prerequisites": prerequisites, "suggested_schedules": entry["schedules"],
            "assigned_by": held.get("assigned_by"), "assigned_by_agent": held.get("assigned_by_agent"),
            "assigned_at": held.get("assigned_at"),
        })
    return out


def _skill_env_map() -> Dict[str, List[str]]:
    """``skill → its SKILL.md requires.env`` across the library (one listing)."""
    try:
        return {sk.get("name"): list((sk.get("requires") or {}).get("env") or [])
                for sk in _skill_service().list_skills()}
    except Exception:  # noqa: BLE001
        return {}


def _member_env(entry: dict, skill_env: Dict[str, List[str]]) -> set:
    """Members' own SKILL.md `requires.env` — the set's prerequisites include theirs."""
    out = set()
    for m in entry["members"]:
        out.update(skill_env.get(m["name"]) or [])
    return out


async def _is_running(agent_name: str) -> bool:
    try:
        from services import docker_utils
        return (await docker_utils.agent_container_state_async(agent_name)) == "running"
    except Exception:  # noqa: BLE001
        return False


async def _probe_env(agent_name: str, env: List[str]) -> Optional[Dict[str, bool]]:
    try:
        res = await _skill_service()._probe_dependencies(agent_name, [], env)
    except Exception:  # noqa: BLE001
        return None
    return (res or {}).get("env") if res is not None else None


def require_set(name: str) -> dict:
    """The library entry for an assignable set, or a named refusal (no partial assign)."""
    entry = library_sets().get(name)
    if entry is None:
        raise SetError(404, "unknown_set", f"No skill set named '{name}' in the library.")
    if entry["status"] == "invalid":
        raise SetError(422, "set_invalid",
                       f"Skill set '{name}' has an invalid definition in its source's catalog.yaml.",
                       problems=entry["problems"])
    missing = [m["name"] for m in entry["members"] if not m["present"]]
    if missing:
        raise SetError(422, "set_member_missing",
                       f"Skill set '{name}' names skills its source does not ship: {', '.join(missing[:10])}.",
                       missing=missing)
    return entry


def assign(agent_name: str, set_name: str, assigned_by: str, assigned_by_agent: Optional[str]) -> dict:
    """Assign (or re-assign, which re-points a drifted set at its current source)."""
    from database import db

    entry = require_set(set_name)
    lib = library_sets()
    held = {**held_sets(agent_name), set_name: entry["source_id"]}
    created, added, removed = db.assign_skill_set(
        agent_name, set_name, entry["source_id"], assigned_by, assigned_by_agent, resolved_members(held, lib))
    return {"created": created, "added": added, "removed": removed, "entry": entry}


def unassign(agent_name: str, set_name: str, assigned_by: str) -> dict:
    """``removal_deferred`` is True when another held set is unresolved: the
    fail-closed rule then removes nothing now, and the next reconcile after it
    resolves finishes the job (review finding 8 — never a silent no-op)."""
    from database import db

    held = {k: v for k, v in held_sets(agent_name).items() if k != set_name}
    resolved = resolved_members(held)
    existed, added, removed = db.unassign_skill_set(agent_name, set_name, resolved, assigned_by)
    return {"existed": existed, "added": added, "removed": removed,
            "removal_deferred": any(v is None for v in resolved.values())}


def replace(agent_name: str, set_names: List[str], assigned_by: str, assigned_by_agent: Optional[str]) -> dict:
    """The bulk PUT's `sets` field: every named set must be assignable (no partial replace)."""
    from database import db

    entries = {n: require_set(n) for n in dict.fromkeys(set_names)}
    lib = library_sets()
    held = {n: e["source_id"] for n, e in entries.items()}
    added, removed = db.replace_skill_sets(
        agent_name, list(entries), held, assigned_by, assigned_by_agent, resolved_members(held, lib))
    return {"added": added, "removed": removed}


def strip_set_prefix(names: List[str]) -> Tuple[List[str], List[str]]:
    """Split a mixed name list into (skills, sets) — `set:<name>` entries are sets."""
    skills, sets = [], []
    for n in names:
        if isinstance(n, str) and n.startswith(SET_PREFIX):
            sets.append(n[len(SET_PREFIX):])
        else:
            skills.append(n)
    return skills, sets
