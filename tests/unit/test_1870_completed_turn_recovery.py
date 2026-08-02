"""
Issue #1870 — a COMPLETED turn is discarded on `error_during_execution`.

Claude Code can report `is_error: true` / `subtype: error_during_execution` for
a turn that actually **finished**. The reproduction is a fan-out turn: the model
reaches `stop_reason: end_turn`, a background subagent's `<task-notification>`
lands *after* it, the follow-on turn is interrupted, and the CLI's terminal-state
check sees a non-terminal last message. `_finalize_headless_result` treats
`error_type == "execution_error"` as terminal and raises 502 — discarding an
answer that is sitting on disk.

Why the existing #678 recovery cannot be reused (pinned by a test below): the
trailing `<task-notification>` **is** a string-content user record, so
`_recover_response_from_jsonl`'s backward walk anchors *past* the completed
answer and the forward scan returns nothing. The boundary rule itself is the
defect — a time window does not help, because the notification is inside any
plausible window.

The fix is a new, additive recovery surface gated on three independent
conditions — *main-thread only* × *turn-scoped* × *finished* — where the
recovered answer is the marker message's **`message.id` group**, fail-closed
when that group carries no text.

  ⚠️ THE most important property in this change (measured, not assumed):
  a thinking-enabled final message is written as TWO records sharing one
  `message.id`, and BOTH carry `stop_reason: end_turn` — the `thinking` record
  first, the answer `text` record second. Over 1,075 real transcripts
  (6,663 main-thread `end_turn` markers) **40.6% are thinking-only**. A window
  rule survives that only by accident, and `_read_jsonl_records` drops the final
  partial line on an interrupted write — which is exactly the #1870 scenario.
  Lose that line and a window rule returns the turn's *narration without the
  answer* as a 200 SUCCESS, stored, never retried. That is strictly worse than
  the bug being fixed. Hence: the answer is the marker's `message.id` group, and
  a group with no text block returns None.

Modules under test:
    docker/base-image/agent_server/services/jsonl_recovery.py
        ::_recover_completed_turn_from_jsonl
    docker/base-image/agent_server/services/headless_executor.py
        ::_try_recover_completed_turn, ::_finalize_headless_result
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

# tests/unit/conftest.py:_preload_real_agent_server() registers
# docker/base-image/agent_server as a namespace package in sys.modules.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services import headless_executor as _he  # noqa: E402
from agent_server.services import jsonl_recovery as _jsonl_module  # noqa: E402
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _finalize_headless_result,
)
from agent_server.services.jsonl_recovery import (  # noqa: E402
    _recover_completed_turn_from_jsonl,
    _recover_response_from_jsonl,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CAPTURED_TAIL = _FIXTURES / "issue_1870_captured_tail.jsonl"

# The captured transcript's own clock. `since_iso` must precede the turn.
_TURN_START = "2026-07-28T18:06:00.000Z"
_PRIOR_TURN = "2026-07-28T17:00:00.000Z"

_SID = "1870f1x7-0000-4000-8000-000000000001"

# The verbatim assistant text at idx 14 of the captured fixture.
_CAPTURED_ANSWER = (
    "Draft creation is running in the background (14 in-thread draft replies, "
    "one at a time, drafts only). When it reports back I'll ledger each created "
    "draft, write the run report and last_run.json, and publish the Trinity KPI "
    "report."
)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def jsonl_dir(tmp_path, monkeypatch):
    """Redirect _JSONL_PROJECTS_DIR to tmp_path (idiom from test_jsonl_recovery)."""
    target = tmp_path / "projects" / "-home-developer"
    target.mkdir(parents=True)
    monkeypatch.setattr(_jsonl_module, "_JSONL_PROJECTS_DIR", str(target))
    return target


def _write_records(jsonl_dir: Path, session_id: str, records: list[dict]) -> Path:
    p = jsonl_dir / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _captured_records() -> list[dict]:
    """The REAL captured #1870 tail (see fixtures/README.md)."""
    return [
        json.loads(line)
        for line in _CAPTURED_TAIL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _install_captured(jsonl_dir: Path, records: list[dict] | None = None) -> Path:
    return _write_records(jsonl_dir, _SID, records or _captured_records())


# -- synthetic record builders (shapes the captured file cannot express) -----


def _user(text, ts="2026-07-28T18:06:10.000Z", **extra):
    """A user-INPUT record: string content. This is what the boundary walk seeks."""
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": ts, **extra}


def _user_list(text="[Request interrupted by user for tool use]",
               ts="2026-07-28T18:10:00.000Z", **extra):
    """A user record with LIST content — the MEASURED shape of the interrupt
    record (261 list / 2 str over 1,075 transcripts). Never a boundary."""
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]},
            "timestamp": ts, **extra}


def _assistant(text=None, *, stop="end_turn", ts="2026-07-28T18:07:45.774Z",
               msg_id="msg_marker", thinking=None, tool_use=False, **extra):
    content = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking,
                        "signature": "sig"})
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.append({"type": "tool_use", "id": "toolu_x", "name": "Bash",
                        "input": {}})
    msg: dict = {"role": "assistant", "content": content, "stop_reason": stop}
    if msg_id is not None:
        msg["id"] = msg_id
    rec = {"type": "assistant", "message": msg, **extra}
    if ts is not None:
        rec["timestamp"] = ts
    return rec


_NOTIFICATION = (
    "<task-notification>\n<task-id>t1</task-id>\n<status>completed</status>\n"
    "<summary>Agent finished</summary>\n</task-notification>"
)


def _make_ctx(
    *,
    session_id: str = _SID,
    task_start_iso: str = _TURN_START,
    error_type="execution_error",
    error_message="[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null",
    response_parts=None,
    raw_messages=None,
    cost_usd=0.0,
    duration_ms=0,
) -> HeadlessRunContext:
    """HeadlessRunContext in the post-subprocess state (mirrors test_1673)."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"],
        task_session_id="task-1870",
        task_start_iso=task_start_iso,
        effective_timeout=3600,
        images=None,
        prompt="dummy",
    )
    ctx.return_code = 0
    ctx.metadata = ExecutionMetadata(
        cost_usd=cost_usd, duration_ms=duration_ms, tool_count=0, num_turns=1,
    )
    ctx.metadata.session_id = session_id
    ctx.metadata.error_type = error_type
    ctx.metadata.error_message = error_message
    ctx.response_parts = list(response_parts or [])
    ctx.raw_messages = list(raw_messages or [])
    return ctx


# ===========================================================================
# A. THE REPRODUCTION — the one red test that names the bug
# ===========================================================================


def test_1870_issue_jsonl_tail_is_recovered(jsonl_dir):
    """THE bug: a real captured tail whose turn completed must return 200.

    Drives the real `_finalize_headless_result` with
    `error_type == "execution_error"` against the REAL captured transcript.
    Pre-fix this raises 502 and the completed answer is discarded.
    """
    _install_captured(jsonl_dir)
    ctx = _make_ctx()

    response_text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert _CAPTURED_ANSWER in response_text
    assert metadata.recovered_from_jsonl is True


def test_existing_recover_response_returns_none_for_this_shape(jsonl_dir):
    """Pins WHY the fix is a new function, not a `since` parameter.

    The `<task-notification>` is itself a string-content user record, so the
    existing backward walk anchors PAST the completed answer and the forward
    scan finds no assistant records at all. It is inside any plausible time
    window, so a `since` parameter could not have rescued it — the boundary
    rule is the defect.
    """
    _install_captured(jsonl_dir)

    assert _recover_response_from_jsonl(_SID) is None
    # ...while the new rule recovers it.
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)


def test_captured_fixture_has_the_shape_the_tests_claim(jsonl_dir):
    """Guards the fixture itself — a silently-regenerated fixture must not
    quietly turn these tests into no-ops (the plan shipped one wrong premise
    that had been typed from prose rather than captured)."""
    recs = _captured_records()
    markers = [
        r for r in recs
        if r.get("type") == "assistant"
        and r["message"].get("stop_reason") == "end_turn"
    ]
    # E1 shape: TWO records, one message.id, thinking first then text.
    assert len(markers) == 2
    assert markers[0]["message"]["id"] == markers[1]["message"]["id"]
    kinds = [b["type"] for m in markers for b in m["message"]["content"]]
    assert kinds == ["thinking", "text"]

    # The trap: a string-content user record AFTER the completed answer.
    notif = [
        r for r in recs
        if r.get("type") == "user"
        and isinstance(r["message"].get("content"), str)
        and "task-notification" in r["message"]["content"]
    ]
    assert len(notif) == 1

    # MEASURED: the interrupt record is LIST content, not a string.
    assert isinstance(recs[-1]["message"]["content"], list)


# ===========================================================================
# B2. THINKING/TEXT SPLIT — the E1 regression pins (CRITICAL)
# ===========================================================================


def test_marker_message_id_group_yields_the_answer(jsonl_dir):
    """A thinking+text split final message recovers the ANSWER, not the thinking."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant(None, thinking="internal deliberation, not the answer",
                   stop="end_turn", ts="2026-07-28T18:07:45.773Z"),
        _assistant("THE ANSWER", stop="end_turn", ts="2026-07-28T18:07:45.774Z"),
        _user(_NOTIFICATION, ts="2026-07-28T18:10:04.527Z"),
        _user_list(),
    ])

    out = _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)

    assert out == "THE ANSWER"
    assert "internal deliberation" not in out


def test_truncated_tail_thinking_only_marker_returns_none(jsonl_dir):
    """THE 2am-Friday test. Same shape, trailing TEXT record dropped.

    `_read_jsonl_records` drops the half-written final line on an interrupted
    write, and #1870 *is* the interrupted-tail case. The marker is then the
    thinking-only record. 40.6% of real markers are thinking-only, so this is
    the modal shape, not an exotic one.

    Must be None — never the narration. Returning the thinking block as a 200
    SUCCESS is a silent partial deliverable with no retry and no alert.
    """
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant(None, thinking="internal deliberation, not the answer",
                   stop="end_turn", ts="2026-07-28T18:07:45.773Z"),
        # the answer record was lost to the truncated write
    ])

    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_truncated_captured_tail_thinking_only_returns_none(jsonl_dir):
    """The same 2am case, driven off the REAL captured transcript: drop the
    final text record (idx 14) and everything after it."""
    recs = _captured_records()
    truncated = recs[:14]          # keeps the thinking marker at idx 13
    assert truncated[-1]["message"]["stop_reason"] == "end_turn"
    assert truncated[-1]["message"]["content"][0]["type"] == "thinking"
    _install_captured(jsonl_dir, truncated)

    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_truncated_captured_tail_502s_through_finalize(jsonl_dir):
    """...and end to end that is still a 502, not a partial-answer 200."""
    _install_captured(jsonl_dir, _captured_records()[:14])
    ctx = _make_ctx()

    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)

    assert exc.value.status_code == 502


def test_recovered_text_matches_final_message_not_the_window(jsonl_dir):
    """Intermediate narration before the final message must NOT be folded in.

    A normal success stores only `result_text` (stream_parser clears and
    replaces), so a `(boundary, marker]` window rule would store narration a
    successful run never stores. Measured: window/final text ratio p90 2.19x,
    max 79.5x. `message.id` grouping is exactly `result_text` semantics.

    Driven off the REAL captured transcript, whose earlier `tool_use` message
    group carries exactly such a narration block.
    """
    _install_captured(jsonl_dir)

    out = _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)

    assert out == _CAPTURED_ANSWER
    assert "INTERMEDIATE-NARRATION-REDACTED" not in out


def test_window_fallback_only_when_message_id_absent(jsonl_dir):
    """No `message.id` on the marker ⇒ fall back to the window walk."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("windowed answer", stop="end_turn", msg_id=None),
        _user(_NOTIFICATION, ts="2026-07-28T18:10:04.527Z"),
    ])

    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "windowed answer"


# ===========================================================================
# B. Boundary-rule shapes A-Q
# ===========================================================================


def test_shape_B_plain_678_stdout_race(jsonl_dir):
    """Parity with the #678 path: a clean completed turn still recovers."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27."),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "Q1 complete: median 0.27."


def test_shape_C_notification_as_list_content(jsonl_dir):
    """The notification also occurs as LIST content (41/1040 measured)."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27."),
        _user_list(_NOTIFICATION),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "Q1 complete: median 0.27."


def test_shape_D_aborted_mid_tool_returns_none(jsonl_dir):
    """AC #2: a genuine abort (no end_turn) must still fail."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("working on it", stop="tool_use", tool_use=True),
        _user_list(),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_E_multiple_text_blocks_concatenated_in_order(jsonl_dir):
    """Multiple text blocks in the marker's message group join in file order."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("part one", ts="2026-07-28T18:07:45.001Z"),
        _assistant("part two", ts="2026-07-28T18:07:45.002Z"),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "part one\npart two"


def test_shape_F_no_stop_reason_returns_none(jsonl_dir):
    """Fail closed when the field is absent (R7: we've never depended on it)."""
    rec = _assistant("looks done but isn't")
    del rec["message"]["stop_reason"]
    _write_records(jsonl_dir, _SID, [_user("do the thing"), rec])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_G_max_tokens_returns_none(jsonl_dir):
    """Truncated output is not a completed turn."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("cut off mid-sen", stop="max_tokens"),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_H_stale_end_turn_from_prior_turn_returns_none(jsonl_dir):
    """THE staleness guard (R1).

    A genuinely-aborted turn on a `--resume` session whose JSONL still holds a
    PREVIOUS turn's `end_turn` must not report that previous answer as this
    turn's success. That is a silent wrong answer — worse than the bug.
    """
    _write_records(jsonl_dir, _SID, [
        _user("earlier question", ts=_PRIOR_TURN),
        _assistant("PRIOR TURN ANSWER", ts="2026-07-28T17:00:05.000Z"),
        # this turn started at _TURN_START and produced nothing
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_I_prior_and_current_turn_both_complete(jsonl_dir):
    """With both in the file, recover THIS turn's answer only."""
    _write_records(jsonl_dir, _SID, [
        _user("earlier question", ts=_PRIOR_TURN),
        _assistant("PRIOR TURN ANSWER", ts="2026-07-28T17:00:05.000Z",
                   msg_id="msg_prior"),
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27.", msg_id="msg_marker"),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "Q1 complete: median 0.27."


def test_shape_J_untimestamped_assistant_in_group_is_kept(jsonl_dir):
    """A record with no parseable timestamp stays in the collected group.

    The since filter drops records that parse AND are older; an absent
    timestamp is not evidence of staleness, and dropping it would lose text.
    """
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("pre", ts=None),
        _assistant("Q1 complete: median 0.27."),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "pre\nQ1 complete: median 0.27."


def test_shape_K_empty_file_returns_none(jsonl_dir):
    (jsonl_dir / f"{_SID}.jsonl").write_text("")
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_L_end_turn_without_text_block_returns_none(jsonl_dir):
    """Fail closed: an end_turn whose message group has no text at all."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant(None, stop="end_turn"),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_M_meta_subagent_end_turn_while_main_interrupted(jsonl_dir):
    """SUB-THREAD GATE. Ungated this returns 200 with a subagent's internal
    thought while the main thread actually crashed."""
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("main pre", stop="tool_use", ts="2026-07-28T18:07:02.000Z",
                   msg_id="msg_main"),
        _user("Subagent prompt", ts="2026-07-28T18:07:03.000Z", isMeta=True),
        _assistant("subagent internal thought", ts="2026-07-28T18:07:04.000Z",
                   msg_id="msg_sub", isMeta=True),
        _user_list(),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_N_sidechain_subagent_end_turn_while_main_interrupted(jsonl_dir):
    """SUB-THREAD GATE, `isSidechain` variant."""
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("main pre", stop="tool_use", ts="2026-07-28T18:07:02.000Z",
                   msg_id="msg_main"),
        _user("Subagent prompt", ts="2026-07-28T18:07:03.000Z", isSidechain=True),
        _assistant("subagent internal thought", ts="2026-07-28T18:07:04.000Z",
                   msg_id="msg_sub", isSidechain=True),
        _user_list(),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_shape_O_late_sidechain_end_turn_after_the_real_answer(jsonl_dir):
    """The actual #1870 production ordering: a subagent finishes LATE.

    The main answer must still be recovered — the late sidechain marker must
    neither become the marker nor suppress the real one.
    """
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("Q1 complete: median 0.27.", ts="2026-07-28T18:07:45.000Z",
                   msg_id="msg_marker"),
        _assistant("subagent late thought", ts="2026-07-28T18:09:00.000Z",
                   msg_id="msg_sub", isSidechain=True),
        _user(_NOTIFICATION, ts="2026-07-28T18:10:04.000Z"),
        _user_list(),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "Q1 complete: median 0.27."


def test_shape_P_sidechain_text_never_leaks_into_the_answer(jsonl_dir):
    """A sidechain record sharing the marker's message.id must not contribute."""
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("main pre", ts="2026-07-28T18:07:40.000Z", msg_id="msg_marker"),
        _assistant("SIDECHAIN LEAK", ts="2026-07-28T18:07:42.000Z",
                   msg_id="msg_marker", isSidechain=True),
        _assistant("Q1 complete: median 0.27.", ts="2026-07-28T18:07:45.000Z",
                   msg_id="msg_marker"),
    ])
    out = _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)
    assert out == "main pre\nQ1 complete: median 0.27."
    assert "SIDECHAIN LEAK" not in out


def test_shape_Q_sidechain_user_string_never_becomes_the_boundary(jsonl_dir):
    """A subagent PROMPT is a string-content user record — it must not capture
    the boundary on the window-fallback path (marker has no message.id)."""
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("main pre", stop="end_turn", ts="2026-07-28T18:07:40.000Z",
                   msg_id=None),
        _user("Subagent prompt", ts="2026-07-28T18:07:42.000Z", isSidechain=True),
        _assistant("Q1 complete: median 0.27.", stop="end_turn",
                   ts="2026-07-28T18:07:45.000Z", msg_id=None),
    ])
    out = _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)
    assert out == "main pre\nQ1 complete: median 0.27."


def test_strictness_last_qualifying_record_must_itself_be_end_turn(jsonl_dir):
    """"Last qualifying record must ITSELF be end_turn" — deliberately stricter
    than "the last end_turn anywhere in scope".

    end_turn at T1 -> notification -> the model CONTINUES and is interrupted
    mid-tool at T2. The turn did not end at the marker, so no earlier end_turn
    may rescue it. (Verified against the real corpus: this ordering genuinely
    occurs — the captured transcript's own follow-on turn has exactly it.)
    """
    _write_records(jsonl_dir, _SID, [
        _user("Main task"),
        _assistant("checkpoint answer", stop="end_turn",
                   ts="2026-07-28T18:07:45.000Z", msg_id="msg_a"),
        _user(_NOTIFICATION, ts="2026-07-28T18:10:04.000Z"),
        _assistant("continuing", stop="tool_use", tool_use=True,
                   ts="2026-07-28T18:10:23.000Z", msg_id="msg_b"),
        _user_list(),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


def test_marker_far_in_the_future_is_rejected(jsonl_dir):
    """Upper staleness bound (S8).

    `task_start_iso` is a naive-UTC value stamped `Z` and `_parse_iso_timestamp`
    coerces naive to UTC, so a container running AHEAD of UTC would let a stale
    marker pass the lower bound — failing OPEN into exactly the wrong-answer
    mode the guard exists to prevent. The upper bound makes it fail closed both
    ways.
    """
    _write_records(jsonl_dir, _SID, [
        _user("do the thing", ts="2999-01-01T00:00:00.000Z"),
        _assistant("answer from the future", ts="2999-01-01T00:00:01.000Z"),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


# ===========================================================================
# C. Truncation (F10) — the branch that actually fires after a 10MB seek
# ===========================================================================


def test_truncated_read_boundary_belongs_to_a_previous_turn(jsonl_dir):
    """After a 10MB seek the realistic failure is "a boundary was found, but
    the WRONG one" — not "no boundary at all". The unconditional since filter
    on collected records is what bounds the blast radius on every path."""
    _write_records(jsonl_dir, _SID, [
        # retained tail of a PREVIOUS turn (its opening prompt was cut away)
        _assistant("PREVIOUS TURN TEXT", stop="tool_use", ts=_PRIOR_TURN,
                   msg_id=None),
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27.", msg_id=None),
    ])
    out = _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START)
    assert out == "Q1 complete: median 0.27."
    assert "PREVIOUS TURN TEXT" not in out


def test_no_boundary_at_all_still_recovers_the_marker_group(jsonl_dir):
    """Secondary: no string-content user record survives the seek. The
    `message.id` group does not need the boundary, so recovery still works."""
    _write_records(jsonl_dir, _SID, [
        _assistant("Q1 complete: median 0.27.", msg_id="msg_marker"),
    ])
    assert _recover_completed_turn_from_jsonl(
        _SID, since_iso=_TURN_START) == "Q1 complete: median 0.27."


def test_no_boundary_and_no_message_id_returns_none(jsonl_dir):
    """...but the window FALLBACK does need it: no boundary + no message.id."""
    _write_records(jsonl_dir, _SID, [
        _assistant("Q1 complete: median 0.27.", msg_id=None),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


# ===========================================================================
# D. Hostile input — must return None and never raise
# ===========================================================================


@pytest.mark.parametrize("bad_sid", [
    "../../etc/passwd",
    "/etc/passwd",
    "..",
    "",
    None,
    "a" * 200,
    "has space",
    "nul\x00byte",
])
def test_hostile_session_id_returns_none_without_raising(jsonl_dir, bad_sid):
    assert _recover_completed_turn_from_jsonl(bad_sid, since_iso=_TURN_START) is None


@pytest.mark.parametrize("record", [
    {"type": "assistant", "message": ["not", "a", "dict"]},
    {"type": "assistant", "message": 7},
    {"type": "assistant", "message": None},
    {"type": "assistant", "message": {"content": "a string not a list",
                                      "stop_reason": "end_turn"}},
    {"type": "assistant", "message": {"content": [{"type": "text"}],
                                      "stop_reason": {"nested": "dict"}}},
    {"type": "assistant", "message": {"content": ["not a dict block"],
                                      "stop_reason": "end_turn"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": None}],
                                      "stop_reason": "end_turn", "id": 12345}},
    {"type": "user", "message": {"content": {"unexpected": "dict"}}},
    {"not_even_a_type": True},
])
def test_malformed_records_return_none_without_raising(jsonl_dir, record):
    _write_records(jsonl_dir, _SID, [_user("do the thing"), record])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=_TURN_START) is None


@pytest.mark.parametrize("bad_since", [None, "", "not-a-timestamp", 12345])
def test_unparseable_since_iso_fails_closed(jsonl_dir, bad_since):
    """A since_iso we cannot parse means we cannot scope the turn ⇒ no recovery.

    Fail closed rather than recovering against an unbounded window — an
    unbounded window is exactly the stale-answer mode (shape H)."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27."),
    ])
    assert _recover_completed_turn_from_jsonl(_SID, since_iso=bad_since) is None


def test_missing_file_returns_none(jsonl_dir):
    assert _recover_completed_turn_from_jsonl(
        "deadbeef-0000-4000-8000-000000000000", since_iso=_TURN_START) is None


# ===========================================================================
# E. Recovery-failure isolation — a bug in new parsing must not become a 500
# ===========================================================================


def test_recovery_exception_still_raises_502_not_500(jsonl_dir, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("parsing blew up")

    monkeypatch.setattr(_he, "_recover_completed_turn_from_jsonl", _boom)
    _install_captured(jsonl_dir)
    ctx = _make_ctx()

    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)

    assert exc.value.status_code == 502


# ===========================================================================
# F. No duplication (F9) — replace, don't append
# ===========================================================================


def test_partial_stdout_text_is_replaced_not_appended(jsonl_dir):
    """`stream_parser` does not clear `response_parts` on this subtype, so a
    partial stdout fragment can already be sitting there. Appending would
    duplicate it; the JSONL is authoritative for this turn's assistant text."""
    _install_captured(jsonl_dir)
    ctx = _make_ctx(response_parts=["partial output"])

    response_text, _raw, _md, _sid = _finalize_headless_result(ctx)

    assert _CAPTURED_ANSWER in response_text
    assert "partial output" not in response_text


# ===========================================================================
# F2. Option C — the distinguishing signals (§2.5, REQUIRED)
# ===========================================================================


def test_recovered_turn_sets_recovered_terminal(jsonl_dir):
    """C1: a recovered turn is SUCCESS but never indistinguishable from clean."""
    _install_captured(jsonl_dir)
    ctx = _make_ctx()

    _text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert metadata.recovered_terminal is True
    assert metadata.recovered_from_jsonl is True


def test_no_recovery_leaves_both_flags_unset(jsonl_dir):
    """On a miss (502) neither flag may be set."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("working", stop="tool_use", tool_use=True),
    ])
    ctx = _make_ctx()

    with pytest.raises(HTTPException):
        _finalize_headless_result(ctx)

    assert ctx.metadata.recovered_terminal is False
    assert ctx.metadata.recovered_from_jsonl is False


def test_recovered_terminal_distinguishes_from_678_telemetry_recovery(jsonl_dir):
    """THE point of C1 — and the test that stops a future cleanup merging the
    two flags.

    `recovered_from_jsonl` is already set by #678 whenever *telemetry* is
    back-filled from disk, so it cannot distinguish "cost was recovered" from
    "a reported FAILURE became a SUCCESS". Here: a pure #678 stdout-race
    recovery (no error_type at all) sets `recovered_from_jsonl` and must leave
    `recovered_terminal` False.
    """
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("Q1 complete: median 0.27."),
    ])
    # #678 shape: clean exit, result line lost ⇒ cost/duration are None.
    ctx = _make_ctx(error_type=None, error_message=None,
                    cost_usd=None, duration_ms=None)

    _text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert metadata.recovered_from_jsonl is True
    assert metadata.recovered_terminal is False, (
        "recovered_terminal must stay SEPARATE from recovered_from_jsonl — "
        "merging them makes 'a reported failure became a success' unmeasurable"
    )


def test_recovered_response_carries_the_notice(jsonl_dir):
    """C2: the human-visible flag, and the answer still first-class content."""
    _install_captured(jsonl_dir)
    ctx = _make_ctx()

    response_text, _raw, _md, _sid = _finalize_headless_result(ctx)

    assert response_text.startswith(_he._RECOVERY_NOTICE)
    assert "#1870" in response_text
    # the notice names the partial-checkpoint risk explicitly
    assert "checkpoint" in response_text.lower()
    # ...and the answer survives intact, not mangled into the notice
    assert _CAPTURED_ANSWER in response_text
    assert response_text.endswith(_CAPTURED_ANSWER)


def test_recovery_notice_flows_into_previous_response_templating(jsonl_dir):
    """C2 interaction pin (scrutiny item): the notice becomes part of the
    STORED response, so it flows into loop templating (`{{previous_response}}`)
    and any fan-out join.

    That is deliberate — a downstream iteration should know its input was
    salvaged and may be a checkpoint. Pinned here so the behaviour is a
    decision, not an accident: if it ever proves too noisy, C1 alone satisfies
    the "machine-readable without reading the response text" constraint and
    this test is the one that must be consciously changed.
    """
    _install_captured(jsonl_dir)
    response_text, _raw, _md, _sid = _finalize_headless_result(_make_ctx())

    rendered = "Prior step said:\n{{previous_response}}".replace(
        "{{previous_response}}", response_text)

    assert _he._RECOVERY_NOTICE in rendered
    assert _CAPTURED_ANSWER in rendered


def test_metadata_error_type_still_set_on_recovered_200(jsonl_dir):
    """R8/T2: `error_type` is deliberately LEFT SET on the recovered 200 as the
    provenance that recovery fired. Pinned so a future "cleanup" cannot
    silently drop it.

    Note the honest scope: nothing in the backend or the agent turns a
    *returned* `error_type` into a failure, and the returned metadata is what
    the caller persists — so this is provenance, not control flow. C1
    (`recovered_terminal`) is the primary machine-readable record.
    """
    _install_captured(jsonl_dir)
    ctx = _make_ctx()

    _text, _raw, metadata, _sid = _finalize_headless_result(ctx)

    assert metadata.error_type == "execution_error"


# ===========================================================================
# G. Sanitization
# ===========================================================================


def test_recovered_text_is_credential_sanitized(jsonl_dir):
    """Recovered on-disk model output is sanitized on the same line as normal
    output (the existing `sanitize_text` over the joined response_parts).

    The dummy value is deliberately NOT key-shaped: this repo is public and a
    realistic prefix would trip secret scanners. The sanitizer keys off the
    variable NAME, so this still exercises the real redaction path.
    """
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("done. ANTHROPIC_API_KEY=dummy-not-a-real-key-999"),
    ])
    ctx = _make_ctx()

    response_text, _raw, _md, _sid = _finalize_headless_result(ctx)

    assert "dummy-not-a-real-key-999" not in response_text


def test_recovery_logs_never_carry_the_recovered_text(jsonl_dir, caplog):
    """Ops rule (canary G-04 shape): identifiers and counts, never content."""
    _install_captured(jsonl_dir)
    with caplog.at_level("INFO"):
        _finalize_headless_result(_make_ctx())

    assert _CAPTURED_ANSWER not in caplog.text
    assert "completed_turn_recovered_from_jsonl" in caplog.text


def test_declined_recovery_logs_a_reason(jsonl_dir, caplog):
    """§8 observability: a fail-closed gate that silently stops firing is
    indistinguishable from "the bug never happened". Every decline logs a
    greppable reason."""
    _write_records(jsonl_dir, _SID, [
        _user("do the thing"),
        _assistant("working", stop="tool_use", tool_use=True),
    ])
    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException):
            _finalize_headless_result(_make_ctx())

    assert "completed_turn_recovery_declined" in caplog.text
    assert "reason=no_marker" in caplog.text


@pytest.mark.parametrize("records,expected_reason", [
    ([], "no_records"),
    ([_user("x"), _assistant("a", stop="tool_use", tool_use=True)], "no_marker"),
    ([_user("x", ts=_PRIOR_TURN),
      _assistant("prior", ts="2026-07-28T17:00:05.000Z")], "stale_marker"),
    ([_user("x"), _assistant(None, stop="end_turn")], "no_text"),
    ([_user("Main"), _assistant("sub", isSidechain=True)], "sub_thread_only"),
])
def test_decline_reasons_are_specific(jsonl_dir, caplog, records, expected_reason):
    _write_records(jsonl_dir, _SID, records)
    with caplog.at_level("WARNING"):
        assert _recover_completed_turn_from_jsonl(
            _SID, since_iso=_TURN_START) is None
    assert f"reason={expected_reason}" in caplog.text


# ===========================================================================
# Regression-pin: source-level signature
# ===========================================================================


def test_source_pins_the_1870_gate():
    """If the gate is deleted or re-gated on the error STRING, fail loudly.

    Gating on the message text is explicitly forbidden: PR #1938 is rewriting
    exactly that text on this path.
    """
    he = (Path(_he.__file__)).read_text()
    jr = (Path(_jsonl_module.__file__)).read_text()

    assert "#1870" in he
    assert "_try_recover_completed_turn" in he
    assert "#1870" in jr
    assert 'stop_reason' in jr
    # the #1673 branch literals must survive verbatim
    assert "#1673" in he
    assert 'error_type == "execution_error"' in he
