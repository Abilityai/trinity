"""Display label at creation + hardened validation (#1640, ent#181 follow-up 2/5).

#1676 shipped the column, the db setter, `PUT /{name}/label`, the resolver, and
rendering. #1640 adds: set-at-creation (`AgentConfig.display_label`), a shared
normalization + **named** validation (not a generic 422 blob), and confirms the
policy — optional, presentation-only, NOT unique, blank clears to the slug.

Pure/deterministic model-level coverage (no DB, no container).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def M():
    try:
        import models as M
    except ImportError:
        pytest.skip("backend venv required")
    return M


# ---------------------------------------------------------------------------
# normalize_display_label — the single shared policy
# ---------------------------------------------------------------------------

def test_trim_and_blank_clears(M):
    assert M.normalize_display_label("  My Bot  ") == "My Bot"
    assert M.normalize_display_label("   ") is None   # blank → clear to slug
    assert M.normalize_display_label("") is None
    assert M.normalize_display_label(None) is None


def test_unicode_preserved(M):
    assert M.normalize_display_label("Café ☕ 日本語") == "Café ☕ 日本語"


def test_overlength_named_error(M):
    with pytest.raises(ValueError, match="at most 120 characters"):
        M.normalize_display_label("x" * 121)


@pytest.mark.parametrize("bad", ["line\nbreak", "tab\there", "car\rriage", "null\x00byte", "sep here"])
def test_control_chars_rejected_named(M, bad):
    with pytest.raises(ValueError, match="control characters or line breaks"):
        M.normalize_display_label(bad)


def test_exactly_max_len_ok(M):
    label = "a" * 120
    assert M.normalize_display_label(label) == label


# ---------------------------------------------------------------------------
# AgentConfig — set at creation
# ---------------------------------------------------------------------------

def test_agentconfig_normalizes_display_label(M):
    c = M.AgentConfig(name="my-agent", display_label="  Marketing Bot  ")
    assert c.display_label == "Marketing Bot"


def test_agentconfig_display_label_optional_default_none(M):
    c = M.AgentConfig(name="my-agent")
    assert c.display_label is None   # absent → renders under the slug (unchanged)


def test_agentconfig_rejects_bad_label(M):
    with pytest.raises(Exception):
        M.AgentConfig(name="my-agent", display_label="x" * 200)
    with pytest.raises(Exception):
        M.AgentConfig(name="my-agent", display_label="bad\nname")


def test_agentconfig_blank_label_clears(M):
    c = M.AgentConfig(name="my-agent", display_label="   ")
    assert c.display_label is None


# ---------------------------------------------------------------------------
# AgentLabelUpdate — same policy on the post-creation PUT
# ---------------------------------------------------------------------------

def test_label_update_shares_policy(M):
    assert M.AgentLabelUpdate(label="  Hi  ").label == "Hi"
    assert M.AgentLabelUpdate(label="").label is None       # clear
    assert M.AgentLabelUpdate(label=None).label is None     # clear, said explicitly
    with pytest.raises(Exception):
        M.AgentLabelUpdate(label="bad\ttab")


def test_label_update_requires_the_label_field(M):
    """#1821: an omitted or misnamed `label` no longer means "clear".

    It used to, which made a typo'd field name — `display_label`, the DB column,
    right next to the `display_name` response field — indistinguishable from a
    deliberate clear: the unknown key was dropped, `label` defaulted to None, and
    the PUT wiped the label while answering 200. Clearing is still supported and
    unchanged in meaning (see the two cases above); it just has to be stated."""
    with pytest.raises(Exception):
        M.AgentLabelUpdate()                              # empty body
    with pytest.raises(Exception):
        M.AgentLabelUpdate(display_label="Typo Name")     # the actual #1821 trigger


def test_not_unique_policy(M):
    # Two agents may carry the SAME display label — the slug guarantees
    # uniqueness, the label is presentation only. Nothing here enforces
    # uniqueness, by design (#1640).
    a = M.AgentConfig(name="agent-one", display_label="Support")
    b = M.AgentConfig(name="agent-two", display_label="Support")
    assert a.display_label == b.display_label == "Support"
