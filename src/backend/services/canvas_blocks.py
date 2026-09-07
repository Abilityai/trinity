"""Canvas block rules — pure, and deliberately importing nothing that touches the DB (ent#536).

The decidable half of the block vocabulary: which image sources a block may
carry, how block ids are assigned and checked, how a partial write merges into
a stored block list, and how the voice panel's "replace / append / clear"
verbs become block edits. Every rule an agent-facing refusal depends on lives
here so it can be tested without a database — and so `gemini_voice`, whose
unit suite stubs `config` down to a handful of names, can import it at module
level without dragging `database` in behind it.

Two writers share these rules — the REST/MCP write path and the Gemini voice
panel tools — which is the whole point: one confinement gate for image paths,
one id scheme, one merge.
"""
from __future__ import annotations

import copy
import posixpath
import re
from typing import Dict, List, Optional, Tuple

from models import (
    CANVAS_BLOCK_ID_RE,
    CANVAS_DIAGRAM_MAX_CHARS,
    CANVAS_IMAGE_INLINE_MAX_BYTES,
    CANVAS_IMAGE_SRC_MAX_CHARS,
    CANVAS_MAX_BLOCKS,
)


class CanvasError(Exception):
    """A refusal the router turns into an HTTP status, 1:1 (Invariant #1)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Image sources
# ---------------------------------------------------------------------------

# Agent workspace root inside the container; a workspace image path must
# resolve under it. The frontend fetches such a path through the authenticated
# `/files/preview` route (a bare `<img src>` would 401), so this is the ONLY
# confinement gate — stricter than the agent server's prefix check, rejecting
# the sibling escape `/home/developer-evil` and any `..` traversal (#979).
WORKSPACE_ROOT = "/home/developer"

IMAGE_SRC_KIND_URL = "url"
IMAGE_SRC_KIND_DATA = "data"
IMAGE_SRC_KIND_PATH = "path"

# Exact: a raster mime the browser decodes without a script context, then
# base64 and nothing else. `svg+xml` is deliberately absent — an SVG is a
# document, and the diagram kind is the sanitised way to show one.
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$"
)
# https ONLY — the CSP admits no http image either, and a plaintext image URL
# is a second way to leak a viewer's network position.
_WEB_URL_RE = re.compile(r"^https://[^\s\x00-\x1f\x7f]+$", re.IGNORECASE)
_CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")


def classify_image_src(src) -> Optional[Tuple[str, str]]:
    """Classify an image `src` as a web URL, an inline data URI, or a
    workspace-confined path.

    Returns ``(value, kind)`` with kind in ``url`` | ``data`` | ``path``, or
    ``None`` when the source is none of those — every other scheme
    (``file:``, ``ftp:``, ``javascript:``, protocol-relative ``//host``), a
    control character, an oversized value, or a path that escapes the
    workspace. The value is normalised (a relative path is resolved under the
    workspace root) so the renderer never has to reason about the raw input.
    """
    if not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    low = src.lower()
    if low.startswith("data:"):
        if len(src) > CANVAS_IMAGE_INLINE_MAX_BYTES:
            return None
        return (src, IMAGE_SRC_KIND_DATA) if _DATA_IMAGE_RE.match(src) else None
    if len(src) > CANVAS_IMAGE_SRC_MAX_CHARS:
        return None
    if low.startswith("https://"):
        return (src, IMAGE_SRC_KIND_URL) if _WEB_URL_RE.match(src) else None
    if "://" in low or src.startswith("//"):
        return None
    if _CONTROL_OR_SPACE_RE.search(src):
        return None

    # A workspace file path. Absolute (/home/developer/...) or relative
    # (resolved against the root); '~/' is workspace-relative.
    path = src[2:] if src.startswith("~/") else src
    # A ':' in the first segment is a URI scheme (javascript:, mailto:, …), not
    # a path segment — reject before resolving.
    if ":" in path.split("/", 1)[0]:
        return None
    if path.startswith("/"):
        candidate = posixpath.normpath(path)
    else:
        candidate = posixpath.normpath(posixpath.join(WORKSPACE_ROOT, path))
    if candidate != WORKSPACE_ROOT and not candidate.startswith(WORKSPACE_ROOT + "/"):
        return None
    return (candidate, IMAGE_SRC_KIND_PATH)


def image_refusal(src) -> str:
    """The named reason a source was refused — one sentence the agent can act on."""
    if isinstance(src, str) and src.strip().lower().startswith("data:"):
        if len(src.strip()) > CANVAS_IMAGE_INLINE_MAX_BYTES:
            return (
                f"inline image exceeds {CANVAS_IMAGE_INLINE_MAX_BYTES} bytes — write it to "
                "your workspace and reference the path instead"
            )
        return "inline image must be data:image/(png|jpeg|gif|webp);base64,…"
    return (
        "image src must be an https:// URL, a data:image/…;base64 URI under "
        f"{CANVAS_IMAGE_INLINE_MAX_BYTES} bytes, or a path inside your workspace "
        f"({WORKSPACE_ROOT}); other schemes and path traversal are refused"
    )


# ---------------------------------------------------------------------------
# Block validation
# ---------------------------------------------------------------------------

def _as_dict_payload(block: Dict, kind: str) -> Dict:
    payload = block.get("payload")
    if not isinstance(payload, dict):
        raise CanvasError(400, f"a {kind} block's payload must be an object")
    return payload


def validate_blocks(blocks: List[Dict]) -> List[Dict]:
    """Return a validated COPY of ``blocks`` with every block carrying an id.

    - Declared ids are charset-checked and must be unique within the write
      (named 400 either way).
    - Id-less blocks are assigned ``b1..bN`` by position, skipping any id the
      agent already used, so a later `patch_canvas` can address them and
      `get_canvas` shows what to address.
    - `image` payloads are normalised to ``{src, src_kind, alt?, caption?}``
      through the one confinement gate; `diagram` sources are capped.

    The count cap is re-checked here because the voice path builds its block
    list without a Pydantic model in front of it.
    """
    if len(blocks) > CANVAS_MAX_BLOCKS:
        raise CanvasError(413, f"a canvas holds at most {CANVAS_MAX_BLOCKS} blocks")

    out: List[Dict] = []
    seen: set = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise CanvasError(400, "each block must be an object")
        b = copy.deepcopy(block)
        bid = b.get("id")
        if bid is not None:
            if not isinstance(bid, str) or not CANVAS_BLOCK_ID_RE.match(bid):
                raise CanvasError(
                    400,
                    "block id must be 1-64 characters of letters, digits, dot, dash or underscore",
                )
            if bid in seen:
                raise CanvasError(400, f"duplicate block id '{bid}'")
            seen.add(bid)
        out.append(b)

    # Auto-ids second, so a declared `b3` further down the list is never
    # shadowed by a positional assignment earlier in it.
    counter = 0
    for b in out:
        if b.get("id") is not None:
            continue
        counter += 1
        while f"b{counter}" in seen:
            counter += 1
        b["id"] = f"b{counter}"
        seen.add(b["id"])

    for b in out:
        kind = b.get("kind")
        if kind == "image":
            payload = _as_dict_payload(b, "image")
            classified = classify_image_src(payload.get("src"))
            if classified is None:
                raise CanvasError(400, image_refusal(payload.get("src")))
            value, src_kind = classified
            normalised = {"src": value, "src_kind": src_kind}
            for key in ("alt", "caption"):
                if isinstance(payload.get(key), str) and payload[key]:
                    normalised[key] = payload[key][:300]
            b["payload"] = normalised
        elif kind == "diagram":
            payload = _as_dict_payload(b, "diagram")
            source = payload.get("mermaid")
            if not isinstance(source, str) or not source.strip():
                raise CanvasError(400, "a diagram block needs {\"mermaid\": \"<Mermaid source>\"}")
            if len(source) > CANVAS_DIAGRAM_MAX_CHARS:
                raise CanvasError(
                    400, f"diagram source exceeds {CANVAS_DIAGRAM_MAX_CHARS} characters"
                )
    return out


# ---------------------------------------------------------------------------
# Merges
# ---------------------------------------------------------------------------

def patch_blocks(existing: List[Dict], patch: List[Dict]) -> List[Dict]:
    """Replace, in place, the stored blocks whose ids the patch names.

    Order is the stored order. Every patch block must name a stored id —
    unknown ids are refused by name, and a canvas written before ids existed
    is refused with the fix spelled out — because a partial write that could
    append would be the append tool #438 deliberately does not have.
    A stored id that occurs more than once (rows written before validation)
    is replaced wherever it occurs.
    """
    stored_ids = [b.get("id") for b in existing if isinstance(b, dict) and b.get("id")]
    if not stored_ids:
        raise CanvasError(
            400,
            "this canvas has no block ids to patch — write it with set_canvas, "
            "which assigns them",
        )
    patch_ids = []
    for b in patch:
        bid = b.get("id")
        if bid in patch_ids:
            raise CanvasError(400, f"duplicate block id '{bid}' in patch")
        patch_ids.append(bid)
    unknown = [bid for bid in patch_ids if bid not in stored_ids]
    if unknown:
        raise CanvasError(
            400,
            f"unknown block id(s): {', '.join(unknown)} — set_canvas writes the full state",
        )
    by_id = {b["id"]: b for b in patch}
    return [
        copy.deepcopy(by_id[b["id"]]) if isinstance(b, dict) and b.get("id") in by_id else b
        for b in existing
    ]


def upsert_block(existing: List[Dict], block: Dict) -> List[Dict]:
    """Replace every stored block with this block's id, else append it."""
    bid = block.get("id")
    if any(isinstance(b, dict) and b.get("id") == bid for b in existing):
        return [copy.deepcopy(block) if isinstance(b, dict) and b.get("id") == bid else b
                for b in existing]
    return [*existing, copy.deepcopy(block)]


# ---------------------------------------------------------------------------
# Voice panel verbs → block edits (ent#536)
# ---------------------------------------------------------------------------

# The voice panel owns the `voice*` ids on the default canvas and nothing else.
# `show_*` and `update_panel` replace the panel — every `voice*` block goes,
# one `voice` block lands where the first one stood; `append_to_panel` grows
# the trailing voice html block (or starts `voice-N`); `clear_panel` removes
# the voice blocks only. Blocks the agent wrote through `set_canvas` survive a
# call untouched, which is what makes one shared default canvas safe.
VOICE_BLOCK_ID = "voice"
VOICE_BLOCK_PREFIX = "voice-"


def is_voice_block(block: Dict) -> bool:
    bid = block.get("id") if isinstance(block, dict) else None
    return bid == VOICE_BLOCK_ID or (isinstance(bid, str) and bid.startswith(VOICE_BLOCK_PREFIX))


def _replace_voice_blocks(existing: List[Dict], block: Dict) -> List[Dict]:
    out: List[Dict] = []
    placed = False
    for b in existing:
        if is_voice_block(b):
            if not placed:
                out.append(block)
                placed = True
            continue
        out.append(b)
    if not placed:
        out.append(block)
    return out


def map_panel_tool(tool_name: str, args: Dict, existing: List[Dict]) -> Tuple[Optional[List[Dict]], str]:
    """Turn one voice panel verb into the new block list for the canvas.

    Returns ``(blocks, message)``; ``blocks`` is ``None`` when the verb was
    refused, in which case ``message`` names why (the model speaks it).
    """
    existing = [b for b in (existing or []) if isinstance(b, dict)]
    args = args or {}
    title = args.get("title") or None

    if tool_name == "show_markdown":
        block = {"id": VOICE_BLOCK_ID, "kind": "markdown", "title": title,
                 "payload": {"markdown": str(args.get("content") or "")}}
        return _replace_voice_blocks(existing, block), "Panel updated."
    if tool_name == "show_diagram":
        source = str(args.get("diagram") or "").strip()
        if not source:
            return None, "No diagram source provided."
        if len(source) > CANVAS_DIAGRAM_MAX_CHARS:
            return None, f"Diagram rejected: source exceeds {CANVAS_DIAGRAM_MAX_CHARS} characters."
        block = {"id": VOICE_BLOCK_ID, "kind": "diagram", "title": title,
                 "payload": {"mermaid": source}}
        return _replace_voice_blocks(existing, block), "Panel updated."
    if tool_name == "show_image":
        src = str(args.get("src") or "").strip()
        if not src:
            return None, "No image source provided."
        classified = classify_image_src(src)
        if classified is None:
            return None, f"Image rejected: {image_refusal(src)}."
        value, src_kind = classified
        payload = {"src": value, "src_kind": src_kind}
        caption = args.get("caption")
        if isinstance(caption, str) and caption:
            payload["caption"] = caption[:300]
        block = {"id": VOICE_BLOCK_ID, "kind": "image", "title": title, "payload": payload}
        return _replace_voice_blocks(existing, block), "Panel updated."
    if tool_name == "update_panel":
        block = {"id": VOICE_BLOCK_ID, "kind": "html", "title": title,
                 "payload": {"html": str(args.get("html") or "")}}
        return _replace_voice_blocks(existing, block), "Panel updated."
    if tool_name == "append_to_panel":
        html = str(args.get("html") or "")
        voice_blocks = [b for b in existing if is_voice_block(b)]
        tail = voice_blocks[-1] if voice_blocks else None
        if tail and tail.get("kind") == "html" and isinstance(tail.get("payload"), dict):
            grown = copy.deepcopy(tail)
            grown["payload"]["html"] = str(grown["payload"].get("html") or "") + html
            return upsert_block(existing, grown), "Panel updated."
        n = 2
        used = {b.get("id") for b in existing}
        while f"{VOICE_BLOCK_PREFIX}{n}" in used:
            n += 1
        block = {"id": f"{VOICE_BLOCK_PREFIX}{n}" if voice_blocks else VOICE_BLOCK_ID,
                 "kind": "html", "title": None, "payload": {"html": html}}
        return [*existing, block], "Panel updated."
    if tool_name == "clear_panel":
        return [b for b in existing if not is_voice_block(b)], "Panel cleared."
    return None, f"Unknown panel tool: {tool_name}"
