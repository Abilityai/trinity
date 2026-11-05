"""Tool-access profiles — the pure policy (trinity-enterprise#341, GUARD-008).

`services/tool_access_profiles.py` owns the bundle table, the legacy
derivation, the "applied" check, the plan for each writer and the per-runtime
coverage copy. Everything here runs the real functions over plain dicts; the
routes that call them are pinned in `test_ent341_tool_access_routes.py`.

Related: docs/memory/requirements/security.md §28.10,
docs/memory/feature-flows/agent-guardrails.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import tool_access_profiles as tap  # noqa: E402

SEND = [
    "mcp__trinity__send_message",
    "mcp__trinity__send_group_message",
    "mcp__trinity__send_notification",
    "mcp__trinity__send_voice_reply",
    "mcp__trinity__call_user",
    "mcp__trinity__post_to_room",
    "mcp__trinity__share_file",
]
REPORT = "mcp__trinity__report"


# ---------------------------------------------------------------------------
# The bundle table (the design gate's approved values)
# ---------------------------------------------------------------------------

class TestBundles:
    def test_profile_ids_in_order(self):
        assert tap.PROFILE_IDS == (
            "full", "workspace_only", "read_only_report", "strict_read_only", "custom",
        )

    @pytest.mark.parametrize("profile,read_only,tools", [
        ("full", False, []),
        ("workspace_only", False, SEND),
        ("read_only_report", True, SEND),
        ("strict_read_only", True, SEND + [REPORT]),
    ])
    def test_named_bundle(self, profile, read_only, tools):
        bundle = tap.BUNDLES[profile]
        assert bundle.read_only is read_only
        assert list(bundle.disallowed_tools) == tools

    def test_send_list_is_closed_at_seven(self):
        assert list(tap.SEND_TOOLS) == [t.removeprefix("mcp__trinity__") for t in SEND]

    def test_every_profile_has_a_label_and_a_statement(self):
        for pid in tap.PROFILE_IDS:
            copy = tap.PROFILE_COPY[pid]
            assert copy["label"] and copy["statement"]

    def test_custom_helper_names_the_common_tools(self):
        for name in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
            assert name in tap.PROFILE_COPY["custom"]["statement"]


# ---------------------------------------------------------------------------
# Resolution — stored profile, legacy derivation, "applied"
# ---------------------------------------------------------------------------

class TestResolve:
    def test_untouched_agent_is_full_and_applied(self):
        r = tap.resolve({}, False)
        assert (r["profile"], r["derived"], r["applied"]) == ("full", True, True)
        assert r["read_only"] is False and r["disallowed_tools"] == []

    def test_legacy_read_only_agent_reads_strict_not_applied(self):
        r = tap.resolve({"max_turns_chat": 40}, True)
        assert (r["profile"], r["derived"], r["applied"]) == ("strict_read_only", True, False)

    def test_legacy_list_reads_custom(self):
        r = tap.resolve({"disallowed_tools": ["WebFetch"]}, False)
        assert (r["profile"], r["derived"], r["applied"]) == ("custom", True, True)
        assert r["custom"] == {"read_only": False, "disallowed_tools": ["WebFetch"]}

    def test_legacy_read_only_with_list_reads_custom_with_read_only(self):
        r = tap.resolve({"disallowed_tools": ["Bash"]}, True)
        assert r["profile"] == "custom"
        assert r["custom"] == {"read_only": True, "disallowed_tools": ["Bash"]}

    def test_stored_profile_wins_and_is_applied_when_enforced_matches(self):
        cfg = {"disallowed_tools": list(SEND), "tool_access": {"profile": "workspace_only"}}
        r = tap.resolve(cfg, False)
        assert (r["profile"], r["derived"], r["applied"]) == ("workspace_only", False, True)

    def test_stored_profile_not_applied_when_bundle_moved(self):
        # A later release adds a tool to the bundle: the stored row no longer
        # matches, and that reads as "not applied", never as silently changed.
        cfg = {"disallowed_tools": SEND[:6], "tool_access": {"profile": "workspace_only"}}
        assert tap.resolve(cfg, False)["applied"] is False

    def test_order_of_the_enforced_list_does_not_matter(self):
        cfg = {"disallowed_tools": list(reversed(SEND)), "tool_access": {"profile": "workspace_only"}}
        assert tap.resolve(cfg, False)["applied"] is True

    def test_unknown_stored_profile_falls_back_to_derivation(self):
        cfg = {"tool_access": {"profile": "made-up"}}
        r = tap.resolve(cfg, True)
        assert (r["profile"], r["derived"]) == ("strict_read_only", True)

    def test_malformed_tool_access_is_ignored(self):
        assert tap.resolve({"tool_access": "strict"}, False)["profile"] == "full"

    def test_before_strict_is_reported_only_while_strict(self):
        cfg = {"disallowed_tools": SEND + [REPORT],
               "tool_access": {"profile": "strict_read_only", "before_strict": "workspace_only"}}
        assert tap.resolve(cfg, True)["before_strict"] == "workspace_only"
        cfg2 = {"tool_access": {"profile": "full", "before_strict": "workspace_only"}}
        assert tap.resolve(cfg2, False)["before_strict"] is None


# ---------------------------------------------------------------------------
# Selecting a profile (the tool-access PUT)
# ---------------------------------------------------------------------------

class TestPlanSelection:
    def test_named_profile_writes_the_enforced_fields(self):
        cfg, ro = tap.plan_selection({"max_turns_chat": 40}, False, "strict_read_only")
        assert ro is True
        assert cfg["disallowed_tools"] == SEND + [REPORT]
        assert cfg["tool_access"]["profile"] == "strict_read_only"
        assert cfg["max_turns_chat"] == 40  # unrelated keys untouched

    def test_full_removes_the_list_key(self):
        start = {"disallowed_tools": list(SEND), "tool_access": {"profile": "workspace_only"}}
        cfg, ro = tap.plan_selection(start, False, "full")
        assert "disallowed_tools" not in cfg and ro is False

    def test_custom_with_settings(self):
        cfg, ro = tap.plan_selection({}, False, "custom",
                                     custom={"read_only": True, "disallowed_tools": ["Bash", "WebFetch"]})
        assert ro is True and cfg["disallowed_tools"] == ["Bash", "WebFetch"]
        assert cfg["tool_access"]["custom"] == {"read_only": True, "disallowed_tools": ["Bash", "WebFetch"]}

    def test_custom_is_remembered_across_a_named_profile(self):
        cfg, ro = tap.plan_selection({}, False, "custom",
                                     custom={"read_only": True, "disallowed_tools": ["Bash"]})
        cfg, ro = tap.plan_selection(cfg, ro, "workspace_only")
        assert cfg["disallowed_tools"] == SEND and ro is False
        cfg, ro = tap.plan_selection(cfg, ro, "custom")
        assert cfg["disallowed_tools"] == ["Bash"] and ro is True

    def test_a_legacy_custom_list_is_remembered_when_leaving_it(self):
        cfg, ro = tap.plan_selection({"disallowed_tools": ["WebFetch"]}, True, "full")
        assert ro is False and "disallowed_tools" not in cfg
        cfg, ro = tap.plan_selection(cfg, ro, "custom")
        assert cfg["disallowed_tools"] == ["WebFetch"] and ro is True

    def test_selector_save_clears_before_strict(self):
        start = {"disallowed_tools": SEND + [REPORT],
                 "tool_access": {"profile": "strict_read_only", "before_strict": "custom"}}
        cfg, _ = tap.plan_selection(start, True, "strict_read_only")
        assert "before_strict" not in cfg["tool_access"]

    @pytest.mark.parametrize("bad", ["", "admin", None, 3])
    def test_unknown_profile_is_a_named_error(self, bad):
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_selection({}, False, bad)
        assert e.value.status == 400 and "profile" in e.value.detail

    def test_custom_settings_only_with_custom(self):
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_selection({}, False, "full", custom={"read_only": False, "disallowed_tools": []})
        assert e.value.status == 400

    @pytest.mark.parametrize("custom,field", [
        ({"read_only": "yes", "disallowed_tools": []}, "read_only"),
        ({"read_only": False, "disallowed_tools": "Bash"}, "disallowed_tools"),
        ({"read_only": False, "disallowed_tools": [""]}, "disallowed_tools"),
        ({"read_only": False, "disallowed_tools": ["x" * 257]}, "disallowed_tools"),
        ({"read_only": False, "disallowed_tools": ["T"] * 51}, "disallowed_tools"),
        ({"read_only": False, "disallowed_tools": [5]}, "disallowed_tools"),
    ])
    def test_custom_validation_names_the_field(self, custom, field):
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_selection({}, False, "custom", custom=custom)
        assert e.value.status == 400 and field in e.value.detail

    def test_custom_list_is_trimmed_and_deduplicated(self):
        cfg, _ = tap.plan_selection({}, False, "custom",
                                    custom={"read_only": False, "disallowed_tools": [" Bash ", "Bash", "Edit"]})
        assert cfg["disallowed_tools"] == ["Bash", "Edit"]


# ---------------------------------------------------------------------------
# The header / list Read-only toggle (ruling 5)
# ---------------------------------------------------------------------------

class TestShortcut:
    @pytest.mark.parametrize("start_profile,custom", [
        ("full", None),
        ("workspace_only", None),
        ("read_only_report", None),
        ("custom", {"read_only": True, "disallowed_tools": ["mcp__trinity__send_message"]}),
        ("custom", {"read_only": False, "disallowed_tools": []}),
    ])
    def test_on_then_off_returns_to_the_profile_it_came_from(self, start_profile, custom):
        cfg, ro = tap.plan_selection({}, False, start_profile, custom=custom)
        before = tap.resolve(cfg, ro)
        cfg, ro = tap.plan_shortcut(cfg, ro, True)
        on = tap.resolve(cfg, ro)
        assert on["profile"] == "strict_read_only" and on["applied"] and ro is True
        cfg, ro = tap.plan_shortcut(cfg, ro, False)
        after = tap.resolve(cfg, ro)
        for key in ("profile", "read_only", "disallowed_tools", "custom"):
            assert after[key] == before[key], key

    def test_on_refuses_to_hand_back_tools_a_custom_list_removes(self):
        """Operator decision on the issue: the toggle never loosens silently.
        Strict is a fixed bundle, so ON from a Custom list that removes tools
        Strict allows is refused with the tools named."""
        cfg, ro = tap.plan_selection({}, False, "custom",
                                     custom={"read_only": False, "disallowed_tools": ["Bash", "WebFetch"]})
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_shortcut(cfg, ro, True)
        assert e.value.status == 409
        assert "Bash, WebFetch" in e.value.detail and "Tool access" in e.value.detail

    def test_on_refuses_for_a_legacy_read_only_agent_with_a_list(self):
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_shortcut({"disallowed_tools": ["WebSearch"]}, True, True)
        assert e.value.status == 409 and "WebSearch" in e.value.detail

    def test_the_refusal_names_at_most_five_tools(self):
        many = [f"Tool{i}" for i in range(8)]
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_shortcut({"disallowed_tools": many}, False, True)
        assert "Tool4" in e.value.detail and "Tool5" not in e.value.detail and "3 more" in e.value.detail

    def test_off_on_a_legacy_read_only_agent_goes_to_full(self):
        cfg, ro = tap.plan_shortcut({"max_turns_task": 9}, True, False)
        r = tap.resolve(cfg, ro)
        assert (r["profile"], r["read_only"]) == ("full", False)
        assert cfg["max_turns_task"] == 9

    def test_on_a_legacy_read_only_agent_applies_strict(self):
        cfg, ro = tap.plan_shortcut({}, True, True)
        r = tap.resolve(cfg, ro)
        assert (r["profile"], r["applied"]) == ("strict_read_only", True)

    def test_off_when_read_only_belongs_to_another_profile_is_a_conflict(self):
        cfg, ro = tap.plan_selection({}, False, "read_only_report")
        with pytest.raises(tap.ToolAccessError) as e:
            tap.plan_shortcut(cfg, ro, False)
        assert e.value.status == 409 and "Read-only + report" in e.value.detail

    def test_off_when_already_off_changes_nothing(self):
        assert tap.plan_shortcut({}, False, False) is None

    def test_on_when_already_strict_changes_nothing(self):
        cfg, ro = tap.plan_selection({}, False, "strict_read_only")
        assert tap.plan_shortcut(cfg, ro, True) is None


# ---------------------------------------------------------------------------
# PUT /guardrails never forges or wipes the profile
# ---------------------------------------------------------------------------

class TestGuardrailsMerge:
    def _workspace(self):
        cfg, _ = tap.plan_selection({"max_turns_chat": 30}, False, "workspace_only")
        return cfg

    def test_body_tool_access_is_ignored_and_stored_kept(self):
        stored = self._workspace()
        merged = tap.merge_guardrails_put(stored, {"max_turns_chat": 50})
        assert merged["tool_access"] == stored["tool_access"]
        assert merged["disallowed_tools"] == SEND and merged["max_turns_chat"] == 50

    def test_empty_body_keeps_the_profile(self):
        merged = tap.merge_guardrails_put(self._workspace(), {})
        assert merged["tool_access"]["profile"] == "workspace_only"
        assert merged["disallowed_tools"] == SEND and "max_turns_chat" not in merged

    @pytest.mark.parametrize("body_list", [[], list(SEND), list(reversed(SEND))])
    def test_empty_or_equal_list_is_kept(self, body_list):
        merged = tap.merge_guardrails_put(self._workspace(), {"disallowed_tools": body_list})
        assert merged["disallowed_tools"] == SEND

    def test_a_different_list_under_a_stored_profile_is_a_conflict(self):
        with pytest.raises(tap.ToolAccessError) as e:
            tap.merge_guardrails_put(self._workspace(), {"disallowed_tools": ["Bash"]})
        assert e.value.status == 409 and "/tool-access" in e.value.detail

    def test_profile_less_agent_keeps_the_old_contract(self):
        assert tap.merge_guardrails_put({"disallowed_tools": ["Bash"]}, {"max_turns_chat": 5}) == {"max_turns_chat": 5}
        assert tap.merge_guardrails_put({}, {"disallowed_tools": ["Bash"]}) == {"disallowed_tools": ["Bash"]}


# ---------------------------------------------------------------------------
# The send block the backend refuses (gate Q-B)
# ---------------------------------------------------------------------------

class TestSinkTools:
    def test_sink_tools_are_the_sends_plus_report(self):
        assert set(tap.SINK_TOOLS) == set(tap.SEND_TOOLS) | {"report"}

    def test_denied_reads_the_enforced_list(self):
        cfg, _ = tap.plan_selection({}, False, "workspace_only")
        assert tap.denies_tool(cfg, "send_message") is True
        assert tap.denies_tool(cfg, "report") is False
        cfg, _ = tap.plan_selection({}, False, "strict_read_only")
        assert tap.denies_tool(cfg, "report") is True

    def test_custom_list_naming_a_send_tool_is_refused_too(self):
        assert tap.denies_tool({"disallowed_tools": ["mcp__trinity__call_user"]}, "call_user") is True

    def test_a_dormant_custom_list_is_not_enforced(self):
        cfg, ro = tap.plan_selection({}, False, "custom",
                                     custom={"read_only": False, "disallowed_tools": ["mcp__trinity__send_message"]})
        cfg, ro = tap.plan_selection(cfg, ro, "full")
        assert tap.denies_tool(cfg, "send_message") is False

    def test_legacy_read_only_agent_is_not_blocked_until_applied(self):
        assert tap.denies_tool({}, "send_message") is False

    @pytest.mark.parametrize("cfg", [None, {}, {"disallowed_tools": "nope"}, {"disallowed_tools": [None]}])
    def test_malformed_config_never_raises(self, cfg):
        assert tap.denies_tool(cfg, "send_message") is False


# ---------------------------------------------------------------------------
# Per-runtime coverage — one line per card for the agent's runtime
# ---------------------------------------------------------------------------

class TestCoverage:
    @pytest.mark.parametrize("runtime,family", [
        ("claude-code", "claude"), ("claude", "claude"), ("", "claude"), (None, "claude"),
        ("codex", "codex"), ("gemini-cli", "gemini"), ("gemini", "gemini"), ("opencode", "other"),
    ])
    def test_runtime_family(self, runtime, family):
        assert tap.runtime_family(runtime) == family

    def test_full_has_nothing_to_enforce(self):
        assert tap.coverage_line("full", "codex") == ""

    def test_send_block_is_refused_on_every_runtime(self):
        for fam in ("claude", "codex", "gemini", "other"):
            assert "refused by Trinity" in tap.coverage_line("workspace_only", fam)

    def test_read_only_on_claude_names_the_shell_gap(self):
        assert "shell commands aren't covered" in tap.coverage_line("strict_read_only", "claude")

    def test_read_only_on_codex_names_the_sandbox(self):
        assert "every shell command" in tap.coverage_line("read_only_report", "codex")

    def test_read_only_on_gemini_is_not_enforced(self):
        assert "isn't enforced on Gemini" in tap.coverage_line("strict_read_only", "gemini")

    def test_custom_list_beyond_sends_is_not_enforced_off_claude(self):
        line = tap.coverage_line("custom", "codex", custom={"read_only": False, "disallowed_tools": ["Bash"]})
        assert "aren't enforced on Codex" in line
        assert "aren't enforced" not in tap.coverage_line(
            "custom", "claude", custom={"read_only": False, "disallowed_tools": ["Bash"]})


# ---------------------------------------------------------------------------
# The names are real: a renamed tool would be a silent no-op
# ---------------------------------------------------------------------------

class TestNamesArePinned:
    def test_every_sink_tool_is_a_registered_mcp_tool(self):
        tools_dir = _ROOT / "src" / "mcp-server" / "src" / "tools"
        names = set()
        for ts in tools_dir.glob("*.ts"):
            if ts.name.endswith(".test.ts"):
                continue
            names |= set(re.findall(r'\bname:\s*"([a-z_]+)"', ts.read_text()))
        missing = [t for t in tap.SINK_TOOLS if t not in names]
        assert not missing, f"MCP tools renamed or removed: {missing}"

    def test_the_mcp_server_key_is_trinity(self):
        src = (_ROOT / "docker" / "base-image" / "agent_server" / "services" / "trinity_mcp.py").read_text()
        assert re.search(r'"trinity"\s*:\s*\{', src)
        assert tap.MCP_PREFIX == "mcp__trinity__"
