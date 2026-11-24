"""Is Claude Code applying Trinity's managed hooks? (abilityai/trinity-enterprise#1176)

Trinity registers its guardrail hooks in file-based managed settings:
`/etc/claude-code/managed-settings.json` (the Bash, file, read-only and output
guards) and the drop-ins in `managed-settings.d/` (the skill gate, the turn-tier
guard). Claude Code composes its managed sources "first-wins" by default: the
highest source that delivers a policy key is the managed tier ALONE, and a
server-managed source ranks above the files. So when Claude Code holds a
server-managed payload with any policy key, none of those hooks run; only `env`
still merges per key. With `managedSourcesBehavior: "merge"` in that payload the
sources compose and the hooks run again, unless the payload also switches
`disableAllHooks` on (a `disable*` switch takes the strictest value under merge).

Claude Code caches that payload as `<config dir>/remote-settings.json`, the
settings object itself, and rewrites it at each fetch. This module reads that
file, before each Claude turn and on `/health`:

* `state()` — `ok`, `skipped`, or `unsupported_runtime` (no Claude hooks to
  skip). A cache that is present but cannot be read as a settings object (not
  JSON, not an object, too large, not a regular file, unreadable) counts as
  `skipped`: "could not tell" must never read as "the hooks run".
* `require_hooks_or_owner(execution_id)` — in the skipped state, a turn whose
  rule lives in a hook is refused before the CLI starts: a shared person's turn
  and a visitor's turn with `visitor_shell` off (the platform's `shell` answer,
  as `turn_scope` asks it), and a schedule run rated below the owner. The
  owner's turns and a visitor's with the shell run.
* `gated_fallback_denials()` — on an agent with skill gates in the skipped
  state, the owner's turns run without the tools that load a skill or a
  subagent, because the gate that would check them is a skipped hook.

A heuristic over the CLI's own input, not proof that a hook ran, and one fetch
behind: on a login that fetches, each session's own fetch lands before its
first tool call and decides that session, so the first restricted turn after
an organization adds a setting, or on a new agent, is not refused. After an
organization removes it, restricted turns stay refused until an owner's turn
refreshes the cache. Never raises; never reports a key name or value from the
cache.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import stat
from typing import Dict, Optional, Tuple

from fastapi import HTTPException

from . import execution_env, turn_scope
from ..runtime_names import is_claude_runtime
from ..state import agent_state

logger = logging.getLogger(__name__)

OK = "ok"
SKIPPED = "skipped"
UNSUPPORTED_RUNTIME = "unsupported_runtime"

AGENT_HOME = "/home/developer"
CACHE_NAME = "remote-settings.json"
# The CLI's own cap on a stored settings document.
CACHE_MAX_BYTES = 2 * 1024 * 1024
# Keys that make no source "win": the CLI reads them as controls, not policy.
CONTROL_KEYS = frozenset({"managedSourcesBehavior", "wslInheritsWindowsSettings"})
MERGE = "merge"
# Root-owned, written by the backend on agents with skill gates (ent#752).
SKILL_GATE_MARKER = "/opt/trinity/skill-gates-active"
GATED_FALLBACK_DENIED_TOOLS = ("Skill", "Agent", "Task")

RUNTIME_LABEL = "Claude Code"
REFUSED = (
    "This agent cannot limit what this turn may do right now, because its guardrails are not "
    "running. Only the agent's owner can use it until that is fixed."
)
REFUSED_SCHEDULE = (
    "This run is {name}, not a chat. {runs_as}, and this agent's guardrails are not running "
    "right now, so it did not run. It will run again once the guardrails are running."
)
REFUSED_SCHEDULE_UNREADABLE = (
    "This run is a scheduled task whose schedule can no longer be read (it was deleted or "
    "changed after the run was queued), not a chat, so it does not have the agent's owner's "
    "access. This agent's guardrails are not running right now, so it did not run."
)

# path -> ((dev, ino, size, mtime_ns, ctime_ns), skips). The change time is in
# the key because a rewrite can restore the mtime but never the ctime.
_memo: Dict[str, Tuple[tuple, bool]] = {}


def _switched_on(value) -> bool:
    """A `disableAllHooks` value the CLI could read as on. Unknown shapes count,
    and any non-empty string does (it is truthy to the CLI whatever it says)."""
    if value is None or value is False or value == 0 or value == "":
        return False
    return True


def payload_skips_files(payload: dict) -> bool:
    """Does a server-managed payload make Claude Code skip the managed files'
    hooks? Keys starting with `$` other than `$schema` are metadata the CLI
    strips before it reads settings."""
    if _switched_on(payload.get("disableAllHooks")):
        return True
    policy = [key for key in payload
              if key not in CONTROL_KEYS and not (key.startswith("$") and key != "$schema")]
    if not policy:
        return False
    return payload.get("managedSourcesBehavior") != MERGE


def _cache_skips(path: str) -> bool:
    """True when the cache at `path` makes the CLI skip the managed files, or is
    present but cannot be read as settings. False only for no cache or a clean
    one. The open never follows a link and never blocks (a FIFO there must not
    hang `/health`)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        return e.errno not in (errno.ENOENT, errno.ENOTDIR)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > CACHE_MAX_BYTES:
            return True
        signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        seen = _memo.get(path)
        if seen and seen[0] == signature:
            return seen[1]
        chunks, total = [], 0
        while total <= CACHE_MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > CACHE_MAX_BYTES:
            return True
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            skips = True
        else:
            skips = not isinstance(payload, dict) or payload_skips_files(payload)
        _memo[path] = (signature, skips)
        return skips
    except OSError:
        return True
    finally:
        os.close(fd)


def config_dirs() -> Tuple[str, ...]:
    """Every config dir a Claude spawn could use: the default and any
    `CLAUDE_CONFIG_DIR` from the container baseline or the agent's `.env` (read
    with the spawn env's own parser; never `build_execution_env`, which warns
    per protected key on every call)."""
    dirs = [os.path.join(AGENT_HOME, ".claude")]
    for source in (execution_env.INITIAL_ENV, execution_env.parse_env_file(execution_env.ENV_FILE)):
        value = (source.get("CLAUDE_CONFIG_DIR") or "").strip()
        if not value:
            continue
        if value == "~" or value.startswith("~/"):
            # Whether the CLI expands `~` is not established: read both spellings.
            dirs.extend((os.path.join(os.getcwd(), value), os.path.join(AGENT_HOME, value)))
            value = AGENT_HOME + value[1:]
        if os.path.isabs(value):
            dirs.append(value)
        else:
            # The spawn passes no cwd, so a relative value resolves against the
            # agent server's own working directory (the home in the image).
            dirs.extend((os.path.join(os.getcwd(), value), os.path.join(AGENT_HOME, value)))
    return tuple(dict.fromkeys(dirs))


def state() -> str:
    """`ok` | `skipped` | `unsupported_runtime` for this agent right now."""
    if not is_claude_runtime(agent_state.agent_runtime):
        return UNSUPPORTED_RUNTIME
    try:
        for directory in config_dirs():
            if _cache_skips(os.path.join(directory, CACHE_NAME)):
                return SKIPPED
    except Exception:  # noqa: BLE001 — a reader bug must not read as "the hooks run"
        logger.warning("[managed-hooks] could not read the settings cache", exc_info=True)
        return SKIPPED
    return OK


def gated() -> bool:
    return os.path.lexists(SKILL_GATE_MARKER)


def gated_fallback_denials() -> tuple:
    """The tools a gated agent's turns lose while the skill-gate hook is skipped."""
    return GATED_FALLBACK_DENIED_TOOLS if gated() and state() == SKIPPED else ()


def schedule_refusal(detail: dict) -> str:
    """A schedule run rated below the owner (trinity#4281), refused because the
    guardrails are not running. No take-over remedy: a take-over would lift the
    run to owner tier while nothing holds it."""
    if detail["cause"] == turn_scope.SCHEDULE_UNREADABLE:
        return REFUSED_SCHEDULE_UNREADABLE
    name = f"the scheduled task '{detail['schedule']}'" if detail["schedule"] else "a scheduled task"
    return REFUSED_SCHEDULE.format(name=name,
                                   runs_as=turn_scope.schedule_runs_as(detail["cause"], detail["creator"]))


async def require_hooks_or_owner(execution_id: Optional[str]) -> None:
    """Raise when this Claude turn depends on a hook Claude Code is skipping.

    Normal state: returns at once, asks nobody. No execution id: not a platform
    turn (a terminal, a manual run), as in the hooks. Otherwise the platform's
    turn-tier answer decides: a turn that may hold the shell runs.
    """
    if not execution_id or state() != SKIPPED:
        return
    tier, may_run, kind, schedule = await turn_scope.fetch_turn(turn_scope._agent_name(), execution_id)
    if may_run:
        return
    cause = schedule["cause"] if schedule else None
    logger.info(f"event=managed_hooks_turn_refused execution_id={execution_id} tier={tier} "
                f"kind={kind} cause={cause}")
    if tier == "unreachable":
        # 502, never 503: a 503 from a Claude agent reads as an auth failure on
        # every caller (the subscription walk, the AUTH breaker code).
        raise HTTPException(status_code=502,
                            detail={"message": turn_scope.COULD_NOT_CHECK.format(runtime=RUNTIME_LABEL)})
    text = schedule_refusal(schedule) if schedule else REFUSED
    raise HTTPException(status_code=403, detail={"message": text, "error_code": turn_scope.ERROR_CODE})
