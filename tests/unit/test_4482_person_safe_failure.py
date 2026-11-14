"""A failed background turn reports a person-safe sentence, never the raw error (#4482).

The execution row's error is the operator's diagnosis: the subscription walk
trail names subscriptions ("Tried: <name> (auth); every candidate refused.")
and the auth message tells the reader to "Check subscription token or API key
configuration". A completion report is read by a Workspace client, or by the
members of a Slack/Telegram channel, so every non-success report goes through
one helper that maps the failure to a fixed sentence. The raw text stays on the
execution row and the operator alert.

Module: src/backend/services/channel_completion_report.py
Issue:  https://github.com/Abilityai/trinity/issues/4482
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import channel_completion_report as ccr  # noqa: E402

pytestmark = pytest.mark.unit

SUB = "alice-max-plan"
AUTH_TRAIL = (
    "Authentication failure: Failed to authenticate. API Error: 401 OAuth access "
    "token is invalid.. Check subscription token or API key configuration. "
    f"Tried: {SUB} (auth); every candidate refused."
)
RATE_TRAIL = (
    "Execution failed: usage cap. "
    f"Tried: {SUB} (rate limit) -> team-pool (rate limit); every candidate refused."
)
CAP_TRAIL = f"Execution failed. Tried: {SUB} (auth); stopped: attempt cap reached."
OPERATOR_WORDS = (SUB, "team-pool", "Check subscription", "Tried:", "API key", "OAuth")


def _leaks(text: str) -> list:
    return [w for w in OPERATOR_WORDS if w in text]


@pytest.mark.parametrize("error", [AUTH_TRAIL, RATE_TRAIL, CAP_TRAIL])
def test_a_subscription_walk_reads_as_usage_limit(error):
    text = ccr.person_safe_failure("failed", error)
    assert text == ccr.USAGE_LIMIT_FAILURE
    assert _leaks(text) == []


@pytest.mark.parametrize("error", [
    "GitHub API returned 403 for repo foo/bar",
    "API rate limit exceeded for this token",
    "the accurate limit was not met",
    "git: Authentication failed for https://github.com/foo/bar",
])
def test_an_agents_own_failed_call_is_not_a_usage_limit(error):
    """Only the platform's walk trail means the credential pool refused the
    turn; an agent's own failed GitHub call is not it running out of usage."""
    assert ccr.person_safe_failure("failed", error) == ccr.GENERIC_FAILURE


def test_timeout_reads_as_timeout():
    assert ccr.person_safe_failure("failed", "Execution timed out after 3600s") == ccr.TIMEOUT_FAILURE


def test_cancelled_is_a_fixed_line_even_when_the_row_carries_an_operator_error():
    """On a pull-pilot agent a terminated turn's row keeps the failure body (#3603)."""
    assert ccr.person_safe_failure("cancelled", AUTH_TRAIL) == ccr.CANCELLED_FAILURE


def test_an_agent_refusal_is_passed_through():
    """The refusal is written for the person (ent#926) and must not be replaced."""
    refusal = "[turn_refused] I only take requests from the finance team."
    text = ccr.person_safe_failure("failed", refusal)
    assert text == "I only take requests from the finance team."


def test_a_refusal_carrying_the_walk_trail_never_shows_it():
    """The trail is checked before the refusal pass-through, so a refusal text
    with a trail appended cannot carry subscription names out."""
    refusal = f"[turn_refused] I only take requests from the finance team. Tried: {SUB} (auth); every candidate refused."
    text = ccr.person_safe_failure("failed", refusal)
    assert _leaks(text) == []


def test_anything_else_is_the_generic_line():
    assert ccr.person_safe_failure("failed", "Traceback: KeyError 'x'") == ccr.GENERIC_FAILURE
    assert ccr.person_safe_failure("failed", None) == ccr.GENERIC_FAILURE


@pytest.mark.parametrize("error", [AUTH_TRAIL, RATE_TRAIL, CAP_TRAIL])
def test_portal_body_never_shows_the_trail(error):
    body = ccr._portal_body(executing_agent="a", session_agent="a",
                            status="failed", summary_or_error=error)
    assert body == f"**Didn't finish** — failed\n\n{ccr.USAGE_LIMIT_FAILURE}"
    assert _leaks(body) == []


@pytest.mark.parametrize("error", [AUTH_TRAIL, RATE_TRAIL, CAP_TRAIL])
def test_slack_and_telegram_summary_never_shows_the_trail(error):
    text = ccr._summarize("failed", error, executing_agent="worker")
    assert text.endswith(ccr.USAGE_LIMIT_FAILURE)
    assert _leaks(text) == []


def test_a_failure_carrying_a_token_posts_neither_token_nor_text():
    token = "sk-ant-api03-" + "D" * 24
    leaky = f"boom: ANTHROPIC_API_KEY={token} while cloning"
    for text in (ccr._summarize("failed", leaky),
                 ccr._portal_body(executing_agent="a", session_agent="a",
                                  status="failed", summary_or_error=leaky)):
        assert token not in text and "while cloning" not in text
        assert text.endswith(ccr.GENERIC_FAILURE)


def test_the_refusal_check_failing_never_falls_back_to_raw_text(monkeypatch):
    import services.public_chat_service as pcs

    def boom(*_a, **_k):
        raise RuntimeError("import-time failure")
    monkeypatch.setattr(pcs, "is_turn_refused", boom)
    assert ccr.person_safe_failure("failed", "Traceback: secret detail") == ccr.GENERIC_FAILURE


def test_success_text_is_still_reported():
    body = ccr._portal_body(executing_agent="a", session_agent="a",
                            status="success", summary_or_error="Report attached.")
    assert "Report attached." in body


def test_work_card_error_is_person_safe():
    from datetime import datetime, timezone
    from client_portal.work import service as work

    row = dict(id="e1", agent_name="a", status="failed", message="nightly",
               started_at="2026-11-13T10:00:00Z", completed_at="2026-11-13T10:01:00Z",
               duration_ms=60000, triggered_by="schedule", source_user_email=None,
               source_agent_name=None, source_channel="portal",
               source_channel_chat_id="s1", loop_id=None, error_summary=AUTH_TRAIL)
    item = work._project(row, email="c@example.com", roster={"a"}, turn_timeout=3600,
                         now=datetime(2026, 11, 13, 10, 5, tzinfo=timezone.utc))
    assert item.outcome == "failed"
    assert item.error == ccr.USAGE_LIMIT_FAILURE
    assert _leaks(item.error) == []


def test_work_card_keeps_the_timeout_outcome():
    """`work_outcome` still reads the raw error, so a timeout stays a timeout."""
    from datetime import datetime, timezone
    from client_portal.work import service as work

    row = dict(id="e1", agent_name="a", status="failed", message="nightly",
               started_at="2026-11-13T10:00:00Z", completed_at="2026-11-13T11:00:00Z",
               duration_ms=3600000, triggered_by="schedule", source_user_email=None,
               source_agent_name=None, source_channel="portal",
               source_channel_chat_id="s1", loop_id=None,
               error_summary="Execution timed out after 3600s")
    item = work._project(row, email="c@example.com", roster={"a"}, turn_timeout=3600,
                         now=datetime(2026, 11, 13, 11, 5, tzinfo=timezone.utc))
    assert item.outcome == "timeout"
    assert item.error == ccr.TIMEOUT_FAILURE


def test_the_own_turn_switch_message_never_names_the_subscription(monkeypatch):
    """Execute the `portal_chat` AUTH/BILLING branch's own statements (the
    test_3470 harness): a switch onto a named subscription must not name it."""
    import inspect
    import textwrap
    from types import SimpleNamespace
    from client_portal import service as portal

    src = inspect.getsource(portal)
    head = 'if code in ("AUTH", "BILLING"):'
    i = src.index(head)
    branch = textwrap.dedent(src[i + len(head):src.index('if code == "CAPACITY"', i)])
    ns = {"ClientPortalError": portal.ClientPortalError,
          "_usage_limit_detail": lambda *a, **k: "LIMIT", "agent_name": "scribe",
          "result": SimpleNamespace(subscription_switch={
              "switched": True, "retried": False, "exhausted": False,
              "new_subscription": SUB})}
    with pytest.raises(portal.ClientPortalError) as exc:
        exec(branch, ns)
    assert exc.value.category == "auth_switched"
    assert "moved onto another subscription" in exc.value.detail
    assert SUB not in exc.value.detail
