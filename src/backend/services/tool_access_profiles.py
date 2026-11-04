"""Tool-access profiles — the pure policy (trinity-enterprise#341, GUARD-008).

A profile is a fixed bundle that sets two enforcement layers together:

* the read-only layer (#887): `agent_ownership.read_only_mode`, written live
  into the container as `/opt/trinity/read-only-config.json`, which the Claude
  hook and the Codex sandbox read;
* the tool deny-list (GUARD-003): `guardrails_config.disallowed_tools`,
  baked into `AGENT_GUARDRAILS` at container create and passed to Claude Code
  as `--disallowedTools`. The send tools on it are also refused by the backend
  routes behind them (`denies_tool`), on every runtime.

Storage is MATERIALIZED: `disallowed_tools` and the read-only column always
hold what is enforced, so every existing reader keeps working unchanged.
`guardrails_config.tool_access` only records the owner's choice:

    {"profile": <id>,
     "custom": {"read_only": bool, "disallowed_tools": [...]},   # kept while
     "before_strict": <id>}                                       # another is on

An agent with no stored profile (every agent before this feature) has its
profile DERIVED from what is enforced, and nothing about it changes until the
owner saves. A profile whose enforced layers differ from its bundle reads as
"not applied" — never silently changed.

No I/O here: the service (`services/agent_service/tool_access.py`) reads the
row, calls these functions, and writes the result in one compare-and-set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PROFILE_IDS = ("full", "workspace_only", "read_only_report", "strict_read_only", "custom")
NAMED_PROFILES = PROFILE_IDS[:4]
FULL = "full"
STRICT = "strict_read_only"
CUSTOM = "custom"

# Trinity's MCP server is registered under the key "trinity" in the agent's
# `.mcp.json` (`agent_server/services/trinity_mcp.py`), so Claude Code names
# its tools `mcp__trinity__<tool>`. An unknown name in `--disallowedTools` is a
# silent no-op, so these names are pinned against the MCP server's registry.
MCP_PREFIX = "mcp__trinity__"

# The tools that reach a person or a channel. Closed at seven by the operator's
# ruling on trinity-enterprise#341: agent-to-agent tools and `ask_operator` are
# not sends. Adding one is a product decision, not a code change.
SEND_TOOLS = (
    "send_message",
    "send_group_message",
    "send_notification",
    "send_voice_reply",
    "call_user",
    "post_to_room",
    "share_file",
)
REPORT_TOOL = "report"
# The Trinity tools whose backend routes refuse an agent that has them on its
# enforced list (the operator's decision on the issue: refused, not only hidden).
SINK_TOOLS = SEND_TOOLS + (REPORT_TOOL,)

# Same contract as `_validate_guardrails_payload` (routers/agent_config.py),
# which reads these, and as `write-runtime-config.py` in the base image.
MAX_LIST_LEN = 50
MAX_STRING_LEN = 256


def mcp_name(tool: str) -> str:
    return MCP_PREFIX + tool


_SEND_DENY = tuple(mcp_name(t) for t in SEND_TOOLS)


@dataclass(frozen=True)
class Bundle:
    read_only: bool
    disallowed_tools: tuple


BUNDLES = {
    "full": Bundle(False, ()),
    "workspace_only": Bundle(False, _SEND_DENY),
    "read_only_report": Bundle(True, _SEND_DENY),
    "strict_read_only": Bundle(True, _SEND_DENY + (mcp_name(REPORT_TOOL),)),
}

# The informed-consent copy (approved at the issue's design gate). One home, so
# the API and the Settings cards cannot disagree.
PROFILE_COPY = {
    "full": {
        "label": "Full access",
        "statement": (
            "Uses every tool the platform allows, including messages, notifications, "
            "calls and file shares. The platform's guardrails still apply."
        ),
    },
    "workspace_only": {
        "label": "Workspace-only",
        "statement": (
            "Reads, writes and runs anything in its own workspace. Can't send messages, "
            "notifications, calls, room posts or file links to people or channels. It still "
            "replies in its own chats and channels (text only, no voice replies), files "
            "reports, asks you questions and works with other agents."
        ),
    },
    "read_only_report": {
        "label": "Read-only + report",
        "statement": (
            "Reads everything, but can't change its code, instructions or config files. It "
            "can still write to content/, output/, reports/ and exports/. Can't send messages, "
            "notifications, calls, room posts or file links. It delivers its work as reports "
            "and as text replies in its own chats and channels."
        ),
    },
    "strict_read_only": {
        "label": "Strict read-only",
        "statement": (
            "Reads and answers when asked. Can't change its code, instructions or config "
            "files, can't send messages, notifications, calls, room posts or file links, and "
            "can't file reports. It still replies in its own chats (text only) and can ask "
            "you a question."
        ),
    },
    "custom": {
        "label": "Custom",
        "statement": (
            "You choose: read-only on or off, and exactly which tools to remove. Common ones: "
            "Bash, Write, Edit, WebFetch, WebSearch. A Trinity tool is written "
            "mcp__trinity__<tool>."
        ),
    },
}


class ToolAccessError(Exception):
    """A refused change, with the HTTP status the route returns."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def label(profile: Optional[str]) -> str:
    return PROFILE_COPY.get(profile or "", {}).get("label", profile or "")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _as_list(value) -> list:
    """Stored tool names as clean strings. Never raises on a malformed row."""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def enforced_tools(cfg) -> list:
    """The tool list a `guardrails_config` (or a container's `AGENT_GUARDRAILS`)
    enforces."""
    return _as_list(cfg.get("disallowed_tools")) if isinstance(cfg, dict) else []


def public_guardrails_view(cfg) -> dict:
    """`guardrails_config` as `GET /guardrails` (access tier) returns it: the
    stored profile's id only. Custom's remembered list and `before_strict` are
    owner-tier, read through `GET /tool-access`."""
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    stored = _stored_tool_access(cfg)
    if stored is not None:
        cfg["tool_access"] = {"profile": stored["profile"]}
    else:
        cfg.pop("tool_access", None)
    return cfg


def _stored_tool_access(cfg) -> Optional[dict]:
    ta = cfg.get("tool_access") if isinstance(cfg, dict) else None
    if isinstance(ta, dict) and ta.get("profile") in PROFILE_IDS:
        return ta
    return None


def _custom_state(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "read_only": raw.get("read_only") is True,
        "disallowed_tools": _as_list(raw.get("disallowed_tools")),
    }


def resolve(cfg, read_only_enabled: bool) -> dict:
    """What the agent's tool access is, from its stored row.

    `cfg` is `guardrails_config` (a dict), `read_only_enabled` the
    `read_only_mode` column. Returns the profile, whether it was derived (no
    stored choice), whether the enforced layers match it (`applied`), the
    enforced layers themselves, Custom's remembered settings, and where the
    Read-only shortcut's OFF returns.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    read_only = bool(read_only_enabled)
    enforced = _as_list(cfg.get("disallowed_tools"))
    stored = _stored_tool_access(cfg)

    if stored is not None:
        profile = stored["profile"]
        derived = False
        custom = _custom_state(stored.get("custom"))
    else:
        derived = True
        if enforced:
            profile = CUSTOM
            custom = {"read_only": read_only, "disallowed_tools": enforced}
        else:
            profile = STRICT if read_only else FULL
            custom = _custom_state(None)

    if profile == CUSTOM:
        want_ro, want_tools = custom["read_only"], custom["disallowed_tools"]
    else:
        bundle = BUNDLES[profile]
        want_ro, want_tools = bundle.read_only, list(bundle.disallowed_tools)
    applied = read_only == want_ro and set(enforced) == set(want_tools)

    before = stored.get("before_strict") if (stored and profile == STRICT) else None
    if before not in PROFILE_IDS or before == STRICT:
        before = None

    return {
        "profile": profile,
        "derived": derived,
        "applied": applied,
        "read_only": read_only,
        "disallowed_tools": enforced,
        "custom": custom,
        "before_strict": before,
    }


def denies_tool(cfg, tool: str) -> bool:
    """True when the agent's ENFORCED list names the Trinity tool `tool`.

    Read by the backend routes behind the send tools: the refusal follows what
    is enforced, so a dormant Custom list and a legacy read-only agent whose
    send block was never applied are not refused. Never raises.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return mcp_name(tool) in _as_list(cfg.get("disallowed_tools"))


# ---------------------------------------------------------------------------
# Writing — each returns (new guardrails_config, new read_only_mode)
# ---------------------------------------------------------------------------

def _validate_custom(custom) -> dict:
    if not isinstance(custom, dict):
        raise ToolAccessError(400, "custom must be an object with read_only and disallowed_tools")
    ro = custom.get("read_only", False)
    if not isinstance(ro, bool):
        raise ToolAccessError(400, "custom.read_only must be true or false")
    tools = custom.get("disallowed_tools", [])
    if not isinstance(tools, list):
        raise ToolAccessError(400, "custom.disallowed_tools must be a list of tool names")
    if len(tools) > MAX_LIST_LEN:
        raise ToolAccessError(
            400, f"custom.disallowed_tools exceeds the maximum of {MAX_LIST_LEN} entries")
    cleaned: list = []
    for item in tools:
        name = item.strip() if isinstance(item, str) else None
        if not name or len(name) > MAX_STRING_LEN:
            raise ToolAccessError(
                400,
                f"custom.disallowed_tools entries must be tool names of 1 to {MAX_STRING_LEN} "
                "characters, for example Bash or mcp__trinity__send_message",
            )
        if name not in cleaned:
            cleaned.append(name)
    return {"read_only": ro, "disallowed_tools": cleaned}


def plan_selection(cfg, read_only_enabled: bool, profile, custom=None):
    """The row after the owner selects `profile` (the tool-access PUT).

    Writes the enforced layers for the profile, records the choice, keeps
    Custom's settings for a later return, and clears `before_strict` (a
    deliberate selection ends the shortcut's memory). Unrelated keys
    (turn caps, timeouts, extra deny lists) are untouched.
    """
    if not isinstance(profile, str) or profile not in PROFILE_IDS:
        raise ToolAccessError(400, "profile must be one of: " + ", ".join(PROFILE_IDS))
    if custom is not None and profile != CUSTOM:
        raise ToolAccessError(400, "custom settings apply only to the custom profile")

    current = resolve(cfg, read_only_enabled)
    new_custom = current["custom"]
    if profile == CUSTOM and custom is not None:
        new_custom = _validate_custom(custom)

    if profile == CUSTOM:
        new_ro, new_tools = new_custom["read_only"], list(new_custom["disallowed_tools"])
    else:
        bundle = BUNDLES[profile]
        new_ro, new_tools = bundle.read_only, list(bundle.disallowed_tools)

    new_cfg = {k: v for k, v in (cfg or {}).items() if k not in ("disallowed_tools", "tool_access")}
    if new_tools:
        new_cfg["disallowed_tools"] = new_tools
    new_cfg["tool_access"] = {
        "profile": profile,
        "custom": {"read_only": new_custom["read_only"],
                   "disallowed_tools": list(new_custom["disallowed_tools"])},
    }
    return new_cfg, new_ro


def plan_shortcut(cfg, read_only_enabled: bool, enabled: bool):
    """The row after the header / list Read-only toggle (ruling 5).

    ON selects Strict read-only and remembers the profile it came from. OFF
    from Strict returns there (Full access if none). OFF on another profile
    whose read-only layer is on is refused: the toggle is not that profile's
    switch. Returns None when nothing changes.
    """
    current = resolve(cfg, read_only_enabled)
    if enabled:
        if current["profile"] == STRICT and current["applied"]:
            return None
        # Never loosen silently (operator decision on trinity-enterprise#341):
        # Strict is a fixed bundle, so ON from a list that removes tools Strict
        # allows would hand them back. The full selector is where that is chosen.
        strict_tools = set(BUNDLES[STRICT].disallowed_tools)
        handed_back = [t for t in current["disallowed_tools"] if t not in strict_tools]
        if handed_back:
            named = ", ".join(handed_back[:5])
            if len(handed_back) > 5:
                named += f" and {len(handed_back) - 5} more"
            raise ToolAccessError(
                409,
                f"Strict read-only would allow {named}, which this agent's {label(current['profile'])} "
                "profile removes. Change it in Settings › Guardrails › Tool access.",
            )
        before = current["before_strict"] if current["profile"] == STRICT else current["profile"]
        new_cfg, new_ro = plan_selection(cfg, read_only_enabled, STRICT)
        if before:
            new_cfg["tool_access"]["before_strict"] = before
        return new_cfg, new_ro

    if current["profile"] == STRICT:
        return plan_selection(cfg, read_only_enabled, current["before_strict"] or FULL)
    if current["read_only"]:
        raise ToolAccessError(
            409,
            f"Read-only is part of this agent's {label(current['profile'])} profile. "
            "Change the profile in Settings › Guardrails › Tool access.",
        )
    return None


def merge_guardrails_put(stored, cleaned: dict) -> dict:
    """The row a `PUT /guardrails` writes, given the stored row and the
    validated body.

    The profile is never taken from the body: the stored `tool_access` is kept,
    so a Settings form that echoes an old copy can neither forge nor wipe it,
    and `{}` clears the numeric overrides but not the profile. Once a profile is
    stored, the tool list belongs to it: absent, `[]` or equal is kept, and a
    different list is refused. An agent with no stored profile keeps the
    previous whole-config contract exactly.
    """
    stored = stored if isinstance(stored, dict) else {}
    cleaned = dict(cleaned or {})
    if _stored_tool_access(stored) is None:
        cleaned.pop("tool_access", None)
        return cleaned

    merged = {k: v for k, v in cleaned.items() if k not in ("tool_access", "disallowed_tools")}
    stored_list = _as_list(stored.get("disallowed_tools"))
    if "disallowed_tools" in cleaned:
        body_list = _as_list(cleaned["disallowed_tools"])
        if body_list and set(body_list) != set(stored_list):
            raise ToolAccessError(
                409,
                "This agent's tool list belongs to its tool-access profile. Change it with "
                "PUT /api/agents/{name}/tool-access (profile custom).",
            )
    if stored_list:
        merged["disallowed_tools"] = stored["disallowed_tools"]
    merged["tool_access"] = stored["tool_access"]
    return merged


# ---------------------------------------------------------------------------
# Per-runtime coverage — honest, one line per card for the agent's runtime
# ---------------------------------------------------------------------------

_RUNTIME_NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini", "other": "this runtime"}


def runtime_family(runtime) -> str:
    rt = (runtime or "").strip().lower() if isinstance(runtime, str) or runtime is None else ""
    if rt in ("", "claude-code", "claude"):
        return "claude"
    if rt == "codex":
        return "codex"
    if rt in ("gemini-cli", "gemini"):
        return "gemini"
    return "other"


def coverage_line(profile: str, family: str, custom=None) -> str:
    """What this profile actually enforces on the agent's runtime.

    Read-only layer: Claude Code's hook covers the file tools only; Codex's
    sandbox blocks every shell command, reads included (bwrap is forbidden in
    the hardened container — `codex_runtime._resolve_sandbox_mode`); Gemini has
    none. Sends: refused by the backend on every runtime. Any other removed
    tool: Claude Code only (Codex logs `disallowed_tools`, Gemini ignores it).
    """
    if profile == CUSTOM:
        state = _custom_state(custom)
        read_only, tools = state["read_only"], state["disallowed_tools"]
    else:
        bundle = BUNDLES.get(profile)
        if bundle is None:
            return ""
        read_only, tools = bundle.read_only, list(bundle.disallowed_tools)

    sink_names = {mcp_name(t) for t in SINK_TOOLS}
    sends = [t for t in tools if t in sink_names]
    others = [t for t in tools if t not in sink_names]
    where = _RUNTIME_NAMES.get(family, _RUNTIME_NAMES["other"])

    parts = []
    if sends:
        parts.append("Sends are refused by Trinity on every runtime." if profile != CUSTOM
                     else "The Trinity send tools you removed are refused by Trinity on every runtime.")
    if read_only:
        if family == "claude":
            parts.append("Read-only: the file tools can't write code or config files; "
                         "shell commands aren't covered.")
        elif family == "codex":
            parts.append("Read-only: the sandbox blocks every shell command, reads included.")
        else:
            parts.append(f"Read-only isn't enforced on {where}.")
    if others:
        if family == "claude":
            parts.append("Removed tools are taken off its tool list.")
        else:
            parts.append(f"Other removed tools aren't enforced on {where}.")
    return " ".join(parts)


def catalog(runtime, resolved: dict) -> list:
    """The five cards for the agent's runtime: bundle, copy and coverage."""
    family = runtime_family(runtime)
    out = []
    for pid in PROFILE_IDS:
        if pid == CUSTOM:
            ro = resolved["custom"]["read_only"]
            tools = list(resolved["custom"]["disallowed_tools"])
            line = coverage_line(pid, family, custom=resolved["custom"])
        else:
            bundle = BUNDLES[pid]
            ro, tools = bundle.read_only, list(bundle.disallowed_tools)
            line = coverage_line(pid, family)
        out.append({
            "id": pid,
            "label": PROFILE_COPY[pid]["label"],
            "statement": PROFILE_COPY[pid]["statement"],
            "read_only": ro,
            "disallowed_tools": tools,
            "coverage": line,
        })
    return out
