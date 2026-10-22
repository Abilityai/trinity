#!/usr/bin/env python3
"""PreToolUse hook: a shared person's turn does not do through the agent what
the API refuses them (trinity-enterprise#926).

An agent acts for whoever chats with it. The shared path policy
(`file_path_policy.py`, the same file the Files API enforces) keeps instruction
files, skills, shell startup files, credentials and conversation transcripts at
owner tier. Without this hook a person the agent is merely shared with asks it
in chat to rewrite its own CLAUDE.md, or to read the owner's past conversations.

Registered in /etc/claude-code/managed-settings.json (root-owned) for the file
tools. The decision is cheap and local for the common case:

  1. the path is allowed to a shared person   -> allow (no network, no lookup)
  2. the path is owner-tier                   -> ask the platform which tier this
     turn acts for; owner -> allow; share or unknown or unreachable -> deny

The turn is identified by `TRINITY_EXECUTION_ID` in the claude process's launch
environment (`/proc/<parent>/environ`), never this hook's own environment (the
registration clears it, and a settings `env` from the agent-writable repo must
not steer it). The agent cannot change that once the process is running. No
execution id means no platform turn (a terminal or manual shell, owner-only
surfaces) and the hook allows.

The shell is owner tier (ent#926 ruling 2026-10-19). A command reaches files
the file tools never see, no filter over a command string is sound, and the
container cannot host an OS sandbox (no user namespaces, the reason Codex runs
without its own). So a shared person's turn gets no Bash at all; the owner's
turns keep it, and so do channel and public-link visitors unless the owner
turned the `visitor_shell` guardrail off. The platform answers that (`shell` in
the turn-tier reply); every Bash call in a platform turn asks it once.

A search reads everything under its root, so Grep/Glob are judged by the tree,
not the `path` argument alone: a search with no path (it runs in the home), one
rooted at or above the home or a fixed owner-only location, one whose name
filter leaves its root, or one whose folder holds an owner-only file (bounded
walk; too large to check counts as holding one) takes the owner tier.

MCP servers the owner added (`.mcp.json`, plugins) are matched too: a
filesystem or exec server is the shell by another name, so their tools follow
the shell's line. The platform's own `trinity` server passes here; the backend
fences it per route from the turn header the MCP server forwards.
"""
import json
import os
import sys

sys.path.insert(0, "/opt/trinity/hooks")
from lib import allow, deny, log_event, read_stdin_json, run_hook  # noqa: E402

PROC_ROOT = "/proc"
HOME = "/home/developer"
DEFAULT_BACKEND_URL = "http://backend:8000"
TURN_TIER_PATH = "/api/agents/{agent}/executions/{run}/turn-tier"
HTTP_TIMEOUT_SECONDS = 4
ENVIRON_MAX_BYTES = 1 << 20
RESPONSE_MAX_BYTES = 4096
MAX_ID_LEN = 128
# A search of a folder holding more than this is refused in a shared turn rather
# than checked partly (narrow the search instead).
SEARCH_WALK_MAX_ENTRIES = 5000
SEARCH_OWNER_TIER = "owner_tier_search"

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob"}
SHELL_TOOLS = {"Bash"}
MCP_PREFIX = "mcp__"
PLATFORM_MCP_PREFIX = "mcp__trinity__"

COULD_NOT_CHECK = (
    "Trinity could not be reached to check whose turn this is, so this path was not "
    "touched. Do not do it another way; tell whoever asked that it could not be checked."
)
SHELL_OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and running commands is "
    "owner-only. Answer without the shell, or ask the agent's owner to run it."
)
SHELL_VISITOR_OFF = (
    "This conversation comes from a channel or a public link, and the agent's owner "
    "has turned off running commands for those. Answer without the shell."
)
MCP_OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and the MCP servers the agent's "
    "owner added are owner-only (they can reach files and commands the agent's own tools "
    "may not). Answer without them, or ask the agent's owner."
)
MCP_VISITOR_OFF = (
    "This conversation comes from a channel or a public link, and the agent's owner has "
    "turned off running commands for those, which includes the MCP servers the owner "
    "added. Answer without them."
)
OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and that path is owner-only. "
    "Ask the agent's owner to do it."
)
SEARCH_OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and that search would read "
    "owner-only files (a search with no path runs over the agent's whole home). Search "
    "a narrower folder that holds only ordinary files, or ask the agent's owner."
)


def read_environ(pid) -> dict:
    """A process's launch environment, first value wins; {} when unreadable."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "environ"), "rb") as f:
            raw = f.read(ENVIRON_MAX_BYTES)
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env.setdefault(key.decode("utf-8", "replace"), value.decode("utf-8", "replace"))
    return env


def resolve(raw: str) -> str:
    """The path a tool would really touch: home-anchored, links followed."""
    # `~` is the agent's home, not whatever HOME this process was launched with.
    expanded = HOME + raw[1:] if raw == "~" or raw.startswith("~/") else raw
    if not os.path.isabs(expanded):
        expanded = os.path.join(HOME, expanded)
    return os.path.realpath(expanded)


def targets(tool: str, tool_input: dict) -> list:
    """(verb, path) pairs this tool call touches."""
    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [("write", raw)] if raw else []
    if tool in READ_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [("read", raw)] if raw else []
    if tool in SEARCH_TOOLS:
        raw = tool_input.get("path") or ""
        if not raw:
            return []
        # A search aimed at a directory reads what is under it; aimed at a file it
        # reads the file. Either way the path itself must pass both; what lies
        # under it is `search_refusal`'s question.
        return [("list", raw), ("read", raw)]
    return []


def _escapes_root(pattern: str) -> bool:
    """A glob filter that can name files outside the search root."""
    return bool(pattern) and (pattern.startswith(("/", "~"))
                              or ".." in pattern.replace("\\", "/").split("/"))


def _owner_only_anchors(policy) -> tuple:
    """Fixed locations that hold owner-tier-to-read files, from the shared
    policy's own path-anchored patterns, plus the home itself (its top level
    holds `.env`, `.mcp.json` and the rest by basename)."""
    patterns = (policy.CREDENTIAL_PATH_PATTERNS + policy.RUNTIME_CONFIG_PATTERNS
                + policy.PATH_ALIAS_PATTERNS + policy.TRINITY_CREDENTIAL_COPY_PATTERNS
                + policy.SECRET_FILE_CLASS_PATTERNS)
    return (HOME,) + tuple(policy.deny_anchor(p, HOME) for p in patterns if "/" in p)


def _tree_holds_owner_only(policy, root: str) -> bool:
    """Whether a search of `root` would read a file a shared person may not.

    Walks the tree (bounded): any file the policy keeps at owner tier to read, a
    link to a folder (the search may follow it anywhere), or more entries than
    the bound counts as reaching one, so a search too large to check is refused,
    never waved through."""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            seen += 1
            if seen > SEARCH_WALK_MAX_ENTRIES:
                return True
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                real = os.path.realpath(full)
                if os.path.isdir(real) or not policy.decide(policy.SHARE, "read", real).allowed:
                    return True
            elif name in filenames and not policy.decide(policy.SHARE, "read", full).allowed:
                return True
    return False


def search_refusal(policy, tool: str, tool_input: dict):
    """(verb, path, code) when a Grep/Glob would reach owner-tier files for a
    shared person, else None (PR #3778 review: judging the `path` argument
    alone let a search with no path, which runs in the home, or one rooted at
    the home read every transcript and `.env`; Grep includes dotfiles).

    Refused, unless the turn is the owner's: no path; a root that is the home,
    above it, or above any fixed owner-only location; a glob filter that can
    leave the root; a root whose tree holds an owner-only file."""
    raw = tool_input.get("path") or ""
    if not raw:
        return ("search", HOME, SEARCH_OWNER_TIER)
    root = resolve(raw)
    if any(root == a or a.startswith(root.rstrip("/") + "/") for a in _owner_only_anchors(policy)):
        return ("search", root, SEARCH_OWNER_TIER)
    # Glob's `pattern` and Grep's `glob` name files (Grep's `pattern` is the text).
    name_filter = tool_input.get("pattern" if tool == "Glob" else "glob")
    if isinstance(name_filter, str) and _escapes_root(name_filter):
        return ("search", root, SEARCH_OWNER_TIER)
    if os.path.isdir(root) and _tree_holds_owner_only(policy, root):
        return ("search", root, SEARCH_OWNER_TIER)
    return None


def ask_tier(container_env: dict, agent: str, run: str) -> str:
    """The platform's answer for this turn: owner | share | unknown | unreachable."""
    return ask_turn(container_env, agent, run)[0]


def ask_turn(container_env: dict, agent: str, run: str):
    """(tier, shell, kind) for this turn; shell is False unless the platform says
    True; kind is `person` or `visitor` for a share turn, else None."""
    import http.client
    from urllib.parse import quote, urlsplit

    key = container_env.get("TRINITY_MCP_API_KEY")
    if not key or not agent:
        return "unreachable", False, None
    url = urlsplit(container_env.get("TRINITY_BACKEND_URL") or DEFAULT_BACKEND_URL)
    if url.scheme not in ("http", "https") or not url.hostname:
        return "unreachable", False, None
    port = url.port or (443 if url.scheme == "https" else 80)
    path = url.path.rstrip("/") + TURN_TIER_PATH.format(agent=quote(agent, safe=""), run=quote(run, safe=""))
    connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    for attempt in (1, 2):
        conn = connection(url.hostname, port, timeout=HTTP_TIMEOUT_SECONDS)
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {key}"})
            response = conn.getresponse()
            body = response.read(RESPONSE_MAX_BYTES)
            if response.status == 404:
                return "unknown", False, None
            if response.status != 200:
                return "unreachable", False, None
            answer = json.loads(body.decode("utf-8"))
            tier = answer.get("tier")
            if tier not in ("owner", "share"):
                return "unreachable", False, None
            kind = answer.get("kind") if answer.get("kind") in ("person", "visitor") else None
            return tier, answer.get("shell") is True, kind
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError):
            if attempt == 2:
                return "unreachable", False, None
        except Exception:  # noqa: BLE001 - timeout, DNS, malformed body: no answer
            return "unreachable", False, None
        finally:
            conn.close()
    return "unreachable", False, None


def turn_context():
    """(agent, execution id, container env) for the running turn; run is '' when
    there is no platform turn (a terminal or manual shell)."""
    claude_env = read_environ(os.getppid())
    run = claude_env.get("TRINITY_EXECUTION_ID") or ""
    if len(run) > MAX_ID_LEN:
        run = ""
    container = read_environ(1)
    agent = container.get("AGENT_NAME") or container.get("TRINITY_AGENT_NAME") or ""
    return agent, run, container


def guard_shell(tool: str, *, verb: str = "exec") -> None:
    """The shell (and anything as strong): allow only a turn the platform says
    may use it."""
    agent, run, container = turn_context()
    if not run:
        allow()  # no platform turn: a terminal or manual shell
    tier, shell, kind = ask_turn(container, agent, run)
    if shell:
        allow()
    if tier == "unreachable":
        deny(COULD_NOT_CHECK, tool=tool, verb=verb, tier=tier)
    if verb == "mcp":
        reason = MCP_VISITOR_OFF if kind == "visitor" else MCP_OWNER_ONLY
    else:
        reason = SHELL_VISITOR_OFF if kind == "visitor" else SHELL_OWNER_ONLY
    deny(reason, tool=tool, verb=verb, tier=tier, kind=kind)


def is_platform_mcp_tool(tool: str) -> bool:
    """A tool of the platform's own `trinity` MCP server (a reserved name the
    owner cannot redefine). Claude Code names tools `mcp__<server>__<tool>`, so a
    server called `trinity_` or `trinity__x` shares the prefix; neither leaves a
    remainder that is one plain tool name."""
    rest = tool[len(PLATFORM_MCP_PREFIX):] if tool.startswith(PLATFORM_MCP_PREFIX) else ""
    return bool(rest) and not rest.startswith("_") and "__" not in rest


def main() -> None:
    data = read_stdin_json()
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        guard_shell(tool)
    if tool.startswith(MCP_PREFIX):
        # The platform's server is fenced per route by the backend, which reads
        # this turn from the request header. Any other server is the owner's,
        # and a filesystem or exec server is the shell by another name.
        if is_platform_mcp_tool(tool):
            allow()
        guard_shell(tool, verb="mcp")
    pairs = targets(tool, tool_input)
    if not pairs and tool not in SEARCH_TOOLS:
        allow()

    import file_path_policy as policy

    refused = []
    for verb, raw in pairs:
        path = resolve(raw)
        decision = policy.decide(policy.SHARE, verb, path)
        if not decision.allowed:
            refused.append((verb, path, decision.code))
    if tool in SEARCH_TOOLS and not refused:
        searched = search_refusal(policy, tool, tool_input)
        if searched:
            refused.append(searched)
    if not refused:
        allow()

    agent, run, container = turn_context()
    if not run:
        allow()  # no platform turn: a terminal or manual shell
    tier = ask_tier(container, agent, run)
    verb, path, code = refused[0]
    if tier == "owner":
        log_event("turn_tier_owner", tool=tool, verb=verb, path=path)
        allow()
    if tier == "unreachable":
        deny(COULD_NOT_CHECK, tool=tool, verb=verb, path=path, tier=tier)
    deny(SEARCH_OWNER_ONLY if code == SEARCH_OWNER_TIER else OWNER_ONLY,
         tool=tool, verb=verb, path=path, tier=tier, rule=code)


if __name__ == "__main__":
    run_hook(main)
