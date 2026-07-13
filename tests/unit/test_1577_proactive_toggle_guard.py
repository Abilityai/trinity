"""Regression guard for #1577 — the proactive-messaging toggle must not be
silently dropped again.

The per-recipient `allow_proactive` toggle was lost when the Sharing tab's Team
Sharing section was replaced by the Access tab (#1317) — a UI-only regression
with no test to catch it. This static guard asserts the toggle's UI + endpoint
wiring survive future panel refactors (the frontend has no JS unit runner, so a
source-level assertion is the runnable guard here; a full render e2e is a
`ui`-labeled follow-up).
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_ACCESS_PANEL = _ROOT / "src" / "frontend" / "src" / "components" / "AccessPanel.vue"
_AGENTS_STORE = _ROOT / "src" / "frontend" / "src" / "stores" / "agents.js"


def test_access_panel_renders_a_proactive_toggle():
    """The Access-tab operator rows must expose the allow_proactive toggle."""
    src = _ACCESS_PANEL.read_text()
    assert "allow_proactive" in src, "AccessPanel lost the allow_proactive binding (#1577)"
    assert "onToggleProactive" in src, "AccessPanel lost the proactive toggle handler (#1577)"
    # An actual interactive control bound to the flag (checkbox reflecting state).
    assert re.search(r'type="checkbox"', src), "proactive toggle control missing"
    assert ":checked=\"op.allow_proactive\"" in src, "toggle not bound to the persisted flag"


def test_toggle_calls_the_proactive_endpoint():
    """The change handler must persist via the store, which hits the backend
    PUT .../shares/proactive endpoint (honest status — not optimistic-only)."""
    panel = _ACCESS_PANEL.read_text()
    store = _AGENTS_STORE.read_text()
    # Panel delegates to the store action on toggle…
    assert "setProactive" in panel, "AccessPanel no longer calls agentsStore.setProactive (#1577)"
    # …and the store action PUTs to the real backend endpoint with the flag.
    assert "setProactive" in store, "agents store lost the setProactive action (#1577)"
    m = re.search(r"async setProactive\s*\(.*?\)\s*\{(.*?)\n    \},", store, re.DOTALL)
    assert m, "setProactive action not found in agents store"
    body = m.group(1)
    assert "shares/proactive" in body, "setProactive must PUT to /shares/proactive"
    assert "allow_proactive" in body, "setProactive must send the allow_proactive flag"


def test_owner_always_allowed_is_communicated():
    """AC: the owner is always allowed — surfaced so there's no misleading state."""
    src = _ACCESS_PANEL.read_text()
    assert re.search(r"owner always receives proactive", src, re.IGNORECASE), \
        "Access tab should state that the owner always receives proactive messages (#1577)"
