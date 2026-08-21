"""#1920 — anti-recurrence guard for hand-rolled single-flight locks.

The reliability value of #1920 is not the cleanup, it's that the SETNX
single-flight lock class **can't quietly recur** — a new `set(..., nx=True, …)`
lock that skips the shared, ownership-checked `redis_breaker_util.SingleFlightLock`
is exactly how #1919's bug lived on in sibling copies (worst class: the
constant-"1" + unconditional tokenless delete in system_seed, and its verbatim
twins cornelius/compat_fix). A doc line doesn't fail CI; this does.

Two invariants:

1. **Every `.set(..., nx=True, …)` call in the backend is accounted for.** Each
   must sit in ``redis_breaker_util`` (the primitive) or in the explicit
   allowlist below — a deliberate divergence (async Lua `ResumeLock`; the
   leader leases), a pre-#1920 hand-rolled lock not in this issue's scope (the
   `LeaderLease`/other-lock follow-up surface), or a genuine non-lock `nx`
   use (a once-guard, a quota seed, a liveness marker). A NEW unlisted site
   fails here, forcing the author to adopt `SingleFlightLock` or justify the
   divergence in one place.

2. **The #1920-adopted sync-lock files no longer hand-roll one.** After
   consolidation, `system_seed_service` / `routers.ops` / `skill_service` /
   `cornelius_agent_service` / `compatibility.fixes` issue NO `nx=True` set of
   their own, and `ephemeral` keeps only its (non-lock) quota seed — proof the
   consolidation actually removed the copies rather than adding another.

Plus the ACL trap: the leaf `redis_breaker_util.py` must never introduce
`KEYS` / `.keys(` / `SCAN` — the backend Redis ACL is `-@dangerous`, so `KEYS`
raises at runtime and a stubbed client hides it (learnings:
reference_backend_redis_keys_blocked_acl.md). The locks are single fixed keys,
so no scan is ever needed.

Mirrors the repo's static-guard convention (#1560 keyspace parity, #293
admin-gate spelling, #1891 python-version parity).
"""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# Files permitted to issue a `set(..., nx=True, …)`. Keyed by backend-relative
# POSIX path → the reason it is not a violation. Adding a NEW single-flight lock
# means adopting `SingleFlightLock` (no entry needed) — NOT extending this map.
_ALLOWED = {
    # THE home of the consolidated single-flight lock.
    "redis_breaker_util.py": "the SingleFlightLock primitive itself (#1920)",
    # --- deliberate divergences (structurally inexpressible by the sync primitive) ---
    "services/session_turn_service.py": "ResumeLock — async redis.asyncio + atomic Lua CAD + blocking poll; "
    "do NOT merge onto the weaker GET-then-DELETE release (#1920)",
    "services/monitoring_service.py": "monitoring:leader — leader lease, stable cross-cycle worker id (#1464)",
    "services/operator_queue_service.py": "opqueue:leader — leader lease, verbatim copy of monitoring (#1632)",
    "services/skills_sync_service.py": "skills:sync:leader — leader lease (ent#236)",
    "services/canary_service.py": "canary:leader — Lua-CAD leader lease, the 8th shape (#1881)",
    "services/subscription_recovery_service.py": "subscription:recovery:leader — leader lease in the #1464 monitoring shape (#447). NOT adoptable: SingleFlightLock mints a UNIQUE token per acquire, so a lease could never recognise — and therefore never refresh — its own grant across cycles; this one keeps a stable per-worker id for exactly that",
    # --- pre-#1920 hand-rolled single-flight locks NOT in this issue's scope ---
    #     (the LeaderLease / other-lock consolidation follow-up surface).
    "routers/agent_data.py": "agent:data_op single-flight lock (#1169) — pre-#1920, follow-up",
    "routers/git.py": "agent:bind_op / agent:bind_dest single-flight locks (ent#109) — pre-#1920, follow-up",
    "services/agent_mcp_key_service.py": "agent MCP-key regen lock (#1854) — pre-#1920, follow-up",
    "services/credential_requirements_service.py": "credential-requirements probe lock (ent#127) — pre-#1920, follow-up",
    "services/db_backup_service.py": "db-backup duplicate-I/O lease (#2216, landed on dev while #1920 was open) — "
    "already token + compare-and-delete, so NOT the #1919 bug class; consolidation only, tracked follow-up",
    # --- genuine non-lock nx=True uses (continued) ---
    "services/docker_service.py": "port_alloc:{port} SSH-port reservation (#2215) — an allocation over a keyspace of "
    "many keys, not a mutex: no release, no token, TTL-expiry only, and it deliberately PROPAGATES Redis errors so the "
    "caller decides. SingleFlightLock would be actively wrong here — its internal fail-open returns True on a Redis "
    "error, which for a port reservation reads as 'reserved' and hands two agents the same SSH port, the exact "
    "collision #2215 fixed",
    # --- genuine non-lock nx=True uses ---
    "adapters/transports/twilio_media_stream.py": "voip_saved:{call_id} single-fire transcript guard — a once-guard, not a mutex",
    "services/agent_service/ephemeral.py": "ephemeral:quota:{owner_id} counter seed — the discard LOCK now uses "
    "SingleFlightLock (#1920); this is a different, non-lock nx use",
    "services/heartbeat_service.py": "agent:heartbeat:seen:{name} liveness marker — not a lock",
}

# The sync-lock files #1920 adopted MUST no longer hand-roll a nx=True set.
# `ephemeral` is the exception: its discard lock is adopted, but its quota SEED
# is a separate, legitimate non-lock nx use — so it stays in _ALLOWED and is
# excluded from this hard "must be clean" set. `cornelius_agent_service` and
# `compatibility/fixes` carried the SAME constant-"1" + unconditional-delete bug
# as system_seed (found while building this guard) and were adopted too.
_ADOPTED_MUST_BE_CLEAN = {
    "services/system_seed_service.py",
    "routers/ops.py",
    "services/skill_service.py",
    "services/cornelius_agent_service.py",
    "services/compatibility/fixes.py",
}


def _iter_backend_py():
    for path in _BACKEND.rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        yield path


def _has_nx_true_set(tree: ast.AST) -> bool:
    """True if the module issues any `<obj>.set(..., nx=True, …)` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "set"):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "nx"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                return True
    return False


def _collect_nx_true_files() -> set[str]:
    found: set[str] = set()
    for path in _iter_backend_py():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if _has_nx_true_set(tree):
            found.add(path.relative_to(_BACKEND).as_posix())
    return found


def test_no_unlisted_hand_rolled_single_flight_lock():
    """A new `set(nx=True, …)` outside the allowlist is a new hand-rolled lock
    (or nx use). Adopt `SingleFlightLock`, or — for a genuine divergence /
    non-lock use — add ONE justified allowlist entry."""
    found = _collect_nx_true_files()
    unlisted = sorted(found - set(_ALLOWED))
    assert not unlisted, (
        "New `set(..., nx=True, …)` call(s) outside redis_breaker_util."
        "SingleFlightLock:\n  " + "\n  ".join(unlisted) + "\n\n"
        "If this is a single-flight lock, adopt SingleFlightLock. If it is a "
        "deliberate divergence or a genuine non-lock nx use, add a justified "
        "entry to _ALLOWED in this file."
    )


def test_adopted_sync_lock_sites_no_longer_hand_roll_one():
    """Proof the #1920 consolidation REMOVED the copies rather than adding a
    sixth: the fully-adopted sites issue no nx=True set of their own."""
    found = _collect_nx_true_files()
    still_hand_rolling = sorted(_ADOPTED_MUST_BE_CLEAN & found)
    assert not still_hand_rolling, (
        "These files were consolidated onto SingleFlightLock but still issue a "
        "raw `set(nx=True, …)`:\n  " + "\n  ".join(still_hand_rolling)
    )


def test_allowlist_has_no_stale_entries():
    """Keep the allowlist honest — an entry whose file no longer issues a
    nx=True set should be removed (it silently permits a future re-introduction
    there). Informational-but-enforced: adopting a listed lock later means
    deleting its row here."""
    found = _collect_nx_true_files()
    stale = sorted(set(_ALLOWED) - found)
    assert not stale, (
        "Stale _ALLOWED entries (file no longer issues nx=True — remove the "
        "row so it can't silently permit a re-introduction):\n  " + "\n  ".join(stale)
    )


def test_helper_has_no_KEYS_or_scan():
    """The backend Redis ACL is `-@dangerous`: `KEYS`/`SCAN` raise at runtime
    and a stubbed client hides it. The single-flight lock uses single fixed
    keys, so the leaf helper must never introduce a keyspace scan."""
    src = (_BACKEND / "redis_breaker_util.py").read_text(encoding="utf-8")
    for needle in (".keys(", "KEYS", ".scan(", ".scan_iter(", "SCAN"):
        assert needle not in src, (
            f"redis_breaker_util.py must not use {needle!r} — the ACL blocks "
            "KEYS/SCAN and the locks are single fixed keys (no scan needed)."
        )
