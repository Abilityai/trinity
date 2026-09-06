"""Best-effort read of an agent's published pipeline state (trinity-enterprise#525).

The #919 contract, read from the backend for the Work card: the agent — never
Trinity — owns the DAG and publishes

    ~/.trinity/pipelines/<pipeline_id>.yaml                 the definition
    ~/.trinity/pipeline-state/<pipeline_id>/<instance>.json the runtime state

and Trinity's only contribution is a read surface. The MCP tools
(`src/mcp-server/src/tools/pipelines.ts`) are that surface for agents; this
module is the same read for the Workspace card, through the same agent-server
file routes, under the same hardening rules — restated here because the two
cannot share code across languages:

  * ids are validated against ``^[A-Za-z0-9._-]+$`` and never contain ``..``
    BEFORE they are interpolated into a download path (the agent-server's
    download route has only a ``/home/developer`` prefix check);
  * a file's ``size`` from the listing is checked BEFORE the download, and the
    download itself streams under a byte budget — a cap applied after
    ``response.text`` is a cap on memory already spent (review S2);
  * YAML goes through ``utils.safe_yaml.load_hardened_yaml`` (the ent#314
    loader), JSON through ``json.loads`` inside a try;
  * no retries (a stopped agent must not turn one read into a gateway
    timeout — review S3), a 2 s per-call timeout inside one 3 s wall budget,
    and a 10 s per-agent cache so a 12 s poll never issues two reads.

Every failure is a VERDICT, not an exception: ``state == "unknown"`` with the
reason in the log. The ledger half of the Work read never waits on this.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from utils.helpers import parse_iso_timestamp
from utils.safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml

from .models import WorkStage, WorkSteps

logger = logging.getLogger(__name__)

#: The MCP tool's grammar, verbatim (`pipelines.ts` ID_PATTERN + the `..` refinement).
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
#: Per-file ceiling, mirrors `pipelines.ts` MAX_FILE_BYTES.
MAX_FILE_BYTES = 256 * 1024
#: Fan-out cap per agent: at most this many instance files are read.
MAX_INSTANCES = 8

PIPELINES_DIR = "/home/developer/.trinity/pipelines"
STATE_DIR = "/home/developer/.trinity/pipeline-state"

HTTP_TIMEOUT_SECONDS = 2.0     # httpx per-phase, like `_BRIEFING_HTTP_TIMEOUT_SECONDS`
WALL_BUDGET_SECONDS = 3.0      # one agent's whole read, like `_BRIEFING_BUDGET_SECONDS`
CACHE_TTL_SECONDS = 10.0

_cache: Dict[str, Tuple[float, WorkSteps]] = {}

_UNKNOWN = WorkSteps(state="unknown")
_NONE = WorkSteps(state="none")


def valid_id(value: Any) -> bool:
    """The `pipelines.ts` rule: the class rejects `/` and `%`, the refinement `..`."""
    return isinstance(value, str) and 0 < len(value) <= 128 \
        and bool(_ID_RE.match(value)) and ".." not in value


def strip_extension(name: str) -> str:
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def instance_candidates(tree: Any) -> List[Tuple[str, str, int, str]]:
    """``(pipeline_id, instance_id, size, modified)`` for every readable state file.

    Walks the `/api/files` tree ONE level: a directory per pipeline, a JSON
    file per instance. Anything with an invalid id, a non-JSON name, a missing
    or oversized `size`, or an unexpected shape is dropped here — before any
    path is built from it.
    """
    out: List[Tuple[str, str, int, str]] = []
    if not isinstance(tree, list):
        return out
    for pipe in tree:
        if not isinstance(pipe, dict) or pipe.get("type") != "directory":
            continue
        pid = pipe.get("name")
        if not valid_id(pid):
            continue
        for child in pipe.get("children") or []:
            if not isinstance(child, dict) or child.get("type") != "file":
                continue
            name = child.get("name")
            if not isinstance(name, str) or not name.endswith(".json"):
                continue
            iid = strip_extension(name)
            if not valid_id(iid):
                continue
            size = child.get("size")
            if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
                continue
            modified = child.get("modified") if isinstance(child.get("modified"), str) else ""
            out.append((pid, iid, size, modified))
    # Newest first by the agent-server's mtime stamp; the id is the tie-break
    # (the state schema asks for time-sortable instance ids).
    out.sort(key=lambda t: (t[3], t[1]), reverse=True)
    return out[:MAX_INSTANCES]


def _ts(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = parse_iso_timestamp(value.strip())
    except Exception:  # noqa: BLE001 — agent-written, any spelling
        return None
    try:
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def fold(definition: Any, state: Any, *, executing_agent: str,
         roster: Optional[set] = None) -> WorkSteps:
    """Definition + instance state → the card's stages. Pure.

    Stage order comes from the definition; `current_stage` from the state.
    A state naming a stage the definition lacks is still `reported` (the agent
    IS publishing) with that id shown as current and no ordering claim beyond
    it. Holders: a stage's `agent` field in the definition, else the state's
    per-stage `agent`, else the executing agent — masked to None when not on
    the caller's roster.
    """
    if not isinstance(state, dict):
        return _UNKNOWN
    current = state.get("current_stage")
    current = current if isinstance(current, str) and current.strip() else None
    stages_def = definition.get("stages") if isinstance(definition, dict) else None
    per_stage = state.get("stages") if isinstance(state.get("stages"), dict) else {}

    def holder_of(stage_id: str, stage_def: Optional[dict]) -> Optional[str]:
        who = None
        if isinstance(stage_def, dict) and isinstance(stage_def.get("agent"), str):
            who = stage_def["agent"].strip() or None
        if who is None:
            entry = per_stage.get(stage_id)
            if isinstance(entry, dict) and isinstance(entry.get("agent"), str):
                who = entry["agent"].strip() or None
        if who is None:
            who = executing_agent
        if roster is not None and who not in roster:
            return None
        return who

    stages: List[WorkStage] = []
    seen_current = False
    if isinstance(stages_def, list):
        ids = [s.get("id") for s in stages_def if isinstance(s, dict) and isinstance(s.get("id"), str)]
        cur_idx = ids.index(current) if current in ids else None
        for i, s in enumerate(stages_def):
            if not isinstance(s, dict) or not isinstance(s.get("id"), str):
                continue
            sid = s["id"]
            if cur_idx is None:
                st = "pending"
            elif i < cur_idx:
                st = "done"
            elif i == cur_idx:
                st = "current"
                seen_current = True
            else:
                st = "pending"
            name = s.get("name") if isinstance(s.get("name"), str) and s.get("name").strip() else sid
            stages.append(WorkStage(id=sid, name=name[:80], state=st, holder=holder_of(sid, s)))
    if current and not seen_current:
        stages.append(WorkStage(id=current, name=current[:80], state="current",
                                holder=holder_of(current, None)))
    holder = next((s.holder for s in stages if s.state == "current"), None)
    pipeline = None
    if isinstance(definition, dict):
        for key in ("name", "id"):
            v = definition.get(key)
            if isinstance(v, str) and v.strip():
                pipeline = v.strip()[:80]
                break
    if pipeline is None and isinstance(state.get("pipeline_id"), str):
        pipeline = state["pipeline_id"][:80]
    health = state.get("health") if isinstance(state.get("health"), str) else None
    updated = state.get("updated_at") if isinstance(state.get("updated_at"), str) else None
    return WorkSteps(state="reported", pipeline=pipeline, current=current, holder=holder,
                     health=health[:32] if health else None, updated_at=updated, stages=stages)


async def _download_capped(client: httpx.AsyncClient, base: str, path: str) -> Optional[bytes]:
    """Stream one agent file under the byte budget; None past it or on any non-200."""
    buf = bytearray()
    async with client.stream("GET", f"{base}/api/files/download", params={"path": path}) as r:
        if r.status_code != 200:
            return None
        async for chunk in r.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_FILE_BYTES:
                return None
    return bytes(buf)


async def _read(agent_name: str, started_at: Optional[str], roster: Optional[set]) -> WorkSteps:
    from services.agent_auth import agent_httpx_client

    base = f"http://agent-{agent_name}:8000"
    started_ts = _ts(started_at)
    async with agent_httpx_client(agent_name, timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(f"{base}/api/files",
                             params={"path": STATE_DIR, "show_hidden": "true"})
        if r.status_code == 404:
            # Reachable, and publishing nothing: the honest "doesn't report steps".
            return _NONE
        if r.status_code != 200:
            return _UNKNOWN
        try:
            tree = (r.json() or {}).get("tree")
        except ValueError:
            return _UNKNOWN
        candidates = instance_candidates(tree)
        if not candidates:
            return _NONE

        # Newest instance whose own `updated_at` is not older than the
        # execution: an instance last touched before this run started belongs
        # to a previous run, and claiming it would attribute stale stages to
        # live work.
        chosen: Optional[Tuple[str, dict]] = None
        for pid, iid, _size, _mod in candidates:
            raw = await _download_capped(client, base, f"{STATE_DIR}/{pid}/{iid}.json")
            if raw is None:
                continue
            try:
                state = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            upd = _ts(state.get("updated_at"))
            if started_ts is not None and upd is not None and upd < started_ts:
                continue
            chosen = (pid, state)
            break
        if chosen is None:
            return _NONE

        pid, state = chosen
        definition: Any = None
        raw_def = await _download_capped(client, base, f"{PIPELINES_DIR}/{pid}.yaml")
        if raw_def:
            try:
                definition = load_hardened_yaml(
                    raw_def.decode("utf-8", errors="replace"),
                    kind="pipeline", alias_policy=AliasPolicy.REJECT,
                    max_bytes=MAX_FILE_BYTES,
                )
            except HardenedYamlError as e:
                logger.info("[ent#525] pipeline definition for %s/%s rejected: %s", agent_name, pid, e)
                definition = None
        return fold(definition, state, executing_agent=agent_name, roster=roster)


async def read_pipeline_steps(agent_name: str, started_at: Optional[str] = None,
                              roster: Optional[set] = None) -> WorkSteps:
    """The steps for the ONE execution running on ``agent_name`` — never raises."""
    now = time.monotonic()
    hit = _cache.get(agent_name)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    try:
        result = await asyncio.wait_for(_read(agent_name, started_at, roster), WALL_BUDGET_SECONDS)
    except (asyncio.TimeoutError, httpx.HTTPError) as e:
        logger.info("[ent#525] pipeline read for %s did not complete: %s", agent_name, type(e).__name__)
        result = _UNKNOWN
    except Exception:  # noqa: BLE001 — a card must never 500 the Work read
        logger.warning("[ent#525] pipeline read for %s failed", agent_name, exc_info=True)
        result = _UNKNOWN
    _cache[agent_name] = (now, result)
    return result


def clear_cache() -> None:
    """Tests only."""
    _cache.clear()
