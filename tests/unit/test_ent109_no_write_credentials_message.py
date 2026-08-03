"""
The `no_write_credentials` surfaces point at the in-place retrofit (ent#109 §6).

ent#230's sharpest acceptance criterion, and the one #109 omitted: *"the
`no_write_credentials` error surfaces point at this action once it exists."*
Ship the feature and leave the strings, and the product keeps teaching the
manual workaround it just replaced — create a new agent with fork-to-own and
import your data, which discards the agent's identity, its 180-day name
reservation and its history.

Two surfaces change together (Invariant #13 — backend, agent-server, MCP must
stay in sync), so this module guards both:

* the backend constant `git_service.NO_WRITE_CREDENTIALS_MESSAGE`, which
  `sync_to_github` and `reset_to_main_preserve_state` return and
  `routers/git.py` maps to a 409;
* the MCP 409 hint in `src/mcp-server/src/tools/git.ts`, which has no Python
  import and is therefore checked by a source guard (the
  `test_agent_auth_header_guard.py` idiom).

**A third, agent-side surface is deliberately unchanged**: the push-remote
blackhole sentinel in `docker/base-image/startup.sh`, which surfaces verbatim
in `git push` stderr. It already names an action and a token, and editing it
would force a base-image rebuild — a release-ordering requirement — for a
cosmetic wording change. Asserted below so it stays a decision.

Modules: src/backend/services/git_service.py
         src/mcp-server/src/tools/git.ts
         docker/base-image/startup.sh
Issue:   abilityai/trinity-enterprise#109 (folds ent#230 AC)
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path


os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_message.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.git_service import NO_WRITE_CREDENTIALS_MESSAGE  # noqa: E402

_GIT_TS = _PROJECT_ROOT / "src" / "mcp-server" / "src" / "tools" / "git.ts"
_STARTUP_SH = _PROJECT_ROOT / "docker" / "base-image" / "startup.sh"

# The workaround being retired, in the phrasings both surfaces used.
_RETIRED_PHRASES = (
    "create a new agent with fork-to-own",
    "fork-to-own a new agent",
    "import your data",
    "Files → Export",
)


def _mcp_hint_block() -> str:
    """The `no_write_credentials` hint expression in `git.ts`, and only it.

    Anchored on the owning conditional rather than the whole file, and the
    anchor is ASSERTED rather than sliced blindly: `str.find` returns -1 on a
    miss, which slices to `''`, and a guard that asserts against an empty
    string passes forever while checking nothing (the exact way a source-grep
    guard dies silently under a refactor).
    """
    src = _GIT_TS.read_text()
    start = src.find('error.conflictType === "no_write_credentials"')
    assert start != -1, (
        f"anchor 'error.conflictType === \"no_write_credentials\"' not found in "
        f"{_GIT_TS}. The MCP 409 hint moved or was renamed — re-anchor this "
        f"guard on its new home; do NOT widen it to the whole file."
    )
    end = src.find(": ", start)
    assert end != -1 and end > start, (
        "found the conflictType anchor but not the ternary's else-arm — the "
        "hint's shape changed; re-read it before trusting this guard."
    )
    return src[start:end]


class TestBackendMessage:
    def test_points_at_binding_this_agent(self):
        low = NO_WRITE_CREDENTIALS_MESSAGE.lower()
        assert "bind to your own repo" in low
        assert "git tab" in low
        # The alternative remedy survives — some users just want to add a token.
        assert "github token" in low

    def test_no_longer_teaches_the_create_a_new_agent_workaround(self):
        low = NO_WRITE_CREDENTIALS_MESSAGE.lower()
        for phrase in _RETIRED_PHRASES:
            assert phrase.lower() not in low, (
                f"NO_WRITE_CREDENTIALS_MESSAGE still teaches the retired "
                f"workaround: {phrase!r}"
            )

    def test_still_explains_why_the_agent_cannot_push(self):
        """The message must stay diagnostic, not just prescriptive — the user
        needs to know this is a tokenless public-template agent (ent#123)."""
        low = NO_WRITE_CREDENTIALS_MESSAGE.lower()
        assert "no write credentials" in low
        assert "read-only" in low

    def test_promises_the_work_is_kept(self):
        """The whole point of the retrofit over the workaround."""
        assert "learned" in NO_WRITE_CREDENTIALS_MESSAGE.lower()


class TestMcpHintParity:
    def test_mcp_hint_points_at_binding_too(self):
        hint = _mcp_hint_block().lower()
        assert "bind this agent to a repo you own" in hint
        assert "github token" in hint

    def test_mcp_hint_no_longer_teaches_the_workaround(self):
        hint = _mcp_hint_block().lower()
        for phrase in _RETIRED_PHRASES:
            assert phrase.lower() not in hint, (
                f"the MCP 409 hint still teaches the retired workaround: "
                f"{phrase!r} — Invariant #13, both surfaces change together"
            )

    def test_mcp_hint_still_suppresses_the_chat_remedy(self):
        """ent#123's reason for the carve-out is unchanged: a chat turn cannot
        conjure credentials, so this branch must not point at chat_with_agent."""
        hint = _mcp_hint_block()
        assert "chat_with_agent" not in hint
        assert "chat turn cannot fix this" in hint.lower()

    def test_both_surfaces_name_the_same_action(self):
        """Invariant #13's actual requirement: not identical prose, but the same
        remedy. Two surfaces naming two different actions is the drift."""
        backend = NO_WRITE_CREDENTIALS_MESSAGE.lower()
        mcp = _mcp_hint_block().lower()
        assert "bind" in backend and "bind" in mcp
        assert "own" in backend and "own" in mcp


class TestAgentSideSentinelUnchanged:
    """The third surface, deliberately left alone — recorded as a decision."""

    def test_blackhole_sentinel_still_present_and_self_describing(self):
        src = _STARTUP_SH.read_text()
        assert "no-write-credentials--this-agent-is-read-only" in src, (
            "the push-remote blackhole sentinel is what makes an in-container "
            "`git push` fail legibly for a tokenless agent (ent#123 FR-4)"
        )

    def test_sentinel_is_out_of_scope_for_the_string_retirement(self):
        """It is a git remote URL, so it must stay a single shell-safe token —
        and changing it forces a base-image rebuild. Confirmed, not discovered.
        """
        src = _STARTUP_SH.read_text()
        match = re.search(r'"(no-write-credentials--[^"]*)"', src)
        assert match, "sentinel literal not found in startup.sh"
        sentinel = match.group(1)
        assert " " not in sentinel, "the sentinel is a remote URL — no spaces"
        assert (
            "add-a-github-token-to-push" in sentinel
        ), "it must keep naming a remedy the user can act on"
