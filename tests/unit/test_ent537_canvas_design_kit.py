"""trinity-enterprise#537 — the canvas design kit, starter layouts and the
`canvas` skill's platform half.

An agent's canvas should look designed without the agent touching CSS. Three
things make that true, and each is pinned here against the surface that
would drift silently:

  * **starter layouts** — `template` on the row, `slot` on the block, and ONE
    slot map shared by `models.py`, the MCP tool (`canvas.ts`) and the frontend
    (`canvasLayouts.js`). A drift there is silent: the agent writes a slot the
    renderer does not know and the block quietly renders unslotted;
  * **every writer carries the template** — `set_canvas` sets it, `patch_canvas`
    and the voice panel verbs keep the stored one (the 2026-08-24 "paired
    writer" learning: a voice `show_markdown` that dropped it would un-layout
    the operator's board);
  * **the kit is taught and enforced from one list** — the platform prompt's
    class names ⊂ `utils/canvasKit.js::KIT_CLASSES`, and every class in that
    set has a rule in `CanvasKit.vue`; a class the prompt teaches but the
    sanitiser drops, or the stylesheet never draws, is the "renders as raw
    JSON" class of failure one level up (Invariant #13).

Plus the DDL pair (Invariant #9), the positional row-read stability the
`_SUMMARY_COLUMNS` append relies on, and the sanitiser wiring: canvas
components never reach the un-allowlisted entry points, every markdown/html
path forbids `<style>`, and the diagram keeps its own explicit SVG path.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from models import (
    CANVAS_LAYOUT_SLOTS,
    CANVAS_SLOT_RE,
    CANVAS_TEMPLATES,
    CanvasBlock,
    CanvasSummary,
    CanvasWrite,
)
from services import canvas_service, platform_prompt_service
from services.canvas_blocks import map_panel_tool, validate_blocks
from services.canvas_service import CanvasError
from services.platform_prompt_service import get_platform_system_prompt

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src/backend"
FRONTEND = REPO / "src/frontend/src"
MCP_TOOL = REPO / "src/mcp-server/src/tools/canvas.ts"
LAYOUTS_JS = FRONTEND / "components/canvas/canvasLayouts.js"
KIT_JS = FRONTEND / "utils/canvasKit.js"
KIT_VUE = FRONTEND / "components/canvas/CanvasKit.vue"
MARKDOWN_JS = FRONTEND / "utils/markdown.js"
CANVAS_DIR = FRONTEND / "components/canvas"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# One slot map across the three surfaces
# ---------------------------------------------------------------------------

def _js_layouts() -> dict:
    """Parse `LAYOUTS` from canvasLayouts.js without a JS runtime."""
    src = LAYOUTS_JS.read_text()
    body = src[src.index("export const LAYOUTS"):src.index("export const CANVAS_TEMPLATES")]
    out = {}
    for m in re.finditer(r"['\"]?([a-z-]+)['\"]?:\s*Object\.freeze\(\{\s*slots:\s*Object\.freeze\(\[(.*?)\]\)", body, re.S):
        out[m.group(1)] = re.findall(r"'([a-z-]+)'", m.group(2))
    assert out, "LAYOUTS parsed empty"
    return out


def _ts_layouts() -> dict:
    src = MCP_TOOL.read_text()
    body = src[src.index("export const CANVAS_LAYOUT_SLOTS"):src.index("const LAYOUT_GUIDE")]
    out = {}
    for m in re.finditer(r"[\"']?([a-z-]+)[\"']?:\s*\[(.*?)\]", body, re.S):
        out[m.group(1)] = re.findall(r'"([a-z-]+)"', m.group(2))
    assert out, "CANVAS_LAYOUT_SLOTS parsed empty"
    return out


def test_the_four_templates_and_their_slots_are_one_set_across_backend_mcp_and_frontend():
    assert set(CANVAS_TEMPLATES) == {"dashboard", "report", "brief", "status-board"}
    assert _ts_layouts() == CANVAS_LAYOUT_SLOTS, "canvas.ts drifted from models.py"
    assert _js_layouts() == CANVAS_LAYOUT_SLOTS, "canvasLayouts.js drifted from models.py"
    ts_enum = re.search(r"export const CANVAS_TEMPLATES = \[(.*?)\]", MCP_TOOL.read_text()).group(1)
    assert re.findall(r'"([a-z-]+)"', ts_enum) == list(CANVAS_TEMPLATES)


def test_every_slot_name_satisfies_the_slot_charset():
    for template, slots in CANVAS_LAYOUT_SLOTS.items():
        for slot in slots:
            assert CANVAS_SLOT_RE.match(slot), f"{template}.{slot}"


def test_every_layout_and_slot_has_a_rule_in_the_kit_stylesheet():
    """A slot the map names but the stylesheet never places would render as a
    plain stacked region inside the grid — a layout that looks broken rather
    than one that hides a block."""
    css = KIT_VUE.read_text()
    for template, slots in CANVAS_LAYOUT_SLOTS.items():
        assert f".canvas-kit .ck-layout-{template} {{" in css, f"no layout rule for {template}"
        for slot in slots:
            assert f".ck-layout-{template} > .ck-slot-{slot} {{ grid-area: {slot};" in css, (
                f"{template}.{slot} has no grid-area rule"
            )


# ---------------------------------------------------------------------------
# Template + slot through the models and the write path
# ---------------------------------------------------------------------------

def test_slot_survives_the_rest_round_trip():
    """`CanvasBlock` is extra='ignore', so without the field `model_dump()`
    would drop `slot` before the service ever saw it (the engineering voice's
    finding) — every unit test on the service would pass while the UI stayed
    stacked."""
    write = CanvasWrite.model_validate({
        "template": "dashboard",
        "blocks": [{"kind": "kpi", "slot": "kpis", "payload": {"tiles": []}}],
    })
    dumped = [b.model_dump() for b in write.blocks]
    assert dumped[0]["slot"] == "kpis"
    assert write.template == "dashboard"
    validated = validate_blocks(dumped)
    assert validated[0]["slot"] == "kpis" and validated[0]["id"] == "b1"


def test_unknown_template_is_refused_by_the_model_and_by_name_in_the_service():
    with pytest.raises(Exception):
        CanvasWrite.model_validate({"template": "poster", "blocks": []})
    with pytest.raises(CanvasError) as e:
        canvas_service.validate_template("poster")
    assert e.value.status_code == 400
    for name in CANVAS_TEMPLATES:
        assert name in e.value.detail, "the refusal must list the valid names"
    assert canvas_service.validate_template(None) is None
    assert canvas_service.validate_template("") is None
    assert canvas_service.validate_template("brief") == "brief"


def test_malformed_slot_is_a_named_400_on_the_pydantic_less_path_but_an_unknown_slot_is_not():
    with pytest.raises(CanvasError) as e:
        validate_blocks([{"kind": "json", "slot": "Kpis!", "payload": {}}])
    assert e.value.status_code == 400 and "slot" in e.value.detail
    with pytest.raises(Exception):
        CanvasBlock.model_validate({"kind": "json", "slot": "-bad"})
    # A slot no layout knows is NOT refused: it renders unslotted, after the
    # layout — losing content to a typo is the worse failure.
    out = validate_blocks([{"kind": "json", "slot": "sidebar-2", "payload": {}}])
    assert out[0]["slot"] == "sidebar-2"


def test_write_canvas_stores_the_template_and_defaults_it_to_none(monkeypatch):
    captured = {}

    def _upsert(agent, canvas_id, **kw):
        captured.update(kw)
        return {"agent_name": agent, "canvas_id": canvas_id, **kw}

    monkeypatch.setattr(canvas_service.db, "upsert_agent_canvas", _upsert)
    canvas_service.write_canvas("a", "main", [], title=None, audience="operator",
                                execution_id=None, template="status-board")
    assert captured["template"] == "status-board"
    canvas_service.write_canvas("a", "main", [], title=None, audience="operator", execution_id=None)
    assert captured["template"] is None, "no template means stacked, never a default layout"
    with pytest.raises(CanvasError):
        canvas_service.write_canvas("a", "main", [], title=None, audience="operator",
                                    execution_id=None, template="poster")


def test_the_rest_router_forwards_the_template_to_the_one_write_path():
    """Found live, not by the unit suite: the model accepted `template`, the
    service stored it, and the router between them never passed it — the PUT
    returned `template: null` on a valid write. Every field `CanvasWrite`
    carries must reach `write_canvas` by name."""
    src = (BACKEND / "routers/canvas.py").read_text()
    call = src[src.index("canvas_service.write_canvas("):]
    call = call[:call.index("except CanvasError")]
    for field in ("title", "audience", "execution_id", "template"):
        assert f"{field}=data.{field}" in call, f"router drops CanvasWrite.{field}"


def test_patch_canvas_keeps_the_stored_template(monkeypatch):
    stored = {"agent_name": "a", "canvas_id": "main", "title": "Board", "audience": "operator",
              "template": "dashboard",
              "blocks": [{"id": "b1", "kind": "kpi", "slot": "kpis", "payload": {"tiles": []}}]}
    monkeypatch.setattr(canvas_service.db, "get_agent_canvas", lambda a, c, audience=None: stored)
    writes = []
    monkeypatch.setattr(canvas_service, "write_canvas",
                        lambda *a, **k: writes.append((a, k)) or {"ok": True})
    canvas_service.patch_canvas("a", "main", [{"id": "b1", "kind": "json", "payload": {}}],
                                execution_id=None)
    assert writes[0][1]["template"] == "dashboard"


def test_the_voice_panel_write_carries_the_stored_template():
    """Every writer of the row makes the same call: the voice path is a
    block EDIT on the agent's board, so it must not un-layout it."""
    src = (BACKEND / "services/gemini_voice.py").read_text()
    call = src[src.index("canvas_service.write_canvas("):]
    call = call[:call.index("return message")]
    assert 'template=(current or {}).get("template")' in call


def test_the_voice_verbs_leave_a_slotted_block_alone():
    existing = [{"id": "b1", "kind": "kpi", "slot": "kpis", "payload": {"tiles": []}}]
    blocks, _ = map_panel_tool("show_markdown", {"content": "hi"}, existing)
    assert blocks[0] == existing[0], "the agent's slotted block survives a voice write untouched"


def test_summary_model_and_empty_canvas_carry_template():
    assert "template" in CanvasSummary.model_fields
    assert canvas_service.empty_canvas("a")["template"] is None


# ---------------------------------------------------------------------------
# DB layer: positional reads and the DDL pair
# ---------------------------------------------------------------------------

def test_summary_row_is_read_by_position_with_template_last():
    """`_row_to_summary` indexes the row; `template` is appended LAST so no
    earlier index shifts, and `_row_to_full` derives the blocks index from the
    column list rather than hard-coding it."""
    from db.canvas import CanvasOperations, _SUMMARY_COLUMNS

    assert _SUMMARY_COLUMNS[-1].name == "template"
    row = ("agent", "main", "T", "roster", 1, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "exec", "brief")
    out = CanvasOperations._row_to_summary(row)
    assert out == {
        "agent_name": "agent", "canvas_id": "main", "title": "T", "audience": "roster",
        "schema_version": 1, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z", "updated_by_execution_id": "exec",
        "template": "brief",
    }
    full = CanvasOperations._row_to_full(row + ('[{"id":"b1","kind":"json"}]',))
    assert full["template"] == "brief" and full["blocks"][0]["id"] == "b1"
    assert CanvasOperations._row_to_summary(row[:-1] + (None,))["template"] is None
    assert "template" in inspect.signature(CanvasOperations.upsert_canvas).parameters


def test_the_template_column_exists_on_both_migration_tracks_and_in_the_ddl():
    schema = (BACKEND / "db/schema.py").read_text()
    canvases_ddl = schema[schema.index('"agent_canvases": """'):]
    canvases_ddl = canvases_ddl[:canvases_ddl.index('"""', 30)]
    assert re.search(r"^\s+template TEXT,", canvases_ddl, re.M), "schema.py DDL lacks the column"
    tables = (BACKEND / "db/tables.py").read_text()
    canvases_tbl = tables[tables.index("agent_canvases = Table("):]
    canvases_tbl = canvases_tbl[:canvases_tbl.index("\n)")]
    assert 'Column("template", Text)' in canvases_tbl
    migrations = (BACKEND / "db/migrations.py").read_text()
    assert '("agent_canvases_template", _migrate_agent_canvases_template)' in migrations
    assert "ALTER TABLE agent_canvases ADD COLUMN template TEXT" in migrations
    alembic = BACKEND / "migrations/versions/0054_agent_canvases_template.py"
    assert alembic.exists(), "the PostgreSQL track is missing (Invariant #9)"
    text = alembic.read_text()
    assert 'down_revision = "0053_user_ui_preferences"' in text
    assert "ADD COLUMN IF NOT EXISTS template TEXT" in text


# ---------------------------------------------------------------------------
# The kit: taught from one list, drawn from one stylesheet
# ---------------------------------------------------------------------------

def _kit_classes() -> set[str]:
    src = KIT_JS.read_text()
    body = src[src.index("export const KIT_CLASSES"):src.index("]))", src.index("export const KIT_CLASSES"))]
    classes = set(re.findall(r"'(ck-[a-z0-9-]+)'", body))
    assert classes, "KIT_CLASSES parsed empty"
    return classes


def _canvas_block() -> str:
    prompt = get_platform_system_prompt("claude-code")
    m = re.search(r"### Your Canvas(.*?)\n### ", prompt, re.S)
    assert m
    return m.group(1)


def test_every_class_the_prompt_teaches_is_in_the_kit_allowlist():
    """A class the prompt teaches but the sanitiser drops is worse than an
    untaught one: the agent uses it, the write succeeds, the style is gone."""
    taught = set(re.findall(r"\bck-[a-z0-9-]+", _canvas_block()))
    assert taught, "the prompt teaches no kit classes"
    assert taught <= _kit_classes(), f"taught but not admitted: {taught - _kit_classes()}"


def test_the_kit_root_contains_its_own_overflow():
    """The inline-style allowlist admits `width` up to 9999px; the kit root
    must scroll that itself (principle 7) rather than widen the page — found
    by the /cso pass on this branch."""
    css = KIT_VUE.read_text()
    root = css[css.index(".canvas-kit {"):]
    root = root[:root.index("}")]
    assert "overflow-x: auto;" in root


def test_every_kit_class_has_a_rule_in_the_stylesheet():
    css = KIT_VUE.read_text()
    styled = set(re.findall(r"\.canvas-kit [^{]*?\.(ck-[a-z0-9-]+)", css))
    missing = {c for c in _kit_classes() if c not in css or c not in styled and f".{c}" not in css}
    assert not missing, f"in KIT_CLASSES but never styled: {missing}"


def test_the_voice_html_rule_names_only_kit_classes():
    src = (BACKEND / "services/gemini_voice.py").read_text()
    rule = src[src.index("HTML rule (for `update_panel`)"):src.index('"""', src.index("HTML rule"))]
    taught = set(re.findall(r"\bck-[a-z0-9-]+", rule))
    assert taught and taught <= _kit_classes()


def test_the_prompt_teaches_every_layout_with_its_slots_and_one_worked_example():
    block = _canvas_block()
    for template, slots in CANVAS_LAYOUT_SLOTS.items():
        assert f"{template}({', '.join(slots)})" in block, f"{template} slots not taught"
    assert 'template="dashboard"' in block, "the worked example"
    assert '"slot":"kpis"' in block or '"slot": "kpis"' in block
    assert "`canvas` skill" in block, "the skill carries the full reference"
    assert "<style>" in block, "the agent must be told <style> is dropped"


def test_the_mcp_tool_advertises_template_and_slot():
    src = MCP_TOOL.read_text()
    assert "template: z.enum(CANVAS_TEMPLATES)" in src
    assert "slot: z.string().regex(" in src
    client = (REPO / "src/mcp-server/src/client.ts").read_text()
    write = client[client.index("async writeCanvas("):client.index("async patchCanvas(")]
    assert "template?: string" in write and "slot?: string" in write


# ---------------------------------------------------------------------------
# Sanitiser wiring
# ---------------------------------------------------------------------------

def test_canvas_components_use_only_the_canvas_mode_entry_points():
    """`sanitizeHtml` / `renderMarkdown` are the un-allowlisted paths; the
    canvas must reach DOMPurify through the kit-mode twins, and the diagram
    through the explicit SVG path."""
    for vue in CANVAS_DIR.glob("*.vue"):
        src = vue.read_text()
        imports = "\n".join(l for l in src.splitlines() if l.lstrip().startswith("import "))
        if vue.name == "CanvasDiagram.vue":
            assert "sanitizeSvg" in imports and "sanitizeHtml" not in imports
            continue
        assert not re.search(r"\bsanitizeHtml\b", imports), f"{vue.name} bypasses the kit allowlist"
        assert not re.search(r"\brenderMarkdown\b", imports), f"{vue.name} bypasses the kit allowlist"
        if "ReportRenderer" in src:
            assert 'display-hint="markdown"' not in src, (
                f"{vue.name} routes canvas prose through the report renderer"
            )


def test_every_markdown_and_html_path_forbids_the_style_element_and_only_the_svg_path_keeps_it():
    src = MARKDOWN_JS.read_text()
    assert "FORBID_TAGS: ['style']" in src
    assert "canvasKit: true" in src and "FORBID_ATTR: ['id']" in src
    calls = re.findall(r"DOMPurify\.sanitize\((.*)\)", src)  # one call per line
    assert len(calls) >= 6
    bare = [c for c in calls if "CONFIG" not in c]
    assert len(bare) == 1 and "svg" in bare[0], (
        "exactly one bare sanitize call — the SVG path — may keep DOMPurify's default tag list"
    )
    hook = src[src.index("DOMPurify.addHook('afterSanitizeAttributes'"):]
    hook = hook[:hook.index("})")]
    assert "(node, _data, config)" in hook and "config.canvasKit" in hook
    assert "restrictToCanvasKit(node)" in hook
