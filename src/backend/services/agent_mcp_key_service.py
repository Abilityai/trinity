"""Agent MCP key — detection, self-heal and deliberate rotation (#1854).

Three reinforcing pieces, in the order that matters — **detect → self-heal →
rotate**:

* **Detect** (:func:`verify_agent_mcp_key`) — ask the *container* what it is
  actually configured with, instead of reporting what the platform believes.
  Every other signal in this file is derived from ``mcp_api_keys``; this one is
  the only thing that can see a hand-pasted user-scoped key, or a second
  Trinity-pointing entry under a non-``trinity`` server name that re-injection
  never touches and rotation cannot fix.
* **Self-heal** (:func:`heal_agent_mcp_key_env`) — called from
  ``start_agent_internal`` when the drift predicate fires. Repairs the fleet
  whether or not a human notices.
* **Rotate** (:func:`regenerate_agent_mcp_key`) — the deliberate owner-driven
  lever, returning **metadata only, never plaintext**.

Ordering contract for a rotation (each step exists because skipping it breaks
something specific — see the inline notes):

    1. refuse system / ephemeral (409), BEFORE any mutation
    2. acquire agent:mcp_key_regen:{name}          [FAIL-CLOSED → 503]
    3. capture the active scope='agent' id set     (BEFORE the mint)
    4. mint the new key
    5. reconcile spawned_by_key_id                 (BEFORE delivery)
    6. deliver: running → recreate + post-condition; stopped → DB-only
    7. DELETE the captured superseded ids          (not "everything else")
    8. audit; return metadata

Mint-first is load-bearing: the heartbeat (``heartbeat.py``), the #1083 result
callback, the #1081 pull worker and the runtime's own MCP client all read
``TRINITY_MCP_API_KEY`` from **process env**, which a DB write cannot change.
Revoking first 401s all four at once.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import uuid
from datetime import timezone
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from database import db
from db.agents import SYSTEM_AGENT_NAME
from models import (
    AgentMcpKeyStatus,
    AgentMcpKeyVerifyEntry,
    AgentMcpKeyVerifyResult,
    AgentMcpKeyRegenerateResult,
    User,
)
from services.agent_runtime_state import clear_agent_breakers
from services.docker_service import execute_command_in_container, get_agent_container
from services.docker_utils import container_reload
from services.platform_audit_service import platform_audit_service, AuditEventType
from utils.helpers import parse_iso_timestamp

logger = logging.getLogger(__name__)

# The regeneration lock. Cross-worker, per agent, and deliberately short-lived:
# the whole operation is one mint plus at most one container recreate.
_REGEN_LOCK_KEY = "agent:mcp_key_regen:{name}"
_REGEN_LOCK_TTL_S = 180

_PROBE_TIMEOUT_S = 15
_MCP_JSON_CAP = 512 * 1024

DEFAULT_TRINITY_MCP_URL = "http://mcp-server:8080/mcp"
DEFAULT_TRINITY_BACKEND_URL = "http://backend:8000"

# A key whose last MCP use predates the agent's most recent execution by more
# than this is `stale`. Generous on purpose: an agent that collaborates weekly
# must not be flagged, but the motivating incident ("unused for months") must.
_STALE_AFTER_SECONDS = 7 * 24 * 3600


# --------------------------------------------------------------------------- #
# In-container probe
# --------------------------------------------------------------------------- #
# Runs inside the agent container and emits ONLY digests. The bearer token and
# the `.mcp.json` body never cross the container boundary — an owner-reachable
# endpoint that returned either would be a credential-exfiltration primitive,
# and the digest is all the backend needs to join against `mcp_api_keys.key_hash`
# (a plain unsalted sha256 of the key).
#
# Server names ARE returned (a `shadow_entry` verdict is meaningless without
# one), but they are agent-authored strings, so they are truncated here and
# never written to a log or to the append-only audit_log.
PROBE_SCRIPT = r'''
import json, os, sys, hashlib

out = {"schema": 1, "present": False, "entries": []}

def emit():
    sys.stdout.write(json.dumps(out))
    raise SystemExit(0)

try:
    with open(MCP_JSON_PATH, "rb") as f:
        raw = f.read(CAP + 1)
except OSError:
    emit()

out["present"] = True
if len(raw) > CAP:
    out["error"] = "too_large"
    emit()

try:
    cfg = json.loads(raw.decode("utf-8", "replace"))
except Exception:
    out["error"] = "unparseable"
    emit()

servers = cfg.get("mcpServers") if isinstance(cfg, dict) else None
if not isinstance(servers, dict):
    emit()

for name, entry in list(servers.items())[:50]:
    if not isinstance(entry, dict):
        continue
    url = entry.get("url") or ""
    if not isinstance(url, str):
        url = ""
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    auth = ""
    for hk, hv in headers.items():
        if isinstance(hk, str) and hk.lower() == "authorization" and isinstance(hv, str):
            auth = hv
            break
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    is_trinity = any(u and u in url for u in TRINITY_URLS) or name == "trinity"
    out["entries"].append({
        "name": str(name)[:64],
        "is_trinity": bool(is_trinity),
        # ONLY the digest. Never the token, never the header, never the body.
        # And only for a TRINITY-pointing entry: the interpreter discards every
        # other digest anyway, so hashing a third-party server's bearer (a PAT, a
        # Slack token) would move an offline-verification oracle for somebody
        # else's credential across the container boundary for no gain.
        "digest": hashlib.sha256(token.encode()).hexdigest() if (token and is_trinity) else None,
    })

emit()
'''


def _build_probe_script(trinity_urls: Sequence[str]) -> str:
    header = (
        f"MCP_JSON_PATH = {'/home/developer/.mcp.json'!r}\n"
        f"CAP = {_MCP_JSON_CAP}\n"
        f"TRINITY_URLS = {list(trinity_urls)!r}\n"
    )
    return header + PROBE_SCRIPT


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _trinity_mcp_url() -> str:
    return os.getenv("TRINITY_MCP_URL") or DEFAULT_TRINITY_MCP_URL


def _trinity_backend_url() -> str:
    return os.getenv("TRINITY_BACKEND_URL") or DEFAULT_TRINITY_BACKEND_URL


def _is_system_agent(agent_name: str) -> bool:
    return agent_name == SYSTEM_AGENT_NAME


def _is_ephemeral(agent_name: str) -> bool:
    try:
        info = db.get_agent_ephemeral_info(agent_name)
    except Exception:
        return False
    return bool(isinstance(info, dict) and info.get("is_ephemeral"))


def _owner_username(agent_name: str) -> Optional[str]:
    owner = db.get_agent_owner(agent_name) or {}
    return owner.get("owner_username") or owner.get("username")


def _mint_and_build_env(agent_name: str, description: str):
    """Mint a fresh agent key and return ``(env_payload, key_row)``.

    Carries **all three** vars, not just the key. This is the defect that made
    the naive version of this feature fail on exactly the population it exists
    to repair: the agent server's ``inject_trinity_mcp_if_configured``
    early-returns unless ``TRINITY_MCP_URL`` **and** ``TRINITY_MCP_API_KEY`` are
    both present, and ``crud._apply_mcp_and_auth_env`` sets URL + key + backend
    URL together inside one ``if agent_mcp_key:`` — so a swallowed mint at
    creation drops all three. Baking only the key leaves the injection still
    short-circuiting and ``.mcp.json`` untouched, while the heartbeat starts
    working: a partial success that reads as a fix. The in-repo precedent
    (``recreate_missing_container``) sets all three.

    Raises if the mint fails — the callers decide whether that is fatal.
    """
    owner_username = _owner_username(agent_name)
    if not owner_username:
        raise RuntimeError(f"no live ownership row for {agent_name}")

    new_key = db.create_agent_mcp_api_key(agent_name, owner_username, description)
    if not new_key or not getattr(new_key, "api_key", None):
        raise RuntimeError(f"MCP key mint returned nothing for {agent_name}")

    env = {
        "TRINITY_MCP_API_KEY": new_key.api_key,
        "TRINITY_MCP_URL": _trinity_mcp_url(),
        # Already `setdefault`-healed on the recreate path for non-system
        # agents, but set explicitly rather than depending on that.
        "TRINITY_BACKEND_URL": _trinity_backend_url(),
    }
    return env, new_key


def build_mcp_key_env_overrides(agent_name: str, *, description: str) -> Dict[str, str]:
    """:func:`_mint_and_build_env` for callers that want only the env payload."""
    env, _key = _mint_and_build_env(agent_name, description)
    return env


# --------------------------------------------------------------------------- #
# Self-heal (called from start_agent_internal on drift)
# --------------------------------------------------------------------------- #
async def heal_agent_mcp_key_env(agent_name: str) -> Optional[Dict[str, str]]:
    """Mint + reconcile + prune for the start-time drift path (#1854).

    Returns the env override payload, or ``None`` on any failure — the caller
    proceeds with the recreate regardless, because credential bookkeeping must
    never block a start. When it returns None the drift predicate simply stays
    unsatisfied and the next manual start retries; starts are never timed, so
    this cannot become a hot recreate loop.

    Prunes the captured superseded rows for the same reason rotation does: #1811
    observed 50 accumulated active rows on one instance, each a live credential
    the container is not using.

    **Serialised under the SAME lock as an owner-initiated rotation, and it does
    nothing at all when it cannot hold it.** The naive reading — "the predicate
    only fires when the container's key matches no active row, so every active
    row is already unusable by this container" — is a statement about the state
    at *predicate* time, while the DELETE runs after a mint and possibly after
    another actor has already replaced the container. There is no per-agent
    start lock (``restart_agent_internal`` says so explicitly), so two
    concurrent ``start_agent_internal`` calls both see the drift; if the second
    captures after the first's mint but before its delete, it deletes the very
    key the first is about to bake in, and whichever container survives the
    ``containers.run`` name-conflict adoption is left holding a deleted key —
    401-ing the heartbeat, the #1083 result callback, the #1081 pull worker and
    the MCP client at once. That is the exact interleave the rotation path
    already treats as fail-closed-worthy (#1644), reached here on an
    *automatic*, unattended path.

    Widening the window is not enough to be safe and narrowing it is not
    possible: a mint taken outside the lock can always be captured-and-deleted
    by the holder, so capture→mint→delete must be atomic as a unit. Hence: no
    lock, no heal. A skipped pass changes nothing (no mint, no delete, the
    recreate proceeds with the container's existing env), the agent is left
    exactly as drifted as it already was, and the next start retries. Redis is a
    hard platform dependency, so "Redis down ⇒ no self-heal this pass" is a
    degraded-platform behaviour, not a functional gap.
    """
    if _is_system_agent(agent_name) or _is_ephemeral(agent_name):
        # Belt-and-braces: the predicate already exempts both, so reaching here
        # would mean a caller bypassed it.
        return None
    with _try_regen_lock(agent_name) as held:
        if not held:
            logger.warning(
                "MCP key self-heal skipped for %s: another rotation/heal holds the "
                "lock (or Redis is unreachable). Nothing was minted or deleted; "
                "the next start retries. (#1854)",
                agent_name,
            )
            return None
        try:
            captured = list(db.list_active_agent_key_ids(agent_name))
            env, new_key = _mint_and_build_env(agent_name, "drift-self-heal (#1854)")
            try:
                children_repointed = db.reconcile_spawn_key_id(agent_name, new_key.id)
            except Exception as e:  # noqa: BLE001
                children_repointed = 0
                logger.warning(
                    "MCP key self-heal: spawn-id reconcile failed for %s: %s", agent_name, e
                )
            deleted = 0
            try:
                deleted = db.delete_superseded_agent_keys(agent_name, new_key.id, captured)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "MCP key self-heal: superseded prune failed for %s: %s", agent_name, e
                )
            logger.info(
                "MCP key drift healed for %s — minted %s, pruned %d superseded row(s), "
                "re-pointed %d child agent(s) (#1854)",
                agent_name, new_key.id, deleted, children_repointed,
            )
            # This path mints AND DELETES credentials with no human in the loop,
            # across the whole fleet, on every drifted start. An INFO log is not
            # an audit trail — the whole premise of #1854 is that the platform
            # could not say what its agents authenticate with, so the repair
            # itself has to be answerable. Metadata only: NEVER the plaintext,
            # the hash, or the env payload (`audit_log` is append-only with a
            # 365-day no-delete trigger). Best-effort by construction —
            # `platform_audit_service.log` swallows its own failures.
            await platform_audit_service.log(
                event_type=AuditEventType.MCP_OPERATION,
                event_action="agent_key_self_heal",
                source="system",
                target_type="mcp_key",
                target_id=new_key.id,
                details={
                    "agent_name": agent_name,
                    "trigger": "start_drift",
                    "new_key_id": new_key.id,
                    "new_key_prefix": new_key.key_prefix,
                    "superseded_key_ids": captured,
                    "superseded_deleted": deleted,
                    "children_repointed": children_repointed,
                },
            )
            return env
        except Exception as e:  # noqa: BLE001 — never block a start
            logger.warning("MCP key self-heal failed for %s: %s", agent_name, e)
            return None


# --------------------------------------------------------------------------- #
# Status / health
# --------------------------------------------------------------------------- #
def get_agent_mcp_key_status(agent_name: str) -> AgentMcpKeyStatus:
    """Owner-facing read surface. Never returns the secret."""
    if _is_system_agent(agent_name):
        # `get_agent_mcp_api_key` filters scope=='agent' and the orchestrator's
        # key is scope='system', so every other branch below would report a
        # permanent false `missing` — the fastest way to train operators to
        # ignore the signal.
        return AgentMcpKeyStatus(
            agent_name=agent_name,
            exists=True,
            health="exempt",
            health_detail=(
                "The platform orchestrator authenticates with a system-scoped "
                "key, which is managed by the system-agent bootstrap."
            ),
            rotatable=False,
        )

    key = db.get_agent_mcp_api_key(agent_name)
    if not key:
        return AgentMcpKeyStatus(
            agent_name=agent_name,
            exists=False,
            health="missing",
            health_detail=(
                "This agent has no active platform key. It cannot call Trinity "
                "MCP tools at all — most often a mint that failed silently at "
                "creation. Starting the agent repairs this automatically."
            ),
            rotatable=True,
        )

    last_used = getattr(key, "last_used_at", None)
    last_exec_raw = None
    try:
        last_exec_raw = db.get_agent_last_execution_at(agent_name)
    except Exception as e:  # noqa: BLE001 — health must never 500
        logger.debug("last-execution lookup failed for %s: %s", agent_name, e)

    # The container's env OUTRANKS every usage-derived state. A row that exists
    # and was used last week says nothing about what the container is running
    # with right now, and reporting "In active use" over an env that carries no
    # key at all is precisely the green-during-the-incident failure this feature
    # exists to remove. Cheap (a Docker inspect, not the `verify` docker exec),
    # so the panel is honest on load rather than only after a click.
    env_health = _container_env_health(agent_name)
    if env_health:
        health, detail = env_health, _ENV_HEALTH_DETAIL[env_health]
    else:
        health, detail = _classify_health(last_used, last_exec_raw)

    return AgentMcpKeyStatus(
        agent_name=agent_name,
        exists=True,
        key_id=getattr(key, "id", None),
        key_prefix=getattr(key, "key_prefix", None),
        scope=getattr(key, "scope", None),
        created_at=getattr(key, "created_at", None),
        last_used_at=last_used,
        usage_count=getattr(key, "usage_count", 0) or 0,
        health=health,
        health_detail=detail,
        rotatable=True,
    )


_ENV_HEALTH_DETAIL = {
    "env_absent": (
        "The container carries no Trinity MCP credential at all, so it cannot "
        "call Trinity tools — the injection needs both the key and the URL and "
        "silently skips when either is missing. Starting the agent repairs this "
        "automatically."
    ),
    "env_mismatch": (
        "The container is running with a key this platform does not recognize "
        "as this agent's own — it may be a personal key, another agent's, or one "
        "that has been rotated out. Use the container check below to see which, "
        "then restart the agent (or regenerate) to repair it."
    ),
}


def _container_env_health(agent_name: str) -> Optional[str]:
    """`env_absent` / `env_mismatch` / None, read from the container's env.

    A Docker inspect, NOT the `verify` docker exec: this answers "what did the
    platform bake in", which is enough to catch a swallowed mint at creation and
    a rotated-out key. Only `verify` can see a hand-edited `.mcp.json` whose
    entry disagrees with the env, which is why both exist.

    The match verdict is delegated to `check_agent_mcp_key_matches` rather than
    re-derived, so the panel and the start-time recreate decision can never
    disagree about what "recognized" means. Lazy import: `lifecycle` reaches
    back into this module for the self-heal, so binding the agent_service
    package at import time would be circular.

    Fail-soft to None (no env verdict) on anything — a read-only health panel
    must degrade to the usage-derived states, never 500 and never claim drift it
    could not observe.
    """
    try:
        container = get_agent_container(agent_name)
        if container is None:
            return None
        env = _container_env(container)
        if not env.get("TRINITY_MCP_API_KEY") or not env.get("TRINITY_MCP_URL"):
            return "env_absent"
        from services.agent_service.helpers import check_agent_mcp_key_matches

        return None if check_agent_mcp_key_matches(container, agent_name) else "env_mismatch"
    except Exception as e:  # noqa: BLE001 — health must never 500
        logger.debug("container env health lookup failed for %s: %s", agent_name, e)
        return None


def _classify_health(last_used, last_execution_at):
    """`stale` is the load-bearing state (#1854).

    The motivating incident's signature is verbatim "the agent-scoped key sat
    unused for months" — non-NULL but old. A binary used/unused predicate
    buckets that as `active` and renders the whole panel green during the exact
    incident it was built for.

    `last_used_at` is a genuine signal because the high-frequency agent paths
    deliberately validate with ``track_usage=False`` (heartbeat, result
    callback, internal), so the field tracks real MCP tool use.
    """
    if not last_used:
        return (
            "never_used",
            "This key has never been used for an MCP tool call. That is normal "
            "for an agent that does not collaborate with other agents.",
        )

    used_at = _as_dt(last_used)
    exec_at = _as_dt(last_execution_at)
    if used_at and exec_at and (exec_at - used_at).total_seconds() > _STALE_AFTER_SECONDS:
        return (
            "stale",
            "This agent has run tasks well after its platform key was last used "
            "for an MCP call — worth checking what the container actually "
            "authenticates with.",
        )
    return ("active", "This key is in active use.")


def _as_dt(value):
    """Coerce to a tz-AWARE datetime, or None.

    The awareness coercion is load-bearing, not tidiness: `stale` subtracts one
    of these from the other, and `datetime - datetime` raises TypeError when
    exactly one side is naive. `last_used_at` arrives already parsed (aware for
    every `utc_now_iso()`-written row, but naive for any legacy row written
    before that convention), while the execution timestamp arrives as a raw
    string. One naive legacy row would otherwise turn an owner's Settings tab
    into a 500.
    """
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return parse_iso_timestamp(str(value))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Detect — container config-truth probe
# --------------------------------------------------------------------------- #
def interpret_probe_payload(payload: Dict[str, Any], agent_name: str) -> AgentMcpKeyVerifyResult:
    """Turn the in-container digest report into a verdict. Pure — no Docker, no HTTP."""
    entries_raw = payload.get("entries") or []
    trinity_entries = [
        e for e in entries_raw
        if isinstance(e, dict) and e.get("is_trinity") and e.get("digest")
    ]

    resolved: List[AgentMcpKeyVerifyEntry] = []
    for entry in trinity_entries:
        row = None
        try:
            row = db.find_mcp_key_by_hash(entry["digest"])
        except Exception as e:  # noqa: BLE001 — degrade, never 500
            logger.warning("key-hash lookup failed during verify: %s", e)
        resolved.append(
            AgentMcpKeyVerifyEntry(
                server_name=str(entry.get("name") or "")[:64],
                verdict=_entry_verdict(row, agent_name),
                key_scope=(row or {}).get("scope"),
                key_prefix=(row or {}).get("key_prefix"),
                key_agent_name=(row or {}).get("agent_name"),
            )
        )

    if not resolved:
        return AgentMcpKeyVerifyResult(
            agent_name=agent_name,
            verdict="not_configured",
            message=(
                # Covers both shapes honestly: no Trinity entry at all, and an
                # entry that carries no bearer token (which the probe filters out
                # because there is nothing to hash). Either way the agent cannot
                # authenticate, and either way a start re-injects.
                "The container has no usable Trinity MCP entry (missing, or "
                "present with no credential). It cannot call Trinity tools; "
                "starting the agent re-injects one."
            ),
            entries=[],
        )

    named_trinity = [e for e in resolved if e.server_name == "trinity"]
    shadows = [e for e in resolved if e.server_name != "trinity"]

    if shadows:
        # Cause (d): a Trinity-pointing entry under any other name survives every
        # restart forever — `inject_trinity_mcp_if_configured` overwrites only
        # the literal `trinity` key — and is NOT fixed by rotating the key. Any
        # credential-only remedy is blind to it, so it outranks the other
        # verdicts.
        return AgentMcpKeyVerifyResult(
            agent_name=agent_name,
            verdict="shadow_entry",
            message=(
                "A second Trinity-pointing MCP entry exists under a different "
                "server name. Re-injection only ever overwrites the entry named "
                "'trinity', so this one persists across every restart and is not "
                "fixed by regenerating the key — remove it from the agent's "
                ".mcp.json."
            ),
            entries=resolved,
        )

    primary = named_trinity[0]
    return AgentMcpKeyVerifyResult(
        agent_name=agent_name,
        verdict=primary.verdict,
        message=_verdict_message(primary),
        entries=resolved,
    )


def _entry_verdict(row: Optional[Dict[str, Any]], agent_name: str) -> str:
    if not row:
        return "unknown_key"
    scope = row.get("scope")
    if scope == "agent" and row.get("agent_name") == agent_name:
        return "ok" if row.get("is_active") else "unknown_key"
    if scope == "agent":
        return "foreign_agent_key"
    # #2323 — an `ops` key is a bounded read-only machine credential, not a
    # person's key. Reporting it as "a personal, user-scoped key" on a security
    # surface is simply false, and the remediation that message prescribes
    # (revoke it in Settings → MCP Keys) would break a monitoring integration.
    if scope == "ops":
        return "foreign_ops_key"
    # ALLOWlist, matching `reject_non_interactive_principal`: everything that is
    # not this agent's own agent-scoped key is foreign. `scope` is free-text with
    # no CHECK constraint, so an explicit ("user","system","portal_delegate")
    # denylist sends `connector` — and any sixth scope a future PR adds — down
    # the `unknown_key` branch, whose message asserts "matches no active key on
    # the platform". That is a false negative on a row we just read, and it
    # under-reports the exact condition this probe exists to surface.
    return "foreign_user_key"


def _verdict_message(entry: AgentMcpKeyVerifyEntry) -> Optional[str]:
    if entry.verdict == "ok":
        return "This agent authenticates with its own platform key."
    if entry.verdict == "foreign_user_key":
        # The badge the issue asked for. Deliberately identifies the offending
        # credential by PREFIX rather than by owner username: the prefix is what
        # Settings → MCP Keys lists (so it is the actionable handle) and it
        # avoids disclosing another account's identity to this agent's owner.
        return (
            "This agent authenticates with a personal, user-scoped key "
            f"({entry.key_prefix or 'unknown'}…) rather than its own — so it acts "
            "as that person and the permissions matrix is not in effect. "
            "Regenerate the key here, then revoke that one in Settings → MCP Keys."
        )
    if entry.verdict == "foreign_ops_key":
        return (
            "This agent authenticates with a bounded read-only ops key "
            f"({entry.key_prefix or 'unknown'}…) rather than its own. That key "
            "cannot reach this agent's own surfaces, so tool calls will fail. "
            "Regenerate this agent's key here; leave the ops key alone — it "
            "belongs to a monitoring integration."
        )
    if entry.verdict == "foreign_agent_key":
        return (
            "This agent authenticates with another agent's key "
            f"({entry.key_agent_name or 'unknown'}) — the permissions matrix is "
            "not in effect for this agent."
        )
    if entry.verdict == "unknown_key":
        return (
            "The key in this container matches no active key on the platform. "
            "Regenerating repairs it."
        )
    return None


async def verify_agent_mcp_key(agent_name: str) -> AgentMcpKeyVerifyResult:
    """Ask the running container what it is configured with. Never 500s."""
    def _unavailable(reason: str) -> AgentMcpKeyVerifyResult:
        return AgentMcpKeyVerifyResult(
            agent_name=agent_name, verdict="unavailable", message=reason, entries=[]
        )

    try:
        container = get_agent_container(agent_name)
        if container is None or getattr(container, "status", None) != "running":
            return _unavailable("The agent is not running, so its configuration cannot be read.")

        script = _build_probe_script([_trinity_mcp_url(), DEFAULT_TRINITY_MCP_URL])
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # b64 charset is [A-Za-z0-9+/=] — shell-safe inside the double quotes.
        command = f'bash -c "echo {b64} | base64 -d | python3 - 2>/dev/null"'
        result = await execute_command_in_container(
            container_name=f"agent-{agent_name}", command=command, timeout=_PROBE_TIMEOUT_S
        )
        raw = (result.get("output") or "").strip()
        if result.get("exit_code") != 0 or not raw:
            return _unavailable("Could not read the agent's MCP configuration.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return _unavailable("Could not read the agent's MCP configuration.")
        if not payload.get("present"):
            return AgentMcpKeyVerifyResult(
                agent_name=agent_name,
                verdict="not_configured",
                message="The container has no .mcp.json at all.",
                entries=[],
            )
        return interpret_probe_payload(payload, agent_name)
    except Exception as e:  # noqa: BLE001 — degrade, never 500
        logger.warning("MCP key verify probe failed for %s: %s", agent_name, e)
        return _unavailable("Could not read the agent's MCP configuration.")


# --------------------------------------------------------------------------- #
# Rotate
# --------------------------------------------------------------------------- #
def _regen_lock_client():
    """Redis client for the regeneration lock. Separate function so it is a
    single, obvious monkeypatch/observation point."""
    from routers.auth import get_redis_client
    return get_redis_client()


class _RegenLock:
    """Cross-worker per-agent rotation lock that fails **CLOSED**.

    Deliberately NOT the fail-open idiom used for agent data export
    (`agent_data.py`) — those precedents guard an idempotent export and a
    gitignore edit. Two concurrent rotations under a failed-open lock interleave
    to "the container holds K1 while the only active row is K2", which
    permanently 401s the heartbeat, the result callback, the pull worker and the
    MCP client at once, with the surviving plaintext unrecoverable. Trinity's
    doctrine for destructive ops is fail-closed (#1644: "a guard that fails open
    manufactures confidence"). Refusing a rare manual action for a few minutes
    costs nothing by comparison.
    """

    def __init__(self, agent_name: str):
        self.key = _REGEN_LOCK_KEY.format(name=agent_name)
        self.token = uuid.uuid4().hex
        self.client = None

    def __enter__(self):
        try:
            self.client = _regen_lock_client()
        except Exception as e:  # noqa: BLE001
            logger.warning("regen lock: Redis client unavailable: %s", e)
            self.client = None
        if self.client is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Key rotation is temporarily unavailable (the coordination "
                    "service is unreachable). Nothing was changed — try again."
                ),
            )
        try:
            held = bool(self.client.set(self.key, self.token, nx=True, ex=_REGEN_LOCK_TTL_S))
        except Exception as e:  # noqa: BLE001
            logger.warning("regen lock: acquire failed: %s", e)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Key rotation is temporarily unavailable (the coordination "
                    "service is unreachable). Nothing was changed — try again."
                ),
            )
        if not held:
            raise HTTPException(
                status_code=409,
                detail="A key rotation is already in progress for this agent.",
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client is not None and self.client.get(self.key) == self.token:
                self.client.delete(self.key)
        except Exception:  # noqa: BLE001 — the TTL backstops
            logger.debug("regen lock release failed for %s", self.key, exc_info=True)
        return False


def _acquire_regen_lock(agent_name: str) -> _RegenLock:
    """Single entry point for the rotation lock (one obvious seam)."""
    return _RegenLock(agent_name)


@contextlib.contextmanager
def _try_regen_lock(agent_name: str):
    """Best-effort sibling of :class:`_RegenLock` for the AUTOMATIC heal path.

    Same Redis key, so a start-time self-heal and an owner-initiated rotation
    are mutually exclusive — the two paths run the identical
    capture→mint→delete sequence and interleave into "the container holds K1
    while the only active row is K2".

    Different failure semantics, deliberately: an owner asked for a rotation, so
    refusing it must be *loud* (503/409); nobody asked for a heal, so refusing
    it must be *silent and inert*. Yields ``True`` when the lock is held and
    ``False`` otherwise; the caller must do nothing at all on ``False``, because
    a mint taken outside the lock can still be captured-and-deleted by whoever
    holds it.
    """
    key = _REGEN_LOCK_KEY.format(name=agent_name)
    token = uuid.uuid4().hex
    client = None
    held = False
    try:
        client = _regen_lock_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("regen lock (heal): Redis client unavailable: %s", e)
        client = None
    if client is not None:
        try:
            held = bool(client.set(key, token, nx=True, ex=_REGEN_LOCK_TTL_S))
        except Exception as e:  # noqa: BLE001
            logger.warning("regen lock (heal): acquire failed: %s", e)
            held = False
    try:
        yield held
    finally:
        if held and client is not None:
            try:
                if client.get(key) == token:
                    client.delete(key)
            except Exception:  # noqa: BLE001 — the TTL backstops
                logger.debug("regen lock (heal) release failed for %s", key, exc_info=True)


async def _recreate_with_env(agent_name: str, container, env_overrides: Dict[str, str]):
    """Replace the running container with one carrying `env_overrides`.

    Imported lazily: `lifecycle` reaches back into this module for the drift
    self-heal, so a module-level import would be circular.
    """
    from services.agent_service.lifecycle import recreate_container_with_updated_config

    # #2186 re-confirmed: the caller gates on `status == "running"` (a stopped
    # agent takes the DB-only path), so the `require_running=True` default is
    # correct here and stays implicit.
    await recreate_container_with_updated_config(
        agent_name, container, "system", env_overrides=env_overrides
    )
    return get_agent_container(agent_name)


def _container_env(container) -> Dict[str, str]:
    env_list = (getattr(container, "attrs", None) or {}).get("Config", {}).get("Env", [])
    return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}


async def regenerate_agent_mcp_key(
    agent_name: str,
    actor: User,
    *,
    actor_ip: Optional[str] = None,
    request_id: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> AgentMcpKeyRegenerateResult:
    """Rotate the agent's platform key. Returns metadata only — never plaintext."""
    # --- 1. refusals, BEFORE any mutation ---------------------------------- #
    if _is_system_agent(agent_name):
        # #1816 makes "a RUNNING trinity-system is NEVER recreated" a structural
        # invariant, and its key is scope='system' — minting a scope='agent'
        # replacement is an irreversible privilege downgrade (the old plaintext
        # is unrecoverable), while a scope='agent'-only delete would leave the
        # weaker key in the container with the strong one still active.
        raise HTTPException(
            status_code=409,
            detail=(
                "The platform orchestrator's key is system-scoped and is managed "
                "by the system-agent bootstrap, not by this endpoint."
            ),
        )
    if _is_ephemeral(agent_name):
        # Ghosts are volume-less by design ("ghosts never recreate"), so the
        # delivery recreate would silently destroy the workspace mid-budget,
        # including ~/.trinity/pending-results/ (#1083). A HUMAN owner reaches
        # this endpoint, so the refusal has to be explicit.
        raise HTTPException(
            status_code=409,
            detail=(
                "Ephemeral agents are volume-less and are never recreated, so "
                "their key cannot be rotated. Discard and re-spawn instead."
            ),
        )

    with _acquire_regen_lock(agent_name):
        return await _regenerate_locked(
            agent_name, actor, actor_ip=actor_ip, request_id=request_id, endpoint=endpoint
        )


async def _regenerate_locked(
    agent_name: str,
    actor: User,
    *,
    actor_ip: Optional[str],
    request_id: Optional[str],
    endpoint: Optional[str],
) -> AgentMcpKeyRegenerateResult:
    async def _audit(action: str, details: Dict[str, Any], target_id: Optional[str]) -> None:
        # NEVER the plaintext, the key hash, env_vars, the probe output, or a raw
        # exception string: audit_log is append-only with a 365-day no-delete
        # trigger and `details` is json.dumps'd unsanitised, so anything written
        # here is permanent and undeletable.
        await platform_audit_service.log(
            event_type=AuditEventType.MCP_OPERATION,
            event_action=action,
            source="api",
            actor_user=actor,
            actor_ip=actor_ip,
            # #2323 — the manual `mcp_scope=getattr(actor, "mcp_scope", None)`
            # that used to sit here is gone: `platform_audit_service.log` now
            # derives all three `mcp_*` columns from `actor_user` itself. Two
            # spellings of one derivation is the ent#297 "third spelling" trap,
            # and this one covered only `mcp_scope`.
            target_type="mcp_key",
            target_id=target_id,
            endpoint=endpoint,
            request_id=request_id,
            details=details,
        )

    # --- 2. capture BEFORE the mint ---------------------------------------- #
    # Capturing first is what makes a concurrent `recreate_missing_container`
    # mint immune: an "everything except the new id" delete would remove the key
    # actually baked into the live container.
    captured = list(db.list_active_agent_key_ids(agent_name))

    # --- 3. mint ----------------------------------------------------------- #
    try:
        env_overrides, new_key = _mint_and_build_env(agent_name, "owner-initiated rotation (#1854)")
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP key rotation: mint failed for %s: %s", agent_name, e)
        await _audit("agent_key_regenerate_failed", {"agent_name": agent_name, "stage": "mint"}, None)
        raise HTTPException(
            status_code=500,
            detail="Could not issue a new key for this agent. Nothing was changed.",
        )

    # --- 4. reconcile children BEFORE delivery ------------------------------ #
    # The instant the mint commits, `get_agent_mcp_api_key` returns the new key
    # while children still store the old id — and `enforce_agent_spawn_scope`
    # compares newest-active vs stored, not the key that authenticated. Left
    # until after delivery this opens a 403 window across the whole recreate,
    # and a crash mid-flight makes it permanent.
    try:
        children_repointed = db.reconcile_spawn_key_id(agent_name, new_key.id)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP key rotation: spawn-id reconcile failed for %s: %s", agent_name, e)
        children_repointed = 0

    # --- 5. deliver --------------------------------------------------------- #
    container = get_agent_container(agent_name)
    running = container is not None and getattr(container, "status", None) == "running"

    if not running:
        # A STOPPED agent takes the DB-only path. `recreate_container_with_
        # updated_config` ends at `containers_run(..., detach=True)`, which
        # CREATES AND STARTS — and stopping an agent is the standard containment
        # response to a suspected compromise, with rotating its key the very next
        # step. Silently booting it would resume schedules, autonomy and spend.
        # The drift predicate bakes the new key on its next deliberate start.
        delivery = "db_only"
    else:
        # #1560: the heartbeat markers and both breakers are keyed by agent NAME,
        # so the replacement inherits the predecessor's verdict and is fast-failed
        # without ever being contacted — read as "the rotation broke my agent".
        # learnings.md names this exact call site.
        clear_agent_breakers(agent_name)
        try:
            new_container = await _recreate_with_env(agent_name, container, env_overrides)
        except Exception as e:  # noqa: BLE001
            # The old container is stopped and REMOVED before the replacement is
            # created, so on failure the agent has no container at all. Say so;
            # do not claim continuity. Superseded keys are deliberately left in
            # place — the drift predicate and `recreate_missing_container` heal
            # on the next start.
            logger.error(
                "MCP key rotation: delivery failed for %s (%s: %s)",
                agent_name, type(e).__name__, e,
            )
            await _audit(
                "agent_key_regenerate_failed",
                {
                    "agent_name": agent_name,
                    "stage": "delivery",
                    "new_key_id": new_key.id,
                    "superseded_key_ids": captured,
                    "superseded_deleted": False,
                    "error_type": type(e).__name__,
                },
                new_key.id,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "A new key was issued, but the container was replaced and "
                    "failed to start — the agent is down. Press Start to rebuild "
                    "it; the new key is applied automatically. The previous keys "
                    "were left in place."
                ),
            )

        # 409-adoption post-condition. On a name conflict the recreate ADOPTS a
        # container someone else created — with someone else's env. Without this
        # check we would delete the superseded keys while the live container
        # holds a different one, 401-ing all four readers.
        if not await _new_key_is_live(new_container, new_key.api_key):
            await _audit(
                "agent_key_regenerate_failed",
                {
                    "agent_name": agent_name,
                    "stage": "postcondition",
                    "new_key_id": new_key.id,
                    "superseded_key_ids": captured,
                    "superseded_deleted": False,
                },
                new_key.id,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "A new key was issued but the running container is not "
                    "carrying it (another process replaced this container "
                    "concurrently). No keys were removed — start the agent again "
                    "to converge."
                ),
            )
        delivery = "recreated"

    # --- 6. delete the CAPTURED superseded ids ------------------------------ #
    deleted = 0
    try:
        deleted = db.delete_superseded_agent_keys(agent_name, new_key.id, captured)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP key rotation: superseded prune failed for %s: %s", agent_name, e)

    # --- 7. audit + metadata-only response ---------------------------------- #
    await _audit(
        "agent_key_regenerate",
        {
            "agent_name": agent_name,
            "new_key_id": new_key.id,
            "new_key_prefix": new_key.key_prefix,
            "superseded_key_ids": captured,
            "superseded_deleted": deleted,
            "delivery": delivery,
            "children_repointed": children_repointed,
        },
        new_key.id,
    )

    return AgentMcpKeyRegenerateResult(
        agent_name=agent_name,
        key_id=new_key.id,
        key_prefix=new_key.key_prefix,
        delivery=delivery,
        superseded_deleted=deleted,
        children_repointed=children_repointed,
        message=(
            "A new platform key was issued and the agent was restarted with it."
            if delivery == "recreated"
            else "A new platform key was issued. The agent is stopped — it will "
                 "pick the key up the next time you start it."
        ),
    )


async def _new_key_is_live(container, expected_key: Optional[str]) -> bool:
    """Does the live container carry EXACTLY the key we just minted?

    Compared in full, constant-time — not by ``key_prefix``. The prefix is
    ``api_key[:20]``, of which the first 12 characters are the literal
    ``trinity_mcp_``, so a prefix test is an eight-character assertion standing
    between a 409-adopted stranger's container and the DELETE of every
    superseded key. The full plaintext is already in this frame (it is what we
    just baked), so the exact comparison is free and leaves no residual
    argument about entropy. Never logged, never returned, never audited.
    """
    if container is None or not expected_key:
        return False
    try:
        await container_reload(container)
    except Exception:  # noqa: BLE001 — fall back to the attrs we already have
        pass
    baked = _container_env(container).get("TRINITY_MCP_API_KEY") or ""
    return secrets.compare_digest(baked, expected_key)
