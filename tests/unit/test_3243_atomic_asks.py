"""Atomic asks: hard caps on an agent's ask (#3243).

Agents wrote paragraph-length options, folded several decisions into one menu
and wrote titles a person cannot read at a glance. The ask contract now caps:
at most `OPERATOR_QUEUE_MAX_OPTIONS` options (the platform's reserved
`(something else)` never counted), at most `OPERATOR_QUEUE_OPTION_MAX_CHARS`
characters per option, and — for an agent's raise — a title of at most
`OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS`. An option that reads as the platform's
chip ("Something else" in any case, parenthesised or not) is refused like the
literal itself. Refused with a named code on the native path, never truncated.

Part 1 — the shared predicate and the native raise (`ask_service.raise_ask`).
Part 2 — the queue-file ingest hold.

Related flow: docs/memory/feature-flows/operating-room.md (Raising an ask)
Requirement: docs/memory/requirements/security.md §26 (operator queue)
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

OWNER = "owner-3243@example.com"


# ===========================================================================
# 1. The predicate — one rule both creation paths call
# ===========================================================================

class TestOptionsCapViolation:
    @staticmethod
    def check(options, max_options=5, max_chars=60):
        from services.operator_queue_choices import options_cap_violation
        return options_cap_violation(options, max_options=max_options, max_chars=max_chars)

    def test_five_options_pass_and_six_are_too_many(self):
        assert self.check(["a", "b", "c", "d", "e"]) is None
        assert self.check(["a", "b", "c", "d", "e", "f"]) == (
            "too_many_options", {"limit": 5, "count": 6})

    def test_the_reserved_literal_is_never_counted(self):
        from services.operator_queue_choices import SOMETHING_ELSE
        assert self.check(["a", "b", "c", "d", "e", SOMETHING_ELSE]) is None

    def test_sixty_characters_pass_and_sixty_one_are_too_long(self):
        assert self.check(["x" * 60, "ok"]) is None
        assert self.check(["ok", "x" * 61]) == (
            "option_too_long", {"limit": 60, "index": 1, "length": 61})

    def test_an_astral_character_counts_as_one(self):
        assert self.check(["\U0001F600" * 60]) is None

    def test_count_is_checked_before_length(self):
        assert self.check(["x" * 99] * 6)[0] == "too_many_options"

    def test_a_mixed_list_is_counted(self):
        assert self.check([1, 2, 3, 4, 5, "six"])[0] == "too_many_options"

    @pytest.mark.parametrize("lookalike", [
        "Something else", "something else", "SOMETHING ELSE", "  something else  ",
        "(Something Else)", "( something else )", "something  else",
        # NFKC: fullwidth letters and fullwidth parentheses fold onto ASCII.
        "\uff33\uff4f\uff4d\uff45\uff54\uff48\uff49\uff4e\uff47 \uff45\uff4c\uff53\uff45",
        "\uff08something else\uff09",
        # Zero-width characters inserted anywhere are dropped before the compare.
        "Some\u200bthing else", "\u200bSomething else\ufeff", "something\u200d else\u2060",
        "(\u200csomething else\u200e)",
    ])
    def test_an_option_that_reads_as_the_chip_is_refused(self, lookalike):
        assert self.check(["approve", lookalike]) == ("invalid_options", {"index": 1})

    @pytest.mark.parametrize("fine", ["Something else entirely", "else", "Do something",
                                      "Something else entirely: escalate",
                                      "\uff33omething else entirely"])
    def test_an_option_that_merely_mentions_it_is_fine(self, fine):
        assert self.check(["approve", fine]) is None

    def test_a_cross_script_confusable_is_out_of_scope(self):
        """Cyrillic "е" (U+0435) is not folded by NFKC — closing that class
        needs a confusables table, which #3243 does not take on."""
        assert self.check(["approve", "Som\u0435thing \u0435lse"]) is None

    def test_not_a_list_is_not_this_rules_question(self):
        assert self.check(None) is None and self.check("a,b") is None

    def test_title_cap(self):
        from services.operator_queue_choices import title_cap_violation
        assert title_cap_violation("t" * 120, max_chars=120) is None
        assert title_cap_violation("t" * 121, max_chars=120) == (
            "title_too_long", {"limit": 120, "length": 121})


# The operator's ruling (#3243): (env name, default, floor). The contract text
# quotes these defaults, never the env-resolved constants.
_DEFAULT_CAPS = (("OPERATOR_QUEUE_MAX_OPTIONS", 5, 2),
                 ("OPERATOR_QUEUE_OPTION_MAX_CHARS", 60, 16),
                 ("OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS", 120, 40))


class TestEnvCaps:
    """Through the loader the module constants use — never a module reload,
    which would hand other suites a stale `OperatorQueueSyncService`."""

    def test_defaults_are_the_operators_ruling(self):
        """The DEFAULTS as written in the module, not the constants — those are
        read from env at import, and a re-tuned install must not go red here."""
        import services.operator_queue_service as oqs
        with open(oqs.__file__, encoding="utf-8") as f:
            src = f.read()
        for name, default, floor in _DEFAULT_CAPS:
            assert f'_floored_env_cap("{name}", {default}, {floor})' in src, name

    def test_a_mis_set_env_is_floored_so_a_skill_gate_can_still_ask(self, monkeypatch):
        from services.operator_queue_service import _floored_env_cap
        monkeypatch.setenv("OPERATOR_QUEUE_MAX_OPTIONS", "1")
        assert _floored_env_cap("OPERATOR_QUEUE_MAX_OPTIONS", 5, 2) == 2

    def test_env_raises_the_cap(self, monkeypatch):
        from services.operator_queue_service import _floored_env_cap
        monkeypatch.setenv("OPERATOR_QUEUE_OPTION_MAX_CHARS", "80")
        assert _floored_env_cap("OPERATOR_QUEUE_OPTION_MAX_CHARS", 60, 16) == 80
        monkeypatch.delenv("OPERATOR_QUEUE_OPTION_MAX_CHARS")
        assert _floored_env_cap("OPERATOR_QUEUE_OPTION_MAX_CHARS", 60, 16) == 60


# ===========================================================================
# 2. The native raise
# ===========================================================================

@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite, the world around it stubbed (the
    ent#611 suite's harness, trimmed): audit, broadcast, owner, workspace
    thread, resume opt-in, and a rate limiter that records every spend."""
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    state = {"spent": 0}

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            pass

    def _check(*a, **k):
        state["spent"] += 1
        return RateLimitResult(True, 10, 0, 60)

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", _check)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, state=state, db=real_db)
    assignment_provider.clear_provider()


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "approval", "title": "Pay invoice",
         "question": "Release 500 USDC to the vendor?", "options": ["approve", "reject"],
         "proposal": {"pay": 500}}
    b.update(over)
    return b


def _refused(ask, agent, body, *, raised_by="agent"):
    with pytest.raises(ask.svc.AskRejected) as info:
        ask.svc.raise_ask(agent, body, raised_by=raised_by,
                          channel="gate" if raised_by == "gate" else "mcp")
    e = info.value
    return e.status_code, e.code, e.extra, e.message


class TestNativeRaise:
    AGENT = "agent-3243-native"

    def test_six_options_are_refused_by_name_and_spend_no_rate_token(self, ask):
        status, code, extra, message = _refused(
            ask, self.AGENT, _body("n-many", options=list("abcdef")))
        assert (status, code, extra) == (422, "too_many_options", {"limit": 5, "count": 6})
        assert "split" in message.lower() and "Do not retry unchanged" in message
        assert ask.state["spent"] == 0
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "n-many") is None

    def test_an_option_over_sixty_characters_is_refused_by_name(self, ask):
        long = "approve — the receptionist sends it with a disclosure and CCs you"
        assert len(long) > 60
        status, code, extra, message = _refused(
            ask, self.AGENT, _body("n-long", options=[long, "reject"]))
        assert (status, code, extra) == (
            422, "option_too_long", {"limit": 60, "index": 0, "length": len(long)})
        # Never echoes the agent's text back.
        assert long not in message and "proposal" in message

    @pytest.mark.parametrize("lookalike", ["Something else", "(SOMETHING ELSE)", " something else ",
                                           "\uff08\uff33omething else\uff09",
                                           "Some\u200bthing\u200b else"])
    def test_a_lookalike_of_the_chip_is_refused_like_the_literal(self, ask, lookalike):
        status, code, extra, _ = _refused(
            ask, self.AGENT, _body("n-look", options=["approve", lookalike]))
        assert (status, code, extra) == (422, "invalid_options", {"index": 1})

    @pytest.mark.parametrize("kind", ["approval", "question", "alert"])
    def test_a_title_over_120_is_refused_for_every_type(self, ask, kind):
        over = {"type": kind, "title": "t" * 121}
        if kind != "approval":
            over["options"] = None
        status, code, extra, message = _refused(ask, self.AGENT, _body(f"n-title-{kind}", **over))
        assert (status, code, extra) == (422, "title_too_long", {"limit": 120, "length": 121})
        assert "question" in message
        assert ask.state["spent"] == 0

    def test_an_agent_title_over_the_outer_belt_names_the_agent_limit_first(self, ask):
        """A title over 300 is refused ONCE, with the limit that will admit it —
        not `field_too_large` at 300 and then `title_too_long` at 120."""
        status, code, extra, message = _refused(ask, self.AGENT, _body("n-title-301", title="t" * 301))
        assert (status, code, extra) == (422, "title_too_long", {"limit": 120, "length": 301})
        assert "question" in message

    def test_a_gate_title_over_the_outer_belt_is_still_field_too_large(self, ask):
        status, code, extra, _ = _refused(
            ask, "agent-3243-gate", _body("gate-3243-301", title="g" * 301,
                                          options=["Approve", "Reject"]), raised_by="gate")
        assert (status, code, extra["limit"]) == (422, "field_too_large", 300)

    def test_an_ask_at_the_caps_is_raised(self, ask):
        receipt = ask.svc.raise_ask(
            self.AGENT, _body("n-ok", title="t" * 120, options=["x" * 60] + list("abcd")),
            raised_by="agent", channel="mcp")
        assert receipt["status"] == "created"

    def test_a_gate_raise_passes_and_its_title_is_not_capped(self, ask):
        receipt = ask.svc.raise_ask(
            "agent-3243-gate", _body("gate-3243-a", title="g" * 200, options=["Approve", "Reject"]),
            raised_by="gate", channel="gate")
        assert receipt["status"] == "created"

    def test_a_gate_raise_is_still_held_to_the_option_caps(self, ask):
        status, code, _, _ = _refused(
            ask, "agent-3243-gate", _body("gate-3243-b", options=list("abcdef")), raised_by="gate")
        assert (status, code) == (422, "too_many_options")

    def test_a_retry_of_an_ask_raised_before_the_caps_replays(self, ask, real_db):
        """An over-cap ask already stored (raised before the caps existed) gets
        its first receipt back on retry, never a refusal it did not earn."""
        agent, rid = "agent-3243-replay", "n-pre-cap"
        out = real_db.create_native_operator_queue_item(
            agent, {"id": rid, "type": "approval", "priority": "high", "title": "T" * 200,
                    "question": "q", "options": list("abcdefg"), "context": {},
                    "expires_at": None},
            max_pending=25, channel="mcp", raised_by="agent", to_role="primary",
            resolved_to=[OWNER], proposal={"pay": 500}, supersedes_expired=None)
        assert out["outcome"] == "created"
        again = ask.svc.raise_ask(agent, _body(rid, title="T" * 200, options=list("abcdefg")),
                                  raised_by="agent", channel="mcp")
        assert again["status"] == "replayed" and again["id"] == out["row"]["id"]
        assert "options" not in again["differs"] and "title" not in again["differs"]


# ===========================================================================
# 3. The queue-file ingest — held, the ids named, never a flood alert
# ===========================================================================

import asyncio  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402


def _fake_db(pending=0, rows=None):
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.side_effect = (
        lambda agent, item_type=None, exclude_request_id_prefixes=None:
        0 if item_type == "queue_flood" else pending)
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_operator_queue_sync_index_for_agent.return_value = {
        "open": rows or [], "terminal": {}, "foreign": []}
    db.set_operator_queue_sync_state.return_value = False
    db.set_operator_queue_delivery_state.return_value = False
    db.create_operator_queue_item_with_outcome.side_effect = (
        lambda agent, item, **kw: (db.create_operator_queue_item(agent, item, **kw), True))
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.get_setting_value.return_value = "24"
    return db


def _wire(monkeypatch, db, requests):
    import services.operator_queue_service as oqs
    from services.rate_limiter import RateLimitResult
    monkeypatch.setattr(oqs, "db", db)
    state = {"content": json.dumps({"requests": requests}), "writes": [], "rate": 0}
    client = MagicMock()

    async def _read(path, timeout=5.0):
        return {"success": True, "content": state["content"]}

    async def _write(path, content, **kw):
        state["writes"].append(json.loads(content))
        state["content"] = content
        return {"success": True}

    def _check(*a, **k):
        state["rate"] += 1
        return RateLimitResult(True, 10, 0, 60)

    client.read_file = AsyncMock(side_effect=_read)
    client.write_file = AsyncMock(side_effect=_write)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", _check)
    oqs.reset_alert_budget_state()
    return oqs.OperatorQueueSyncService(), state


def _entry(rid, **over):
    e = {"id": rid, "type": "approval", "status": "pending", "title": "Ship it?",
         "question": "q", "options": ["approve", "reject"]}
    e.update(over)
    return e


def _run(svc, cycles=1):
    for _ in range(cycles):
        asyncio.run(svc._sync_agent("agent-3243-file"))


def _marker(state):
    return json.loads(state["content"]).get("platform", {}).get("ingestion")


def _created_ids(db):
    return [c.args[1].get("id") for c in db.create_operator_queue_item.call_args_list]


class TestFileHold:
    def test_an_over_cap_entry_is_held_and_named_while_a_good_one_is_ingested(self, monkeypatch):
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, [
            _entry("too-many", options=list("abcdef")),
            _entry("too-long", options=["x" * 61, "no"]),
            _entry("lookalike", options=["approve", "(Something Else)"]),
            _entry("long-title", type="question", options=None, title="t" * 121),
            _entry("fine"),
        ])
        _run(svc)
        assert _created_ids(db) == ["fine"]
        marker = _marker(state)
        assert marker["reason"] in ("invalid_options", "invalid_title")
        assert marker["invalid_options"] == ["too-many", "too-long", "lookalike"]
        assert marker["invalid_title"] == ["long-title"]
        assert (marker["max_options"], marker["max_option_chars"], marker["max_title_chars"]) == (5, 60, 120)
        assert state["rate"] == 2  # only the admitted entry spent tokens (agent + fleet)

    @pytest.mark.parametrize("lookalike", ["\uff33omething else", "\uff08something else\uff09",
                                           "some\u200bthing else"])
    def test_a_normalised_lookalike_is_held_on_the_file_path(self, monkeypatch, lookalike):
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, [
            _entry("look", options=["approve", lookalike]),
            _entry("fine", options=["approve", "Something else entirely: escalate"])])
        _run(svc)
        assert _created_ids(db) == ["fine"]
        assert _marker(state)["invalid_options"] == ["look"]

    def test_the_reserved_literal_is_admitted_and_not_counted(self, monkeypatch):
        db = _fake_db()
        svc, _ = _wire(monkeypatch, db, [
            _entry("five-plus", options=list("abcde") + ["(something else)"])])
        _run(svc)
        assert _created_ids(db) == ["five-plus"]

    def test_a_cap_hold_never_fires_the_flood_alert(self, monkeypatch):
        db = _fake_db()
        svc, _ = _wire(monkeypatch, db, [_entry(f"bad-{i}", options=list("abcdef")) for i in range(30)])
        _run(svc, cycles=3)
        assert not [i for i in _created_ids(db) if str(i).startswith("queue-flood-")]

    def test_a_full_queue_keeps_its_reason_and_still_names_the_held_ids(self, monkeypatch):
        import services.operator_queue_service as oqs
        db = _fake_db(pending=oqs.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, [_entry("bad", options=list("abcdef")), _entry("ok")])
        _run(svc)
        marker = _marker(state)
        assert marker["reason"] == "queue_full" and marker["invalid_options"] == ["bad"]

    def test_an_unchanged_hold_is_not_rewritten_and_a_fix_clears_it(self, monkeypatch):
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, [_entry("bad", options=list("abcdef"))])
        _run(svc, cycles=3)
        assert len(state["writes"]) == 1
        data = json.loads(state["content"])
        data["requests"][0]["options"] = ["approve", "reject"]
        state["content"] = json.dumps(data)
        _run(svc)
        assert _created_ids(db) == ["bad"]
        assert _marker(state) is None

    def test_a_fixed_entry_drops_its_id_while_the_queue_stays_full(self, monkeypatch):
        import services.operator_queue_service as oqs
        db = _fake_db(pending=oqs.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, [_entry("bad", options=list("abcdef")), _entry("ok")])
        _run(svc)
        data = json.loads(state["content"])
        data["requests"][0]["options"] = ["approve", "reject"]
        state["content"] = json.dumps(data)
        _run(svc)
        marker = _marker(state)
        assert marker["reason"] == "queue_full" and "invalid_options" not in marker

    def test_an_ask_already_in_the_queue_is_never_re_judged(self, monkeypatch):
        """A pending row ingested before the caps existed stays as it is: no
        hold, no marker, no new create."""
        row = {"id": "row-1", "request_id": "old", "status": "pending", "type": "approval",
               "title": "t", "question": "q", "options": list("abcdefg"), "context": {},
               "priority": "medium", "expires_at": None, "channel": "file"}
        db = _fake_db(rows=[row])
        svc, state = _wire(monkeypatch, db, [_entry("old", options=list("abcdefg"), title="t")])
        _run(svc)
        assert _created_ids(db) == []
        assert _marker(state) is None


# ===========================================================================
# 4. The contract text quotes the configured numbers (drift guard)
# ===========================================================================

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestContractText:
    """`operator_queue.ts` and the prompt are static text; the caps are Python
    constants. A cap change that leaves the text behind makes the tool lie.
    The live consumer of these files is the agent reading them, so the pin is
    on the exact phrases built from the constants, not bare digits."""

    def _read(self, rel):
        with open(os.path.join(_REPO, rel), encoding="utf-8") as f:
            return f.read()

    def _phrases(self):
        # The literal defaults: the text says "by default", and the refusal
        # carries the limit in force, so this stays green on a re-tuned install.
        (_, options, _), (_, chars, _), (_, title, _) = _DEFAULT_CAPS
        return (f"by default at most {options} options, each at most {chars} characters",
                f"by default at most {title}")

    def test_the_tool_description_quotes_the_caps_and_codes(self):
        text = self._read("src/mcp-server/src/tools/operator_queue.ts")
        options_phrase, title_phrase = self._phrases()
        assert options_phrase in text
        assert f"{title_phrase} (title_too_long)" in text
        assert "a refusal names the limit in " in text  # the value in force rides on the refusal
        for code in ("too_many_options", "option_too_long", "title_too_long", "invalid_options"):
            assert code in text, code

    @pytest.mark.parametrize("rel", ["src/backend/services/platform_prompt_service.py",
                                     "config/trinity-meta-prompt/prompt.md"])
    def test_the_prompt_quotes_the_caps_and_points_at_the_tool(self, rel):
        text = self._read(rel)
        options_phrase, title_phrase = self._phrases()
        assert options_phrase in text and f"{title_phrase} characters" in text
        assert "The `ask_operator` description has the full rules." in text
        assert "`invalid_options` / `invalid_title`" in text
