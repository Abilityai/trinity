"""ent#536 — one rich block vocabulary for the agent and its voice mode.

What is pinned here:

  * the block vocabulary is ONE list across the three surfaces (backend enum,
    MCP tool, frontend renderer) and the default canvas id is one string;
  * every block stored carries an id (`b1..bN` assigned to id-less blocks), so
    `patch_canvas` can address anything `set_canvas` wrote;
  * `patch_canvas` replaces in place, keeps order, and refuses unknown ids and
    id-less canvases BY NAME — never appends;
  * the image confinement gate is one function for both writers, admits an
    inline raster only under a stated cap, and refuses every other scheme;
  * the voice verbs are block edits on the `voice*` ids and leave the agent's
    own blocks alone;
  * a writer may not land on a canvas wider than its own audience;
  * the one write path validates, caps and resolves provenance identically
    whoever calls it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from models import (
    CANVAS_DIAGRAM_MAX_CHARS,
    CANVAS_IMAGE_INLINE_MAX_BYTES,
    CANVAS_MAX_BLOCKS,
    DEFAULT_CANVAS_ID,
    CanvasBlock,
    CanvasPatch,
    CanvasWrite,
)
from services import canvas_service
from services.canvas_blocks import (
    VOICE_BLOCK_ID,
    CanvasError,
    classify_image_src,
    map_panel_tool,
    patch_blocks,
    validate_blocks,
)

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "src" / "frontend" / "src"
_BACKEND = _REPO / "src" / "backend"
_MCP = _REPO / "src" / "mcp-server" / "src"

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# One vocabulary, three surfaces
# ---------------------------------------------------------------------------

def _mcp_kinds() -> set:
    tool = (_MCP / "tools" / "canvas.ts").read_text()
    body = tool[tool.index("const BLOCK_KINDS"):tool.index("] as const")]
    return set(re.findall(r'"(\w+)"', body))


def _frontend_kinds() -> set:
    utils = (_FRONTEND / "components" / "canvas" / "canvasUtils.js").read_text()
    body = utils[utils.index("REPORT_DELEGATED_KINDS = ["):utils.index("export const CANVAS_BLOCK_KINDS")]
    return set(re.findall(r"'(\w+)'", body))


def _backend_kinds() -> set:
    from typing import get_args
    from models import CanvasBlockKind
    return set(get_args(CanvasBlockKind))


def test_the_three_surfaces_agree_on_the_kinds():
    assert _backend_kinds() == _mcp_kinds() == _frontend_kinds()
    assert {"image", "diagram", "chart", "html"} <= _backend_kinds()


def test_the_default_canvas_id_is_one_string_everywhere():
    assert DEFAULT_CANVAS_ID == "main"
    assert 'export const DEFAULT_CANVAS_ID = "main"' in (_MCP / "tools" / "canvas.ts").read_text()
    assert "export const DEFAULT_CANVAS_ID = 'main'" in (
        _FRONTEND / "components" / "canvas" / "canvasUtils.js").read_text()
    voice = (_BACKEND / "services" / "gemini_voice.py").read_text()
    assert "_CANVAS_ID = DEFAULT_CANVAS_ID" in voice
    assert '"voice"' not in voice.split("WORKSPACE_PANEL_INSTRUCTIONS")[0].replace('"voice-', ''), (
        "the voice panel must not silo into a canvas named 'voice'"
    )


def test_the_report_display_hint_enum_is_still_not_widened():
    models = (_BACKEND / "models.py").read_text()
    hint = models[models.index("ReportDisplayHint = Literal["):]
    hint = hint[:hint.index("]") + 1]
    for kind in ("chart", "html", "image", "diagram"):
        assert kind not in hint


def test_the_panel_endpoint_serves_the_canvas_row():
    router = (_BACKEND / "routers" / "voice.py").read_text()
    body = router[router.index("async def get_voice_panel"):]
    body = body[:body.index("\n@router")]
    assert "get_agent_canvas(name, DEFAULT_CANVAS_ID)" in body
    assert "panel_state" not in body
    assert "empty_canvas" in body


def test_the_router_declares_its_mcp_surface_and_a_gated_patch_route():
    router = (_BACKEND / "routers" / "canvas.py").read_text()
    assert router.splitlines()[0].startswith("# mcp: canvas.ts")
    assert '@router.patch("/{name}/canvas/{canvas_id}"' in router
    patch_fn = router[router.index("async def patch_canvas"):]
    patch_fn = patch_fn[:patch_fn.index("\n@router")]
    assert "_gate_write(current_user, name, request)" in patch_fn
    assert "canvas_service.patch_canvas" in patch_fn
    # The full write goes through the same service path — no router-side upsert.
    assert "db.upsert_agent_canvas" not in router


def test_the_leaves_are_shared_and_markdown_never_imports_the_dispatcher():
    md = (_FRONTEND / "components" / "canvas" / "CanvasMarkdown.vue").read_text()
    assert not re.search(r"import\s+CanvasBlock\b", md), "fences do not nest; a cycle here is the fork this file avoids"
    for leaf in ("CanvasChart", "CanvasDiagram", "ReportRenderer"):
        assert leaf in md
    block = (_FRONTEND / "components" / "canvas" / "CanvasBlock.vue").read_text()
    for leaf in ("CanvasChart", "CanvasDiagram", "CanvasImage", "CanvasMarkdown", "ReportRenderer"):
        assert leaf in block
    diagram = (_FRONTEND / "components" / "canvas" / "CanvasDiagram.vue").read_text()
    # ent#537 split the SVG path out by name: mermaid's own id-scoped <style>
    # must survive while every html/markdown path forbids the element.
    assert "import('mermaid')" in diagram and "sanitizeSvg(" in diagram


# ---------------------------------------------------------------------------
# Block ids
# ---------------------------------------------------------------------------

def test_id_less_blocks_are_assigned_positional_ids_without_shadowing_declared_ones():
    out = validate_blocks([
        {"kind": "kpi", "payload": {"tiles": []}},
        {"id": "b1", "kind": "markdown", "payload": {"markdown": "x"}},
        {"kind": "json", "payload": {}},
    ])
    assert [b["id"] for b in out] == ["b2", "b1", "b3"]


def test_duplicate_block_ids_are_refused_by_name():
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"id": "x", "kind": "json"}, {"id": "x", "kind": "json"}])
    assert e.value.status_code == 400 and "duplicate block id 'x'" in e.value.detail


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "ünïcode", "a/b"])
def test_bad_block_ids_are_refused(bad):
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"id": bad, "kind": "json"}])
    assert e.value.status_code == 400


def test_the_pydantic_model_carries_the_id_and_the_patch_model_requires_it():
    assert CanvasBlock(kind="kpi", payload={}).id is None
    assert CanvasBlock(kind="kpi", id="tile-1", payload={}).id == "tile-1"
    with pytest.raises(Exception):
        CanvasPatch(blocks=[{"kind": "kpi", "payload": {}}])
    with pytest.raises(Exception):
        CanvasPatch(blocks=[])
    patch = CanvasPatch(blocks=[{"id": "b1", "kind": "kpi", "payload": {}}])
    assert patch.blocks[0].id == "b1"
    assert CanvasWrite(blocks=[{"kind": "image", "payload": {"src": "https://x/y.png"}}]).blocks[0].kind == "image"


def test_the_count_cap_is_enforced_on_the_pure_path_too():
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "json"}] * (CANVAS_MAX_BLOCKS + 1))
    assert e.value.status_code == 413


# ---------------------------------------------------------------------------
# Image sources — one gate for both writers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,kind,value", [
    ("https://example.com/a.png", "url", "https://example.com/a.png"),
    ("HTTPS://EXAMPLE.COM/A.PNG", "url", "HTTPS://EXAMPLE.COM/A.PNG"),
    ("data:image/png;base64,AAAA", "data", "data:image/png;base64,AAAA"),
    ("data:image/webp;base64,AAAA==", "data", "data:image/webp;base64,AAAA=="),
    ("content/x.png", "path", "/home/developer/content/x.png"),
    ("~/content/x.png", "path", "/home/developer/content/x.png"),
    ("/home/developer/a/../b.png", "path", "/home/developer/b.png"),
    ("/home/developer", "path", "/home/developer"),
])
def test_accepted_image_sources(src, kind, value):
    assert classify_image_src(src) == (value, kind)


@pytest.mark.parametrize("bad", [
    "", "   ", None, 42,
    "../escape.png", "/home/developer/../../etc/passwd", "/etc/passwd", "/home/developer-evil/x.png",
    "data:image/svg+xml;base64,AAAA", "data:text/html,<script>", "data:image/png;base64,not base64!",
    "data:image/png;base64," + "A" * CANVAS_IMAGE_INLINE_MAX_BYTES,
    "file:///etc/passwd", "ftp://host/x.png", "javascript:alert(1)", "//evil.example/x.png",
    "http://example.com/a.png",  # plaintext image URLs are refused, not merely CSP-blocked
    "http://x y", "https://x/\x00", "content/a\nb.png",
    "https://example.com/" + "a" * 3000,
])
def test_refused_image_sources(bad):
    assert classify_image_src(bad) is None


def test_an_image_block_is_normalised_at_write_and_refused_by_name():
    out = validate_blocks([{"kind": "image", "payload": {"src": "content/a.png", "caption": "c", "alt": "a", "junk": 1}}])
    assert out[0]["payload"] == {"src": "/home/developer/content/a.png", "src_kind": "path", "caption": "c", "alt": "a"}
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "image", "payload": {"src": "javascript:alert(1)"}}])
    assert e.value.status_code == 400 and "image src must be" in e.value.detail
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "image", "payload": {"src": "data:image/png;base64," + "A" * CANVAS_IMAGE_INLINE_MAX_BYTES}}])
    assert str(CANVAS_IMAGE_INLINE_MAX_BYTES) in e.value.detail


def test_a_diagram_needs_source_and_is_capped():
    ok = validate_blocks([{"kind": "diagram", "payload": {"mermaid": "graph TD; A-->B"}}])
    assert ok[0]["payload"]["mermaid"] == "graph TD; A-->B"
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "diagram", "payload": {"mermaid": "   "}}])
    assert e.value.status_code == 400
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "diagram", "payload": {"mermaid": "x" * (CANVAS_DIAGRAM_MAX_CHARS + 1)}}])
    assert str(CANVAS_DIAGRAM_MAX_CHARS) in e.value.detail


# ---------------------------------------------------------------------------
# Patch — replace in place, never append
# ---------------------------------------------------------------------------

_STORED = [
    {"id": "b1", "kind": "kpi", "payload": {"tiles": [1]}},
    {"id": "b2", "kind": "markdown", "payload": {"markdown": "old"}},
    {"id": "b3", "kind": "json", "payload": {}},
]


def test_patch_replaces_in_place_and_keeps_order():
    out = patch_blocks(_STORED, [{"id": "b2", "kind": "chart", "payload": {"series": []}}])
    assert [b["id"] for b in out] == ["b1", "b2", "b3"]
    assert out[1]["kind"] == "chart"
    assert out[0] is _STORED[0]


def test_patch_refuses_unknown_ids_by_name_and_writes_nothing():
    with pytest.raises(CanvasError) as e:
        patch_blocks(_STORED, [{"id": "b2", "kind": "json"}, {"id": "b9", "kind": "json"}])
    assert e.value.status_code == 400
    assert "unknown block id(s): b9" in e.value.detail and "set_canvas" in e.value.detail


def test_patch_refuses_an_id_less_canvas_with_the_fix_spelled_out():
    with pytest.raises(CanvasError) as e:
        patch_blocks([{"kind": "kpi", "payload": {}}], [{"id": "b1", "kind": "kpi"}])
    assert "no block ids" in e.value.detail and "set_canvas" in e.value.detail


def test_patch_refuses_duplicate_ids_in_the_patch():
    with pytest.raises(CanvasError):
        patch_blocks(_STORED, [{"id": "b1", "kind": "json"}, {"id": "b1", "kind": "json"}])


def test_a_stored_duplicate_id_is_replaced_wherever_it_occurs():
    stored = [{"id": "d", "kind": "json", "payload": {"n": 1}}, {"id": "d", "kind": "json", "payload": {"n": 2}}]
    out = patch_blocks(stored, [{"id": "d", "kind": "kpi", "payload": {"tiles": []}}])
    assert [b["kind"] for b in out] == ["kpi", "kpi"]


# ---------------------------------------------------------------------------
# Voice verbs are block edits on the voice ids
# ---------------------------------------------------------------------------

_BOARD = {"id": "b1", "kind": "kpi", "payload": {"tiles": []}}


@pytest.mark.parametrize("tool,args,kind,payload", [
    ("show_markdown", {"content": "# hi"}, "markdown", {"markdown": "# hi"}),
    ("show_diagram", {"diagram": "graph TD; A-->B"}, "diagram", {"mermaid": "graph TD; A-->B"}),
    ("show_image", {"src": "https://x/y.png", "caption": "c"}, "image",
     {"src": "https://x/y.png", "src_kind": "url", "caption": "c"}),
    ("update_panel", {"html": "<b>x</b>"}, "html", {"html": "<b>x</b>"}),
])
def test_each_voice_verb_maps_onto_exactly_one_block_kind(tool, args, kind, payload):
    blocks, message = map_panel_tool(tool, args, [_BOARD])
    assert message == "Panel updated."
    assert blocks[0] == _BOARD
    assert blocks[1] == {"id": VOICE_BLOCK_ID, "kind": kind, "title": None, "payload": payload}


def test_show_replaces_every_voice_block_where_the_first_one_stood():
    existing = [
        {"id": "voice", "kind": "html", "payload": {"html": "a"}},
        _BOARD,
        {"id": "voice-2", "kind": "html", "payload": {"html": "b"}},
    ]
    blocks, _ = map_panel_tool("show_markdown", {"content": "m", "title": "T"}, existing)
    assert [b["id"] for b in blocks] == ["voice", "b1"]
    assert blocks[0]["title"] == "T" and blocks[0]["kind"] == "markdown"


def test_append_grows_the_trailing_voice_html_block_or_starts_a_new_one():
    grown, _ = map_panel_tool("append_to_panel", {"html": "B"},
                              [{"id": "voice", "kind": "html", "title": None, "payload": {"html": "A"}}])
    assert grown == [{"id": "voice", "kind": "html", "title": None, "payload": {"html": "AB"}}]
    fresh, _ = map_panel_tool("append_to_panel", {"html": "B"}, [_BOARD])
    assert [b["id"] for b in fresh] == ["b1", "voice"]
    after_md, _ = map_panel_tool("append_to_panel", {"html": "B"},
                                 [{"id": "voice", "kind": "markdown", "payload": {"markdown": "m"}}])
    assert [b["id"] for b in after_md] == ["voice", "voice-2"]


def test_clear_removes_only_the_voice_blocks():
    blocks, message = map_panel_tool("clear_panel", {}, [
        {"id": "voice", "kind": "html", "payload": {}}, _BOARD, {"id": "voice-2", "kind": "html", "payload": {}},
    ])
    assert blocks == [_BOARD] and message == "Panel cleared."


@pytest.mark.parametrize("tool,args", [
    ("show_image", {"src": "javascript:alert(1)"}),
    ("show_image", {"src": ""}),
    ("show_diagram", {"diagram": ""}),
    ("show_diagram", {"diagram": "x" * (CANVAS_DIAGRAM_MAX_CHARS + 1)}),
    ("teleport", {}),
])
def test_a_refused_verb_returns_no_blocks_and_a_reason(tool, args):
    blocks, message = map_panel_tool(tool, args, [_BOARD])
    assert blocks is None and message


# ---------------------------------------------------------------------------
# Audience width and the one write path
# ---------------------------------------------------------------------------

def test_audience_within_never_widens():
    assert canvas_service.audience_within("operator", "operator")
    assert canvas_service.audience_within("operator", "roster")
    assert canvas_service.audience_within("roster", "roster")
    assert not canvas_service.audience_within("roster", "operator")
    assert not canvas_service.audience_within("roster", None)
    assert canvas_service.audience_within(None, "operator")


def test_write_canvas_validates_caps_and_resolves_provenance_once(monkeypatch):
    captured = {}

    def _upsert(agent, canvas_id, **kw):
        captured.update(agent=agent, canvas_id=canvas_id, **kw)
        return {"agent_name": agent, "canvas_id": canvas_id, **kw}

    monkeypatch.setattr(canvas_service.db, "upsert_agent_canvas", _upsert)
    monkeypatch.setattr(canvas_service, "resolve_and_validate_execution",
                        lambda eid, agent: {"id": eid} if eid == "mine" else None)
    canvas_service.write_canvas(
        "a", "main", [{"kind": "kpi", "payload": {"tiles": []}}],
        title="T", audience="garbage", execution_id="mine",
    )
    assert captured["blocks"][0]["id"] == "b1"
    assert captured["audience"] == "operator", "an unknown audience normalises closed"
    assert captured["execution_id"] == "mine"

    with pytest.raises(CanvasError) as e:
        canvas_service.write_canvas("a", "bad id!", [], title=None, audience="operator", execution_id=None)
    assert e.value.status_code == 400
    with pytest.raises(CanvasError) as e:
        canvas_service.write_canvas("a", "main", [{"kind": "json", "payload": {"x": "y" * 600_000}}],
                                    title=None, audience="operator", execution_id=None)
    assert e.value.status_code == 413


def test_write_canvas_audience_is_required_not_defaulted():
    with pytest.raises(TypeError):
        canvas_service.write_canvas("a", "main", [], title=None, execution_id=None)  # type: ignore[call-arg]


def test_patch_canvas_keeps_title_and_audience_and_restamps_provenance(monkeypatch):
    stored = {"agent_name": "a", "canvas_id": "main", "title": "Board", "audience": "roster",
              "blocks": [{"id": "b1", "kind": "kpi", "payload": {"tiles": [1]}},
                         {"id": "b2", "kind": "json", "payload": {}}]}
    monkeypatch.setattr(canvas_service.db, "get_agent_canvas", lambda a, c, audience=None: stored)
    writes = []
    monkeypatch.setattr(canvas_service, "write_canvas",
                        lambda *a, **k: writes.append((a, k)) or {"ok": True})
    out = canvas_service.patch_canvas("a", "main", [{"id": "b2", "kind": "markdown", "payload": {"markdown": "m"}}],
                                      execution_id="e2")
    assert out == {"ok": True}
    (agent, canvas_id, merged), kw = writes[0]
    assert [b["id"] for b in merged] == ["b1", "b2"] and merged[1]["kind"] == "markdown"
    assert kw == {"title": "Board", "audience": "roster", "execution_id": "e2", "template": None}


def test_patch_canvas_on_a_missing_canvas_is_none(monkeypatch):
    monkeypatch.setattr(canvas_service.db, "get_agent_canvas", lambda a, c, audience=None: None)
    assert canvas_service.patch_canvas("a", "main", [{"id": "b1", "kind": "json"}], execution_id=None) is None


def test_empty_canvas_shape_matches_the_canvas_model_keys():
    from models import Canvas
    shape = canvas_service.empty_canvas("a")
    assert shape["canvas_id"] == DEFAULT_CANVAS_ID and shape["blocks"] == []
    assert set(Canvas.model_fields) <= set(shape)
    json.dumps(shape)  # serialisable as-is
