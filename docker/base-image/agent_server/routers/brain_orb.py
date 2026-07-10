"""
Brain Orb data endpoint (#58, trinity-enterprise).

Read-only: serves the agent-produced visualization (`data.json`) that the
Brain Orb page renders. The agent owns generation (export_data.py) — Invariant
#8; this server only reads the last-written export. The path is fixed (no user
input), so there is no traversal surface. Inbound auth is enforced globally by
AgentAuthMiddleware (#1159) — only /health is exempt.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Cornelius-class convention: the read-only exporter writes here.
DATA_PATH = Path("/home/developer/resources/agent-visualization/data.json")

# #58 Phase 2 — live scope control. The agent provides two executable convention
# hooks (mirrors ~/.trinity/pre-check, #454); Trinity stays generic and never
# contains agent-specific scope logic (Invariant #8). Absent hooks ⇒ 404 (the
# agent doesn't support scope control), and the orb's scope panel degrades.
HOOK_DIR = Path("/home/developer/.trinity/brain-orb")
_SCOPES_HOOK = HOOK_DIR / "scopes"   # GET: print JSON {active, available}
_SCOPE_HOOK = HOOK_DIR / "scope"     # POST: read JSON {tokens|mount|unmount} on stdin,
                                     #       mutate + re-export (rewrites data.json),
                                     #       print JSON {ok, active, nodes, edges}
# #58 Phase 3 (trinity-enterprise#60) — read-only KB search. The voice tile's
# navigate_to_note fallback (and any vault search) pipes a JSON query on stdin;
# the hook runs a SCOPE-AWARE, READ-ONLY search over the vault (fails closed to
# the core scope when none is mounted) and prints JSON results. No writes.
_SEARCH_HOOK = HOOK_DIR / "search"   # POST: read JSON {query, ...} on stdin, print JSON {results|...}
# #58 Phase 4a (trinity-enterprise#61) — owner-gated KB WRITES. The agent ships one
# `action` hook that dispatches on the request's `action` field (read on stdin):
#   {action:"list"}            → print {enabled, skills}  (the run_skill allow-list; 4b)
#   {action:"capture", ...}    → write a note to the agent's inbox → {ok, title|...}
#   {action:"link", from,to}   → connect two notes ([[wikilink]])   → {ok, already?}
# The agent OWNS the write semantics + (in 4b) the allow-list/job lifecycle
# (Invariant #8); Trinity only brokers + gates (owner-only, upstream). Absent hook
# ⇒ 404 (the agent doesn't support writes), and the orb's action panel stays hidden.
# run_skill / capture_transcript are Phase 4b (trinity-enterprise#66) — NOT handled here.
_ACTION_HOOK = HOOK_DIR / "action"
# #73 — post-voice-processing config {enabled, prompt}, edited from the Brain tab.
# JSON is authoritative; the legacy .md is a prompt-only read fallback.
_POSTPROCESS_CONFIG = HOOK_DIR / "voice-postprocess.json"
_POSTPROCESS_MD = HOOK_DIR / "voice-postprocess.md"
_HOME = Path("/home/developer")
_MAX_HOOK_BODY = 64 * 1024           # scope/search requests are tiny (token list or query)
_MAX_HOOK_OUT = 4 * 1024 * 1024      # hooks return small JSON; cap defensively


def _hook_ready(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


async def _run_hook(path: Path, *, stdin: bytes = b"", timeout: float):
    """Run an agent convention hook (shebang-selected interpreter) and parse its
    JSON stdout. Hardened: timeout-kill, output cap, JSON-parse + non-zero-exit
    guards. Never trusts the hook beyond returning structured JSON.

    Sweep-safe (#1501): the hook pid is registered as a transient allowlist
    entry for the duration of the run — a hook is a live agent-server child,
    but the orphan sweep's hard-protect walk only goes UP the parent chain,
    so without registration any 30s periodic sweep firing mid-run SIGKILLs
    the hook (exit -9 → 502; the 180s refresh/scope hooks straddled a sweep
    boundary essentially every run). Descendants (the hook's own git/python
    children) are covered by the allowlist's ppid walk."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            cwd=str(_HOME),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.warning("brain-orb hook %s not executable: %s", path.name, e)
        raise HTTPException(status_code=502, detail="Scope hook not executable")
    # Fail-open registration: a registry error must never break the hook call —
    # worst case is the pre-#1501 sweep exposure. Lazy import mirrors the other
    # low-level registry consumers (orphan_sweeper, subprocess_pgroup).
    registry = None
    try:
        from ..services.process_registry import get_process_registry  # lazy
        registry = get_process_registry()
        # Protection window derived from THIS run's budget (+headroom for the
        # kill/collect tail) — a fixed TTL below a future hook's timeout would
        # lazily evict mid-run and silently reintroduce the #1501 kill.
        registry.add_transient_pid(proc.pid, ttl_seconds=timeout + 60)
    except Exception:
        logger.warning(
            "brain-orb hook %s: transient-pid registration failed — hook runs "
            "unprotected from the orphan sweep", path.name, exc_info=True,
        )
        registry = None
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail="Scope hook timed out")
        except asyncio.CancelledError:
            # #1501: client disconnected mid-hook. Kill deterministically —
            # the finally below drops the pid off the allowlist, so leaving
            # the hook alive would just hand it to the next sweep's SIGKILL
            # at a random point mid-write. Reap with a tight bound (mirrors
            # the timeout path) so the subprocess transport closes promptly;
            # suppressed because a second cancellation may land mid-wait —
            # the original CancelledError is re-raised either way.
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except BaseException:  # noqa: BLE001 — bounded best-effort reap
                pass
            raise
    finally:
        if registry is not None:
            try:
                registry.remove_transient_pid(proc.pid)
            except Exception:  # noqa: BLE001 — removal is best-effort; TTL backstops
                pass
    if proc.returncode != 0:
        logger.warning(
            "brain-orb hook %s exit %s: %s",
            path.name, proc.returncode, (err or b"")[:500].decode(errors="replace"),
        )
        raise HTTPException(status_code=502, detail="Scope hook failed")
    if len(out) > _MAX_HOOK_OUT:
        raise HTTPException(status_code=502, detail="Scope hook output too large")
    try:
        return json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=502, detail="Scope hook returned invalid JSON")


@router.get("/api/brain-orb/data")
async def get_brain_orb_data():
    """Stream the agent's visualization data.json (multi-MB; FileResponse avoids
    buffering it in memory). 404 when the agent hasn't produced an export yet —
    the frontend renders an empty state, never a 500."""
    if not DATA_PATH.is_file():
        raise HTTPException(status_code=404, detail="Brain Orb data not found")
    return FileResponse(
        path=str(DATA_PATH),
        media_type="application/json",
        # The orb fetch sets no filename expectation; inline keeps it a data read.
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/brain-orb/scopes")
async def get_brain_orb_scopes():
    """List the agent's selectable + active vault scopes (for the orb's scope
    panel). 404 when the agent ships no `scopes` hook (scope control unsupported)."""
    if not _hook_ready(_SCOPES_HOOK):
        raise HTTPException(status_code=404, detail="Scope control not supported")
    return await _run_hook(_SCOPES_HOOK, timeout=30)


@router.post("/api/brain-orb/scope")
async def post_brain_orb_scope(request: Request):
    """Mutate the active scope set (mount/unmount), re-export, and return the new
    state. The hook rewrites data.json; the orb then re-fetches /brain-orb/data and
    rebuilds. 404 when the agent ships no `scope` hook. The mutating side is
    owner-gated upstream at the backend proxy."""
    if not _hook_ready(_SCOPE_HOOK):
        raise HTTPException(status_code=404, detail="Scope control not supported")
    body = await request.body()
    if len(body) > _MAX_HOOK_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    # 180s: a re-export over a large vault can be slow; the orb shows a spinner.
    return await _run_hook(_SCOPE_HOOK, stdin=body, timeout=180)


@router.post("/api/brain-orb/tool")
async def post_brain_orb_tool(request: Request):
    """Run the agent's read-only KB-search hook (#58 Phase 3). Pipes the request
    body (a JSON `{query, ...}`) to `~/.trinity/brain-orb/search`, which searches
    the vault scope-aware and read-only and prints JSON results. 404 when the agent
    ships no `search` hook (KB search unsupported → the orb degrades to in-graph
    search). Read-only by hook contract; the mutating side stays the `scope` hook.
    The backend proxy gates this at `AuthorizedAgentByName` (read access)."""
    if not _hook_ready(_SEARCH_HOOK):
        raise HTTPException(status_code=404, detail="KB search not supported")
    body = await request.body()
    if len(body) > _MAX_HOOK_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    # 30s: a vault search is read-only and quick; no re-export.
    return await _run_hook(_SEARCH_HOOK, stdin=body, timeout=30)


@router.get("/api/brain-orb/actions")
async def get_brain_orb_actions():
    """Report the agent's write-surface capability + skill allow-list (#58 Phase 4a).
    Runs the `action` hook with `{action:"list"}` on stdin → `{enabled, skills}`. 404
    when the agent ships no `action` hook (KB writes unsupported → the orb hides the
    action panel). The owner gate lives upstream at the backend proxy."""
    if not _hook_ready(_ACTION_HOOK):
        raise HTTPException(status_code=404, detail="KB writes not supported")
    return await _run_hook(_ACTION_HOOK, stdin=b'{"action":"list"}', timeout=30)


# capture/link bodies are tiny, but capture_transcript (#66) carries a whole voice
# conversation — allow up to 1 MiB (the backend proxy gates owner-only + rate-limited).
_MAX_ACTION_BODY = 1024 * 1024


@router.post("/api/brain-orb/action")
async def post_brain_orb_action(request: Request):
    """Run an owner-gated KB-write action. Pipes the request body (a JSON `{action, ...}`)
    to `~/.trinity/brain-orb/action`, which performs the write and prints JSON. 404 when
    the agent ships no `action` hook. The backend proxy gates this at `OwnedAgentByName`
    (owner/admin) and enum-restricts the verb (capture/link + capture_transcript, #66;
    run_skill stays out). Post-session processing is dispatched by the BACKEND as a
    standard execution (#102) — the backend strips `capture_transcript`'s `process` flag
    and never forwards `process_transcript`, so the hook only writes files here (a
    hook-forked detached `claude -p` is unregistered and reaped by the orphan sweep)."""
    if not _hook_ready(_ACTION_HOOK):
        raise HTTPException(status_code=404, detail="KB writes not supported")
    body = await request.body()
    if len(body) > _MAX_ACTION_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    # 60s: note write / link / transcript render are quick file ops.
    return await _run_hook(_ACTION_HOOK, stdin=body, timeout=60)


@router.post("/api/brain-orb/refresh")
async def post_brain_orb_refresh():
    """Reindex + re-export the graph so newly captured notes/links appear (#66/#67).
    Runs the `action` hook with `{action:"refresh"}`; the agent folds inbox writes into
    the graph and regenerates data.json (agent owns generation, Invariant #8). 180s —
    a large-vault reindex can be slow (the backend proxy sits at 200s above this).
    404 when the agent ships no `action` hook."""
    if not _hook_ready(_ACTION_HOOK):
        raise HTTPException(status_code=404, detail="Refresh not supported")
    return await _run_hook(_ACTION_HOOK, stdin=b'{"action":"refresh"}', timeout=180)


@router.get("/api/brain-orb/postprocess")
async def get_brain_orb_postprocess():
    """Read the post-voice-processing config {enabled, prompt} (#73) for the Brain tab.
    JSON preferred; legacy .md is a prompt-only fallback. Never 500 — absent ⇒ disabled."""
    try:
        c = json.loads(_POSTPROCESS_CONFIG.read_text())
        return {"enabled": bool(c.get("enabled")), "prompt": str(c.get("prompt") or "")}
    except Exception:
        pass
    try:
        if _POSTPROCESS_MD.is_file():
            p = _POSTPROCESS_MD.read_text().strip()
            return {"enabled": bool(p), "prompt": p}
    except Exception:
        pass
    return {"enabled": False, "prompt": ""}


@router.put("/api/brain-orb/postprocess")
async def put_brain_orb_postprocess(request: Request):
    """Write the post-voice-processing config {enabled, prompt} (#73). Owner-gated at
    the backend proxy. The agent's `action` hook reads this to decide whether to run
    the post-session `claude -p` — `enabled:false` keeps the saved prompt but skips it."""
    body = await request.body()
    if len(body) > _MAX_HOOK_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    cfg = {"enabled": bool(data.get("enabled")), "prompt": str(data.get("prompt") or "")[:8000]}
    _POSTPROCESS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _POSTPROCESS_CONFIG.write_text(json.dumps(cfg))
    return {"ok": True, **cfg}
