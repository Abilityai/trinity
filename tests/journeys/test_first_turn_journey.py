"""Journey J03: a person's first turn with an agent produces real output (#2336).

**Credential-bound, and that is a real constraint rather than an oversight.**
"Real output" means a real model answered, which needs a provider key. Every
PR-triggered workflow in this repo is deliberately credential-free —
`integration-nightly.yml` states the reason in full: `pull_request`-family
triggers expose repository secrets to fork PRs while running arbitrary shell
from the PR's own tree, so a key named there is readable by anyone who opens a
PR.

So this journey runs where a key can safely exist (a developer's stack, the
nightly), and the per-PR gate runs the credential-free lifecycle journey beside
it. The skip below is allowlisted in `tests/harness/audit_skips.py` — visible,
reasoned, and never silent.
"""
import os

import pytest

from .conftest import poll_until

pytestmark = pytest.mark.journey

FIRST_TURN_DEADLINE_S = float(os.getenv("JOURNEY_FIRST_TURN_DEADLINE_S", "180"))


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") == "placeholder",
    reason="journey needs a real provider key",
)
def test_first_chat_turn_returns_real_output(journey_client, journey_agent):
    """Send the first message and get a real answer back.

    Asserts the promise, not the plumbing: a non-empty response the platform
    persisted, reachable afterwards through the history the user would read.
    """
    name = journey_agent["name"]

    resp = journey_client.post(
        f"/api/agents/{name}/chat",
        json={"message": "Reply with the single word: ready"},
        timeout=FIRST_TURN_DEADLINE_S,
    )
    assert resp.status_code == 200, (
        f"first chat turn with '{name}' answered {resp.status_code}: "
        f"{resp.text[:400]} — this is the first thing a person does after "
        f"creating an agent"
    )
    body = resp.json()
    answer = (body.get("response") or "").strip()
    assert answer, (
        f"agent '{name}' answered the first turn with an EMPTY response. "
        f"A blank reply is indistinguishable from a working agent in every "
        f"status field, which is why this asserts the words and not the 200."
    )

    # ...and the platform kept it, so the person can come back to it.
    def persisted():
        hist = journey_client.get(f"/api/agents/{name}/chat/history/persistent")
        if hist.status_code != 200:
            return None
        messages = hist.json().get("messages") or hist.json() or []
        return messages if messages else None

    poll_until(
        persisted,
        deadline_s=30,
        describe=(
            f"agent '{name}' answered the first turn but nothing was persisted — "
            f"the conversation would be gone on reload"
        ),
    )
