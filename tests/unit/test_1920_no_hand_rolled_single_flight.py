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

**Both trees are walked (#2742).** This guard originally walked `src/backend`
only, and Invariant #5 names exactly that failure — *"a guard that walks only one
of the two trees is not a guard"* (ent#314, whose AST scan had an empty allowlist
over the whole backend and still missed six bare `yaml.safe_load` calls in the
agent server, because it never looked there). #2742 gave the agent-server tree a
single-flight of its own, so the walk now covers
`docker/base-image/agent_server/` too, with agent-server paths reported under an
`agent_server/` prefix so the two trees can never collide in `_ALLOWED`.

That tree currently issues **no** `nx=True` set, and deliberately gets no
allowlist row: agents physically cannot route to Redis (they are on
`trinity-agent-network`, Redis is on `trinity-platform-network`), so a
distributed lock there would itself be the bug. #2742's coalescing is a
different class — an in-process `asyncio.Future` slot on a single-process server
— and is pinned behaviourally by `test_2742_git_status_lock_free.py`
(`test_only_one_thread_is_used_per_inflight_computation` and the `shield` /
slot-clearing tests) plus the one-home assertion at the bottom of this file.

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

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_AGENT_SERVER = _REPO / "docker" / "base-image" / "agent_server"

# The trees this guard walks → the prefix their findings are reported under.
# Backend files keep their bare backend-relative path so every existing
# `_ALLOWED` key is unchanged; agent-server files get an `agent_server/` prefix.
_TREES = ((_BACKEND, ""), (_AGENT_SERVER, "agent_server/"))

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
    "services/sync_health_service.py": "synchealth:leader — leader lease in the #1464 monitoring shape (#2742). NOT adoptable: SingleFlightLock mints a unique token per acquire, so a lease could never recognise — and therefore never refresh — its own grant across cycles; a stable per-worker id is kept for exactly that",
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
    "services/gemini_voice.py": "voice_session:{id}:saved transcript-save CLAIM (ent#534) — a one-shot 'who writes' "
    "decision between the WebSocket `finally` and `/stop` on two workers: no release, no token, TTL-expiry only, over "
    "redis.asyncio (the sync primitive cannot be awaited from the bridge). Fails OPEN by design — losing a transcript "
    "is worse than a duplicate, and the in-process `_transcript_saved` flag covers the same-worker case",
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


def _iter_guarded_py():
    """Every .py in BOTH guarded trees, with the prefix it reports under."""
    for root, prefix in _TREES:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            yield path, root, prefix


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
    for path, root, prefix in _iter_guarded_py():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if _has_nx_true_set(tree):
            found.add(prefix + path.relative_to(root).as_posix())
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


def test_both_trees_are_actually_walked():
    """Meta-assertion with teeth: if the agent-server root is renamed or moved,
    the walk would silently degrade to the single-tree guard ent#314 was burned
    by, and every other test here would still pass."""
    assert _AGENT_SERVER.is_dir(), (
        f"the agent-server tree is not where this guard looks ({_AGENT_SERVER}) — "
        "the walk has silently degraded to backend-only"
    )
    walked = {root for _path, root, _prefix in _iter_guarded_py()}
    assert walked == {_BACKEND, _AGENT_SERVER}


def test_agent_server_single_flight_has_exactly_one_home():
    """#2742 planted an in-process single-flight in the agent server. That class
    is invisible to the `nx=True` scan above (it uses no Redis — agents cannot
    route to it at all), so its "one home" property is asserted directly: a
    second copy is how a pattern quietly becomes a family."""
    homes = sorted(
        path.relative_to(_AGENT_SERVER).as_posix()
        for path, _root, _prefix in _iter_guarded_py()
        if _root == _AGENT_SERVER and "_STATUS_INFLIGHT" in path.read_text(encoding="utf-8")
    )
    assert homes == ["routers/git.py"], (
        "the /api/git/status single-flight must live in exactly one module "
        f"(found: {homes}). A second copy means a second slot with its own "
        "clearing rules — adopt the existing one or justify the divergence."
    )
