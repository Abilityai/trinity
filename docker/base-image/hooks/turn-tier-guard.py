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
turns and the turns the owner configured keep it. Every Bash call in a platform
turn therefore asks the platform once.
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

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob"}
SHELL_TOOLS = {"Bash"}

COULD_NOT_CHECK = (
    "Trinity could not be reached to check whose turn this is, so this path was not "
    "touched. Do not do it another way; tell whoever asked that it could not be checked."
)
SHELL_OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and running commands is "
    "owner-only. Answer without the shell, or ask the agent's owner to run it."
)
OWNER_ONLY = (
    "This turn is for a person the agent is shared with, and that path is owner-only. "
    "Ask the agent's owner to do it."
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
        # reads the file. Either way the path must pass both, so the answer never
        # depends on what happens to exist.
        return [("list", raw), ("read", raw)]
    return []


def ask_tier(container_env: dict, agent: str, run: str) -> str:
    """The platform's answer for this turn: owner | share | unknown | unreachable."""
    import http.client
    from urllib.parse import quote, urlsplit

    key = container_env.get("TRINITY_MCP_API_KEY")
    if not key or not agent:
        return "unreachable"
    url = urlsplit(container_env.get("TRINITY_BACKEND_URL") or DEFAULT_BACKEND_URL)
    if url.scheme not in ("http", "https") or not url.hostname:
        return "unreachable"
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
                return "unknown"
            if response.status != 200:
                return "unreachable"
            tier = json.loads(body.decode("utf-8")).get("tier")
            return tier if tier in ("owner", "share") else "unreachable"
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError):
            if attempt == 2:
                return "unreachable"
        except Exception:  # noqa: BLE001 - timeout, DNS, malformed body: no answer
            return "unreachable"
        finally:
            conn.close()
    return "unreachable"


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


def guard_shell(tool: str) -> None:
    """The shell is owner tier: allow only a turn the platform says is the owner's."""
    agent, run, container = turn_context()
    if not run:
        allow()  # no platform turn: a terminal or manual shell
    tier = ask_tier(container, agent, run)
    if tier == "owner":
        allow()
    if tier == "unreachable":
        deny(COULD_NOT_CHECK, tool=tool, verb="exec", tier=tier)
    deny(SHELL_OWNER_ONLY, tool=tool, verb="exec", tier=tier)


def main() -> None:
    data = read_stdin_json()
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        guard_shell(tool)
    pairs = targets(tool, tool_input)
    if not pairs:
        allow()

    import file_path_policy as policy

    refused = []
    for verb, raw in pairs:
        path = resolve(raw)
        decision = policy.decide(policy.SHARE, verb, path)
        if not decision.allowed:
            refused.append((verb, path, decision.code))
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
    deny(OWNER_ONLY, tool=tool, verb=verb, path=path, tier=tier, rule=code)


if __name__ == "__main__":
    run_hook(main)
