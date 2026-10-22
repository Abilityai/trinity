"""Playbook dispatch for the OpenCode runtime (#2981, checkpoint E).

A prompt whose first non-blank line is ``/<skill> [args]`` runs that skill the
way Claude Code runs a slash command: deterministically, not as prose the model
may or may not act on. OpenCode's own mechanism is a *command*: a named prompt
template dispatched with ``opencode run --command <name> -- <arguments>``.

Ruling T2: the commands are rendered as ``command:`` entries into the per-turn
``opencode.json`` (the file ``_prepare_turn_config`` already writes), NOT as a
``.opencode/command/`` shim directory. Nothing is written to the workspace and
nothing outlives the turn.

Verified against the pinned OpenCode source, v1.18.32 (github.com/sst/opencode):

* ``packages/opencode/src/cli/cmd/run.ts:143-146`` declares ``--command`` ("the
  command to run, use message for args"); ``run.ts:420-422`` accepts an empty
  message when ``--command`` is given; ``run.ts:845-851`` dispatches through
  ``client.session.command({command, arguments: message, ...})``; the message
  is the positional array joined with one space (``run.ts:272``), so a single
  argv element keeps its newlines.
* One entry's schema is ``packages/core/src/v1/config/command.ts:5-12``:
  ``template`` (string, REQUIRED) plus optional ``description``, ``agent``,
  ``model``, ``variant``, ``subtask``. The config key is
  ``command: Record<string, Info>`` (``core/src/v1/config/config.ts:41``) — the
  same ``Info`` a ``.opencode/command/<name>.md`` file decodes to, frontmatter
  plus the body as ``template`` (``opencode/src/config/command.ts:21-31``).
  Config entries are listed after the built-ins (``command/index.ts:90-103``),
  so a skill named ``init`` or ``review`` shadows OpenCode's own
  (``Default = {INIT, REVIEW}``, ``command/index.ts:51-71``). The skill still
  registers — it is the operator's name — and discovery logs one warning per
  collision (:data:`OPENCODE_BUILTIN_COMMANDS`). An MCP server's prompt can
  shadow a command the same way (``command/index.ts:86``), but its names are
  only known once the server is connected, so discovery cannot see it.
* The handler (``opencode/src/session/prompt.ts:1372-1395``) replaces ``$1``…
  positionally and ``$ARGUMENTS`` with the raw argument string; with neither in
  the template it appends the arguments after a blank line.
* The schema puts NO constraint on a name (any string key). The constraint
  below is ours: the name travels as an argv value and is parsed off a prompt,
  so a leading ``-`` or whitespace would be a flag or a different token.

**Shell expansion — why some prompts are NOT dispatched.** After the argument
substitution, the handler runs every ``!`cmd` `` in the template through the
shell (``prompt.ts:1397-1407``, ``config/markdown.ts:6``) via ``Process.text``
— outside the tool layer, so a ``permission: {bash: deny}`` (read-only,
checkpoint D) does not apply. Arguments come from the prompt, i.e. from a chat
user or a schedule. A prompt whose arguments carry that shape therefore falls
back to prose passthrough (logged), where no expansion happens.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

TEMPLATE = "Follow the skill at {path} with arguments: $ARGUMENTS"

# Argv- and parse-safe: starts alphanumeric, no whitespace, no `/`, no shell
# metacharacters. Typical skill names (`implement`, `canon-publish`) pass.
COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

# `config/markdown.ts:6` SHELL_REGEX, v1.18.32.
OPENCODE_SHELL_SHAPE = re.compile(r"!`[^`]+`")

DESCRIPTION_MAX = 200

# OpenCode's built-in commands at v1.18.32 (`command/index.ts:51-71`,
# `Default = {INIT, REVIEW}`). A skill of the same name replaces the built-in.
OPENCODE_BUILTIN_COMMANDS = frozenset({"init", "review"})


def _one_line(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    text = " ".join(str(description).split())
    if len(text) > DESCRIPTION_MAX:
        text = text[: DESCRIPTION_MAX - 1].rstrip() + "…"
    return text or None


def discover_playbook_commands(home_dir: Path) -> Dict[str, Dict[str, str]]:
    """``{name: {template, description?}}`` for every skill the agent has.

    The skills are read through the Playbooks tab's own reader
    (``routers/skills.py``: ``skill_search_paths`` + ``scan_skills_directory``)
    — one reader, so the commands are exactly the playbooks ``GET /api/skills``
    lists. Precedence is that endpoint's: the first directory to name a skill
    wins (project over personal). The template references SKILL.md by absolute
    path; the body is never embedded.
    """
    # Lazy: `routers/__init__` imports the chat router, which imports this
    # runtime — a module-level import here would be a cycle.
    from ..routers.skills import scan_skills_directory, skill_search_paths

    commands: Dict[str, Dict[str, str]] = {}
    seen = set()
    rejected = []
    for skills_dir in skill_search_paths(home_dir):
        for skill in sorted(scan_skills_directory(skills_dir), key=lambda s: s.name):
            if skill.name in seen:
                continue
            seen.add(skill.name)
            skill_md = skills_dir.absolute() / (skill.dir or skill.name) / "SKILL.md"
            if not COMMAND_NAME.fullmatch(skill.name) or "`" in str(skill_md):
                rejected.append(skill.name)
                continue
            if skill.name in OPENCODE_BUILTIN_COMMANDS:
                logger.warning(
                    "[OpenCode] skill %r replaces OpenCode's built-in /%s "
                    "command for this agent", skill.name, skill.name,
                )
            entry = {"template": TEMPLATE.format(path=skill_md)}
            description = _one_line(skill.description)
            if description:
                entry["description"] = description
            commands[skill.name] = entry
    if rejected:
        logger.warning(
            "[OpenCode] skills not offered as /commands (name is not a safe "
            "command name, or the path carries a backtick): %s",
            ", ".join(repr(n) for n in rejected),
        )
    return commands


def split_playbook_prefix(
    prompt: str, command_names: Iterable[str]
) -> Tuple[Optional[str], str]:
    """``(command, message)`` for a ``/<name> [args]`` prompt, else ``(None, prompt)``.

    Only the first non-blank line can name a command, and only a name in
    ``command_names`` (the scanned skills) counts — the value returned for
    ``--command`` is never a free string from the prompt. ``message`` is the
    rest of that line followed by the remaining lines unchanged, so an
    orchestrator dispatch's riders reach the command as its arguments. An
    unknown ``/name`` is prose, exactly as Claude Code treats it.
    """
    names = set(command_names)
    if not prompt or not names:
        return None, prompt
    lines = prompt.split("\n")
    index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if index is None:
        return None, prompt
    first = lines[index].lstrip()
    match = re.match(r"/(\S+)", first)
    if not match or match.group(1) not in names:
        return None, prompt
    rest = lines[index + 1:]
    args = first[match.end():].strip()
    message = "\n".join([args, *rest]) if args else "\n".join(rest).lstrip("\n")
    if not message.strip():
        message = ""
    if OPENCODE_SHELL_SHAPE.search(message):
        logger.warning(
            "[OpenCode] /%s not dispatched as a command: its arguments carry the "
            "!`...` shape OpenCode would run through the shell outside the "
            "permission layer; sent as prose instead",
            match.group(1),
        )
        return None, prompt
    return match.group(1), message
