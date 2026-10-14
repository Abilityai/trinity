"""Edge-case matrix for the operator-queue service — parametrized examples.

/edge-cases run 2026-10-06 (slug `ec_operator_queue`). Companion to
`test_ec_operator_queue_properties.py` (invariants over the whole input space).
The full matrix, with the row numbers used in the param ids below, lives in
the 2026-10-06 /edge-cases matrix.

TARGETS (the riskiest pure / state functions of
`services/operator_queue_service.py` and its leaf `services/operator_queue_choices.py`,
weighted towards the recent #3242 / #3243 / #2915 / #3024 work):

* id handling — `_ID_RE` / `_valid_execution_id`, `is_platform_minted`,
  `is_about_a_person` (#1631 agent-scoped ids, #1632 reserved prefixes)
* authoring caps — `options_cap_violation`, `title_cap_violation`,
  `reads_as_something_else` (#3243)
* answers — `validate_response_choice`, `usable_options` (#2376, #3242)
* the #2915 fingerprint — `changed_fields` / `_entry_content` / `_row_content`
  against what `db.create_operator_queue_item_with_outcome` REALLY stores
* the ingestion marker — `_stronger_hold`, `_ingestion_marker`,
  `_marker_differs`, `_apply_ingestion_marker` (#3130, #3243)
* status/TTL predicates — `awaits_terminal_flip` (#3024), `is_aged` /
  `_aged_at` / `aging_hours` (#2915)

Rows already pinned by the existing suites (test_1631/1632/2915/3242/3243/2376)
are NOT repeated here; only UNCOVERED rows are.

REAL BUGS are kept as `xfail(strict=True)` — never watered down to green. Each
carries its reachability evidence in its docstring.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_choices import (  # noqa: E402
    OPTIONS_DROPPED_MARKER,
    SOMETHING_ELSE,
    InstructionRequiredError,
    ReservedValueError,
    ResponseNotOfferedError,
    options_cap_violation,
    reads_as_something_else,
    title_cap_violation,
    usable_options,
    validate_response_choice,
)

pytestmark = pytest.mark.unit

BUG = "BUG: {} — found by /edge-cases 2026-10-06"
_seq = count()


@pytest.fixture
def real_db():
    """The unit conftest pins TRINITY_DB_PATH to a per-process temp file; refuse
    to run if DATABASE_URL would redirect writes anywhere else."""
    from db.engine import resolve_database_url
    url = resolve_database_url()
    assert url.startswith("sqlite:///") and "trinity-unit-tests" in url, url
    from database import db
    return db


def _ingest_and_read_back(db, entry: dict) -> dict:
    """The production ingest seam (`_sync_agent`: clamp → create_with_outcome,
    channel=file, raised_by=agent) followed by the poller's own read
    (`get_operator_queue_sync_index_for_agent`). Returns the open row."""
    agent = f"ec-oq-{next(_seq)}-{os.getpid()}"
    clamped = oqs._clamp_ingested_item(entry, agent)
    db.create_operator_queue_item_with_outcome(agent, clamped, channel="file", raised_by="agent")
    rows = db.get_operator_queue_sync_index_for_agent(agent)["open"]
    assert len(rows) == 1
    return rows[0]


# ===========================================================================
# 1. Ids — shape regex, reserved prefixes, the about-a-person subset
# ===========================================================================

class TestIdShape:
    @pytest.mark.parametrize("value,ok", [
        pytest.param("a", True, id="r1-single-char"),
        pytest.param("x" * oqs.OPERATOR_QUEUE_EXECUTION_ID_MAX, True, id="r2-exactly-at-cap"),
        pytest.param("x" * (oqs.OPERATOR_QUEUE_EXECUTION_ID_MAX + 1), False, id="r3-cap-plus-one"),
        pytest.param("", False, id="r4-empty"),
        pytest.param("exec 1", False, id="r5-inner-space"),
        pytest.param("exéc", False, id="r6-non-ascii"),
        pytest.param(123, False, id="r7-non-str"),
        pytest.param(None, False, id="r8-none"),
        pytest.param("a/b", False, id="r9-slash"),
    ])
    def test_valid_execution_id_boundaries(self, value, ok):
        out = oqs._valid_execution_id(value)
        assert (out is not None and out == value) is ok

    @pytest.mark.xfail(strict=True, reason="#3315: " + BUG.format(
        "_ID_RE uses `$`, which matches before a trailing newline, so 'abc\\n' passes the id-shape check"))
    @pytest.mark.parametrize("value", [
        pytest.param("abc\n", id="r10-trailing-newline"),
    ])
    def test_a_trailing_newline_is_not_id_shaped(self, value):
        """REACHABILITY: the file poller rejects malformed ids with exactly this
        regex (`operator_queue_service.py` `_sync_agent`, the `_ID_RE.match(req_id)`
        guard before the #3243 caps) and so does the native raise
        (`ask_service.py:675`, `_validated_ask`). Both are fed agent-authored JSON
        strings, and `"id": "abc\\n"` is a valid JSON string. The contract both
        sites state ("letters, digits, '.', '_', ':' or '-'"; the audit row
        comment "req_id passed the `_ID_RE` shape check") is then false: the row
        is created with a newline in its request_id. `\\Z` (or fullmatch) fixes it."""
        assert not oqs._ID_RE.match(value)
        assert oqs._valid_execution_id(value) is None


class TestPlatformMinted:
    @pytest.mark.parametrize("item,minted", [
        pytest.param("poison-x", True, id="r11-bare-string"),
        pytest.param("  POISON-x", True, id="r12-string-case-and-pad-folded"),
        pytest.param({"request_id": "gate-1", "id": "uuid"}, True, id="r13-request-id-wins"),
        pytest.param({"request_id": "", "id": "poison-legacy"}, True, id="r14-empty-request-id-falls-back-to-id"),
        pytest.param({"request_id": None, "id": "agent-own"}, False, id="r15-agent-row"),
        pytest.param({}, False, id="r16-empty-dict"),
        pytest.param(None, False, id="r17-none"),
        pytest.param(type("R", (), {"request_id": "val_x", "id": "u"})(), True, id="r18-attr-object"),
        pytest.param(type("R", (), {})(), False, id="r19-attr-object-without-fields"),
        pytest.param("poison", False, id="r20-prefix-without-its-dash"),
        pytest.param("xpoison-x", False, id="r21-prefix-not-at-start"),
    ])
    def test_is_platform_minted(self, item, minted):
        assert oqs.is_platform_minted(item) is minted

    def test_every_about_a_person_prefix_is_reserved(self):
        """#715 claims `_ABOUT_A_PERSON_ID_PREFIXES` is a subset of the reserved
        tuple ("so no agent can mint a row into it"). Pinned, so adding a person
        prefix without reserving it fails here rather than in production. (r22)"""
        for p in oqs._ABOUT_A_PERSON_ID_PREFIXES:
            assert p.startswith(oqs._RESERVED_ID_PREFIXES), p

    @pytest.mark.parametrize("rid,about", [
        pytest.param("gate-note-1", True, id="r23-gate-note"),
        pytest.param(" Workspace-Problem-7", True, id="r24-folded"),
        pytest.param("poison-1", False, id="r25-reserved-but-not-about-a-person"),
        pytest.param(None, False, id="r26-null-request-id"),
    ])
    def test_is_about_a_person(self, rid, about):
        assert oqs.is_about_a_person({"request_id": rid}) is about

    def test_every_reserved_prefix_is_lowercase_and_unpadded(self):
        """The sync loop's guard folds the AGENT's id (`strip().lower()`) and
        compares against the tuple verbatim — a mixed-case or padded entry in the
        tuple could therefore never match anything. (r27)"""
        for p in oqs._RESERVED_ID_PREFIXES:
            assert p == p.strip().lower() and p, p


# ===========================================================================
# 2. #3243 authoring caps and the "(something else)" lookalike guard
# ===========================================================================

class TestCaps:
    @staticmethod
    def check(options, max_options=5, max_chars=60):
        return options_cap_violation(options, max_options=max_options, max_chars=max_chars)

    @pytest.mark.parametrize("options,expected", [
        pytest.param([], None, id="r28-empty-list"),
        pytest.param([SOMETHING_ELSE] * 9, None, id="r29-only-the-literal-never-counted"),
        pytest.param(["a"] * 5 + [SOMETHING_ELSE], None, id="r30-cap-plus-the-literal"),
        pytest.param([None] * 6, ("too_many_options", {"limit": 5, "count": 6}), id="r31-non-str-elements-counted"),
        pytest.param(["", "", ""], None, id="r32-empty-strings-pass"),
        pytest.param([{"x": "y" * 500}], None, id="r33-non-str-element-not-length-checked"),
        pytest.param(["a", "Something else", "b" * 61], ("invalid_options", {"index": 1}),
                     id="r34-lookalike-before-length"),
        pytest.param(["b" * 61, "Something else"], ("invalid_options", {"index": 1}),
                     id="r35-lookalike-wins-even-when-the-long-one-is-earlier"),
        pytest.param(("a",) * 9, None, id="r36-tuple-is-not-a-list"),
    ])
    def test_options_cap_violation(self, options, expected):
        assert self.check(options) == expected

    @pytest.mark.parametrize("title,expected", [
        pytest.param("t" * 120, None, id="r37-at-cap"),
        pytest.param("t" * 121, ("title_too_long", {"limit": 120, "length": 121}), id="r38-cap-plus-one"),
        pytest.param("", None, id="r39-empty"),
        pytest.param(None, None, id="r40-none"),
        pytest.param(["t" * 500], None, id="r41-non-str"),
        pytest.param("\U0001F600" * 120, None, id="r42-astral-counts-one"),
    ])
    def test_title_cap_violation(self, title, expected):
        assert title_cap_violation(title, max_chars=120) == expected

    @pytest.mark.parametrize("option,reads", [
        pytest.param("((something else))", True, id="r43-doubled-parens"),
        pytest.param("something\telse", True, id="r44-tab-inside"),
        pytest.param("Something　else", True, id="r45-ideographic-space-nfkc"),
        pytest.param("Something else", True, id="r46-nbsp"),
        pytest.param("Something else.", False, id="r47-trailing-period-is-visible"),
        pytest.param("", False, id="r48-empty"),
        pytest.param(None, False, id="r49-none"),
        pytest.param(42, False, id="r50-int"),
    ])
    def test_reads_as_something_else(self, option, reads):
        assert reads_as_something_else(option) is reads

    @pytest.mark.xfail(strict=True, reason="#3315: " + BUG.format(
        "invisible format chars outside the 7-char _ZERO_WIDTH table (soft hyphen, bidi isolates, "
        "U+2061, U+180E) hide a '(something else)' lookalike from the #3243 guard"))
    @pytest.mark.parametrize("option", [
        pytest.param("Some­thing else", id="r51-soft-hyphen"),
        pytest.param("⁦Something else⁩", id="r52-bidi-isolates"),
        pytest.param("‪Something else‬", id="r53-bidi-embedding"),
        pytest.param("Something⁡ else", id="r54-invisible-function-application"),
        pytest.param("Some᠎thing else", id="r55-mongolian-vowel-separator"),
    ])
    def test_an_invisible_format_char_does_not_hide_the_chip(self, option):
        """REACHABILITY: options are agent-authored on both paths — the file
        poller runs `options_cap_violation` on every new entry
        (`operator_queue_service.py` `_sync_agent`, the #3243 block) and the
        native raise runs it in `ask_service._validated_ask`. The guard's own
        docstring states its scope as "Zero-width / invisible format characters"
        and already strips LRM/RLM (U+200E/F); every char here is the same
        Unicode category (Cf, invisible when rendered), survives NFKC, and is
        not in `_ZERO_WIDTH`. The rendered option is indistinguishable from the
        platform's chip — the two-chips-that-mean-different-things case #3243
        exists to prevent. Cross-script confusables stay out of scope (those
        are visible glyphs); this is not that class. Fix: drop every
        `unicodedata.category(c) == "Cf"` instead of a hand-listed table."""
        assert reads_as_something_else(option) is True


# ===========================================================================
# 3. Answers — usable_options / validate_response_choice (#2376, #3242)
# ===========================================================================

def _approval(options, **over):
    return {"type": "approval", "options": options, **over}


class TestAnswers:
    @pytest.mark.parametrize("item,expected", [
        pytest.param(None, None, id="r56-none-item"),
        pytest.param({"type": "Approval", "options": ["a"]}, None, id="r57-type-is-case-sensitive"),
        pytest.param(_approval(["a", 1, None, "b"]), ["a", "b"], id="r58-non-str-dropped"),
        pytest.param(_approval([1, 2]), None, id="r59-only-non-str"),
        pytest.param(_approval([OPTIONS_DROPPED_MARKER, "x"]), ["x"], id="r60-marker-filtered-from-a-mix"),
        pytest.param(_approval({"a": 1}), None, id="r61-dict-options"),
        pytest.param(_approval("approve"), None, id="r62-string-options"),
        pytest.param(_approval([SOMETHING_ELSE]), [SOMETHING_ELSE], id="r63-literal-kept"),
    ])
    def test_usable_options(self, item, expected):
        assert usable_options(item) == expected

    @pytest.mark.parametrize("item,response,text,raises", [
        pytest.param(_approval(["Approve", "Deny"]), "", None, None, id="r64-empty-response-not-this-rules-question"),
        pytest.param(_approval(["Approve", "Deny"]), None, None, None, id="r65-none-response"),
        pytest.param(_approval(["Approve", "Deny"]), " Approve", None, ResponseNotOfferedError,
                     id="r66-exact-match-no-strip"),
        pytest.param(_approval(["Approve", "Approve"]), "Approve", None, None, id="r67-duplicate-options"),
        pytest.param(_approval(["Approve"]), SOMETHING_ELSE, "​", None,
                     id="r68-zero-width-instruction-counts-as-text"),
        pytest.param(_approval(["Approve"]), SOMETHING_ELSE, "　\n", InstructionRequiredError,
                     id="r69-unicode-whitespace-instruction-is-blank"),
        pytest.param({"type": None, "options": ["a"]}, SOMETHING_ELSE, "do x", ReservedValueError,
                     id="r70-null-type-reserved"),
        pytest.param({"type": "question", "options": ["a"]}, "anything", None, None,
                     id="r71-question-unconstrained"),
        pytest.param(_approval([1, 2]), "anything", None, None, id="r72-non-str-options-unconstrained"),
    ])
    def test_validate_response_choice(self, item, response, text, raises):
        if raises is None:
            validate_response_choice(item, response, response_text=text)
        else:
            with pytest.raises(raises):
                validate_response_choice(item, response, response_text=text)


# ===========================================================================
# 4. The #2915 fingerprint against what the DB really stores
# ===========================================================================

class TestFingerprintRoundTrip:
    """A pending entry the agent did NOT touch must compare as unchanged against
    its own freshly ingested row — otherwise the row goes `changed`, and
    `routers/operator_queue.py:318` (and the portal answer path) refuse every
    answer with a 409 `item_diverged` until the human clicks twice."""

    @pytest.mark.parametrize("over", [
        pytest.param({}, id="r73-plain"),
        pytest.param({"options": None}, id="r74-options-null"),
        pytest.param({"options": ["approve", "reject"]}, id="r75-options-list"),
        pytest.param({"context": {}}, id="r76-context-empty-dict"),
        pytest.param({"context": []}, id="r77-context-list"),
        pytest.param({"context": "free text"}, id="r78-context-string"),
        pytest.param({"title": ""}, id="r79-title-empty"),
        pytest.param({"type": ""}, id="r80-type-empty"),
        pytest.param({"type": 7}, id="r81-type-int"),
        pytest.param({"priority": "HIGH"}, id="r82-priority-wrong-case"),
        pytest.param({"expires_at": "tomorrow"}, id="r83-expires-unparseable"),
        pytest.param({"expires_at": "2026-10-10T10:00:00+05:00"}, id="r84-expires-offset"),
        pytest.param({"expires_at": 5}, id="r85-expires-int"),
        pytest.param({"context": {"workspace_session_id": "forged"}}, id="r86-platform-key-authored"),
        pytest.param({"options": "x" * (oqs.OPERATOR_QUEUE_OPTIONS_MAX_BYTES + 10)}, id="r87-options-over-cap"),
    ])
    def test_an_untouched_entry_is_unchanged(self, real_db, over):
        entry = {"id": "rt-1", "status": "pending", "title": "T", "question": "Q", **over}
        row = _ingest_and_read_back(real_db, entry)
        assert oqs.changed_fields(row, entry) == []

    @pytest.mark.parametrize("options", [
        pytest.param([], id="r88-options-empty-list"),
        pytest.param("", id="r89-options-empty-string"),
    ])
    def test_a_falsy_options_value_is_not_a_rewrite(self, real_db, options):
        """Regression for #3314. `_sync_agent` ingests the entry (clamp keeps `[]` —
        `options is not None` and it is under the byte cap), then
        `db/operator_queue.py:426` stores `json.dumps(options) if options else
        None` → NULL. Next cycle `changed_fields` compares `_row_content`
        (`_canonical_options(None)` = "") with `_entry_content`
        (`_canonical_options([])` = "[]") → `["options"]` → SYNC_CHANGED →
        `REFUSE_RESPONSE_STATES` → `routers/operator_queue.py:318` 409s the
        operator's answer and an `diverged` audit row is written. `"options": []`
        on a question is the natural reading of the schema the platform prompt
        teaches (`platform_prompt_service.py:183` shows the key on every entry).
        Same root cause, lower plausibility: a non-string `title`/`question`
        (stored verbatim, compared as the default) — see the next test."""
        entry = {"id": "rt-2", "type": "question", "status": "pending",
                 "title": "T", "question": "Q", "options": options}
        row = _ingest_and_read_back(real_db, entry)
        assert oqs.changed_fields(row, entry) == []

    @pytest.mark.parametrize("over", [
        pytest.param({"title": 123}, id="r90-title-int"),
        pytest.param({"question": 5}, id="r91-question-int"),
        pytest.param({"title": True}, id="r90b-title-bool"),
    ])
    def test_a_non_string_title_is_not_a_rewrite(self, real_db, over):
        """Regression for #3314. Before the fix `_clamp_ingested_item` only
        truncates `str` titles and passes others through, `_insert_values`
        stores `item.get("title") or "Agent request"` (the int survives), while
        `_entry_content` maps any non-str to None → "Agent request". Lower
        plausibility than r88 (an agent writing a numeric title), same
        consequence."""
        entry = {"id": "rt-3", "status": "pending", "title": "T", "question": "Q", **over}
        row = _ingest_and_read_back(real_db, entry)
        assert oqs.changed_fields(row, entry) == []

    @pytest.mark.xfail(strict=True, reason="#3315: " + BUG.format(
        "an explicit `\"type\": null` entry fails the NOT NULL insert every cycle and is "
        "quarantined — the ask never reaches anyone and the agent is never told"))
    def test_a_null_type_is_ingested_as_a_question(self, real_db):
        """r92. REACHABILITY: `_sync_agent` → `_clamp_ingested_item` (does not
        default `type`) → `db/operator_queue.py:469` `type=item.get("type",
        "question")` — `.get` with a default returns None for a PRESENT null →
        `operator_queue.type` is NOT NULL → IntegrityError → the #1525
        quarantine after MAX_CREATE_ATTEMPTS. No `platform.ingestion` hold is
        written (the failure is not a cap), so the agent sees its entry sitting
        `pending` forever. The native path already defaults it
        (`ask_service._validated_ask`: `ask.get("type") or "question"`), and
        #1426 fixed exactly this class for the other hard-indexed fields."""
        entry = {"id": "rt-4", "type": None, "status": "pending", "title": "T", "question": "Q"}
        row = _ingest_and_read_back(real_db, entry)
        assert row["type"] == "question"


# ===========================================================================
# 5. Ingestion marker (#3130 / #3243)
# ===========================================================================

class TestIngestionMarker:
    @pytest.mark.parametrize("current,new,expected", [
        pytest.param(None, "invalid_title", "invalid_title", id="r93-first-reason"),
        pytest.param("invalid_options", "invalid_title", "invalid_options", id="r94-equal-rank-keeps-current"),
        pytest.param("invalid_id", "invalid_options", "invalid_id", id="r95-weaker-never-replaces"),
        pytest.param("rate_limited", "queue_full", "queue_full", id="r96-stronger-wins"),
    ])
    def test_stronger_hold(self, current, new, expected):
        assert oqs._stronger_hold(current, new) == expected

    def test_marker_names_at_most_ten_ids(self):
        """r97: the id list is bounded so an agent with 1000 held entries cannot
        grow the file the platform writes back."""
        m = oqs._ingestion_marker("invalid_options", [f"id{i}" for i in range(25)])
        assert m["invalid_options"] == [f"id{i}" for i in range(10)]

    def test_a_reasonless_marker_is_none_even_with_ids(self):
        """r98: None reason means "nothing held" and wins over stray id lists."""
        assert oqs._ingestion_marker(None, ["a"], ["b"]) is None

    def test_removing_the_last_key_drops_the_platform_block(self):
        """r99: `wanted=None` on a file whose `platform` block holds ONLY the
        marker removes the whole block (no empty `platform: {}` left behind)."""
        data = {"requests": [], "platform": {"ingestion": {"reason": "queue_full"}}}
        assert oqs._apply_ingestion_marker(data, None, "now") is True
        assert "platform" not in data

    def test_removing_the_marker_keeps_other_platform_keys(self):
        """r100"""
        data = {"requests": [], "platform": {"ingestion": {"reason": "x"}, "other": 1}}
        assert oqs._apply_ingestion_marker(data, None, "now") is True
        assert data["platform"] == {"other": 1}

    def test_a_non_dict_platform_block_is_replaced(self):
        """r101: an agent-written `platform: "junk"` is data only — overwritten."""
        data = {"requests": [], "platform": "junk"}
        wanted = oqs._ingestion_marker("queue_full")
        assert oqs._apply_ingestion_marker(data, wanted, "T0") is True
        assert data["platform"]["ingestion"] == {**wanted, "since": "T0"}

    def test_since_restarts_when_the_reason_changes(self):
        """r102"""
        data = {"requests": [], "platform": {"ingestion": {
            **oqs._ingestion_marker("rate_limited"), "since": "T0"}}}
        oqs._apply_ingestion_marker(data, oqs._ingestion_marker("queue_full"), "T1")
        assert data["platform"]["ingestion"]["since"] == "T1"

    def test_a_non_string_since_is_restamped(self):
        """r103: an agent-forged `since: 0` under the same reason is not kept."""
        wanted = oqs._ingestion_marker("queue_full", ["a"])
        data = {"requests": [], "platform": {"ingestion": {"reason": "queue_full", "since": 0}}}
        oqs._apply_ingestion_marker(data, wanted, "T1")
        assert data["platform"]["ingestion"]["since"] == "T1"

    def test_no_marker_wanted_and_none_present_is_no_write(self):
        """r104"""
        assert oqs._apply_ingestion_marker({"requests": []}, None, "now") is False


# ===========================================================================
# 6. Status / TTL predicates
# ===========================================================================

class TestTerminalFlip:
    @pytest.mark.parametrize("row,awaits", [
        pytest.param({"status": "cancelled"}, True, id="r105-never-attempted"),
        pytest.param({"status": "expired", "delivery_state": "undelivered"}, True, id="r106-retry"),
        pytest.param({"status": "expired", "delivery_state": "undelivered",
                      "delivery_detail": "entry_missing"}, False, id="r107-entry-missing-never-retried"),
        pytest.param({"status": "cancelled", "delivery_state": "delivered"}, False, id="r108-landed"),
        pytest.param({"status": "cancelled", "delivery_state": "not_applicable"}, False, id="r109-n-a"),
        pytest.param({"status": "acknowledged"}, False, id="r110-not-a-flipped-status"),
        pytest.param({"status": "responded"}, False, id="r111-responded"),
        pytest.param({"status": "Cancelled"}, False, id="r112-status-case-sensitive"),
        pytest.param({"status": "expired", "delivery_detail": None}, True, id="r113-null-detail"),
    ])
    def test_awaits_terminal_flip(self, row, awaits):
        assert oqs.awaits_terminal_flip(row) is awaits


class TestAging:
    NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("created,hours,aged", [
        pytest.param("2026-10-05T12:00:00Z", 24, True, id="r114-exactly-at-the-bound-is-aged"),
        pytest.param("2026-10-05T12:00:01Z", 24, False, id="r115-one-second-short"),
        pytest.param("2026-10-05T12:00:00", 24, True, id="r116-naive-read-as-utc"),
        pytest.param("2026-10-05T14:00:00+02:00", 24, True, id="r117-offset-normalised"),
        pytest.param("2026-10-05 12:00:00", 24, True, id="r118-space-separator"),
        pytest.param("garbage", 24, False, id="r119-unparseable-never-aged"),
        pytest.param("", 24, False, id="r120-empty-created"),
        pytest.param("2026-10-01T00:00:00Z", -5, False, id="r121-negative-bound-disabled"),
        pytest.param("2026-10-01T00:00:00Z", 10 ** 12, False, id="r122-overflowing-bound-not-a-crash"),
    ])
    def test_is_aged(self, created, hours, aged):
        assert oqs.is_aged({"status": "pending", "created_at": created}, hours, self.NOW) is aged

    @pytest.mark.parametrize("raw,expected", [
        pytest.param("0", 0, id="r123-zero-disables"),
        pytest.param("-3", 0, id="r124-negative-clamped-to-disabled"),
        pytest.param(" 48 ", 48, id="r125-padded"),
        pytest.param("1.5", oqs.OPERATOR_QUEUE_AGING_HOURS_DEFAULT, id="r126-float-falls-back"),
        pytest.param("abc", oqs.OPERATOR_QUEUE_AGING_HOURS_DEFAULT, id="r127-garbage-falls-back"),
    ])
    def test_aging_hours(self, monkeypatch, raw, expected):
        monkeypatch.setattr(oqs.db, "get_setting_value", lambda k, d: raw)
        assert oqs.aging_hours() == expected

    def test_aging_hours_survives_a_db_raise(self, monkeypatch):
        """r128"""
        def boom(*a):
            raise RuntimeError("db down")
        monkeypatch.setattr(oqs.db, "get_setting_value", boom)
        assert oqs.aging_hours() == oqs.OPERATOR_QUEUE_AGING_HOURS_DEFAULT


# ===========================================================================
# 7. Fold helpers — closed vocabulary for durable, operator-visible columns
# ===========================================================================

class TestFolds:
    @pytest.mark.parametrize("token,out", [
        pytest.param("title,options", "title,options", id="r129-field-list"),
        pytest.param("Title", "title", id="r130-lowered"),
        pytest.param("a" * 97, "other", id="r131-over-96"),
        pytest.param("a" * 96, "a" * 96, id="r132-at-96"),
        pytest.param("x\ny", "other", id="r133-inner-newline"),
        pytest.param("ok\n", "ok", id="r134-trailing-newline-stripped-first"),
        pytest.param(None, "other", id="r135-none"),
        pytest.param(0, "other", id="r136-zero"),
    ])
    def test_detail(self, token, out):
        assert oqs._detail(token) == out

    @pytest.mark.parametrize("result,out", [
        pytest.param({"status_code": 503}, "http_503", id="r137-status"),
        pytest.param({"error": "ReadTimeout"}, "timeout", id="r138-timeout-class"),
        pytest.param({"error": "ConnectError"}, "unreachable", id="r139-other"),
        pytest.param(None, "unreachable", id="r140-not-a-dict"),
        pytest.param({"status_code": "503"}, "unreachable", id="r141-string-code-not-echoed"),
    ])
    def test_read_failure_detail(self, result, out):
        assert oqs._read_failure_detail(result) == out

    @pytest.mark.parametrize("value", [
        pytest.param({1, 2}, id="r142-set-unserialisable"),
        pytest.param([object()], id="r143-object-unserialisable"),
    ])
    def test_unserialisable_options_compare_as_the_dropped_marker(self, value):
        assert oqs._canonical_options(value) == oqs._canonical_options([OPTIONS_DROPPED_MARKER])
        assert oqs._entry_content({"options": value})["options"] == \
            oqs._canonical_options([OPTIONS_DROPPED_MARKER])

    def test_unserialisable_context_compares_as_truncated(self):
        """r144: the clamp turns it into a `_truncated` marker; the entry side
        must land on the same sentinel."""
        assert oqs._comparable_context({"k": {1, 2}}) == oqs._CONTEXT_TRUNCATED_SENTINEL
        clamped = oqs._clamp_ingested_item({"id": "x", "context": {"k": {1, 2}}})
        assert oqs._comparable_context(clamped["context"]) == oqs._CONTEXT_TRUNCATED_SENTINEL


# ===========================================================================
# 8. Unhashable agent-authored values reach `in <set/dict>` unguarded
# ===========================================================================

class TestUnhashableEntryValues:
    """The file is free-form agent JSON, so any field can be a list or an
    object. Two membership tests are reached with such a value and no
    isinstance guard: `_VALID_PRIORITIES` (a set) and `open_by_rid` (a dict)."""

    @staticmethod
    def _harness():
        if str(Path(__file__).resolve().parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_2915_operator_queue_sync_honesty as h
        return h

    @pytest.mark.parametrize("priority", [
        pytest.param(["high"], id="r145-priority-list"),
        pytest.param({"level": "high"}, id="r146-priority-object"),
    ])
    def test_the_clamp_never_raises_on_an_unhashable_priority(self, priority):
        """REACHABILITY: `_sync_agent` calls the clamp on every new pending
        entry (`clamped = _clamp_ingested_item(req, agent_name)`); the line
        `if out.get("priority") not in _VALID_PRIORITIES` raises TypeError for
        a list/dict. The surrounding try counts it as a create failure → #1525
        quarantine after 3 cycles; no `platform.ingestion` hold tells the agent.
        The clamp docstring promises "written to NEVER raise (every branch is
        isinstance-guarded)" and test_1632's hostile-shapes test never tried a
        non-scalar priority."""
        out = oqs._clamp_ingested_item({"id": "u-1", "status": "pending", "priority": priority})
        assert out["priority"] == "medium"

    def test_an_unhashable_id_does_not_stop_the_agents_answers(self, monkeypatch):
        """r147. REACHABILITY: in `_sync_agent`'s loop the reserved-prefix,
        native, seen and terminal checks are each `isinstance(req_id, str)`-
        guarded, but `if req_id in open_by_rid:` is not — `{"id": ["x"]}`
        reaches it (truthy id, default status `pending`) and raises
        `TypeError: unhashable type`. The exception leaves `_sync_agent` before
        step 4 (the write-back), and `_poll_cycle` runs it under
        `asyncio.gather(..., return_exceptions=True)`, which discards it without
        a log line. Net: an answered approval for this agent is never written
        into its file, every 5 s, with nothing in the logs. The shape check
        that WOULD hold this entry (`invalid_id`) sits later in the loop."""
        h = self._harness()
        resp = h._row(status="responded")
        db = h._fake_db(open_rows=[resp], responded=[resp])
        client = h._client(h._file({"id": ["x"], "status": "pending"}, h._entry()))
        svc, _ = h._wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_awaited_once()

    def test_an_unhashable_priority_rewrite_is_a_change_not_a_crash(self, monkeypatch):
        """r148. REACHABILITY: for an entry whose id matches an open pending row,
        `_sync_agent` calls `changed_fields(row, req)` outside any try;
        `_entry_content` → `_comparable_priority(["high"])` →
        `value in _VALID_PRIORITIES` raises TypeError. Same silent
        `gather(return_exceptions=True)` swallow as r147 — and here the trigger
        is an agent EDITING an ask it already filed, the exact case #2915 exists
        to report as `changed`."""
        h = self._harness()
        pending = h._row(rid="req-p", status="pending")
        resp = h._row(rid="req-1", status="responded")
        db = h._fake_db(open_rows=[pending, resp], responded=[resp])
        client = h._client(h._file(h._entry(rid="req-p", priority=["high"]), h._entry()))
        svc, _ = h._wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_awaited_once()

    def test_an_unhashable_status_is_folded_not_a_crash(self, monkeypatch):
        """r149 (passes): a list `status` on an ingested entry is folded to
        `other` and recorded closed_by_filer — the guard that r147/r148 lack."""
        h = self._harness()
        pending = h._row(rid="req-p", status="pending")
        db = h._fake_db(open_rows=[pending])
        client = h._client(h._file(h._entry(rid="req-p", status=["x"])))
        svc, _ = h._wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert ("closed_by_filer", "other") in [
            (c.args[1], c.args[2]) for c in db.set_operator_queue_sync_state.call_args_list]
