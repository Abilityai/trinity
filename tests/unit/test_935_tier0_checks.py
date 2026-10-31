"""Tier-0 checks, part 1 (trinity-enterprise#935, checkpoint A1).

Four gate checks — `execution_exists`, `credential_echo`, `validate_001`,
`deliverable_landed` — each driven as a pure function over the inputs the
evaluator gathers once, plus `evaluate_execution` end-to-end on the real schema
(one raising check, the trigger gate, a failed write).

The null test is the binding acceptance criterion (TD-6): a run whose reply is
a bare "Done" and which left no receipt of any kind FAILS `deliverable_landed`;
an honest no-op reply skips with `no_claim`. A window-matched report is never a
pass (TD-3): it skips `unattributable`.

Evidence is counts / ids / hashes only (plan decision 17) — asserted, not
assumed, because the graded agent reads its own row.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import count, db_backend  # noqa: E402,F401

from services import tier0_evaluator as t0  # noqa: E402
from utils.credential_sanitizer import count_hits  # noqa: E402

pytestmark = pytest.mark.unit

FAKE_PAT = "ghp_" + "A" * 36


def _receipts(asks=0, effects=0, metrics=0, reports=0):
    return {
        "asks": {"count": asks, "ids": [f"q{i}" for i in range(asks)]},
        "effects": {"count": effects},
        "metrics": {"count": metrics},
        "reports_in_window": {"count": reports, "ids": [f"r{i}" for i in range(reports)]},
    }


def _ctx(response="", *, error=None, receipts=None, staged=(), **row):
    execution = {"id": "ex-1", "agent_name": "agent-a", "status": "success",
                 "triggered_by": "schedule", "message": "/x", "response": response,
                 "error": error, "turn_integrity": None, "business_status": None,
                 "validation_execution_id": None}
    execution.update(row)
    return {"execution": execution,
            "intent": t0.resolve_intent(execution["message"], response=response),
            "receipts": receipts if receipts is not None else _receipts(),
            "staged_values": list(staged)}


def _run(check_id, ctx):
    fn = {c.id: c.fn for c in t0.CHECKS}[check_id]
    return fn(ctx)


# --- count_hits ------------------------------------------------------------------

def test_count_hits_counts_without_returning_the_secret():
    assert count_hits("") == 0 and count_hits(None) == 0
    assert count_hits("nothing to see") == 0
    assert count_hits(f"token {FAKE_PAT} and API_KEY=abc123") == 2
    # text already carrying the marker is not double-counted
    assert count_hits("value was ***REDACTED*** already") == 0


def test_count_hits_is_linear_on_a_hostile_line():
    import time
    start = time.monotonic()
    count_hits("!=" * 40_000)
    assert time.monotonic() - start < 2.0


# --- the registry -----------------------------------------------------------------

def test_the_a1_checks_are_registered_as_gates_graded_by_the_platform():
    by_id = {c.id: c for c in t0.CHECKS}
    for cid in ("execution_exists", "credential_echo", "validate_001", "deliverable_landed"):
        assert by_id[cid].tier == "gate"
        assert by_id[cid].source == "platform"
    assert len(by_id) == len(t0.CHECKS)          # ids are unique


# --- execution_exists ---------------------------------------------------------------

def test_execution_exists_skips_when_the_run_left_no_integrity_record():
    """#2467: NULL means 'no evidence', never 'verified healthy'."""
    assert _run("execution_exists", _ctx())["status"] == "skip"
    assert _run("execution_exists", _ctx())["reason"] == "no_integrity_record"


def test_execution_exists_fails_on_killed_or_pending_background_work():
    killed = json.dumps({"background_tasks_killed": [{"id": "b1"}, {"id": "b2"}]})
    r = _run("execution_exists", _ctx(turn_integrity=killed))
    assert r["status"] == "fail"
    assert r["evidence"] == {"background_tasks_killed": 2, "background_tasks_pending_at_exit": 0}
    pending = json.dumps({"background_tasks_pending_at_exit": 3})
    assert _run("execution_exists", _ctx(turn_integrity=pending))["status"] == "fail"


def test_execution_exists_passes_a_clean_integrity_record():
    r = _run("execution_exists", _ctx(turn_integrity=json.dumps({})))
    assert r["status"] == "pass"


def test_execution_exists_skips_an_unreadable_record():
    r = _run("execution_exists", _ctx(turn_integrity="{not json"))
    assert (r["status"], r["reason"]) == ("skip", "integrity_unreadable")


# --- credential_echo ----------------------------------------------------------------

def test_credential_echo_passes_a_clean_reply():
    r = _run("credential_echo", _ctx("Posted the weekly report."))
    assert r["status"] == "pass"
    assert r["evidence"] == {"pattern_hits": 0, "redaction_markers": 0, "staged_hits": 0}


def test_credential_echo_fails_on_a_pattern_and_stores_only_the_count():
    r = _run("credential_echo", _ctx(f"here is the key: {FAKE_PAT}"))
    assert r["status"] == "fail"
    assert r["evidence"]["pattern_hits"] == 1
    assert FAKE_PAT not in json.dumps(r)


def test_credential_echo_fails_on_a_redaction_marker():
    """The stored reply is sanitized before persistence, so the marker is what
    is left of an echo that already happened."""
    r = _run("credential_echo", _ctx("my token is ***REDACTED***"))
    assert r["status"] == "fail" and r["evidence"]["redaction_markers"] == 1


def test_credential_echo_fails_on_a_staged_runtime_secret_in_the_error():
    secret = "plain-staged-value-123"
    r = _run("credential_echo", _ctx("ok", error=f"crashed with {secret}", staged=[secret]))
    assert r["status"] == "fail" and r["evidence"]["staged_hits"] == 1
    assert secret not in json.dumps(r)


def test_credential_echo_skips_a_run_with_no_output():
    r = _run("credential_echo", _ctx("", error=None))
    assert (r["status"], r["reason"]) == ("skip", "no_output")


# --- validate_001 -------------------------------------------------------------------

@pytest.mark.parametrize("business_status, status, reason", [
    (None, "skip", "not_run_yet"),
    ("pending_validation", "skip", "not_run_yet"),
    ("validated", "pass", None),
    ("failed_validation", "fail", "failed_validation"),
    ("validation_unavailable", "skip", "validation_unavailable"),
    ("skipped", "skip", "validation_skipped"),
    ("something-new", "skip", "unknown_business_status"),
])
def test_validate_001_reads_the_business_status(business_status, status, reason):
    r = _run("validate_001", _ctx(business_status=business_status,
                                  validation_execution_id="val-1"))
    assert r["status"] == status
    assert r.get("reason") == reason


# --- deliverable_landed (incl. the null test) ---------------------------------------

@pytest.mark.parametrize("reply", ["Done", "Done.", "completed", "All tasks completed."])
def test_null_test_a_bare_claim_with_zero_receipts_fails(reply):
    r = _run("deliverable_landed", _ctx(reply))
    assert (r["status"], r["reason"]) == ("fail", "claim_without_receipt")
    assert r["evidence"]["receipts"] == {"asks": 0, "effects": 0, "metrics": 0,
                                         "reports_in_window": 0}


@pytest.mark.parametrize("reply", [
    "No new items.", "Nothing to do today.", "Nothing to report",
    "Checked the queue — no changes needed.", "Already up to date.",
    "Done — nothing to do.",
])
def test_null_test_an_explicit_no_op_skips_no_claim(reply):
    r = _run("deliverable_landed", _ctx(reply))
    assert (r["status"], r["reason"]) == ("skip", "no_claim")


@pytest.mark.parametrize("reply", [
    "Filed the report and sent the email. No action needed from you.",
    "Published the digest. No changes needed elsewhere.",
    "Posted the update — nothing to do on your side.",
    "Done. Sent the invoice; no updates required.",
])
def test_a_no_op_sign_off_never_hides_a_deliverable_claim(reply):
    """Review I4: a no-op phrase skips only when every claim is a generic
    completion word. A deliverable verb next to a sign-off is still a claim,
    and with zero receipts it is the null test's fail (TD-6)."""
    r = _run("deliverable_landed", _ctx(reply))
    assert (r["status"], r["reason"]) == ("fail", "claim_without_receipt")


def test_no_claim_at_all_skips():
    r = _run("deliverable_landed", _ctx("Here is a summary of the market."))
    assert (r["status"], r["reason"]) == ("skip", "no_claim")


def test_a_negated_claim_or_the_fleet_footer_is_not_a_claim():
    reply = "I could not complete the export.\nNot done: the export\nDeviations: none"
    r = _run("deliverable_landed", _ctx(reply))
    assert (r["status"], r["reason"]) == ("skip", "no_claim")


@pytest.mark.parametrize("receipts", [
    _receipts(asks=1), _receipts(effects=2), _receipts(metrics=1),
])
def test_a_claim_with_an_attributable_receipt_passes(receipts):
    r = _run("deliverable_landed", _ctx("Sent the digest. Done.", receipts=receipts))
    assert r["status"] == "pass"


def test_a_window_matched_report_is_unattributable_never_a_pass():
    r = _run("deliverable_landed", _ctx("Published the report.", receipts=_receipts(reports=1)))
    assert (r["status"], r["reason"]) == ("skip", "unattributable")
    assert r["evidence"]["report_ids"] == ["r0"]


def test_a_commit_claim_has_no_receipt_source_yet():
    """P6: no OSS table records a push against an execution — skip, never a
    fabricated fail."""
    r = _run("deliverable_landed", _ctx("Committed and pushed the fix."))
    assert (r["status"], r["reason"]) == ("skip", "no_receipt_source")


def test_the_claim_excerpt_is_short_and_scrubbed():
    reply = ("x" * 400) + f" Done, key {FAKE_PAT} " + ("y" * 400)
    r = _run("deliverable_landed", _ctx(reply))
    excerpt = r["evidence"]["claim_excerpt"]
    assert len(excerpt) <= 200
    assert FAKE_PAT not in excerpt
    assert r["evidence"]["truncated"] is False


def test_the_staged_secret_is_scrubbed_from_the_excerpt_too():
    secret = "plain-staged-value-123"
    r = _run("deliverable_landed", _ctx(f"Done with {secret}", staged=[secret]))
    assert secret not in r["evidence"]["claim_excerpt"]


@pytest.mark.parametrize("edge", ["start", "end"])
@pytest.mark.parametrize("kind", ["staged", "pattern"])
def test_a_secret_straddling_the_excerpt_window_leaves_no_fragment(edge, kind):
    """Review I2: the excerpt window is cut from text that was already
    scrubbed whole, so a secret crossing either window edge cannot survive
    as a half that no longer matches its pattern or its staged value."""
    secret = "plainStagedSecretValue0123456789" if kind == "staged" else FAKE_PAT
    staged = [secret] if kind == "staged" else []
    if edge == "start":
        # window starts 80 chars before the claim: put the secret across it
        reply = "q" * 10 + secret + " " + "w" * (90 - 10 - len(secret) + len(secret) // 2) + " Done"
        lo = reply.index("Done") - 80
        assert reply.index(secret) < lo < reply.index(secret) + len(secret)
    else:
        # a redaction ahead of the claim shortens the window's text, so what
        # lay past the slice end is pulled into the 200-char cut
        reply = (f"{FAKE_PAT} " * 3) + "Done " + "e" * 128 + " " + secret + " tail"
    r = _run("deliverable_landed", _ctx(reply, staged=staged))
    excerpt = r["evidence"]["claim_excerpt"]
    assert not any(secret[i:i + 8] in excerpt for i in range(len(secret) - 7)), excerpt


def test_an_oversized_reply_is_bounded_and_says_so():
    reply = "Done. " + ("z" * (t0.MAX_SCAN_CHARS + 10))
    r = _run("deliverable_landed", _ctx(reply))
    assert r["status"] == "fail" and r["evidence"]["truncated"] is True


# --- evaluate_execution end-to-end ----------------------------------------------------

def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id="ex-e2e", **cols):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    values = dict(id=exec_id, schedule_id="sched-1", agent_name="agent-a",
                  status="success", triggered_by="schedule", message="/report-weekly",
                  response="Done", started_at=_ago(300), completed_at=_ago(120))
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))


@pytest.fixture
def no_staged(monkeypatch):
    monkeypatch.setattr(t0, "_staged_values", lambda: [])


def test_evaluate_execution_writes_one_tier0_row(db_backend, no_staged):
    _seed()
    row = t0.evaluate_execution("ex-e2e")
    assert row["evaluator"] == "platform:tier0" and row["target_id"] == "ex-e2e"
    assert row["completion"] is True and row["archetype"] == "playbook"
    checks = {c["id"]: c for c in row["checks"]["checks"]}
    assert checks["deliverable_landed"]["status"] == "fail"       # the null test, live
    assert checks["execution_exists"]["status"] == "skip"
    assert row["checks"]["intent_ref"] == {"kind": "playbook", "ref": "report-weekly",
                                           "unresolved": False}
    assert row["checks"]["schema"] == 1 and row["checks"]["evaluated_at"]
    for c in row["checks"]["checks"]:
        assert set(c) >= {"id", "tier", "source", "status", "evidence"}


def test_the_receipts_come_from_the_runs_own_ids(db_backend, no_staged):
    """An ask raised by THIS run is a receipt; one raised by another run of the
    same agent is not."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import operator_queue
    _seed()
    with get_engine().begin() as conn:
        conn.execute(insert(operator_queue).values(
            id="oq-1", agent_name="agent-a", request_id="r-1", status="pending",
            title="t", question="q", type="approval", execution_id="ex-e2e", created_at=_ago(200)))
        conn.execute(insert(operator_queue).values(
            id="oq-2", agent_name="agent-a", request_id="r-2", status="pending",
            title="t", question="q", type="approval", execution_id="ex-other", created_at=_ago(200)))
    row = t0.evaluate_execution("ex-e2e")
    landed = {c["id"]: c for c in row["checks"]["checks"]}["deliverable_landed"]
    assert landed["status"] == "pass"
    assert landed["evidence"]["receipts"]["asks"] == 1
    assert landed["evidence"]["ask_ids"] == ["oq-1"]


def test_a_check_that_raises_is_a_skip_and_the_row_is_still_written(db_backend, no_staged,
                                                                    monkeypatch):
    def boom(ctx):
        raise RuntimeError("secret detail must not be stored")
    patched = [t0.Check(c.id, c.tier, c.source, boom) if c.id == "validate_001" else c
               for c in t0.CHECKS]
    monkeypatch.setattr(t0, "CHECKS", patched)
    _seed()
    row = t0.evaluate_execution("ex-e2e")
    v = {c["id"]: c for c in row["checks"]["checks"]}["validate_001"]
    assert (v["status"], v["reason"]) == ("skip", "evaluator_error")
    assert v["evidence"] == {"error": "RuntimeError"}
    assert "secret detail" not in json.dumps(row["checks"])


@pytest.mark.parametrize("cols", [
    dict(triggered_by="manual"), dict(triggered_by="webhook"),
    dict(status="running", completed_at=None), dict(status="skipped"),
])
def test_only_terminal_schedule_and_agent_runs_are_evaluated(db_backend, no_staged, cols):
    _seed(**cols)
    assert t0.evaluate_execution("ex-e2e") is None
    assert count("agent_evaluations") == 0


def test_a_missing_execution_writes_nothing(db_backend, no_staged):
    assert t0.evaluate_execution("nope") is None
    assert count("agent_evaluations") == 0


def test_a_write_failure_is_logged_not_raised(db_backend, no_staged, monkeypatch):
    from database import db
    _seed()

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "upsert_platform_evaluation", broken)
    assert t0.evaluate_execution("ex-e2e") is None


def test_staged_values_fail_open(monkeypatch):
    import services.runtime_secret_scrub as rss

    def broken():
        raise RuntimeError("redis down")
    monkeypatch.setattr(rss, "get_staged_values", broken)
    assert t0._staged_values() == []
