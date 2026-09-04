"""ent#438 — the agent canvas: a durable surface, and the one workspace.

Two halves of one issue.

**The canvas.** Before this the only canvas Trinity had was
`VoiceSession.panel_state`: in-memory, written only by the Gemini Live voice
tools, on one page behind `WORKSPACE_ENABLED && GEMINI_API_KEY`, and gone when
the session ended. Agents that produce results which are not chat messages had
nowhere to put them. The canvas is a ROW keyed `(agent_name, canvas_id)`, so a
write is an upsert and the surface is addressable — that composite key is the
whole difference from `agent_reports`, where each publish is a new immutable
row that accumulates.

**The workspace merge.** `/agents/:name/workspace` is deleted and redirects to
`/workspace?agent=`. Safe only because ent#440 already put voice conversation
inside the Workspace, so once the canvas moved the page had no capability of
its own left — which is why the voice-panel bridge below is pinned as part of
the same change rather than deferred.

What is pinned here:

  * the audience default is `operator` and an unrecognised stored value reads
    as `operator` — an allowlist, so a canvas never widens who sees the agent's
    output by accident (AC 8);
  * staleness is DERIVED, not a clock, and never claims more than it observed
    (AC 7);
  * the write is self-gated, so one agent cannot paint on another's canvas;
  * ids are charset-validated with a NAMED refusal, and blocks are byte-capped;
  * the retired route still resolves, carrying its agent (AC 1);
  * every block kind the tools advertise has a renderer, and the five shared
    ones delegate to the CI-pinned `components/reports/` dispatch rather than a
    second rendering layer (AC 4).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from db.canvas import (
    AUDIENCE_OPERATOR,
    AUDIENCE_ROSTER,
    VALID_AUDIENCES,
    normalize_audience,
)
from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_ID_RE,
    CANVAS_MAX_BLOCKS,
    CanvasWrite,
)
from services import canvas_service
from services.canvas_service import CanvasError

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO / "src" / "frontend" / "src"
_BACKEND = _REPO / "src" / "backend"

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Audience — the property that keeps a canvas from widening disclosure
# ---------------------------------------------------------------------------

def test_a_canvas_is_operator_only_unless_the_agent_says_otherwise():
    """AC 8. Reaching a client is an explicit agent act, mirroring ent#365's
    rule that an unaddressed report stays operator-only."""
    assert CanvasWrite().audience == AUDIENCE_OPERATOR
    assert CanvasWrite(audience="roster").audience == AUDIENCE_ROSTER


@pytest.mark.parametrize("stored", ["public", "everyone", "", None, 0, ["roster"]])
def test_an_unrecognised_audience_reads_as_operator(stored):
    """An ALLOWLIST, never a blocklist (#2396's rule). `audience` is a plain
    TEXT column with no CHECK constraint, so the next value someone writes by
    hand — or a future value this build has not heard of — must fail closed."""
    assert normalize_audience(stored) == AUDIENCE_OPERATOR


def test_the_audience_is_a_column_not_a_block():
    """The ent#364 rule: `blocks` is agent-authored, so an audience buried in
    it would let a prompt-injected agent choose its own readers."""
    fields = set(CanvasWrite.model_fields)
    assert "audience" in fields
    written = CanvasWrite(audience="roster", blocks=[])
    assert written.audience == "roster"
    # And an unknown top-level field is refused outright rather than ignored.
    with pytest.raises(Exception):
        CanvasWrite(audience="roster", visible_to="someone@example.com")


def test_the_portal_read_narrows_in_the_query():
    """ent#365 FR-2's lesson: a gate applied after the fetch has already loaded
    what it was meant to withhold. Pinned on the source because the defect is
    the SHAPE of the call, not its result."""
    source = (_BACKEND / "client_portal" / "agent_page.py").read_text()
    body = source[source.index("def canvases("):source.index("def _rating_tally(")]
    assert "audience=CANVAS_AUDIENCE_ROSTER" in body, (
        "the Workspace canvas read no longer narrows by audience in the query — "
        "an operator-only canvas would reach a client"
    )
    assert "AUDIENCE_ROSTER" in (_BACKEND / "db" / "canvas.py").read_text()


# ---------------------------------------------------------------------------
# Staleness — derived, and never claiming more than it observed
# ---------------------------------------------------------------------------

def test_stale_when_the_agent_finished_a_run_after_the_write():
    canvas = {"updated_at": "2026-09-02T10:00:00Z"}
    assert canvas_service.is_stale(canvas, "2026-09-02T10:05:00Z") is True


def test_not_stale_when_nothing_ran_since():
    canvas = {"updated_at": "2026-09-02T10:00:00Z"}
    assert canvas_service.is_stale(canvas, "2026-09-02T09:55:00Z") is False


def test_the_turn_that_wrote_the_canvas_does_not_mark_it_stale():
    """The writing turn completes AFTER it writes, so a naive comparison would
    mark every canvas stale the moment its own run ended. It does not, because
    the canvas is written during the turn and the run completes later — this
    pins the case explicitly since it is the one that would make the mark
    meaningless by firing always."""
    canvas = {"updated_at": "2026-09-02T10:00:05Z"}
    assert canvas_service.is_stale(canvas, "2026-09-02T10:00:00Z") is False


@pytest.mark.parametrize("last_completed", [None, ""])
def test_no_evidence_is_not_a_staleness_claim(last_completed):
    """Fail-quiet is deliberate HERE and only here: the mark is an ADDITION to
    an always-rendered timestamp, so missing evidence costs the mark and not
    the honesty. Crying wolf on every read would train the reader to ignore
    it, which is the failure mode this feature exists to avoid."""
    assert canvas_service.is_stale({"updated_at": "2026-09-02T10:00:00Z"}, last_completed) is False


def test_a_canvas_with_no_timestamp_makes_no_claim():
    assert canvas_service.is_stale({}, "2026-09-02T10:00:00Z") is False


def test_staleness_is_derived_once_per_agent_not_once_per_canvas(monkeypatch):
    """An agent with eight canvases must not pay eight identical queries."""
    calls = []

    class _Db:
        def last_completed_execution_at(self, agent):
            calls.append(agent)
            return "2026-09-02T10:05:00Z"

    monkeypatch.setattr(canvas_service, "db", _Db())
    rows = [{"updated_at": "2026-09-02T10:00:00Z"} for _ in range(8)]
    out = canvas_service.decorate(rows, "alpha")
    assert calls == ["alpha"]
    assert all(r["stale"] for r in out)


def test_a_failing_staleness_read_never_fails_the_render(monkeypatch):
    class _Db:
        def last_completed_execution_at(self, agent):
            raise RuntimeError("db down")

    monkeypatch.setattr(canvas_service, "db", _Db())
    out = canvas_service.decorate([{"updated_at": "2026-09-02T10:00:00Z"}], "alpha")
    assert out[0]["stale"] is False


# ---------------------------------------------------------------------------
# Ids and bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("canvas_id", ["status", "weekly.summary", "a-b_c", "A1", "x" * 64])
def test_reasonable_canvas_ids_are_accepted(canvas_id):
    assert canvas_service.validate_canvas_id(canvas_id) == canvas_id


@pytest.mark.parametrize("canvas_id", [
    "", "x" * 65, "has space", "../etc/passwd", "a/b", "slash\\back", "emoji🙂", None, 7,
])
def test_bad_canvas_ids_are_refused_by_name(canvas_id):
    """A NAMED 400: an over-long or punctuation-bearing id is the routine agent
    mistake, and the framework's generic 422 says nothing about how to fix it."""
    with pytest.raises(CanvasError) as exc:
        canvas_service.validate_canvas_id(canvas_id)
    assert exc.value.status_code == 400
    assert "canvas_id" in exc.value.detail


def test_the_id_pattern_is_anchored():
    """An unanchored pattern would accept `../../etc/passwd` because it
    contains a matching run — the guard would read as present and do nothing."""
    assert CANVAS_ID_RE.pattern.startswith("^") and CANVAS_ID_RE.pattern.endswith("$")


def test_a_block_list_over_the_byte_cap_is_refused():
    """The COUNT cap cannot express this: fifty one-row blocks and fifty
    ten-thousand-row blocks are the same to `max_length`."""
    fat = [{"kind": "json", "payload": {"x": "y" * (CANVAS_BLOCKS_MAX_BYTES // 2)}} for _ in range(3)]
    with pytest.raises(CanvasError) as exc:
        canvas_service.serialize_blocks(fat)
    assert exc.value.status_code == 413


def test_a_block_list_over_the_count_cap_is_refused():
    many = [{"kind": "json", "payload": {}} for _ in range(CANVAS_MAX_BLOCKS + 1)]
    with pytest.raises(CanvasError) as exc:
        canvas_service.serialize_blocks(many)
    assert exc.value.status_code == 413


def test_a_normal_canvas_serializes():
    blocks = [{"kind": "markdown", "title": "Status", "payload": {"markdown": "# ok"}}]
    assert json.loads(canvas_service.serialize_blocks(blocks)) == blocks


# ---------------------------------------------------------------------------
# Provenance is provenance, not authorization
# ---------------------------------------------------------------------------

def test_a_foreign_execution_id_is_dropped_not_refused(monkeypatch):
    """The id stamps which run wrote the canvas. Losing the stamp is a smaller
    harm than losing the canvas the agent just rendered, so an unresolvable id
    degrades rather than refusing the write."""
    monkeypatch.setattr(canvas_service, "resolve_and_validate_execution", lambda e, a: None)
    assert canvas_service.resolve_execution_id("exec-from-another-agent", "alpha") is None


def test_an_owned_execution_id_is_kept(monkeypatch):
    monkeypatch.setattr(canvas_service, "resolve_and_validate_execution", lambda e, a: object())
    assert canvas_service.resolve_execution_id("exec-1", "alpha") == "exec-1"


def test_absent_execution_id_is_none(monkeypatch):
    monkeypatch.setattr(
        canvas_service, "resolve_and_validate_execution",
        lambda e, a: pytest.fail("must not be consulted for an absent id"),
    )
    assert canvas_service.resolve_execution_id(None, "alpha") is None


# ---------------------------------------------------------------------------
# The write is self-gated
# ---------------------------------------------------------------------------

def test_the_write_routes_are_self_gated():
    """`AuthorizedAgent` proves the KEY OWNER can reach the path agent; it does
    not stop an agent-scoped key writing as a SIBLING agent the same owner
    shares. That is a disclosure surface as well as a correctness one, because
    a `roster` canvas is client-visible (the #918 rule, restated)."""
    source = (_BACKEND / "routers" / "canvas.py").read_text()
    assert "_require_self" in source
    guard = source[source.index("def _require_self"):source.index("@router.get")]
    assert "current_user.agent_name" in guard and "403" in guard
    for handler in ("def write_canvas", "def clear_canvas"):
        body = source[source.index(handler):]
        body = body[:body.index("@router.") if "@router." in body[10:] else len(body)]
        assert "_require_self(" in body, f"{handler} is not self-gated"


def test_reads_are_not_self_gated():
    """An operator reading through the UI is a user-scoped principal with no
    `agent_name`, and the {self} ∪ permitted narrowing for agent keys lives at
    the MCP layer. Self-gating the READ would break the Agent Detail tab."""
    source = (_BACKEND / "routers" / "canvas.py").read_text()
    listing = source[source.index("async def list_canvases"):source.index("async def get_canvas")]
    assert "_require_self(" not in listing


# ---------------------------------------------------------------------------
# One workspace (AC 1)
# ---------------------------------------------------------------------------

def test_the_per_agent_workspace_page_is_gone():
    assert not (_FRONTEND / "views" / "AgentWorkspace.vue").exists(), (
        "AgentWorkspace.vue is back — ent#438 retires it; the canvas lives on "
        "the agent's Workspace page and the Agent Detail tab"
    )


def test_the_retired_route_still_resolves_and_carries_its_agent():
    """A deleted page must not become a dead bookmark. The redirect is a
    FUNCTION so query and hash survive (the ent#381 shape), and it passes the
    agent through — a link to one agent's workspace lands on that agent."""
    router = (_FRONTEND / "router" / "index.js").read_text()
    block = router[router.index("path: '/agents/:name/workspace'"):]
    block = block[:block.index("},\n  {")]
    assert "redirect:" in block
    assert "agent: to.params.name" in block
    assert "to.query" in block and "to.hash" in block
    assert "AgentWorkspace.vue" not in router


def test_nothing_still_navigates_to_the_deleted_page():
    offenders = []
    for path in list(_FRONTEND.rglob("*.vue")) + list(_FRONTEND.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "AgentWorkspace.vue" in text or "name: 'AgentWorkspace'" in text:
            offenders.append(path.name)
    assert offenders == [], f"still reference the retired page: {offenders}"


# ---------------------------------------------------------------------------
# One rendering layer (AC 4)
# ---------------------------------------------------------------------------

def test_every_advertised_block_kind_has_a_renderer():
    """The MCP tool advertises the kinds; the frontend must render all of them.
    A kind an agent can write and nothing can draw is a silently empty canvas."""
    tool = (_REPO / "src" / "mcp-server" / "src" / "tools" / "canvas.ts").read_text()
    advertised = set(re.findall(r'"(\w+)",? ?', tool[tool.index("const BLOCK_KINDS"):tool.index("] as const")]))
    utils = (_FRONTEND / "components" / "canvas" / "canvasUtils.js").read_text()
    known = set(re.findall(r"'(\w+)'", utils[utils.index("REPORT_DELEGATED_KINDS = ["):utils.index("export const CANVAS_BLOCK_KINDS")]))
    assert advertised <= known, f"advertised but not renderable: {sorted(advertised - known)}"


def test_the_shared_kinds_delegate_rather_than_fork():
    """`components/reports/` renderer keys are CI-pinned as the canonical
    contract (test_1535). The canvas REUSES that dispatch; a second copy of
    those renderers is what §5.11 and §5.14 both refused."""
    block = (_FRONTEND / "components" / "canvas" / "CanvasBlock.vue").read_text()
    assert "ReportRenderer" in block
    assert "from '../reports/ReportRenderer.vue'" in block


def test_the_report_display_hint_enum_is_not_widened():
    """A canvas is a superset of a report's rendering, not a change to what a
    report is — widening the report enum here would move a contract test#1535
    owns and change an unrelated surface."""
    models = (_BACKEND / "models.py").read_text()
    hint = models[models.index("ReportDisplayHint = Literal["):]
    hint = hint[:hint.index("]") + 1]
    assert "chart" not in hint and "html" not in hint


def test_agent_authored_html_is_sanitized():
    """A `roster` canvas reaches a customer's browser, and `html` is exactly
    what the voice panel writes (H-005)."""
    block = (_FRONTEND / "components" / "canvas" / "CanvasBlock.vue").read_text()
    assert "sanitizeHtml" in block
    util = (_FRONTEND / "utils" / "markdown.js").read_text()
    assert "export function sanitizeHtml" in util
    assert "DOMPurify.sanitize" in util[util.index("export function sanitizeHtml"):]


# ---------------------------------------------------------------------------
# The voice panel moved rather than being dropped (AC 2 / FR-7)
# ---------------------------------------------------------------------------

def test_the_voice_panel_writes_the_durable_canvas():
    source = (_BACKEND / "services" / "gemini_voice.py").read_text()
    assert "_persist_panel_to_canvas" in source
    body = source[source.index("def _persist_panel_to_canvas"):]
    assert "upsert_agent_canvas" in body
    assert 'audience="operator"' in body, (
        "a voice panel that silently became client-visible is the widening "
        "FR-4 exists to prevent"
    )


def test_the_voice_panel_capability_still_has_a_caller():
    """Deleting the per-agent workspace removed the only caller that passed
    `workspace_mode: true`. Bridging the panel to the canvas while leaving it
    unreachable would be dead code wearing a fix's name — the chat voice
    overlay now enables it."""
    chat = (_FRONTEND / "components" / "ChatPanel.vue").read_text()
    assert "voice.start(currentSessionId.value, null, true)" in chat


def test_a_canvas_write_failure_never_breaks_the_voice_turn():
    source = (_BACKEND / "services" / "gemini_voice.py").read_text()
    body = source[source.index("def _persist_panel_to_canvas"):]
    body = body[:body.index("\n    async def ") if "\n    async def " in body else len(body)]
    assert "except Exception" in body and "logger.warning" in body


# ---------------------------------------------------------------------------
# Dual-track migration (Invariant #3)
# ---------------------------------------------------------------------------

def test_both_migration_tracks_carry_the_table():
    sqlite_track = (_BACKEND / "db" / "migrations.py").read_text()
    assert "agent_canvases_table" in sqlite_track
    assert "_migrate_agent_canvases_table" in sqlite_track
    alembic = _BACKEND / "migrations" / "versions" / "0050_agent_canvases.py"
    assert alembic.exists(), "PostgreSQL half of the dual-track pair is missing"
    revision = alembic.read_text()
    assert 'down_revision = "0049_execution_turn_integrity"' in revision
    assert "agent_canvases" in revision


def test_the_table_is_registered_for_rename_and_purge():
    """`agent_name` is half the PRIMARY KEY, so an unregistered table would
    leave a renamed agent's canvas addressed to a name nothing resolves — and
    its next write would mint a SECOND canvas under the new name while the old
    one stayed visible."""
    from db.agent_cleanup import AGENT_REFS

    assert any(r.table == "agent_canvases" and r.column == "agent_name" for r in AGENT_REFS)
