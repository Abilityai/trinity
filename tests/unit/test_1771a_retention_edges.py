"""#1771 target 1 — edge cases for the retention blast-radius guard (/edge-cases, P-38).

Companion to `test_1644_retention_guard.py`, which pins the guard's headline
properties (predicate parity, fail-closed). This file works the BOUNDARIES and the
surfaces #1644's suite executes but never asserts:

* exactly-at-threshold decisions (`<=` at `retention_guard.py:183`) — the existing
  suite only probes 12-vs-1000 and 1001-vs-1000, so the one comparison that decides
  whether a prune runs is never tested at its own boundary;
* the refusal-episode transition memo, the alarm's log LEVEL, its COUNT, and its
  PAYLOAD — `announce_refusal`/`note_allowed`/`_alarm_id` all run today
  (`test_1644:177`, `test_cleanup_inner_sweeps:105`) but nothing asserts what they
  did, which is precisely the state in which a mutant survives;
* `_read_retention_settings`, whose body never executes anywhere in the suite —
  all 8 references are `patch.object(_CS, "_read_retention_settings", ...)`;
* the per-sweep floors, which `MAX_ROWS_PER_SWEEP`'s direction test does NOT cover
  (`retention_guard.py:170` is `MAX_ROWS_PER_SWEEP if floor is None else floor` —
  there is no `min()`, so a floor OVERRIDES the global rather than being bounded by
  it, and raising `FLOOR_SCHEDULES` would disarm that sweep with every test green).

HOUSE RULE THIS FILE OBEYS (docs/memory/learnings.md:101-103)
------------------------------------------------------------
#1638 shipped green because a test asserted the DESTRUCTIVE VALUE as the
requirement (`assert OPS_SETTINGS_DEFAULTS[key] == "5"`). Nothing here pins a
retention value or a threshold value. Assertions pin DIRECTIONS
(`FLOOR_SCHEDULES <= MAX_ROWS_PER_SWEEP`) and STRUCTURAL INVARIANTS (every
`RETENTION_OPS_KEYS` window has a `_guard_allows` call site) — never a number that
is safe today and catastrophic tomorrow.

Invocation: `cd tests && python -m pytest unit/test_1771a_retention_edges.py`.
`tests/unit/pytest.ini` wins ini-discovery, so rootdir is `tests/unit` and the root
`pyproject.toml` `pythonpath`/`markers` do NOT apply; `tests/` reaches `sys.path`
only via the `cd tests` + `python -m pytest` form.
"""

import ast
import logging
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

import services.retention_guard as _RG  # noqa: E402
from services import cleanup_service as _CS  # noqa: E402
from utils.helpers import iso_cutoff  # noqa: E402

_SRC_CLEANUP = _BACKEND / "services" / "cleanup_service.py"


def _days_ago_iso(days: int) -> str:
    return iso_cutoff(hours=days * 24)


# ---------------------------------------------------------------------------
# Doubles + isolation
# ---------------------------------------------------------------------------


class _DbDouble:
    """In-memory stand-in for the four `db` methods this subsystem touches.

    Verified signatures: `get_setting_value(key, default=None)`, `set_setting(key,
    value)`, `delete_setting(key)` (database.py:1705-1711) and
    `create_operator_queue_item(agent_name, item)` (database.py:2513).

    A dict double rather than a real DB because these tests assert the exact alarm
    PAYLOAD and the exact alarm COUNT, which a real queue table would only let us
    observe indirectly.
    """

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.queue_items = []
        self.raise_on_get = False
        self.raise_on_create = False

    def get_setting_value(self, key, default=None):
        if self.raise_on_get:
            raise RuntimeError("settings read exploded")
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def create_operator_queue_item(self, agent_name, item):
        if self.raise_on_create:
            raise RuntimeError("operator queue is down")
        self.queue_items.append((agent_name, item))


@pytest.fixture(autouse=True)
def _reset_transition_memo():
    """`retention_guard._refusal_episodes` is module state that outlives every test.

    `test_1644` already refuses on "execution_row_retention_days" and
    `test_cleanup_inner_sweeps` pops keys via `note_allowed`. CI runs
    `pytest-randomly` under three seeds (backend-unit-test.yml:99,117), so without
    this reset a "first refusal fires ERROR + exactly one alarm" assertion silently
    degrades into the repeat-refusal case whenever another file ran first — a test
    that passes for the wrong reason.

    #1834 renamed the state (`_last_refused` -> `_refusal_episodes`, now a record
    per episode rather than a window int) and published `reset_transition_memo()`
    precisely so the suites stop reaching for a private global by name. An episode
    now carries a `_clock()` stamp, so a leaked one is no longer merely a wrong log
    level — it can put a later test's first attempt on the escalation branch.
    """
    _RG.reset_transition_memo()
    yield
    _RG.reset_transition_memo()


@pytest.fixture
def guard_db(monkeypatch):
    """Patch `db` BY OBJECT on the guard module.

    `retention_guard.py:56` does `from database import db`, binding its own
    module-global — patching `database.db` is inert here (learnings.md:99, and the
    same note at test_cleanup_inner_sweeps.py:32-35).
    """
    double = _DbDouble()
    monkeypatch.setattr(_RG, "db", double)
    return double


@pytest.fixture
def cleanup_db(monkeypatch):
    """Same, for `cleanup_service`'s own `db` binding."""
    double = _DbDouble()
    monkeypatch.setattr(_CS, "db", double)
    return double


# ---------------------------------------------------------------------------
# A — evaluate() decision boundaries
# ---------------------------------------------------------------------------


class TestEvaluateBoundaries:
    """The `candidates <= threshold` comparison decides whether data dies."""

    @pytest.mark.parametrize(
        "row,floor,available,expect_allowed,expect_reason",
        [
            # A3/A5: exactly AT the threshold passes — `<=`, not `<`.
            (
                "A5-rows-at-threshold",
                None,
                _RG.MAX_ROWS_PER_SWEEP,
                True,
                "under_threshold",
            ),
            (
                "A3-schedules-at-floor",
                _RG.FLOOR_SCHEDULES,
                _RG.FLOOR_SCHEDULES,
                True,
                "under_threshold",
            ),
            # A6: one below / one above, both floors.
            (
                "A6-rows-below",
                None,
                _RG.MAX_ROWS_PER_SWEEP - 1,
                True,
                "under_threshold",
            ),
            (
                "A4-rows-above",
                None,
                _RG.MAX_ROWS_PER_SWEEP + 1,
                False,
                "over_threshold",
            ),
            (
                "A6-schedules-below",
                _RG.FLOOR_SCHEDULES,
                _RG.FLOOR_SCHEDULES - 1,
                True,
                "under_threshold",
            ),
            (
                "A4-schedules-above",
                _RG.FLOOR_SCHEDULES,
                _RG.FLOOR_SCHEDULES + 1,
                False,
                "over_threshold",
            ),
            # A2: floor=0 must NOT fall back to MAX_ROWS_PER_SWEEP (`is None`, not falsy).
            ("A2-agents-at-zero", _RG.FLOOR_AGENTS, 0, True, "under_threshold"),
            ("A2-agents-one-candidate", _RG.FLOOR_AGENTS, 1, False, "over_threshold"),
        ],
    )
    def test_threshold_boundary(
        self, row, floor, available, expect_allowed, expect_reason, guard_db
    ):
        """`available` is what a real bounded count would see; count_fn is
        limit-honouring (`min`), exactly like `_bounded_count`."""
        v = _RG.evaluate(
            "execution_row_retention_days",
            90,
            lambda limit: min(available, limit),
            floor=floor,
        )
        assert (v.allowed, v.reason) == (expect_allowed, expect_reason), row

    def test_count_fn_receives_exactly_threshold_plus_one(self, guard_db):
        """A14: the bound must be `threshold + 1` — with `threshold`, a candidate
        set of exactly `threshold + 1` would count as `threshold` and slip through
        the `<=` as 'under threshold'."""
        seen = []

        def counting(limit):
            seen.append(limit)
            return 0

        _RG.evaluate("execution_row_retention_days", 90, counting, floor=7)
        assert seen == [8]

    @pytest.mark.parametrize("count", [-1, -5, -(10**9)])
    def test_negative_candidate_count_refuses(self, count, guard_db):
        """A11 — REWRITTEN BY #1833, deliberately and visibly.

        This test used to pin `(True, "under_threshold")`: a negative return is
        `<= threshold`, so it proceeded. Its own docstring named the hazard — "`-1`
        is also the sentinel `count_failed` uses, and only the REASON
        distinguishes them" — and pinned the behaviour as observed-not-endorsed so
        that changing it would be a deliberate, visible edit. This is that edit.

        It was a genuine fail-OPEN inside a fail-closed guard: a `count_fn` that
        reported failure with this module's own "unknown" idiom AUTHORISED the
        prune, and the count does not bound the delete (`prune_execution_logs`
        takes no count and drains fully), so the result is #1638 replayed.
        """
        v = _RG.evaluate("execution_row_retention_days", 90, lambda limit: count)
        assert (v.allowed, v.reason) == (False, "count_negative")
        assert v.candidates == -1, "unknown is reported as the -1 sentinel"

    def test_nan_count_without_ack_refuses(self, guard_db):
        """A10: `nan <= threshold` is False, so a NaN count falls through to the
        ack path and refuses — the safe direction, but by accident of IEEE-754
        rather than by an explicit check."""
        v = _RG.evaluate("execution_row_retention_days", 90, lambda limit: float("nan"))
        assert (v.allowed, v.reason) == (False, "over_threshold")

    def test_nan_count_with_ack_is_allowed(self, guard_db):
        """A10b: the NaN fail-safe is NOT unconditional. With an ack on file the
        same NaN yields allowed=True. Pinned so nobody reads the test above as
        'NaN always refuses'."""
        _RG.record_acknowledgement("execution_row_retention_days", 90)
        v = _RG.evaluate("execution_row_retention_days", 90, lambda limit: float("nan"))
        assert v.allowed is True
        assert v.reason == "acknowledged"

    def test_negative_floor_refuses_regardless_of_data(self, guard_db):
        """A17: no caller passes a negative floor; pin the fail-safe direction in
        case one ever does. threshold=-1 makes the bound 0, the accessors
        short-circuit on `limit <= 0` and return 0, and `0 <= -1` is False — so it
        refuses on an EMPTY table. Refusing too much is the correct direction."""
        v = _RG.evaluate(
            "execution_row_retention_days",
            90,
            lambda limit: 0 if limit <= 0 else 1,
            floor=-1,
        )
        assert v.allowed is False

    def test_zero_candidates_never_trips_any_floor(self, guard_db):
        """A7: an empty candidate set must pass at every floor — otherwise a
        healthy install refuses forever and the alarm becomes noise."""
        for floor in (None, _RG.FLOOR_AGENTS, _RG.FLOOR_SCHEDULES):
            v = _RG.evaluate(
                "execution_row_retention_days", 90, lambda limit: 0, floor=floor
            )
            assert v.allowed is True, f"floor={floor}"


class TestEvaluateRaisesContract:
    """`evaluate` does not raise, and fails CLOSED. #1833 made that true of the
    code rather than only of the docstring."""

    def test_non_numeric_count_refuses_instead_of_raising(self, guard_db):
        """A9 — REWRITTEN BY #1833, deliberately and visibly.

        This test used to pin `pytest.raises(TypeError)`: `candidates <= threshold`
        sat OUTSIDE the try that wraps `count_fn`, so a `count_fn` returning a
        non-number raised straight out of `evaluate`, contradicting its own
        docstring and the `cleanup_service._guard_allows` comment that leans on it.
        It was pinned as OBSERVED-not-endorsed so that changing it would be a
        deliberate, visible edit. This is that edit.

        The raise never caused a destructive pass (every call site invokes the
        guard before its `db.prune_*` and inside a try) — it destroyed the SIGNAL:
        `announce_refusal` was never reached, so there was no operator-queue alarm
        and no "REFUSED" ERROR, only a nondescript "Error pruning ...". At the
        second, UNWRAPPED caller (GET /api/settings/retention) it was a 500 on the
        panel an operator uses to approve the very prune that refused.
        """
        v = _RG.evaluate("execution_row_retention_days", 90, lambda limit: None)
        assert (v.allowed, v.reason) == (False, "count_uninterpretable")

    def test_numeric_returns_never_raise(self, guard_db):
        """The half of the contract that IS true, pinned as the real guarantee."""
        for value in (0, 1, -1, 10**12, 0.5, float("inf"), float("nan")):
            _RG.evaluate("execution_row_retention_days", 90, lambda limit: value)


# ---------------------------------------------------------------------------
# B — ack lifecycle
# ---------------------------------------------------------------------------


class TestAckLifecycle:

    def test_consume_without_an_ack_is_a_no_op(self, guard_db):
        """B5: the common path. Every under-threshold sweep calls this."""
        _RG.consume_acknowledgement("execution_row_retention_days")
        assert guard_db.settings == {}

    def test_stored_value_is_stripped_before_comparison(self, guard_db):
        """B6: `is_acknowledged` does `str(raw).strip()`, so a value written with
        stray whitespace still matches."""
        guard_db.settings["retention_ack_execution_row_retention_days"] = "  30  "
        assert _RG.is_acknowledged("execution_row_retention_days", 30) is True

    @pytest.mark.parametrize("stored", ["030", "+30", "30.0", "3 0", "", "thirty"])
    def test_non_canonical_stored_values_do_not_acknowledge(self, stored, guard_db):
        """B7: the comparison is on the STRING, so only the canonical `str(int)`
        form authorizes. Fail-safe: an unparseable ack must never approve a mass
        delete."""
        guard_db.settings["retention_ack_execution_row_retention_days"] = stored
        assert _RG.is_acknowledged("execution_row_retention_days", 30) is False

    def test_ack_key_is_namespaced_under_the_blocklisted_prefix(self, guard_db):
        """The prefix is what `PUT /api/settings/{key}` blocklists; an ack written
        outside it would be self-writable through the catch-all."""
        _RG.record_acknowledgement("execution_row_retention_days", 5)
        assert list(guard_db.settings) == [
            f"{_RG.ACK_KEY_PREFIX}execution_row_retention_days"
        ]


# ---------------------------------------------------------------------------
# C — announce_refusal / _refusal_episodes / note_allowed / _alarm_id
# ---------------------------------------------------------------------------


def _verdict(candidates=5000, threshold=1000, reason="over_threshold"):
    return _RG.GuardVerdict(False, candidates, threshold, reason)


class TestRefusalAlarm:
    """These functions execute today; nothing asserts what they DID."""

    def test_first_refusal_logs_error_and_raises_exactly_one_alarm(
        self, guard_db, caplog
    ):
        """C1. 'Exactly one' is the point: `create_item` is INSERT ... ON CONFLICT
        DO NOTHING, so a duplicate is silent — only a count proves the transition
        gate works."""
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 1
        levels = [r.levelno for r in caplog.records]
        assert logging.ERROR in levels

    def test_repeat_refusal_drops_to_info_and_raises_no_second_alarm(
        self, guard_db, caplog
    ):
        """C2. 288 identical ERRORs a day is how an alert gets muted — and a muted
        alert is the #1638 failure mode repeated."""
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 1, "the repeat must not re-alarm"
        assert [r.levelno for r in caplog.records] == [logging.INFO]

    def test_a_narrowed_window_is_a_fresh_transition(self, guard_db, caplog):
        """C3. The memo is keyed by (setting, window). A window narrowing 30 -> 5
        is a NEW blast radius and must re-alarm even though the key is unchanged —
        that narrowing IS the #1638 event."""
        _RG.announce_refusal("execution_row_retention_days", "rows", 30, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 2
        assert logging.ERROR in [r.levelno for r in caplog.records]

    def test_note_allowed_rearms_the_error_level(self, guard_db, caplog):
        """C4. A sweep that recovers and then degrades again must shout again."""
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _RG.note_allowed("execution_row_retention_days")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 2
        assert logging.ERROR in [r.levelno for r in caplog.records]

    def test_note_allowed_only_clears_its_own_key(self, guard_db, caplog):
        """A recovering sweep must not re-arm an unrelated sweep's alarm."""
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _RG.announce_refusal("health_check_retention_days", "health", 5, _verdict())
        _RG.note_allowed("execution_row_retention_days")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal("health_check_retention_days", "health", 5, _verdict())

        assert (
            len(guard_db.queue_items) == 2
        ), "health was still refusing — no new alarm"

    def test_settings_read_failure_degrades_source_and_never_propagates(self, guard_db):
        """C7: `announce_refusal` is decorative and must never raise into a caller
        that has already refused."""
        guard_db.raise_on_get = True
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

        _agent, item = guard_db.queue_items[0]
        assert item["context"]["window_source"] == "unknown"

    def test_alarm_failure_does_not_propagate(self, guard_db):
        """C5, restated at the alarm surface (test_1644:170 asserts only that the
        verdict object is unchanged, which it trivially is — it is frozen)."""
        guard_db.raise_on_create = True
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())

    def test_alarm_id_is_the_natural_key_and_expiry_is_null(self, guard_db):
        """C9 + C10. `expires_at` must stay None: `mark_operator_queue_expired`
        flips any pending row past `expires_at` to `expired` fleet-wide every 5s,
        which would silently retire the alarm.

        The id is compared against a LITERAL format, not against `_alarm_id()`
        itself. Asserting `item["id"] == _alarm_id(...)` is a tautology — both
        sides move together, so it survives any change to the id's shape. The
        mutation gate caught exactly that: mutating the `"retention-guard-"`
        prefix left the original assertion green. The format is load-bearing:
        `create_item` is INSERT ... ON CONFLICT DO NOTHING on `id`, so the natural
        key is what makes re-emission idempotent (one alarm per setting+window).
        """
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _agent, item = guard_db.queue_items[0]

        assert item["id"] == "retention-guard-execution_row_retention_days-5"
        assert item["expires_at"] is None

    def test_alarm_is_raised_as_a_critical_alert(self, guard_db):
        """The alarm's ROUTING and VISIBILITY fields, not just its key set.

        `priority` decides how loudly a refused mass-deletion surfaces to an
        operator; downgrading it to `low` is a silent re-run of the #1638
        failure mode (the signal exists but nobody sees it). `type` decides
        which queue surface renders it, and `alert_type` is the discriminator
        consumers filter on. The C8 payload test pins the key SET; these are the
        VALUES, which the mutation gate showed were unasserted.
        """
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _agent, item = guard_db.queue_items[0]

        assert item["type"] == "alert"
        assert item["priority"] == "critical"
        assert item["context"]["alert_type"] == "retention_blast_radius"
        assert item["context"]["setting_key"] == "execution_row_retention_days"
        assert item["context"]["window_days"] == 5

    def test_window_source_distinguishes_a_db_row_from_a_code_default(self, guard_db):
        """The provenance field whose ABSENCE made #1638 invisible.

        A `code-default` window is one a future edit to `OPS_SETTINGS_DEFAULTS`
        can move under the operator's feet; a `db-row` window is one they chose.
        Reporting the wrong one on a refusal alarm points the investigation at
        the wrong cause. Both branches asserted — the mutation gate showed only
        the `db-row` side was covered.
        """
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _agent, item = guard_db.queue_items[0]
        assert (
            item["context"]["window_source"] == "code-default"
        ), "no setting row exists, so the window came from the code default"

        guard_db.settings["health_check_retention_days"] = "7"
        _RG.announce_refusal("health_check_retention_days", "health", 7, _verdict())
        _agent, item = guard_db.queue_items[1]
        assert item["context"]["window_source"] == "db-row"


class TestVerdictImmutability:

    def test_guard_verdict_cannot_be_mutated_after_the_decision(self):
        """`GuardVerdict` is a FROZEN dataclass, and that is a safety property.

        The verdict is handed to `announce_refusal` and returned through
        `_guard_allows` to the sweep that is about to delete rows. If it were
        mutable, any of those hops — or a future one — could flip `allowed` from
        False to True after the decision was made, turning a refusal into a
        prune with no re-evaluation. `test_1644:170` asserts `v.allowed is False`
        after a failed alarm, which only proves anything BECAUSE the object is
        frozen; the mutation gate showed `frozen=True` -> `frozen=False` survived
        the whole suite.
        """
        import dataclasses

        v = _RG.GuardVerdict(False, 5000, 1000, "over_threshold")
        with pytest.raises(dataclasses.FrozenInstanceError):
            v.allowed = True
        assert v.allowed is False

    def test_alarm_hosts_on_the_uncreatable_sentinel_agent(self, guard_db):
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        agent, _item = guard_db.queue_items[0]
        assert agent == _RG.ALARM_AGENT_NAME

    def test_alarm_payload_carries_counts_and_identifiers_only(self, guard_db):
        """C8 — SECURITY. The queue row is durable and operator-visible, and
        `schedule_executions.message`/`response`/`error`/`backlog_metadata` hold
        user content and credential-bearing agent output (canary G-04 exists
        because that blob leaked secrets into exactly this kind of state).

        Allowlists the WHOLE item, not just `context`: `question` embeds the
        free-text message built at `retention_guard.py:255-260`, so a
        context-only assertion would false-green on a leak through the prose.
        Any future `"sample_rows"` / `"examples"` key fails this test.
        """
        guard_db.settings["execution_row_retention_days"] = "SENTINEL-WINDOW-VALUE"
        _RG.announce_refusal("execution_row_retention_days", "rows", 5, _verdict())
        _agent, item = guard_db.queue_items[0]

        assert set(item) == {
            "id",
            "type",
            "priority",
            "title",
            "question",
            "context",
            "expires_at",
        }
        assert set(item["context"]) == {
            "alert_type",
            "setting_key",
            "window_days",
            "window_source",
            "candidate_count",
            "threshold",
            "reason",
        }
        # No nested containers anywhere in `context` — a row payload could only
        # arrive as a list/dict of rows.
        for key, value in item["context"].items():
            assert isinstance(value, (str, int, type(None))), key

        # The setting's stored VALUE must never be echoed; only its provenance
        # ("db-row" / "code-default") may be. A mutant that reported the raw value
        # instead of the derived source leaks operator config into a durable row.
        flat = repr(item)
        assert "SENTINEL-WINDOW-VALUE" not in flat
        assert item["context"]["window_source"] == "db-row"


# ---------------------------------------------------------------------------
# D — _read_retention_settings coercion (body never executes elsewhere)
# ---------------------------------------------------------------------------


class TestRetentionSettingsCoercion:

    @pytest.mark.parametrize(
        "row,raw,expected",
        [
            ("D1-plain-int-string", "30", 30),
            ("D1-zero", "0", 0),
            ("D6-surrounding-space", " 7 ", 7),
            ("D2-alpha", "abc", 0),
            ("D2-empty", "", 0),
            ("D2-blank", "   ", 0),
            ("D2-float-string", "1.5", 0),
            ("D2-scientific", "1e5", 0),
            ("D2-hex", "0x10", 0),
            ("D3-none", None, 0),
            ("D4-negative", "-5", 0),
            ("D4-negative-large", "-999999", 0),
        ],
    )
    def test_raw_value_coercion(self, row, raw, expected, cleanup_db, monkeypatch):
        """Invalid or negative -> 0 (sweep disabled). The failure direction is
        'keep everything', which is the correct one: a malformed setting must
        never enable an unbounded prune."""
        monkeypatch.setattr(
            cleanup_db, "get_setting_value", lambda key, default=None: raw
        )
        assert _CS._read_retention_settings() == (expected,) * 4, row

    def test_returns_the_four_windows_in_a_fixed_order(self, cleanup_db, monkeypatch):
        """The caller unpacks positionally
        (`log_days, row_days, hc_days, reports_days`) — a reordering would swap
        two retention windows silently."""
        values = {
            "execution_log_retention_days": "11",
            "execution_row_retention_days": "22",
            "health_check_retention_days": "33",
            "agent_reports_retention_days": "44",
        }
        monkeypatch.setattr(
            cleanup_db,
            "get_setting_value",
            lambda key, default=None: values.get(key, default),
        )
        assert _CS._read_retention_settings() == (11, 22, 33, 44)

    def test_a_missing_row_falls_back_to_that_keys_code_default(
        self, cleanup_db, monkeypatch
    ):
        """The un-configured install — the DEFAULT state, and #1638's blast radius.

        Every other test in this class overrides `get_setting_value` to IGNORE the
        `default` it is handed, so nothing asserted where that default comes from.
        The reader passes `OPS_SETTINGS_DEFAULTS.get(key, "0")`
        (cleanup_service.py:174); replacing that lookup with a literal, or reading
        it under the wrong key, hands every install-with-no-row a window nobody
        chose — the #1638 mechanism one layer above the constant it moved.

        The expectation is READ from `OPS_SETTINGS_DEFAULTS` at runtime, never
        transcribed: pinning the numbers here would BE the anti-pattern this file
        exists to avoid (learnings.md:101-103). The DIRECTION those defaults may
        move is pinned by `test_1638_retention_upgrade_safety.py`, which is where
        that belongs. This test pins only the WIRING.
        """
        from services.settings_service import OPS_SETTINGS_DEFAULTS

        # Returning the handed-in `default` is exactly "there is no system_settings
        # row for this key" — `db.get_setting_value` is a get-or-default accessor.
        monkeypatch.setattr(
            cleanup_db, "get_setting_value", lambda key, default=None: default
        )
        assert _CS._read_retention_settings() == tuple(
            int(OPS_SETTINGS_DEFAULTS[key])
            for key in (
                "execution_log_retention_days",
                "execution_row_retention_days",
                "health_check_retention_days",
                "agent_reports_retention_days",
            )
        )

    def test_unicode_digits_are_accepted_by_int(self, cleanup_db, monkeypatch):
        """D5 — OBSERVED, NOT ENDORSED. `int()` accepts non-ASCII decimal digits,
        so an Arabic-Indic '١٢' resolves to a real 12-day window. Harmless (the
        result is still a bounded positive int that the guard then gates), but
        recorded so the behaviour is known rather than discovered."""
        monkeypatch.setattr(
            cleanup_db, "get_setting_value", lambda key, default=None: "١٢"
        )
        assert _CS._read_retention_settings() == (12,) * 4

    def test_absurdly_large_window_stays_an_int_and_disables_nothing(
        self, cleanup_db, monkeypatch
    ):
        """D7: a huge window is not coerced to 0 — it is a legitimately enormous
        int. It reaches `iso_cutoff(hours=days*24)`, whose overflow is slice b's
        target; the fail-closed chain is that the COUNT raises and the guard
        refuses. Asserted here only as 'the reader does not silently disable'."""
        monkeypatch.setattr(
            cleanup_db, "get_setting_value", lambda key, default=None: "9" * 40
        )
        result = _CS._read_retention_settings()
        assert all(isinstance(v, int) and v > 0 for v in result)


# ---------------------------------------------------------------------------
# E — _log_prune level rule
# ---------------------------------------------------------------------------


class TestLogPruneLevels:
    """Chunking splits one catastrophic delete into a sequence of unremarkable
    lines; the level rule is the only thing that makes a big prune visible."""

    @pytest.mark.parametrize(
        "row,pruned,expected_level",
        [
            ("E3-zero", 0, logging.INFO),
            ("E2-trickle", 3, logging.INFO),
            ("E2-just-under-cap", _CS.RETENTION_CHUNK_SIZE_PER_CYCLE - 1, logging.INFO),
            ("E1-exactly-cap", _CS.RETENTION_CHUNK_SIZE_PER_CYCLE, logging.WARNING),
            ("E1-over-cap", _CS.RETENTION_CHUNK_SIZE_PER_CYCLE + 1, logging.WARNING),
        ],
    )
    def test_level_escalates_at_the_chunk_boundary(
        self, row, pruned, expected_level, caplog
    ):
        with caplog.at_level(logging.INFO, logger=_CS.logger.name):
            _CS._log_prune(pruned, "prune happened")
        assert [r.levelno for r in caplog.records] == [expected_level], row


# ---------------------------------------------------------------------------
# H — the cleanup_service guard adapters
# ---------------------------------------------------------------------------


class TestGuardAdapters:
    """`_guard_allows` and `_after_guarded_prune` are the only things standing
    between a verdict and a DELETE."""

    def test_allowed_verdict_clears_the_memo_and_permits_the_prune(self, monkeypatch):
        """H1."""
        calls = []
        monkeypatch.setattr(
            _RG,
            "evaluate",
            lambda *a, **k: _RG.GuardVerdict(True, 1, 1000, "under_threshold"),
        )
        monkeypatch.setattr(
            _RG, "note_allowed", lambda key: calls.append(("allow", key))
        )
        monkeypatch.setattr(
            _RG,
            "announce_refusal",
            lambda *a: calls.append(("refuse", a[0])),
        )

        assert _CS._guard_allows("k", "label", 5, lambda limit: 1) is True
        assert calls == [("allow", "k")]

    def test_refused_verdict_announces_and_blocks_the_prune(self, monkeypatch):
        """H2. Returning True here is the unrecoverable failure — it is the
        difference between 'refused' and '95.7% of the table is gone'.

        The WHOLE forwarded argument tuple is asserted, not just the setting key.
        `announce_refusal(setting_key, label, window_days, verdict)` is positional,
        so transposing `label` and `window_days` would key the transition memo on a
        label string and stamp the alarm's `context.window_days` with prose — a
        wrong blast-radius report on the one row an operator reads to decide.
        """
        calls = []
        refused = _RG.GuardVerdict(False, 9999, 1000, "over_threshold")
        monkeypatch.setattr(_RG, "evaluate", lambda *a, **k: refused)
        monkeypatch.setattr(
            _RG, "note_allowed", lambda key: calls.append(("allow", key))
        )
        monkeypatch.setattr(
            _RG, "announce_refusal", lambda *a: calls.append(("refuse", a))
        )

        assert _CS._guard_allows("k", "label", 5, lambda limit: 9999) is False
        assert calls == [("refuse", ("k", "label", 5, refused))]

    def test_floor_is_forwarded_to_evaluate(self, monkeypatch):
        """A dropped `floor` would silently promote the agent sweep (floor 0, every
        candidate destroys Docker volumes) to the 1000-row default."""
        seen = {}

        def fake_evaluate(setting_key, window_days, count_fn, floor=None):
            seen["floor"] = floor
            return _RG.GuardVerdict(True, 0, 0, "under_threshold")

        monkeypatch.setattr(_RG, "evaluate", fake_evaluate)
        monkeypatch.setattr(_RG, "note_allowed", lambda key: None)
        _CS._guard_allows("k", "label", 5, lambda limit: 0, floor=_RG.FLOOR_AGENTS)
        assert seen["floor"] == _RG.FLOOR_AGENTS

    def test_after_guarded_prune_is_a_no_op_without_an_ack(self, guard_db):
        """H3: the common, under-threshold path."""
        _CS._after_guarded_prune("execution_row_retention_days")
        assert guard_db.settings == {}

    def test_after_guarded_prune_consumes_a_present_ack(self, guard_db):
        """Single-use: one approval buys one complete drain, then the guard re-arms."""
        _RG.record_acknowledgement("execution_row_retention_days", 5)
        _CS._after_guarded_prune("execution_row_retention_days")
        assert _RG.is_acknowledged("execution_row_retention_days", 5) is False

    def test_after_guarded_prune_never_raises_into_a_completed_sweep(
        self, monkeypatch, caplog
    ):
        """H4. This runs AFTER rows were deleted. A raise here would abort the
        sweep's remaining bookkeeping over a stale-ack problem, which is a (small)
        safety hole, not a data-loss one."""
        monkeypatch.setattr(
            _RG,
            "consume_acknowledgement",
            lambda key: (_ for _ in ()).throw(RuntimeError("settings write failed")),
        )
        with caplog.at_level(logging.ERROR, logger=_CS.logger.name):
            _CS._after_guarded_prune("execution_row_retention_days")
        assert [r.levelno for r in caplog.records] == [logging.ERROR]


# ---------------------------------------------------------------------------
# F — predicate edges (real DB)
# ---------------------------------------------------------------------------


def _seed(eid, completed_at, status="success", log="x"):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, completed_at, message, "
        " triggered_by, execution_log) "
        "VALUES (:id, 's1', 'a1', :st, :ts, :done, 'm', 'schedule', :log)",
        id=eid,
        st=status,
        ts=_days_ago_iso(200),
        done=completed_at,
        log=log,
    )


@pytest.fixture
def sched(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    from db.schedules import ScheduleOperations

    return ScheduleOperations(None, None)


class TestPredicateEdges:

    def test_all_four_terminal_states_are_counted_and_pruned_together(self, sched):
        """F4: `test_execution_retention_prune` covers all four for the PRUNE, and
        `test_1644` covers parity for `success` only — so parity across the full
        terminal set was never proven. A state counted but not pruned (or vice
        versa) is exactly the drift the shared predicate exists to prevent."""
        for i, status in enumerate(("success", "failed", "cancelled", "skipped")):
            _seed(f"term{i}", _days_ago_iso(100), status=status)
        _seed("running", _days_ago_iso(100), status="running")
        _seed("queued", _days_ago_iso(100), status="queued")

        counted = sched.count_execution_row_candidates(90, limit=1000)
        pruned = sched.prune_execution_rows(90, chunk_size=1000)
        assert counted == pruned == 4
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == 2

    def test_terminal_row_with_null_completed_at_is_excluded_by_both_sides(self, sched):
        """F5: the predicate's `completed_at IS NOT NULL` term, untested on either
        side. A terminal row that never recorded a completion has no age, so it
        must not be aged out."""
        _seed("no-completion", None)
        _seed("old", _days_ago_iso(100))

        counted = sched.count_execution_row_candidates(90, limit=1000)
        pruned = sched.prune_execution_rows(90, chunk_size=1000)
        assert counted == pruned == 1
        assert (
            _hscalar(
                "SELECT COUNT(*) FROM schedule_executions WHERE id = 'no-completion'"
            )
            == 1
        )

    def test_a_row_exactly_at_the_cutoff_survives(self, sched, monkeypatch):
        """F6: the predicate is a strict `<`, so `completed_at == cutoff` is kept.

        The cutoff MUST be frozen. `iso_cutoff` is evaluated INSIDE the accessor at
        call time (retention.py:134), so a row seeded 'at the cutoff' at T0 is
        compared against a cutoff computed at T1 > T0 and is strictly older by the
        elapsed microseconds — written naively this test fails on CORRECT code.

        The neighbouring row is seeded ONE TICK older, and its removal is asserted:
        `counted == pruned` alone is satisfied by `0 == 0`, so without a positive
        control a predicate that matched NOTHING would pass this test. `iso_cutoff`
        always returns `...ffffffZ`, so swapping the trailing `Z` (0x5A) for `0`
        (0x30) yields a same-length string that sorts strictly BEFORE the cutoff —
        the tightest possible input for `<` versus `<=`.
        """
        import db.schedules.retention as _RET

        frozen = _days_ago_iso(90)
        assert frozen.endswith("Z"), f"iso_cutoff format changed: {frozen!r}"
        monkeypatch.setattr(_RET, "iso_cutoff", lambda **kwargs: frozen)

        _seed("at-cutoff", frozen)
        _seed("one-tick-older", frozen[:-1] + "0")

        counted = sched.count_execution_row_candidates(90, limit=1000)
        pruned = sched.prune_execution_rows(90, chunk_size=1000)
        assert counted == pruned == 1, "exactly the strictly-older row is a candidate"
        assert (
            _hscalar("SELECT COUNT(*) FROM schedule_executions WHERE id = 'at-cutoff'")
            == 1
        ), "a row exactly at the cutoff is not yet past it"
        assert (
            _hscalar(
                "SELECT COUNT(*) FROM schedule_executions WHERE id = 'one-tick-older'"
            )
            == 0
        ), "one tick past the cutoff IS past it"

    def test_prune_execution_rows_drains_fully_with_a_small_chunk_size(self, sched):
        """F11: `chunk_size` bounds each TRANSACTION, not the call — the loop
        drains the entire candidate set (`cleanup_service.py:84-96`, 'READ THIS,
        THE NAME LIES'). This accessor is the one #1638 accessor lacking a drain
        proof; #1638 destroyed 5352 rows in one sweep, which a real per-call cap
        could not have produced. A regression to a single-chunk return would make
        that comment a lie and understate every reported blast radius."""
        for i in range(23):
            _seed(f"old{i}", _days_ago_iso(100))

        assert sched.prune_execution_rows(90, chunk_size=5) == 23
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == 0

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_chunk_size_prunes_nothing(self, bad, sched):
        """A `chunk_size <= 0` must be a no-op, never an unbounded delete."""
        _seed("old", _days_ago_iso(100))
        assert sched.prune_execution_rows(90, chunk_size=bad) == 0
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == 1


class TestDisabledSweepGuards:
    """`retention_days <= 0 or limit <= 0` on every accessor.

    `0` means DISABLED. If that guard weakens, `iso_cutoff(hours=0)` is *now* and
    the predicate `completed_at < now` matches the entire terminal table — the
    #1638 blast radius reached directly, bypassing the window entirely. The
    mutation gate showed these guards almost completely unasserted: only
    `count_execution_row_candidates(0, ...)` had a test, and nothing covered
    `limit == 0`, `chunk_size == 0`, or the `or`.

    `limit == 0` is genuinely reachable, not theoretical: the guard calls
    `count_fn(threshold + 1)`, so a floor of −1 (matrix row A17) makes the bound
    exactly 0.
    """

    @pytest.mark.parametrize(
        "days,limit", [(0, 10), (10, 0), (0, 0), (-1, 10), (10, -1)]
    )
    @pytest.mark.parametrize(
        "accessor",
        [
            "count_execution_log_candidates",
            "count_execution_row_candidates",
            "count_soft_deleted_schedules_past_retention",
        ],
    )
    def test_non_positive_window_or_limit_counts_nothing(
        self, accessor, days, limit, sched
    ):
        _seed("old", _days_ago_iso(100))
        assert getattr(sched, accessor)(days, limit=limit) == 0

    @pytest.mark.parametrize(
        "days,limit", [(0, 10), (10, 0), (0, 0), (-1, 10), (10, -1)]
    )
    def test_non_positive_window_or_limit_finds_no_schedules(self, days, limit, sched):
        assert sched.find_soft_deleted_schedules_past_retention(days, limit=limit) == []

    @pytest.mark.parametrize(
        "days,chunk", [(0, 10), (10, 0), (0, 0), (-1, 10), (10, -1)]
    )
    @pytest.mark.parametrize("pruner", ["prune_execution_logs", "prune_execution_rows"])
    def test_non_positive_window_or_chunk_prunes_nothing(
        self, pruner, days, chunk, sched
    ):
        _seed("old", _days_ago_iso(100))
        assert getattr(sched, pruner)(days, chunk_size=chunk) == 0
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == 1

    @pytest.mark.parametrize("chunk", [0, -1])
    def test_non_positive_chunk_scrubs_nothing(self, chunk, sched):
        """`scrub_terminal_backlog_metadata` is not age-gated (it is a security
        invariant, not a retention window), so `chunk_size` is its only guard."""
        assert sched.scrub_terminal_backlog_metadata(chunk_size=chunk) == 0


class TestCutoffStrictness:
    """`completed_at < cutoff` is STRICT on all three predicates.

    F6 covers the row predicate. The mutation gate showed the sibling log
    predicate (`retention.py:36`) and the soft-deleted-schedules predicate
    (`:58`) had no exactly-at-cutoff test, so `<` -> `<=` survived on both. An
    inclusive comparison prunes one window-boundary cohort early on every cycle.
    """

    def test_log_predicate_keeps_a_row_exactly_at_the_cutoff(self, sched, monkeypatch):
        import db.schedules.retention as _RET

        frozen = _days_ago_iso(90)
        monkeypatch.setattr(_RET, "iso_cutoff", lambda **kw: frozen)

        _seed("at-cutoff", frozen, log="transcript")
        counted = sched.count_execution_log_candidates(90, limit=100)
        pruned = sched.prune_execution_logs(90, chunk_size=100)

        assert counted == pruned == 0, "a row exactly at the cutoff is not past it"
        assert (
            _hscalar(
                "SELECT COUNT(*) FROM schedule_executions WHERE execution_log IS NOT NULL"
            )
            == 1
        )

    def test_soft_deleted_schedule_exactly_at_the_cutoff_survives(
        self, sched, monkeypatch
    ):
        """Also the only coverage of `find_soft_deleted_schedules_past_retention`'s
        row extraction — it binds its own `iso_cutoff` via a function-local import,
        so both bindings are patched."""
        import db.schedules.retention as _RET
        import utils.helpers as _H

        frozen = _days_ago_iso(30)
        monkeypatch.setattr(_RET, "iso_cutoff", lambda **kw: frozen)
        monkeypatch.setattr(_H, "iso_cutoff", lambda **kw: frozen)

        for sid, deleted in (("at-cutoff", frozen), ("older", _days_ago_iso(60))):
            _hrun(
                "INSERT INTO agent_schedules "
                "(id, agent_name, name, cron_expression, message, owner_id, "
                " created_at, updated_at, deleted_at) "
                "VALUES (:id, 'a1', 'n', '* * * * *', 'm', 1, :t, :t, :d)",
                id=sid,
                t=_days_ago_iso(100),
                d=deleted,
            )

        found = sched.find_soft_deleted_schedules_past_retention(30, limit=100)
        counted = sched.count_soft_deleted_schedules_past_retention(30, limit=100)

        assert found == ["older"], "only the strictly-older schedule is past retention"
        assert counted == 1


class TestWindowArithmetic:
    """`iso_cutoff(hours=retention_days * 24)` — the days->hours conversion.

    A wrong multiplier silently rescales EVERY retention window (24->25 makes a
    90-day window prune at ~86 days). Row-seeded tests cannot see a 4% shift, so
    the mutation gate found all six call sites unasserted. Asserting the call
    itself is the only way to pin arithmetic that no realistic fixture reveals.
    """

    @pytest.mark.parametrize("days", [1, 7, 90, 365])
    @pytest.mark.parametrize(
        "call",
        [
            ("count_execution_log_candidates", {"limit": 10}),
            ("count_execution_row_candidates", {"limit": 10}),
            ("count_soft_deleted_schedules_past_retention", {"limit": 10}),
            ("prune_execution_logs", {"chunk_size": 10}),
            ("prune_execution_rows", {"chunk_size": 10}),
        ],
    )
    def test_window_is_converted_to_hours_by_exactly_24(
        self, call, days, sched, monkeypatch
    ):
        import db.schedules.retention as _RET

        seen = []

        def spy(**kwargs):
            seen.append(kwargs)
            return _days_ago_iso(3650)

        monkeypatch.setattr(_RET, "iso_cutoff", spy)
        name, kwargs = call
        getattr(sched, name)(days, **kwargs)

        assert seen and seen[0] == {
            "hours": days * 24
        }, f"{name} must convert its window with exactly 24 hours/day; got {seen}"

    @pytest.mark.parametrize("days", [1, 7, 90, 365])
    def test_soft_delete_finder_also_converts_by_exactly_24(
        self, days, sched, monkeypatch
    ):
        """`find_soft_deleted_schedules_past_retention` re-imports `iso_cutoff`
        INSIDE the function body (retention.py:99), so it binds
        `utils.helpers.iso_cutoff` rather than the module-level `_RET.iso_cutoff`
        every other accessor uses. The module-level spy above cannot see it — the
        mutation gate caught that this one call site was still unasserted."""
        import utils.helpers as _H

        seen = []

        def spy(**kwargs):
            seen.append(kwargs)
            return _days_ago_iso(3650)

        monkeypatch.setattr(_H, "iso_cutoff", spy)
        sched.find_soft_deleted_schedules_past_retention(days, limit=10)

        assert seen and seen[0] == {
            "hours": days * 24
        }, f"the soft-delete finder must convert by 24 hours/day; got {seen}"


# ---------------------------------------------------------------------------
# G — structural invariants
# ---------------------------------------------------------------------------


class TestStructuralInvariants:

    def test_every_retention_window_has_a_guard_call_site(self):
        """G3. Read the SOURCE, don't import it (learnings.md:111): importing
        `cleanup_service` for a structural check drags in `settings_service` ->
        `database` -> `init_database()` at module-import time.

        Strict SET EQUALITY in both directions, and deliberately NO hardcoded
        count — `architecture.md` still says '7 window-driven destructive prunes'
        while there are 8 since #1296, and hardcoding a count is the #1638
        'pin the value' trap in miniature. Set equality also catches the inverse
        bug: a guarded sweep whose key is not a registered retention window.
        """
        from services.settings_service import RETENTION_OPS_KEYS

        tree = ast.parse(_SRC_CLEANUP.read_text())
        guarded = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_guard_allows"
            ):
                first = node.args[0] if node.args else None
                assert isinstance(first, ast.Constant), (
                    "a _guard_allows call site whose setting_key is not a literal "
                    "cannot be audited statically"
                )
                guarded.append(first.value)

        assert len(guarded) == len(
            set(guarded)
        ), f"a retention window is guarded twice: {guarded}"
        # #2216: `backup_retention_days` is a retention window (it joins
        # RETENTION_OPS_KEYS for the write-path protections) whose prune is
        # deliberately NOT a #1644 count-threshold/ack-gated sweep. It prunes
        # FILE artifacts from the backup service's own tail, and its
        # bounded-destruction guarantee is structural — the fixed
        # BACKUP_MIN_KEEP floor (never zero recovery points) — rather than a
        # refusable count: an ack-gated refusal would fail in the INVERTED
        # direction here (refused prune → backups fill the disk, #1871 class),
        # so the prune must run unconditionally within its floor. That floor is
        # pinned by tests/unit/test_2216_backup_primitives.py. The carve-out
        # set lives in CODE beside RETENTION_OPS_KEYS (not test-locally), so
        # the next file-artifact window is declared where the key is added.
        from config import NON_ROW_RETENTION_OPS_KEYS
        assert NON_ROW_RETENTION_OPS_KEYS <= set(RETENTION_OPS_KEYS), (
            "a non-row carve-out must itself be a registered retention window"
        )
        row_windows = set(RETENTION_OPS_KEYS) - NON_ROW_RETENTION_OPS_KEYS
        assert set(guarded) == row_windows, (
            "every registered ROW-retention window must have exactly one "
            "_guard_allows call site, and every guarded sweep must be a "
            "registered window.\n"
            f"  unguarded windows: {row_windows - set(guarded)}\n"
            f"  guarded non-windows: {set(guarded) - row_windows}"
        )
        # The carve-out is exactly the backup key and it does carry a floor.
        from db.backup_primitives import BACKUP_MIN_KEEP
        assert BACKUP_MIN_KEEP >= 2

    def test_the_prune_terminal_set_matches_the_platform_terminal_set(self):
        """A new terminal status must not fall silently out of BOTH the prune and
        its coverage.

        `db/schedules/retention.py:_RETENTION_TERMINAL` is a hand-mirrored copy of
        the platform's terminal set (`cleanup_service._TERMINAL_EXECUTION_STATUSES`,
        itself mirroring `models.TaskExecutionStatus`). Two failure directions, and
        this pins both because it is an EQUALITY, not a subset:
          * a status added to the platform set but not here -> those rows are
            terminal forever and never age out (an unbounded table, no signal);
          * a status added here but not there -> the prune deletes rows the rest of
            the platform still considers live.

        It is also the coverage tripwire: `test_1771a_retention_properties.py`'s
        `_TERMINAL` / `_row` strategy samples this set literally, so widening the
        product set without widening the strategy would leave the new status
        UNGENERATED and untested. Compared as SETS against another module's
        constant, so nothing here is a hardcoded literal.
        """
        from db.schedules.retention import _RETENTION_TERMINAL

        assert set(_RETENTION_TERMINAL) == set(_CS._TERMINAL_EXECUTION_STATUSES), (
            "the retention predicate's terminal set drifted from the platform's. "
            "Update both, plus `_TERMINAL` in test_1771a_retention_properties.py "
            "(its `_row` strategy generates from that tuple) and the four-state "
            "parity test above."
        )

    def test_ack_prefix_is_blocklisted_from_the_generic_settings_endpoint(self):
        """G6 — SECURITY. `retention_guard.py:61-63` claims the ack prefix is
        blocklisted from the generic `PUT /api/settings/{key}`; nothing asserted
        it. Without that blocklist, anything reaching the catch-all could write
        its own approval and disarm the guard — the ack IS the gate, so this is
        the one setting whose writability is a security property.

        Read the SOURCE, don't import it (learnings.md:111): `routers/settings.py`
        pulls `routers/__init__.py`, which imports every router in the app.

        Deliberately matched on the SYMBOL, not the literal string: the router
        does `from services.retention_guard import ACK_KEY_PREFIX` rather than
        duplicating `"retention_ack_"`, which is the correct choice (no mirror to
        drift). An earlier draft of this test grepped for the literal and failed
        on correct code.
        """
        tree = ast.parse((_BACKEND / "routers" / "settings" / "generic.py").read_text())  # 1028: catch-all module

        handler = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "update_setting"
            ),
            None,
        )
        assert handler is not None, (
            "the generic PUT /api/settings/{key} handler was renamed — re-point "
            "this test, do not delete it (#1644)"
        )

        guards = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Attribute)
            and node.test.func.attr == "startswith"
            and any(
                isinstance(a, ast.Name) and a.id == "ACK_KEY_PREFIX"
                for a in node.test.args
            )
            and any(isinstance(stmt, ast.Raise) for stmt in ast.walk(node))
        ]
        assert guards, (
            "PUT /api/settings/{key} no longer refuses keys starting with "
            "ACK_KEY_PREFIX. An ack written through this unvalidated endpoint "
            "pre-approves a mass deletion — the blast-radius guard is disarmed by "
            "the same route that causes the bug it exists to catch (#1644)."
        )

    def test_per_sweep_floors_are_pinned_by_direction_not_by_value(self):
        """G7. `test_1644:285` pins only `MAX_ROWS_PER_SWEEP`. But
        `retention_guard.py:170` is `MAX_ROWS_PER_SWEEP if floor is None else
        floor` — there is no `min()`, so a floor OVERRIDES the global rather than
        being bounded by it. Raising `FLOOR_SCHEDULES` 100 -> 100000 disarms that
        sweep entirely while every existing test stays green: exactly the failure
        the threshold-direction test exists to prevent, one level down.

        Lowering a floor is always safe; raising one weakens every install."""
        from services.retention_guard import FLOOR_AGENTS, FLOOR_SCHEDULES

        PREVIOUS_AGENTS = 0
        PREVIOUS_SCHEDULES = 100
        assert FLOOR_AGENTS <= PREVIOUS_AGENTS, (
            "FLOOR_AGENTS is 0 because every agent purge destroys Docker volumes "
            "(#1581). Raising it lets volume destruction through unacknowledged."
        )
        assert FLOOR_SCHEDULES <= PREVIOUS_SCHEDULES, (
            "Raising a per-sweep floor disarms that sweep's guard. If intentional, "
            "it needs a reviewer and a docs/migrations/ note, not a one-line diff."
        )

    def test_no_per_sweep_floor_exceeds_the_global_ceiling(self):
        """G8. The floors are meant to be STRICTER than the global threshold (they
        encode how unrecoverable each sweep's loss is). A floor above
        MAX_ROWS_PER_SWEEP would be a per-sweep RELAXATION wearing the word
        'floor' — and `evaluate` would honour it, because there is no `min()`."""
        from services.retention_guard import (
            FLOOR_AGENTS,
            FLOOR_SCHEDULES,
            MAX_ROWS_PER_SWEEP,
        )

        assert max(FLOOR_AGENTS, FLOOR_SCHEDULES) <= MAX_ROWS_PER_SWEEP
