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
import models
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
    the SHAPE of the call, not its result.

    ent#534 made the audience a parameter of the read (a platform principal
    sees every audience, an external client `roster` only), so the guard moved
    with it: the narrowing still happens IN the query, and the parameter's
    default is the fail-closed one, so a caller that forgets to state the
    principal gets the client's view, never the operator's.
    """
    source = (_BACKEND / "client_portal" / "agent_page.py").read_text()
    body = source[source.index("def canvases("):source.index("def _rating_tally(")]
    assert "db.list_agent_canvases(agent_name, audience=audience)" in body, (
        "the Workspace canvas read no longer narrows by audience in the query — "
        "an operator-only canvas would reach a client"
    )
    assert "audience: Optional[str] = CANVAS_AUDIENCE_ROSTER" in body, (
        "the read's default audience must be the client's (fail-closed)"
    )
    assert "db.get_agent_canvas(agent_name, canvas_id, audience=audience)" in body
    assert "AUDIENCE_ROSTER" in (_BACKEND / "db" / "canvas.py").read_text()


def test_the_workspace_audience_is_decided_by_principal_kind():
    """ent#534: a platform user reads every audience (they already can on Agent
    Detail for any agent on their roster); an external client stays roster-only.
    The two callers in the router pass this function's answer, never a literal."""
    from client_portal import agent_page
    from db.canvas import AUDIENCE_ROSTER
    assert agent_page.canvas_audience_for(False) == AUDIENCE_ROSTER
    assert agent_page.canvas_audience_for(True) is None
    router = (_BACKEND / "client_portal" / "router.py").read_text()
    assert router.count("agent_page.canvas_audience_for(principal.is_platform)") == 2


# ---------------------------------------------------------------------------
# Staleness — derived, and never claiming more than it observed
# ---------------------------------------------------------------------------

def test_stale_when_the_agent_finished_a_run_after_the_write():
    """`is_stale` is RETIRED IN PLACE as of #2734: it is still computed and still
    ships on the payload, and nothing renders it. These six assertions pin the
    derivation as it stands so it stays recoverable — they do NOT describe what a
    reader sees. The header renders two facts and draws no conclusion; see the
    #2734 block below."""
    canvas = {"updated_at": "2026-09-02T10:00:00Z"}
    assert canvas_service.is_stale(canvas, "2026-09-02T10:05:00Z") is True


def test_not_stale_when_nothing_ran_since():
    canvas = {"updated_at": "2026-09-02T10:00:00Z"}
    assert canvas_service.is_stale(canvas, "2026-09-02T09:55:00Z") is False


def test_a_completion_that_predates_the_write_is_not_a_run_since():
    """What this fixture actually pins, said correctly (#2734).

    It was named for the writing turn, but the data is the opposite orientation:
    the canvas is written at 10:00:05 and the completion is at 10:00:00, i.e. the
    write comes AFTER the completion — which a writing run cannot produce, since
    a run completes after it writes. So it never pinned the writer case at all,
    and the mark did fire on every writing run's own output. That inversion is
    why the reported contradiction ("Updated just now" beside "may be out of
    date") shipped green, and it is why #2734 retired the verdict rather than
    correcting it. The assertion is unchanged: an earlier completion is not a run
    "since"."""
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
# Freshness — two facts, never a verdict (#2734)
#
# The header stopped rendering a derived verdict and started rendering two
# neutral facts: when the canvas was written, and when the agent last FINISHED a
# run. The second one is the payload's new `agent_last_run_at`, carried by the
# read `decorate` already performs once per agent. These tests pin the honest
# half: the value is the one that was read, the read costs one query for a whole
# list, and every way of not having it degrades to OMISSION — never to a
# fabricated time and never to a claim about the agent.
# ---------------------------------------------------------------------------

def _canvas_row(**over):
    """A canvas metadata row shaped like the db read, so a model round trip is
    possible without hand-building a second, differently-spelled dict."""
    row = {
        "agent_name": "alpha",
        "canvas_id": "main",
        "title": "Status",
        "audience": AUDIENCE_OPERATOR,
        "schema_version": 1,
        "created_at": "2026-09-02T09:00:00.000000Z",
        "updated_at": "2026-09-02T10:00:00.000000Z",
        "updated_by_execution_id": None,
        "template": None,
    }
    row.update(over)
    return row


class _RunTimeDb:
    """Defines ONLY `last_completed_execution_at` — deliberately. An
    implementation that reached for any other db call (a `MAX(started_at)`, say)
    raises AttributeError here, so the field's shorter name cannot quietly come
    to mean something else."""

    def __init__(self, value="2026-09-02T10:05:00.000000Z"):
        self.value = value
        self.calls = []

    def last_completed_execution_at(self, agent):
        self.calls.append(agent)
        return self.value


def test_decorate_carries_the_agents_last_completed_run_time(monkeypatch):
    monkeypatch.setattr(canvas_service, "db", _RunTimeDb())
    out = canvas_service.decorate([_canvas_row()], "alpha")
    assert out[0]["agent_last_run_at"] == "2026-09-02T10:05:00.000000Z"


def test_the_run_time_is_read_once_per_agent_not_once_per_canvas(monkeypatch):
    """The one-read invariant covers the NEW field too: eight canvases still pay
    one query, and all eight carry the same answer. A future contributor moving
    the read inside the loop fails here, not in production."""
    stub = _RunTimeDb()
    monkeypatch.setattr(canvas_service, "db", stub)
    out = canvas_service.decorate([_canvas_row() for _ in range(8)], "alpha")
    assert stub.calls == ["alpha"]
    assert {r["agent_last_run_at"] for r in out} == {"2026-09-02T10:05:00.000000Z"}


def test_an_agent_that_never_finished_a_run_makes_no_run_claim(monkeypatch):
    monkeypatch.setattr(canvas_service, "db", _RunTimeDb(value=None))
    out = canvas_service.decorate([_canvas_row()], "alpha")
    assert out[0]["agent_last_run_at"] is None


def test_a_failing_run_time_read_never_fails_the_render_and_never_fabricates_one(monkeypatch):
    """The honesty test. A read failure degrades to None — which the header
    renders as OMISSION — never to a timestamp nobody read and never to "the
    agent has not run yet", which would turn "we could not read it" into a claim
    about the agent."""

    class _Db:
        def last_completed_execution_at(self, agent):
            raise RuntimeError("db down")

    monkeypatch.setattr(canvas_service, "db", _Db())
    out = canvas_service.decorate([_canvas_row()], "alpha")
    assert out[0]["agent_last_run_at"] is None


def test_the_run_time_is_the_read_value_not_a_constant(monkeypatch):
    """Non-vacuity: two different reads must produce two different answers."""
    first = _RunTimeDb(value="2026-09-02T10:05:00.000000Z")
    monkeypatch.setattr(canvas_service, "db", first)
    a = canvas_service.decorate([_canvas_row()], "alpha")[0]["agent_last_run_at"]
    second = _RunTimeDb(value="2026-09-03T08:00:00.000000Z")
    monkeypatch.setattr(canvas_service, "db", second)
    b = canvas_service.decorate([_canvas_row()], "alpha")[0]["agent_last_run_at"]
    assert a == "2026-09-02T10:05:00.000000Z"
    assert b == "2026-09-03T08:00:00.000000Z"


def test_a_naive_stored_timestamp_is_normalised_to_Z(monkeypatch):
    """`last_completed_execution_at` is a raw `MAX(completed_at)` — not one of
    the read boundaries #1474 normalised. A naive row used to fail QUIET (the
    lexicographic compare just lost); rendered, `Date.parse` reads it as LOCAL
    time and the fact is silently offset by the viewer's UTC offset. So the
    normalisation happens in the service, beside the read.

    `stale` keeps receiving the RAW value, and this fixture proves it: the raw
    naive string loses the lexicographic compare (`' '` sorts below `'T'`), so
    `stale` is False here, while the NORMALISED value would have made it True.
    `is_stale` is retired in place and its comparison is not this change's to
    alter — a normalised input would be a silent behaviour change to it."""
    monkeypatch.setattr(canvas_service, "db", _RunTimeDb(value="2026-09-02 10:05:00"))
    out = canvas_service.decorate([_canvas_row()], "alpha")
    assert out[0]["agent_last_run_at"] == "2026-09-02T10:05:00.000000Z"
    assert out[0]["stale"] is False


def test_the_empty_canvas_placeholder_declares_the_field_and_claims_nothing():
    """The voice panel's pre-first-write response is built by hand and bypasses
    `decorate`, so the two constructors of the same response shape drift unless
    something pins them together. None, not a read: a placeholder for a canvas
    that does not exist makes no claims, and that route polls every ~3s."""
    placeholder = canvas_service.empty_canvas("alpha")
    assert "agent_last_run_at" in placeholder
    assert placeholder["agent_last_run_at"] is None


def test_the_response_model_carries_the_run_time_through_pydantic(monkeypatch):
    """The seam no other test crosses: `decorate` writes a string KEY and
    `models` declares a FIELD NAME, and nothing binds the two spellings. Every
    canvas read route declares `response_model=`, and FastAPI filters a dict
    through it — an undeclared key is dropped SILENTLY, so a misspelling would
    lose the fact on Agent Detail while the voice route (no response_model) and
    the portal payload (plain dict) kept it. That is a per-surface failure that
    looks like a frontend bug.

    So this starts from `decorate`'s own output rather than a hand-built dict; a
    hand-built dict re-opens the hole it exists to close."""
    monkeypatch.setattr(canvas_service, "db", _RunTimeDb())
    row = canvas_service.decorate([_canvas_row()], "alpha")[0]

    assert models.CanvasSummary(**row).model_dump()["agent_last_run_at"] == "2026-09-02T10:05:00.000000Z"
    detail = models.Canvas(**{**row, "blocks": []})
    assert detail.model_dump()["agent_last_run_at"] == "2026-09-02T10:05:00.000000Z"
    assert models.CanvasSummary.model_fields["agent_last_run_at"].description


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
    # ent#536: the two content writes share `_gate_write` (self-gate + rate +
    # size hint); the delete keeps the bare self-gate. Either spelling proves
    # the gate is applied — what must never appear is a write with neither.
    gate = source[source.index("def _gate_write"):source.index("@router.get")]
    assert "_require_self(current_user, name)" in gate
    for handler in ("def write_canvas", "def patch_canvas", "def clear_canvas"):
        body = source[source.index(handler):]
        body = body[:body.index("@router.") if "@router." in body[10:] else len(body)]
        # ent#553: `clear_canvas` gates through `_gate_human_removal`, which
        # calls `_require_self` for an agent principal and additionally
        # requires ownership for a human. Still self-gated — via one more hop.
        assert (
            "_require_self(" in body
            or "_gate_write(" in body
            or "_gate_human_removal(" in body
        ), f"{handler} is not self-gated"


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
    # ent#537: the canvas-mode twin — same DOMPurify instance and hook, plus
    # the design-kit allowlist on `class` / `style`.
    assert "sanitizeCanvasHtml" in block
    util = (_FRONTEND / "utils" / "markdown.js").read_text()
    for fn in ("sanitizeHtml", "sanitizeCanvasHtml"):
        assert f"export function {fn}" in util
        assert "DOMPurify.sanitize" in util[util.index(f"export function {fn}"):]


# ---------------------------------------------------------------------------
# The voice panel moved rather than being dropped (AC 2 / FR-7)
# ---------------------------------------------------------------------------

def test_the_voice_panel_writes_the_durable_canvas():
    """ent#536: the panel IS the agent's default canvas — every verb goes
    through the one service write path, and there is no in-memory copy."""
    source = (_BACKEND / "services" / "gemini_voice.py").read_text()
    assert "panel_state" not in source, "the voice panel must not keep a second copy of the canvas"
    body = source[source.index("def _execute_panel_tool"):]
    body = body[:body.index("\n    async def ")]
    assert "canvas_service.write_canvas" in body
    assert "map_panel_tool" in body
    assert "_CANVAS_ID = DEFAULT_CANVAS_ID" in source, "voice writes the shared default canvas, not a silo"


def test_the_voice_panel_never_widens_who_can_see_a_canvas():
    """Behavioural pin (ent#536, replacing the old literal `audience="operator"`
    pin): the write carries the SESSION audience, defaulting to operator, and a
    canvas stored wider than that is refused rather than written."""
    from services.canvas_service import audience_within

    assert audience_within("operator", "operator")
    assert audience_within("operator", "roster")
    assert audience_within("roster", "roster")
    assert not audience_within("roster", "operator"), "an operator call must not land on a roster canvas"
    assert not audience_within("roster", "garbage"), "an unknown writer audience reads as operator"
    source = (_BACKEND / "services" / "gemini_voice.py").read_text()
    assert 'canvas_audience: str = "operator"' in source


def test_the_voice_panel_capability_still_has_a_caller():
    """Deleting the per-agent workspace removed the only caller that passed
    `workspace_mode: true`. Bridging the panel to the canvas while leaving it
    unreachable would be dead code wearing a fix's name.

    #2559 moved that caller, so this test moved with it: the chat-panel voice
    overlay is retired and the **Workspace** call is what enables the capability
    now (`client_portal/voice.py::start_workspace_voice`). The property under
    test is unchanged — some live caller must still pass it — which is why this
    is re-pointed rather than deleted, and why the ChatPanel arm is inverted
    instead of dropped: the retired front door must not quietly come back.
    """
    workspace_start = (_BACKEND / "client_portal" / "voice.py").read_text()
    body = workspace_start[workspace_start.index("def start_workspace_voice"):]
    assert "workspace_mode=True" in body
    assert 'canvas_audience="operator"' in body

    chat = (_FRONTEND / "components" / "ChatPanel.vue").read_text()
    assert "voice.start(" not in chat, "the Agent Detail voice overlay is retired (#2559)"


def test_a_canvas_write_failure_never_breaks_the_voice_turn():
    source = (_BACKEND / "services" / "gemini_voice.py").read_text()
    body = source[source.index("def _execute_panel_tool"):]
    body = body[:body.index("\n    async def ") if "\n    async def " in body else len(body)]
    assert "except Exception" in body and "logger.warning" in body
    assert "could not be saved" in body, "a failed write must be reported, not claimed as success"


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
