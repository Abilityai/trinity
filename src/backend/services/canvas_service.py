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
from db.canvas import (
    AUDIENCE_OPERATOR,
    AUDIENCE_ROSTER,
    CanvasLimitExceeded,
    normalize_audience,
)
from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_ID_RE,
    CANVAS_MAX_BLOCKS,
    CANVAS_TEMPLATES,
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


def validate_template(template: Optional[str]) -> Optional[str]:
    """Normalise a starter-layout name, refusing an unknown one BY NAME (ent#537).

    ``None`` and ``""`` both mean "stacked blocks", the default. An unknown
    name is refused rather than silently stacked because silently stacking
    would teach the agent that the wrong name works; the refusal lists the
    four so the fix is in the message.
    """
    if template is None or template == "":
        return None
    if not isinstance(template, str) or template not in CANVAS_TEMPLATES:
        raise CanvasError(
            400,
            "unknown canvas template — use one of: " + ", ".join(CANVAS_TEMPLATES)
            + " (or omit it for stacked blocks)",
        )
    return template


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
# The open canvas as shared context (ent#555)
# ---------------------------------------------------------------------------


def open_canvas_for_execution(execution_id: Optional[str], agent_name: str) -> Optional[str]:
    """The canvas the user had open when they sent this turn, or None.

    CONTEXT, NEVER AUTHORITY (ent#555 AC #6). This answers "what is the user
    looking at", and it is deliberately incapable of answering "may the agent
    touch it": the id was validated against THIS agent's own canvases at the
    boundary that stamped it, and every read and write still goes through the
    same audience and ownership gates it always did. A canvas the caller could
    not otherwise reach does not become reachable by being named as open.

    Fail-open to None, matching `resolve_execution_id` directly above: an agent
    on an old image sends no execution_id, and a turn with no open canvas is
    the ordinary case rather than an error. None means "fall back to the
    default", never "refuse".
    """
    execution = resolve_and_validate_execution(execution_id, agent_name)
    if execution is None:
        return None
    canvas_id = getattr(execution, "open_canvas_id", None)
    if not canvas_id:
        return None
    # Re-checked at READ time, not trusted from the stamp: the canvas may have
    # been deleted since the turn started (ent#553 made that a one-click act),
    # and pointing the agent at a row that is gone would have it create a NEW
    # canvas under that id — silently resurrecting something a person deleted.
    return canvas_id if db.get_agent_canvas(agent_name, canvas_id) else None


def effective_canvas_id(canvas_id: Optional[str], execution_id: Optional[str],
                        agent_name: str) -> tuple[str, str]:
    """Which canvas a tool call acts on, and WHY — `(canvas_id, source)`.

    The precedence the issue asks for, in one place so all three tools agree:

        explicit id  >  the canvas the user has open  >  the default canvas

    `source` is returned because the agent has to be able to SAY which canvas
    it wrote to when nobody named one (AC #7). "I updated the canvas" is not
    good enough when there are eight of them and the user is looking at one.
    """
    if canvas_id:
        return canvas_id, "explicit"
    open_id = open_canvas_for_execution(execution_id, agent_name)
    if open_id:
        return open_id, "open"
    return DEFAULT_CANVAS_ID, "default"


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
    template: Optional[str] = None,
) -> Dict:
    """Validate and store a canvas — the ONLY path that writes ``agent_canvases``.

    Both writers come through here: the REST/MCP `set_canvas` and the Gemini
    voice panel tools. One validation (ids, per-kind rules, the byte cap), one
    execution resolution, one upsert — so a block renders identically whoever
    wrote it, and a cap the router enforces cannot be bypassed by the voice
    path. ``audience`` is REQUIRED: the caller decides who may read, never a
    default hidden in here. ``template`` (ent#537) is the starter layout, or
    None for stacked blocks; a writer that is EDITING an existing canvas
    (`patch_canvas`, the voice verbs) passes the stored value through, so an
    edit never silently un-layouts a board.
    """
    validate_canvas_id(canvas_id)
    template = validate_template(template)
    validated = validate_blocks(blocks)
    # Serialize once HERE purely to enforce the byte cap before anything
    # touches the DB; db/canvas.py serializes again for storage. The double
    # encode is deliberate — the alternative is a service that returns a
    # string and a db layer that trusts it, and the db layer is the one every
    # future caller reaches through.
    serialize_blocks(validated)
    resolved = resolve_execution_id(execution_id, agent_name)
    try:
        return db.upsert_agent_canvas(
            agent_name,
            canvas_id,
            blocks=validated,
            title=title,
            audience=normalize_audience(audience),
            execution_id=resolved,
            template=template,
        )
    except CanvasLimitExceeded as e:
        # ent#553 — the cap is enforced in the db layer (it needs the count and
        # the insert in one transaction), and translated here so the router
        # keeps one error vocabulary. 409, not 413: nothing about this payload
        # is too large, the agent is out of room and must retire a canvas.
        raise CanvasError(409, str(e))


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
    Title, audience and template are kept; the provenance stamp becomes this
    write's.
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
        template=current.get("template"),
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
        "template": None,
        "stale": False,
        # ent#553 — a canvas that does not exist yet is not pinned. Present
        # rather than omitted because the shape is contractually the Canvas
        # model's, and the voice poll deserializes it.
        "pinned": False,
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
