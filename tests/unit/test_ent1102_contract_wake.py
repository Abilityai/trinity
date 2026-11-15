"""ent#1102 — the delegation contract teaches the wake, on both surfaces.

ent#568 made the contract ONE text carried verbatim by the platform prompt and
by the MCP ``chat_with_agent`` / ``chat_with_<agent>`` descriptions, and
``test_ent568_delegation_contract.py`` pins that parity byte for byte. What this
file adds is the ent#1102 fact itself: a receipt now says whether the caller
will be *told* the run ended, so the contract's own advice changes — "set a
reminder and end your turn" was the only way to finish later, and for a
``wake: on_ending`` receipt it is now the wrong move.

Both copies are asserted separately on the wake text. The ent#568 parity test
would catch a one-sided edit, but it reports it as "the copies differ", which
is the same message for any drift; these tests name which surface is missing
the wake. The budget is pinned where it bites — the published MCP description,
in ``src/mcp-server/src/delegation-contract.test.ts`` — because the contract
shares Claude Code's 2,048-char description cap with each tool's own lead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import (
    DELEGATION_CONTRACT,
    get_platform_system_prompt,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_CONTRACT = REPO_ROOT / "src" / "mcp-server" / "src" / "delegation_contract.ts"
CHAT_TS = REPO_ROOT / "src" / "mcp-server" / "src" / "tools" / "chat.ts"

_LINES_DECL = re.compile(
    r"^export const DELEGATION_CONTRACT_LINES(?::[^=\n]+)? = \[\n(?P<body>.*?)\n\];$",
    re.M | re.S,
)


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


def _ts_lines() -> list[str]:
    m = _LINES_DECL.search(TS_CONTRACT.read_text(encoding="utf-8"))
    assert m, "DELEGATION_CONTRACT_LINES is not a plain array"
    body = re.sub(r",\s*\Z", "", m.group("body").strip())
    return json.loads(f"[{body}]")


def _wake_bullet(text: str) -> str:
    """The one bullet that teaches the wake field."""
    bullets = [b for b in text.split("\n") if "`wake`" in b]
    assert len(bullets) == 1, f"expected exactly one wake bullet, got {len(bullets)}: {bullets}"
    return bullets[0]


# ---------------------------------------------------------------------------
# The fact: a receipt says whether the ending is brought to you
# ---------------------------------------------------------------------------

def test_the_contract_names_the_wake_field_and_both_its_values():
    bullet = _wake_bullet(DELEGATION_CONTRACT)
    assert "`on_ending`" in bullet
    assert "`none`" in bullet
    assert "`wake_reason`" in bullet


def test_on_ending_tells_the_caller_to_end_its_turn_without_a_reminder():
    """The whole point of ent#1102: the retrospective's cost was callers
    polling and re-dispatching runs that had already finished. A receipt that
    promises a wake must say plainly that ending the turn is correct, and that
    a reminder for that outcome is not."""
    bullet = _wake_bullet(DELEGATION_CONTRACT)
    on_ending = bullet.split("`on_ending`", 1)[1].split("`none`", 1)[0]
    assert "end your turn" in on_ending
    assert "reminder" in on_ending


def test_none_still_points_at_the_fallback_the_caller_must_run_itself():
    """`wake: none` is the honest-status half (Product Quality Bar): nobody is
    coming, so the pre-ent#1102 advice still applies and must still be here."""
    bullet = _wake_bullet(DELEGATION_CONTRACT)
    tail = bullet.split("`none`", 1)[1]
    assert "`set_reminder`" in tail


def test_the_reminder_advice_is_no_longer_unconditional():
    """Before ent#1102 the contract told every async caller to set a reminder.
    That sentence must not survive beside the wake bullet, or the text gives
    two contradictory instructions for the same receipt."""
    others = [b for b in DELEGATION_CONTRACT.split("\n")
              if "`set_reminder`" in b and "`wake`" not in b]
    for bullet in others:
        assert "To finish later" not in bullet, (
            "an unconditional 'to finish later, set a reminder' survives outside "
            f"the wake bullet: {bullet}"
        )


# ---------------------------------------------------------------------------
# Both surfaces, named separately
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", ["claude-code", "codex", "gemini-cli", ""])
def test_every_agent_reads_the_wake_in_its_prompt(runtime):
    assert _wake_bullet(get_platform_system_prompt(runtime))


def test_the_mcp_copy_carries_the_same_wake_bullet():
    """The only copy an external MCP client, or an agent at PromptTier.MINIMAL,
    ever sees."""
    assert _wake_bullet("\n".join(_ts_lines())) == _wake_bullet(DELEGATION_CONTRACT)


# ---------------------------------------------------------------------------
# Budget: the contract shares a 2,048-char cap with chat_with_agent's lead
# ---------------------------------------------------------------------------

def test_the_contract_plus_chat_with_agents_lead_still_fits_the_description_cap():
    """A source-level smoke check on the real binding constraint. The published
    description is pinned over a real transport in
    ``src/mcp-server/src/delegation-contract.test.ts``; this fails the Python
    lane too, because a contract that grew past the cap is cut in the middle and
    the model simply never reads the end of it.

    @source-text-pin: the lead is a TypeScript expression; the live consumer of
    its LENGTH is Claude Code's cap, and the only Python-side way to see the
    lead at all is to read it.
    """
    src = CHAT_TS.read_text(encoding="utf-8")
    lead = re.search(
        r'description:\s*\n?\s*("Delegate a task.*?)\+\s*\n\s*DELEGATION_CONTRACT',
        src, re.S)
    assert lead, "chat_with_agent's description is no longer `<lead> + DELEGATION_CONTRACT`"
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', lead.group(1))
    assert literals, "no string literals found in the lead"
    lead_text = "".join(json.loads(f'"{lit}"') for lit in literals)
    published = len(lead_text) + len(DELEGATION_CONTRACT)
    assert published <= 2048, (
        f"chat_with_agent would publish {published} chars; Claude Code cuts at 2,048, "
        "so the end of the contract would never be read"
    )
