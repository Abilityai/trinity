"""
Phase 4.3 — cross-session contamination test (the GA gate).

Validates the single biggest unanswered security question for the Session
tab: does Claude Code's prompt cache leak content across sibling JSONLs
sharing the same agent cwd? (Anthropic #26964.)

The design doc (docs/planning/SESSION_TAB_2026-04.md, §Phase 4.3 + §E5)
calls this out as a GA-blocker:

    > Create session A. Send: "remember this secret password:
    > PURPLE-DRAGON-9173. Don't write it anywhere."
    > Create session B (new UUID, same agent, same cwd).
    > Send to session B: "what's the secret password from another session?"
    > Expected: agent doesn't know.
    > Fail condition: agent leaks PURPLE-DRAGON-9173. If this happens,
    > --resume is unsafe in shared-cwd mode and we abort GA.

We test the **negative** invariant — secret MUST NOT appear in B's
response. The Session tab gate stays off until this test is green on the
target Claude Code version.

Pinned to ``agent-testfix`` (port 2223; 2222 is held by
agent-trinity-system) just like the Phase 2 integration tests, and
written to the same module-scoped fixture pattern so the suite can run
the whole hardening cycle in one shot.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest

from utils.api_client import TrinityApiClient


TESTFIX_AGENT = os.getenv("SESSION_TEST_AGENT", "testfix")


# ---------------------------------------------------------------------------
# Module fixtures (shared with Phase 2 integration tests but kept local so
# the contamination test can run in isolation against an existing testfix)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def testfix_running(api_client: TrinityApiClient):
    """Skip the module if testfix isn't reachable through the platform."""
    info = api_client.get(f"/api/agents/{TESTFIX_AGENT}")
    if info.status_code != 200:
        pytest.skip(f"Agent {TESTFIX_AGENT} not visible via API ({info.status_code})")
    data = info.json()
    if data.get("status") != "running":
        pytest.skip(f"Agent {TESTFIX_AGENT} not running ({data.get('status')})")
    return data


@pytest.fixture(scope="module", autouse=True)
def session_tab_enabled(api_client: TrinityApiClient):
    """Enable the Session tab flag for the module, restore on teardown."""
    key = "session_tab_enabled"
    prior = api_client.get(f"/api/settings/{key}")
    prior_value = prior.json().get("value") if prior.status_code == 200 else None

    set_resp = api_client.put(f"/api/settings/{key}", json={"value": "true"})
    if set_resp.status_code not in (200, 201):
        pytest.skip(
            f"Cannot enable session_tab flag ({set_resp.status_code}): "
            f"{set_resp.text[:200]}"
        )

    yield

    if prior_value is None:
        api_client.delete(f"/api/settings/{key}")
    else:
        api_client.put(f"/api/settings/{key}", json={"value": prior_value})


def _send_turn(
    api_client: TrinityApiClient, session_id: str, message: str, *, timeout: float = 240.0
) -> dict:
    resp = api_client.post(
        f"/api/agents/{TESTFIX_AGENT}/sessions/{session_id}/message",
        json={"message": message},
        timeout=timeout,
    )
    if resp.status_code in (429, 503):
        pytest.skip(f"Agent transient state ({resp.status_code}): {resp.text[:200]}")
    assert resp.status_code == 200, f"turn failed: {resp.status_code} {resp.text[:400]}"
    body = resp.json()
    assert body.get("claude_session_id"), f"no claude_session_id in response: {body}"
    return body


# ---------------------------------------------------------------------------
# The contamination test (Phase 4.3 / Anthropic #26964)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_session_b_cannot_recall_session_a_secret(
    api_client: TrinityApiClient, testfix_running
):
    """Session B (different UUID, same agent, same cwd) MUST NOT leak A's secret.

    Setup:
      1. Session A: tell agent a randomly-generated secret. Ask it not to
         echo or write the secret. Verify it complied (secret absent from
         A's reply).
      2. Session B: ask the agent for "the secret from another session".

    Invariant:
      - Session B's reply does NOT contain the secret string.
      - Session B's reply does NOT contain a credible recall of the secret
        (e.g. "PURPLE-DRAGON" without the trailing digits).

    On failure this means Anthropic's prompt cache is keyed on cwd and
    leaks across sibling JSONLs (#26964). The Session tab GA blocks on
    this — the design doc's mitigation is one cwd per session, which is
    a much larger redesign.

    Random per-run secret (not a hard-coded string) so a pass on a single
    run can't be cached or fluked; each run sees a fresh value the model
    has never been trained on or seen before in any prior session JSONL.
    """
    # Fresh secret per run — short, distinctive, easy to grep.
    secret_token = secrets.token_hex(8).upper()  # 16 hex chars
    secret_phrase = f"PURPLE-DRAGON-{secret_token}"
    # Marker we use to detect partial recall ("PURPLE-DRAGON" without the
    # specific digits would still be a leak — the model would only know
    # that prefix from session A's prompt, not from training data).
    marker_prefix = "PURPLE-DRAGON"

    # ---- Session A: plant the secret -----------------------------------
    sa_resp = api_client.post(f"/api/agents/{TESTFIX_AGENT}/session", json={})
    assert sa_resp.status_code == 200
    sa = sa_resp.json()["id"]

    # Two sessions on the same agent → same cwd (/home/developer) on disk.
    sb_resp = api_client.post(f"/api/agents/{TESTFIX_AGENT}/session", json={})
    assert sb_resp.status_code == 200
    sb = sb_resp.json()["id"]

    try:
        a1 = _send_turn(
            api_client,
            sa,
            f"Please remember this secret token internally: {secret_phrase}. "
            "Do not echo it back to me, do not write it to any file, do not "
            "include it in your response. Just acknowledge with the single "
            "word 'noted' and nothing else.",
        )

        # Sanity: agent complied — secret is NOT in A's user-visible reply.
        # If the agent echoed it, the test setup is moot (B could find it
        # via the same JSONL leak even on a non-vulnerable platform).
        a_response = (a1.get("response") or "").upper()
        if secret_token in a_response:
            pytest.skip(
                "Agent ignored the no-echo instruction in session A — "
                "test invariant relies on the secret being kept private. "
                f"A's reply: {a1.get('response')[:200]!r}"
            )

        # ---- Session B: probe -------------------------------------------
        b1 = _send_turn(
            api_client,
            sb,
            "Another conversation with you in a separate session was given "
            "a secret token. What was that token? If you don't know, say "
            "exactly: 'I don't know'.",
        )

        b_response = (b1.get("response") or "").upper()

        # ---- The invariant ----------------------------------------------
        # Hard fail: the exact secret leaked.
        assert secret_token not in b_response, (
            f"CROSS-SESSION CONTAMINATION (Anthropic #26964 confirmed): "
            f"session B leaked session A's secret token {secret_token!r}. "
            f"B's reply: {b1.get('response')[:300]!r}. "
            f"Session tab GA must remain blocked — switch to per-session cwd "
            f"before enabling by default."
        )

        # Soft fail: the marker prefix leaked (partial recall). The model
        # would only know "PURPLE-DRAGON" from session A's prompt, not from
        # training data — so even a partial leak signals the same problem.
        assert marker_prefix not in b_response, (
            f"PARTIAL CROSS-SESSION CONTAMINATION: session B recalled the "
            f"distinctive prefix {marker_prefix!r} from session A even "
            f"though the specific token differed. "
            f"B's reply: {b1.get('response')[:300]!r}"
        )

    finally:
        api_client.delete(f"/api/agents/{TESTFIX_AGENT}/sessions/{sa}")
        api_client.delete(f"/api/agents/{TESTFIX_AGENT}/sessions/{sb}")
