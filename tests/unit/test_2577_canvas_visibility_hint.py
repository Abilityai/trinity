"""#2577 — a canvas the requester cannot see must not answer like one they can.

An agent in a public-link / Workspace session wrote a canvas with the default
`audience: "operator"`. `set_canvas` answered `success: true` with the whole
canvas echoed back, `list_canvases` showed it present and current, and the
person who asked for it saw nothing: `operator` canvases render on Agent
Detail, and that session's reader is on the agent's Workspace page.

The failure mode is the bug, not the default: **a success indistinguishable
from one the user can see.** The agent reports "published", the user reports
"I see nothing", and neither has the information to work out why — the
audience→surface mapping appears nowhere in the tool contract.

This pins the fix the issue ranks first: `set_canvas` reports whether the
chosen audience reaches the session it was written from, so the agent can
correct itself in the same turn.

The rule is CONSERVATIVE by design, and the conservatism is the interesting
part. Widening to `roster` is a real visibility change — it publishes to
everyone the agent is shared with — so a wrong `False` costs an over-share.
The verdict is therefore three-state and only claims what it can prove:

  * a turn from a surface whose reader is known → True / False;
  * a channel turn, an agent-to-agent turn, an unknown label, or no
    `execution_id` at all → None, and no note.

That last group is deliberately silent rather than warned: a Telegram reader
has no canvas surface in the channel at all, so "widen to roster" would not put
it in front of them either, and a hint that nudges a pointless widening is
worse than none.
"""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def cv():
    from services.canvas_service import canvas_visibility
    return canvas_visibility


class TestTheReportedBug:
    def test_operator_canvas_from_a_public_link_turn_is_reported_invisible(self, cv):
        visible, note = cv(audience="operator", triggered_by="public")
        assert visible is False
        # The note has to name the fix, not merely the problem — the whole
        # point is that the agent can self-correct in the same turn.
        assert "roster" in note

    def test_a_roster_canvas_from_the_same_turn_is_reported_visible(self, cv):
        visible, note = cv(audience="roster", triggered_by="public")
        assert visible is True
        assert note is None

    def test_a_paid_turn_is_the_same_door(self, cv):
        assert cv(audience="operator", triggered_by="paid")[0] is False


class TestAPortalTurnGetsNoClaim:
    """The correction this rule needed, and the reason it is three-state.

    A Workspace turn carries `triggered_by="public"` as well; the two are told
    apart only by `source_channel="portal"`. They are NOT the same audience:
    `client_portal.agent_page.canvas_audience_for(is_platform=True)` returns
    `None` — no narrowing — so a signed-in OPERATOR working in the Workspace
    reads every audience, `operator` included.

    Claiming `False` there would tell an operator their own canvas is invisible
    and push them to publish it to the whole roster: the exact over-share the
    three-state design exists to prevent, on the most ordinary Workspace path
    there is. The row records the client's email, never whether that email is a
    platform principal, so the honest answer is no answer.
    """

    @pytest.mark.parametrize("audience", ["operator", "roster"])
    def test_a_portal_turn_makes_no_claim_either_way(self, cv, audience):
        assert cv(audience=audience, triggered_by="public",
                  source_channel="portal") == (None, None)

    def test_the_public_link_verdict_survives_an_unrelated_source_channel(self, cv):
        # Only `portal` is excluded. A stray value must not silence the branch
        # the report came from.
        assert cv(audience="operator", triggered_by="public",
                  source_channel=None)[0] is False
        assert cv(audience="operator", triggered_by="public",
                  source_channel="")[0] is False


class TestTheOperatorSide:
    @pytest.mark.parametrize("trigger", ["manual", "chat", "schedule", "webhook", "mcp",
                                         "loop", "reminder", "event", "operator_response"])
    @pytest.mark.parametrize("audience", ["operator", "roster"])
    def test_an_operator_side_turn_sees_every_audience(self, cv, trigger, audience):
        # `GET /{name}/canvas` is "unfiltered by audience on purpose: this is
        # the operator read", so both audiences render on Agent Detail.
        visible, note = cv(audience=audience, triggered_by=trigger)
        assert visible is True
        assert note is None


class TestItRefusesToGuess:
    @pytest.mark.parametrize("trigger", ["slack", "telegram", "whatsapp", "voip"])
    def test_a_channel_turn_makes_no_claim(self, cv, trigger):
        # No canvas renders inside a channel, so neither audience reaches that
        # reader where they are — "widen to roster" would be false advice.
        assert cv(audience="operator", triggered_by=trigger) == (None, None)

    @pytest.mark.parametrize("trigger", ["agent", "a2a", "fan_out", "room", "system"])
    def test_a_non_human_or_ambiguous_requester_makes_no_claim(self, cv, trigger):
        assert cv(audience="operator", triggered_by=trigger) == (None, None)

    def test_an_unknown_label_makes_no_claim(self, cv):
        # Fails toward silence, never toward "invisible" — an unrecognised
        # label must not nudge an agent into publishing to the whole roster.
        assert cv(audience="operator", triggered_by="something_new") == (None, None)

    @pytest.mark.parametrize("trigger", [None, ""])
    def test_no_trigger_makes_no_claim(self, cv, trigger):
        assert cv(audience="operator", triggered_by=trigger) == (None, None)

    def test_an_unknown_audience_normalises_the_closed_way(self, cv):
        # `normalize_audience` defaults closed; the verdict must agree with the
        # value that will actually be STORED, not with what was asked for.
        assert cv(audience="nonsense", triggered_by="public")[0] is False


class TestTheWriteResponseCarriesIt:
    def test_the_model_exposes_a_tri_state_and_a_note(self):
        from models import CanvasWriteResult
        fields = CanvasWriteResult.model_fields
        assert "visible_to_requester" in fields
        assert "visibility_note" in fields
        # Both default to "no claim", so every existing caller keeps working
        # and an unresolvable execution says nothing rather than something.
        assert fields["visible_to_requester"].default is None
        assert fields["visibility_note"].default is None

    def test_it_is_a_superset_of_canvas_so_the_write_shape_is_unchanged(self):
        from models import Canvas, CanvasWriteResult
        assert issubclass(CanvasWriteResult, Canvas)
        # The MCP tool echoes the write response as `canvas`; nesting it under
        # a new key would break that shape for no gain.
        for name in Canvas.model_fields:
            assert name in CanvasWriteResult.model_fields

    def test_the_stored_wrapper_threads_the_source_channel(self):
        # The portal exclusion lives in `canvas_visibility`, so a wrapper that
        # forgets to pass `source_channel` re-opens the over-share while every
        # pure test above still passes — the defect would be entirely in the
        # glue. Pinned from source for that reason.
        import inspect
        from services import canvas_service
        src = inspect.getsource(canvas_service.visibility_for_canvas)
        assert 'source_channel=getattr(execution, "source_channel", None)' in src

    def test_only_the_write_route_returns_it(self):
        # list/get answer about a canvas, not about a requester — a verdict
        # there would be a claim with no session to make it about.
        import inspect
        import routers.canvas as canvas_router
        src = inspect.getsource(canvas_router)
        assert "response_model=CanvasWriteResult" in src
        assert src.count("response_model=CanvasWriteResult") == 1


class TestTheToolSaysSo:
    def test_the_tool_description_warns_that_the_default_is_often_wrong(self):
        from pathlib import Path
        tool = Path(__file__).resolve().parents[2] / "src/mcp-server/src/tools/canvas.ts"
        src = tool.read_text()
        # The issue's "related nudge": the description said WHERE each audience
        # renders and never that the default is usually wrong off the operator
        # path — which is the only thing that would have prevented this.
        assert "visible_to_requester" in src


class TestTheWrapperReadsTheKeyTheRowActuallyCarries:
    """The #2603 review finding, pinned.

    Every test above drives the pure rule or reads source. The wrapper between
    them — `visibility_for_canvas` — took its execution id from
    `canvas["execution_id"]`, a key no canvas dict has ever carried: the write
    path (`db.upsert_agent_canvas`) and both read paths (`_row_to_summary`,
    `_row_to_full`) all name it `updated_by_execution_id`. So the wrapper
    answered `(None, None)` on every single write, the feature was inert, and
    the suite was green — the exact failure a pure-rule test cannot see.

    These drive the wrapper with the shape the database layer really returns,
    so the two can no longer disagree without something going red.
    """

    def _stored(self, **over):
        """The shape `db.upsert_agent_canvas` returns, key for key."""
        row = {
            "agent_name": "scribe",
            "canvas_id": "default",
            "title": "t",
            "audience": "operator",
            "schema_version": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "updated_by_execution_id": "exec-1",
            "template": None,
            "blocks": [],
        }
        row.update(over)
        return row

    def test_the_stored_shape_reaches_a_real_verdict(self, monkeypatch):
        from types import SimpleNamespace
        from services import canvas_service

        monkeypatch.setattr(
            canvas_service, "resolve_and_validate_execution",
            lambda eid, agent: SimpleNamespace(
                triggered_by="public", source_channel=None
            ),
        )
        visible, note = canvas_service.visibility_for_canvas(
            self._stored(), "scribe"
        )
        assert visible is False, (
            "the wrapper answered no-claim on a row that carries an execution id"
        )
        assert "roster" in note

    def test_the_key_it_reads_is_the_key_the_db_layer_writes(self):
        """Named separately so a rename on either side fails HERE, with the
        reason, rather than as a silent return to no-claim."""
        import inspect
        from db import canvas as canvas_db
        from services import canvas_service

        assert '"updated_by_execution_id"' in inspect.getsource(
            canvas_db.CanvasOperations.upsert_canvas
        )
        assert '"updated_by_execution_id"' in inspect.getsource(
            canvas_service.visibility_for_canvas
        )

    def test_a_row_with_no_execution_id_still_makes_no_claim(self, monkeypatch):
        from services import canvas_service

        def _boom(*_a, **_kw):  # pragma: no cover — must never be reached
            raise AssertionError("resolved an execution for a row that has none")

        monkeypatch.setattr(
            canvas_service, "resolve_and_validate_execution", _boom
        )
        assert canvas_service.visibility_for_canvas(
            self._stored(updated_by_execution_id=None), "scribe"
        ) == (None, None)
