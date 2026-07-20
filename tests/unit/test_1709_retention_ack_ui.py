"""#1709 — the retention blast-radius guard must keep an in-product approval path.

`#1644` ships the guard with `FLOOR_AGENTS = 0` ("every purge destroys Docker
volumes — always ack"), so every agent hard-purge is refused until an admin
acknowledges via `POST /api/settings/retention/acknowledge`. That endpoint is
*inert* without a frontend caller — which was the bug: the `#834` purge sweep and
`#1581`'s volume reclaim could never run.

These are static guards over `Settings.vue`. They fail if the UI ever loses the
approval control again (the exact regression #1709 fixed), which no backend test
can catch — the endpoint keeps working; it just becomes unreachable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_SETTINGS_VUE = (
    Path(__file__).resolve().parents[2]
    / "src" / "frontend" / "src" / "views" / "Settings.vue"
)


@pytest.fixture(scope="module")
def settings_src() -> str:
    assert _SETTINGS_VUE.exists(), f"Settings.vue not found at {_SETTINGS_VUE}"
    return _SETTINGS_VUE.read_text(encoding="utf-8")


def test_settings_vue_calls_the_retention_acknowledge_endpoint(settings_src):
    """The panel must POST to the ack endpoint — otherwise the guard is inert."""
    assert "/api/settings/retention/acknowledge" in settings_src, (
        "Settings.vue no longer calls POST /api/settings/retention/acknowledge. "
        "The retention guard's approval endpoint is unreachable again (#1709): "
        "every agent hard-purge and the #1581 volume reclaim will be refused "
        "forever, with no operator way to approve."
    )


def test_acknowledge_call_sends_the_window(settings_src):
    """The ack is window-bound (endpoint 409s on mismatch), so the caller must
    send `window_days` — approving blind would fail or authorize the wrong window."""
    idx = settings_src.find("/api/settings/retention/acknowledge")
    # window_days must appear near the call site, not just anywhere in the file.
    nearby = settings_src[idx: idx + 400]
    assert "window_days" in nearby, (
        "the acknowledge POST must include window_days (the endpoint binds the "
        "approval to the window in force and 409s on mismatch)."
    )


def test_settings_vue_renders_pending_acknowledgements_from_the_get(settings_src):
    """The approve control is driven by GET /retention's pending list — without
    rendering it the admin has nothing to approve from (honest empty/prompt)."""
    assert "pending_acknowledgements" in settings_src, (
        "Settings.vue must render retention.pending_acknowledgements — the field "
        "GET /api/settings/retention now returns to prompt the admin (#1709)."
    )
