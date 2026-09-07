"""Agent canvas service (ent#438, widened by ent#536).

The decidable half of the canvas: what a canvas id may look like, how big a
block list may be, how a write and a partial write are validated and stored,
and — the part worth reading — how "this may be out of date" is derived rather
than guessed.

HTTP-free by design (Invariant #1): every failure is a ``CanvasError`` the thin
router maps 1:1, the shape ``chat_execution_service`` established in #1483.
The pure block rules (image confinement, ids, merges, the voice verbs) live in
``services/canvas_blocks.py`` and are re-exported here; this module is the
half that touches the database.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from database import db
from db.canvas import AUDIENCE_OPERATOR, AUDIENCE_ROSTER, normalize_audience
from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_ID_RE,
    CANVAS_MAX_BLOCKS,
    DEFAULT_CANVAS_ID,
)
from services.canvas_blocks import (  # noqa: F401 — re-exported for callers
    CanvasError,
    classify_image_src,
    map_panel_tool,
    patch_blocks,
    validate_blocks,
)
from services.idempotency_service import resolve_and_validate_execution

logger = logging.getLogger(__name__)


def validate_canvas_id(canvas_id: str) -> str:
    """Charset-validate an agent-chosen canvas id.

    A canvas id reaches a URL path and is half a primary key, so it carries the
    #919 pipeline-id guard rather than a bare length cap. A **named** 400 —
    over-long or punctuation-bearing ids are the routine agent mistake, and the
    generic 422 the framework would raise says nothing about how to fix it.
    """
    if not isinstance(canvas_id, str) or not CANVAS_ID_RE.match(canvas_id):
        raise CanvasError(
            400,
            "canvas_id must be 1-64 characters of letters, digits, dot, dash or "
            "underscore",
        )
    return canvas_id


def serialize_blocks(blocks: List[Dict]) -> str:
    """JSON for storage, refusing an oversized block list with a named 413.

    The count cap is a Pydantic constraint on the model; this is the BYTE cap,
    which the count cannot express — fifty blocks of one row each and fifty
    blocks of ten thousand rows each are the same to `max_length`.
    """
    if len(blocks) > CANVAS_MAX_BLOCKS:
        raise CanvasError(413, f"a canvas holds at most {CANVAS_MAX_BLOCKS} blocks")
    encoded = json.dumps(blocks)
    if len(encoded.encode("utf-8")) > CANVAS_BLOCKS_MAX_BYTES:
        raise CanvasError(
            413, f"canvas blocks exceed {CANVAS_BLOCKS_MAX_BYTES} bytes"
        )
    return encoded


def resolve_execution_id(execution_id: Optional[str], agent_name: str) -> Optional[str]:
    """The writing turn, confirmed to belong to this agent — or None.

    The agent supplies an id and never its own identity (the MEM-001 rule). A
    foreign or unknown id degrades to None rather than refusing the write: the
    id is provenance, not authorization, and losing a stamp is a smaller harm
    than losing the canvas the agent just rendered.
    """
    if not execution_id:
        return None
    execution = resolve_and_validate_execution(execution_id, agent_name)
    return execution_id if execution else None


# ---------------------------------------------------------------------------
# Audience width (ent#536)
# ---------------------------------------------------------------------------

# `operator` ⊂ `roster`. A writer may land on a canvas that is at most as wide
# as the writer's own audience — never wider, because that would publish the
# writer's content to readers it never chose.
_AUDIENCE_WIDTH = {AUDIENCE_OPERATOR: 0, AUDIENCE_ROSTER: 1}


def audience_within(stored: Optional[str], writer: Optional[str]) -> bool:
    """May a writer with audience ``writer`` write onto a canvas stored at ``stored``?"""
    return _AUDIENCE_WIDTH[normalize_audience(stored)] <= _AUDIENCE_WIDTH[normalize_audience(writer)]


# ---------------------------------------------------------------------------
# The one write path (ent#536)
# ---------------------------------------------------------------------------

def write_canvas(
    agent_name: str,
    canvas_id: str,
    blocks: List[Dict],
    *,
    title: Optional[str],
    audience: str,
    execution_id: Optional[str],
) -> Dict:
    """Validate and store a canvas — the ONLY path that writes ``agent_canvases``.

    Both writers come through here: the REST/MCP `set_canvas` and the Gemini
    voice panel tools. One validation (ids, per-kind rules, the byte cap), one
    execution resolution, one upsert — so a block renders identically whoever
    wrote it, and a cap the router enforces cannot be bypassed by the voice
    path. ``audience`` is REQUIRED: the caller decides who may read, never a
    default hidden in here.
    """
    validate_canvas_id(canvas_id)
    validated = validate_blocks(blocks)
    # Serialize once HERE purely to enforce the byte cap before anything
    # touches the DB; db/canvas.py serializes again for storage. The double
    # encode is deliberate — the alternative is a service that returns a
    # string and a db layer that trusts it, and the db layer is the one every
    # future caller reaches through.
    serialize_blocks(validated)
    resolved = resolve_execution_id(execution_id, agent_name)
    return db.upsert_agent_canvas(
        agent_name,
        canvas_id,
        blocks=validated,
        title=title,
        audience=normalize_audience(audience),
        execution_id=resolved,
    )


def patch_canvas(
    agent_name: str,
    canvas_id: str,
    patch: List[Dict],
    *,
    execution_id: Optional[str],
) -> Optional[Dict]:
    """Replace only the named blocks of an existing canvas, or None if absent.

    Read-modify-write under the same last-writer-wins contract the db layer
    documents for the full write (one writer per canvas — the agent itself).
    Title and audience are kept; the provenance stamp becomes this write's.
    The merged list goes back through `write_canvas`, so a patch can no more
    exceed a cap or smuggle a bad image source than a full write can.
    """
    validate_canvas_id(canvas_id)
    current = db.get_agent_canvas(agent_name, canvas_id)
    if not current:
        return None
    merged = patch_blocks(current.get("blocks") or [], patch)
    return write_canvas(
        agent_name,
        canvas_id,
        merged,
        title=current.get("title"),
        audience=current.get("audience") or AUDIENCE_OPERATOR,
        execution_id=execution_id,
    )


def empty_canvas(agent_name: str, canvas_id: str = DEFAULT_CANVAS_ID) -> Dict:
    """The shape a reader sees for a canvas that does not exist yet.

    Used by the voice panel poll, which must answer 200 during the teardown
    window and before the first tool call rather than 404 into the client's
    poll loop.
    """
    return {
        "agent_name": agent_name,
        "canvas_id": canvas_id,
        "title": None,
        "audience": AUDIENCE_OPERATOR,
        "schema_version": 1,
        "created_at": None,
        "updated_at": None,
        "updated_by_execution_id": None,
        "stale": False,
        "blocks": [],
    }


# ---------------------------------------------------------------------------
# Staleness (ent#438)
# ---------------------------------------------------------------------------

def is_stale(canvas: Dict, last_completed_at: Optional[str]) -> bool:
    """Has the agent finished a run since this canvas was last written?

    This is the whole of AC 7, and it is deliberately NOT a clock. An age
    threshold has to be picked without knowing what the canvas is for, so it
    either cries wolf on a monthly summary or stays silent on a minute-by-minute
    one. "The agent did work and did not refresh this surface" is a fact about
    *this* canvas, needs no configuration, and is checkable against
    `updated_by_execution_id`.

    Both sides are ISO-Z strings written by `utc_now_iso`, so the comparison is
    lexicographic over one fixed format (Invariant #16 — the trap is comparing
    such a column to `datetime('now')`, which nothing here does).

    Fail-QUIET is not available: an unreadable timestamp cannot prove freshness,
    but claiming staleness on every read would train the reader to ignore the
    mark. Missing data therefore reads as "not stale" and the rendered
    `updated_at` carries the honesty on its own — the mark is an ADDITION to a
    visible timestamp, never a replacement for one.
    """
    updated_at = canvas.get("updated_at")
    if not last_completed_at or not updated_at:
        return False
    return str(last_completed_at) > str(updated_at)


def decorate(canvases: List[Dict], agent_name: str) -> List[Dict]:
    """Attach the derived `stale` flag to each canvas.

    One `last_completed_execution_at` read for the whole list, not one per
    canvas — the input is a property of the AGENT, and an agent with eight
    canvases should not pay eight identical queries to render its page.
    """
    if not canvases:
        return canvases
    try:
        last_completed = db.last_completed_execution_at(agent_name)
    except Exception as e:  # noqa: BLE001 — a staleness read never fails a render
        logger.warning("canvas: staleness read failed for %s: %s", agent_name, e)
        last_completed = None
    for canvas in canvases:
        canvas["stale"] = is_stale(canvas, last_completed)
    return canvases
