"""The in-container skill-gate hook — the decision (trinity-enterprise#752).

Claude Code runs `skill-gate.py` (the bootstrap) as a PreToolUse hook on `Skill`,
`Agent` and `Task`, registered by the managed-settings drop-in
`/etc/claude-code/managed-settings.d/50-skill-gate.json` as

    /usr/bin/env -i /usr/local/bin/python3 -I -S /opt/trinity/hooks/skill-gate.py

and this module asks the platform whether the run may load the skill(s) the
call loads (`POST /api/skill-gate/check`, with the agent's own key):

* exit 0 — the platform said yes; the call loads no skill; or the platform gave
  no answer and this agent has no gates (no marker);
* exit 2 — the platform said no (its words go to the model); or it gave no
  answer and the root-owned marker says this agent has gates.

It never exits 1. For the CLI this was verified against, exit 1, a traceback, a
crash, a spawn failure and the CLI's own hook timeout all LET THE TOOL RUN — so
every path ends in 0 or 2, the whole decision runs under its own deadline in a
daemon thread, and the bootstrap imports this module inside a catch-all.

This is deliberately the OPPOSITE failure direction from `read-only-guard.py`,
which fails open on a corrupt config: a gated skill must not load because
something broke. Do not unify them.

Identity never comes from this process's environment — settings `env` from the
agent-writable repo reaches a hook's environment. The run is read from the
claude process's launch environment (`/proc/<parent>/environ`), the backend URL
and key from the container's (`/proc/1/environ`); the environment is cleared
before anything that could be steered by it is imported or called, and the
request follows no proxy and no redirect.

Not a boundary against an adversarial executor (passwordless sudo; acting on a
SKILL.md it read; a nested `claude` it starts, with an execution id it sets or a
`/<skill>` prompt that expands with no tool call) — the boundary
is credential confinement. This stops the ordinary path: a request that names a
gated skill in prose, and the model loading it.

Stdlib only; runs under `-I -S`.
"""
import json
import os
import re
import stat
import sys
import threading
from collections import namedtuple
from typing import Dict, List, Optional, Tuple

# The Claude Code release the facts below were read from. A unit test fails
# when the image's pin moves; re-check every item, then move this.
SKILL_GATE_CLI_VERIFIED = "2.1.281"
CLI_FACTS_TO_REAUDIT = (
    "Hook entries with `args` run in exec form: spawned directly, never through a "
    "shell, never wrapped by CLAUDE_CODE_SHELL_PREFIX.",
    "A matcher of letters, digits, `_`, `|`, `,`, space and `-` is compared as exact tool "
    "names (the plain-name grammar); `Task` is an alias of `Agent`.",
    "A model-invoked skill is the `Skill` tool with input {skill, args}: `skill` trimmed and "
    "one leading `/` stripped; the per-skill `skill__<name>` tools are compiled off.",
    "Exit 2 blocks the tool and shows stderr to the model; exit 1, a crash, a spawn failure "
    "and a hook timeout (600 s by default) let it run.",
    "Files in /etc/claude-code/managed-settings.d/ are validated one by one: a rejected file "
    "voids only itself, never managed-settings.json.",
    "Policy (managed) settings env is applied after every other scope, so its LD_* pins win "
    "over a repo settings env.",
    "The hook's parent process is claude itself (no wrapper): /proc/<parent>/environ is its "
    "launch environment and /proc/<parent>/cwd its working directory; the claude process "
    "stays dumpable, so the agent user can read them.",
    "A subagent definition's `skills:` preloads them with no Skill call; definitions live in "
    "~/.claude/agents and .claude/agents (recursive, identified by front-matter `name`) and "
    "in plugin agents/ directories (`plugin:name`).",
    "A prompt that starts with /<skill> expands without a Skill call, so this hook never sees "
    "it (the dispatch-time check's lane).",
    "An Agent call with no subagent_type falls back to `general-purpose`, and a user or project "
    "definition with a built-in's name overrides the built-in.",
    "Only a configured sandbox wrapper runs an exec-form hook through a shell (its quoted "
    "argv); Trinity agents run unsandboxed.",
    "The CLI reads user skills and agents from CLAUDE_CONFIG_DIR when it is set in its "
    "environment, else ~/.claude.",
)

MARKER = "/opt/trinity/skill-gates-active"
PROC_ROOT = "/proc"
USER_HOME: Optional[str] = None          # None: the running user's passwd entry
DEFAULT_BACKEND_URL = "http://backend:8000"   # trinity-system has no TRINITY_BACKEND_URL
CHECK_PATH = "/api/skill-gate/check"

DEADLINE_SECONDS = 10.0                  # far inside the registered 30 s timeout
HTTP_TIMEOUT_SECONDS = 4.0               # per attempt; two attempts fit the deadline
LOG_JOIN_SECONDS = 1.0

STDIN_MAX_BYTES = 8 * 1024 * 1024
ENVIRON_MAX_BYTES = 256 * 1024
FRONT_MATTER_MAX_BYTES = 256 * 1024
RESPONSE_MAX_BYTES = 64 * 1024
MAX_ENTRIES = 2000                        # per skills directory
MAX_DEFINITION_ENTRIES = 5000             # one agents tree
MAX_PLUGIN_ENTRIES = 20000                # the plugins tree
MAX_DEPTH = 8
MAX_PLUGIN_DEPTH = 12
MAX_NAMES = 64
MAX_NAME_LEN = 256
MAX_EXECUTION_ID_LEN = 128
MESSAGE_MAX_CHARS = 4000

COULD_NOT_CHECK = (
    "Trinity could not be reached to check whether this skill needs approval, so it was not "
    "run. Do not carry out its steps another way; tell whoever asked that it could not be checked."
)
REFUSED_WITHOUT_WORDS = (
    "Trinity refused to load this skill into the run. Do not carry out its steps another way."
)

_SKILL_TOOLS = ("Skill",)
_AGENT_TOOLS = ("Agent", "Task")

Call = namedtuple("Call", "via invoked names resolved subagent")
Outcome = namedtuple("Outcome", "code message event fields")
SubagentLookup = namedtuple("SubagentLookup", "found skills resolved")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    """The hook. Returns 0 or 2; the bootstrap exits with it."""
    os.environ.clear()                    # nothing below may be steered by our own env
    box: Dict[str, object] = {}
    worker = threading.Thread(target=_decide_into, args=(box,), daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    outcome = box.get("outcome")
    if not isinstance(outcome, Outcome):
        outcome = _no_verdict("deadline", box.get("call"))
    try:
        _log(outcome)
    except BaseException:  # noqa: BLE001 — a log can never change the verdict
        pass
    if outcome.code == 2:
        _say(outcome.message or COULD_NOT_CHECK)
        return 2
    return 0


def _decide_into(box: Dict[str, object]) -> None:
    try:
        box["outcome"] = _decide(box)
    except BaseException:  # noqa: BLE001 — every failure takes the marker branch
        box["outcome"] = _no_verdict("error", box.get("call"))


def _decide(box: Dict[str, object]) -> Outcome:
    payload = _read_payload()
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool not in _SKILL_TOOLS and tool not in _AGENT_TOOLS:
        return Outcome(0, None, None, {})  # not a skill load: not this hook's business
    claude_env = read_environ(os.getppid())   # the claude process's launch env
    if tool in _SKILL_TOOLS:
        call = _skill_call(tool_input, claude_env)
    else:
        call = _subagent_call(tool_input, claude_env)
    if call is None:                      # a subagent that preloads nothing
        return Outcome(0, None, None, {})
    box["call"] = call
    return _ask(call, claude_env)


def _read_payload() -> dict:
    raw = sys.stdin.buffer.read(STDIN_MAX_BYTES + 1)
    if len(raw) > STDIN_MAX_BYTES:
        raise ValueError("hook input too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hook input is not an object")
    return payload


# ---------------------------------------------------------------------------
# What the call loads
# ---------------------------------------------------------------------------

def _skill_call(tool_input, claude_env: Dict[str, str]) -> Call:
    raw = tool_input.get("skill") if isinstance(tool_input, dict) else None
    invoked = _invoked_name(raw)
    if invoked is None:
        return Call("skill_tool", None, [], False, None)
    home = _user_home()
    roots = skill_roots(home, _project_dir(), _config_dir(claude_env))
    names, complete = resolve_skill_names(invoked, roots)
    return Call("skill_tool", invoked, names, complete and home is not None, None)


def _config_dir(claude_env: Dict[str, str]) -> Optional[str]:
    """Where the CLI reads user skills and agents when `.env` moved them."""
    value = (claude_env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return value if value.startswith("/") else None


def _invoked_name(raw) -> Optional[str]:
    """The CLI trims the name and strips one leading `/`."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if name.startswith("/"):
        name = name[1:].strip()
    if not name or len(name) > MAX_NAME_LEN:
        return None
    return name


def _subagent_call(tool_input, claude_env: Dict[str, str]) -> Optional[Call]:
    """A subagent definition's `skills:` preloads them with no Skill call. A
    subagent with no definition on disk (a built-in) preloads nothing. No
    `subagent_type` means `general-purpose`, which a user or project
    definition of that name overrides."""
    raw = tool_input.get("subagent_type") if isinstance(tool_input, dict) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = "general-purpose"
    if not isinstance(raw, str):
        return Call("subagent_preload", None, [], False, None)
    subagent = raw.strip()
    if len(subagent) > MAX_NAME_LEN:
        return Call("subagent_preload", None, [], False, None)
    home = _user_home()
    if home is None:
        return Call("subagent_preload", None, [], False, subagent)
    config = _config_dir(claude_env)
    lookup = find_subagent(subagent, home, _project_dir(), config)
    if not lookup.found:
        return None if lookup.resolved else Call("subagent_preload", None, [], False, subagent)
    if not lookup.skills:
        return None if lookup.resolved else Call("subagent_preload", None, [], False, subagent)
    names: List[str] = []
    resolved = lookup.resolved
    roots = skill_roots(home, _project_dir(), config)
    for skill in lookup.skills:
        found, complete = resolve_skill_names(skill, roots)
        resolved = resolved and complete
        for name in found:
            if name not in names:
                names.append(name)
    return Call("subagent_preload", None, names, resolved, subagent)


def _call_names(invoked: str) -> List[str]:
    """The invoked name and its last `:` segment — a plugin skill (`acme:pay`)
    and a nested one (`apps/web:pay`) also answer to the bare last segment."""
    names = [invoked]
    last = invoked.rsplit(":", 1)[-1].strip()
    if last and last not in names:
        names.append(last)
    return names


def resolve_skill_names(invoked: str, roots: List[str]) -> Tuple[List[str], bool]:
    """Every name the skill `invoked` answers to: the invoked name, its last
    segment, and every name of each skill directory that answers to either
    (its directory name, #751's front-matter read, a YAML-faithful `name:`).
    Generous on purpose — over-matching only adds refusals. `complete` is
    False when a scan hit a bound or could not read a directory, and when no
    directory answers to the call by its own name while one has a `name:`
    this cannot read: that one may be the skill the CLI resolves."""
    names = _call_names(invoked)
    wanted = {n.casefold() for n in names}
    complete = True
    by_directory = False
    unreadable = False
    for root in roots:
        entries, ok = _list_dir(root)
        complete = complete and ok
        for entry in entries:
            aliases, readable = _skill_dir_aliases(root, entry)
            if not aliases:
                continue
            unreadable = unreadable or not readable
            by_directory = by_directory or entry.casefold() in wanted
            if any(a.casefold() in wanted for a in aliases):
                for alias in aliases:
                    if alias not in names:
                        names.append(alias)
    return names, complete and (by_directory or not unreadable)


def skill_roots(home: Optional[str], cwd: Optional[str],
                config_dir: Optional[str] = None) -> List[str]:
    return _roots(home, cwd, "skills", config_dir)


def agent_roots(home: Optional[str], cwd: Optional[str],
                config_dir: Optional[str] = None) -> List[str]:
    return _roots(home, cwd, "agents", config_dir)


def _roots(home: Optional[str], cwd: Optional[str], kind: str,
           config_dir: Optional[str] = None) -> List[str]:
    roots: List[str] = []
    if home:
        roots.append(os.path.join(home, ".claude", kind))
    if config_dir and os.path.join(config_dir, kind) not in roots:
        roots.append(os.path.join(config_dir, kind))
    if cwd:
        d = os.path.abspath(cwd)
        for _ in range(MAX_DEPTH):
            path = os.path.join(d, ".claude", kind)
            if path not in roots:
                roots.append(path)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return roots


def _list_dir(root: str) -> Tuple[List[str], bool]:
    try:
        with os.scandir(root) as it:
            out: List[str] = []
            for entry in it:
                if len(out) >= MAX_ENTRIES:
                    return out, False
                out.append(entry.name)
            return out, True
    except (FileNotFoundError, NotADirectoryError):
        return [], True
    except OSError:
        return [], False


def _skill_dir_aliases(root: str, entry: str) -> Tuple[List[str], bool]:
    """`(names one entry of a skills directory answers to, whether its name
    could be read)`; `[]` when it is not a skill directory. A symlinked
    directory also answers to its target's name."""
    path = os.path.join(root, entry)
    try:
        st = os.lstat(path)
    except OSError:
        return [], True
    aliases = [entry]
    if stat.S_ISLNK(st.st_mode):
        real = os.path.realpath(path)
        base = os.path.basename(real.rstrip("/"))
        if base and base not in aliases:
            aliases.append(base)
        try:
            if not stat.S_ISDIR(os.stat(path).st_mode):
                return [], True
        except OSError:
            return [], True
        path = real
    elif not stat.S_ISDIR(st.st_mode):
        return [], True
    text = _read_text(os.path.join(path, "SKILL.md"), FRONT_MATTER_MAX_BYTES)
    readable = True
    if text:
        names, readable = front_matter_names(text)
        for name in [_fm_name_751(text), *names]:
            if name and name not in aliases:
                aliases.append(name)
    return aliases, readable


def _read_text(path: str, limit: int) -> Optional[str]:
    """A REGULAR file's text, bounded. Never blocks: opened non-blocking and
    checked after opening, so a FIFO or a device swapped in is skipped."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, total = [], 0
        while total < limit:
            chunk = os.read(fd, min(65536, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _fm_name_751(text: str) -> Optional[str]:
    """#751's own read (`skill_gate_service._FINGERPRINT_SCRIPT.fm_name`),
    byte for byte: a gate key it resolves must be found here too."""
    head = text[:4096]
    if not head.startswith("---"):
        return None
    for line in head.splitlines()[1:]:
        if line.strip() == "---":
            return None
        if line.lower().startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def front_matter(text: str) -> Optional[List[str]]:
    """The lines between the opening and the closing `---`, or None."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return lines[1:i]
    return None


def yaml_names(text: str) -> List[str]:
    return front_matter_names(text)[0]


def front_matter_names(text: str) -> Tuple[List[str], bool]:
    """Every top-level `name:` value in the front matter, read as YAML reads
    it — plain, quoted, with a ` # comment`, or a `|` / `>` block — and
    whether every one could be read. Every value: a duplicate key
    over-matches. A file that opens front matter and does not close it within
    the bound cannot be read; a file with no front matter has no name."""
    body = text[1:] if text.startswith("\ufeff") else text
    lines = front_matter(text)
    if lines is None:
        first = body.splitlines()[:1]
        return [], not (first and first[0].rstrip() == "---")
    out: List[str] = []
    readable = True
    for i, line in enumerate(lines):
        m = re.match(r"""^(?:name|"name"|'name')\s*:(.*)$""", line)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw[:1] in ("|", ">"):
            block = []
            for following in lines[i + 1:]:
                if not following.strip():
                    continue
                if following[:1] not in (" ", "\t"):
                    break
                block.append(following.strip())
            if not block or not re.match(r"^[|>][+-]?[0-9]?\s*(#.*)?$", raw):
                readable = False
                continue
            values = (" ".join(block), "\n".join(block))
        elif not raw or raw.startswith("#") or raw.split(" #")[0].strip() in ("~", "null", "Null", "NULL"):
            continue                      # an empty name: the CLI falls back to the directory
        else:
            value = _scalar(raw)
            if value is None:
                readable = False
                continue
            values = (value,)
        for value in values:
            if value and value not in out:
                out.append(value)
    return out, readable


def _scalar(raw: str) -> Optional[str]:
    """One YAML scalar on one line, or None for anything that is not one."""
    v = raw.strip()
    if not v:
        return None
    if v[0] == '"':
        out, i = [], 1
        while i < len(v):
            if v[i] == "\\" and i + 1 < len(v):
                out.append(v[i + 1])
                i += 2
                continue
            if v[i] == '"':
                return "".join(out)
            out.append(v[i])
            i += 1
        return None
    if v[0] == "'":
        out, i = [], 1
        while i < len(v):
            if v[i] == "'":
                if i + 1 < len(v) and v[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out)
            out.append(v[i])
            i += 1
        return None
    comment = re.search(r"\s#", v)
    if comment:
        v = v[:comment.start()].strip()
    if not v or v[0] in "[]{}|>&*!%@`" or v in ("~", "null", "Null", "NULL"):
        return None
    return v


def parse_skills_field(front: str) -> Optional[List[str]]:
    """The `skills:` of a subagent definition's front matter (its text between
    the `---` lines): a list, [] when absent or empty, None when it cannot be
    read — never [] for something unreadable, which would read as "preloads
    nothing"."""
    lines = front.splitlines()
    skills: List[str] = []
    for i, line in enumerate(lines):
        m = re.match(r"""^(?:skills|"skills"|'skills')\s*:(.*)$""", line)
        if not m:
            continue
        value = m.group(1).strip()
        comment = re.search(r"(^|\s)#", value)
        if comment and not value.startswith(("'", '"')):
            value = value[:comment.start()].strip()
        if not value:
            items = _block_sequence(lines[i + 1:])
        elif value.startswith("["):
            items = _flow_sequence(value)
        elif value in ("~", "null", "Null", "NULL"):
            items = []
        elif value[0] in "{|>&*!":
            items = None
        else:
            scalar = _scalar(value)
            items = None if scalar is None else [s.strip() for s in scalar.split(",") if s.strip()]
        if items is None:
            return None
        for item in items:
            if item not in skills:
                skills.append(item)
    return skills


def _block_sequence(lines: List[str]) -> Optional[List[str]]:
    items: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "-" or stripped.startswith("- "):
            item = _scalar(stripped[1:])
            if item is None:
                return None
            items.append(item)
            continue
        if len(line) == len(stripped):    # the next top-level key
            break
        return None                       # indented, not an item: a nested mapping
    return items


def _flow_sequence(value: str) -> Optional[List[str]]:
    if not value.endswith("]"):
        comment = re.search(r"\]\s+#", value)
        if not comment:
            return None
        value = value[:comment.start() + 1]
    body = value[1:-1].strip()
    if not body:
        return []
    items = []
    for part in body.split(","):
        item = _scalar(part)
        if item is None:
            return None
        items.append(item)
    return items


def find_subagent(subagent_type: str, home: Optional[str], cwd: Optional[str],
                  config_dir: Optional[str] = None) -> SubagentLookup:
    """The definition(s) answering to `subagent_type`, by front-matter `name`
    (its identity), or the file name as a generous fallback; a plugin type is
    looked up by its last segment under the plugins tree. A plugin type that is
    not found cannot be resolved; any other is a built-in, which preloads
    nothing — unless some definition's name could not be read, which may be
    the one the CLI uses."""
    if not isinstance(subagent_type, str) or not subagent_type.strip():
        return SubagentLookup(False, [], False)
    wanted = subagent_type.strip()
    plugin = ":" in wanted
    last = wanted.rsplit(":", 1)[-1].casefold()
    if plugin:
        trees = [os.path.join(base, "plugins")
                 for base in ([os.path.join(home, ".claude")] if home else []) + ([config_dir] if config_dir else [])]
        files, complete = _definition_files(trees, max_entries=MAX_PLUGIN_ENTRIES,
                                            max_depth=MAX_PLUGIN_DEPTH, only_under_agents=True)
    else:
        files, complete = _definition_files(agent_roots(home, cwd, config_dir),
                                            max_entries=MAX_DEFINITION_ENTRIES,
                                            max_depth=MAX_DEPTH, only_under_agents=False)
    matched: List[Optional[List[str]]] = []
    unknown = False
    for path in files:
        text = _read_text(path, FRONT_MATTER_MAX_BYTES)
        if not text:
            continue
        names, readable = front_matter_names(text)
        front = front_matter(text)
        if front is None and readable:
            continue                       # no front matter at all: not a definition
        stem = os.path.basename(path)[:-3].casefold()
        if last in {n.casefold() for n in names} or stem == last:
            matched.append(front)          # None: opened and never closed — its skills are unknown
        elif not readable:
            unknown = True                 # its name cannot be read: it may be this one
    if not matched:
        return SubagentLookup(False, [], complete and not plugin and not unknown)
    skills: List[str] = []
    resolved = complete
    for front in matched:
        parsed = parse_skills_field("\n".join(front)) if front is not None else None
        if parsed is None:
            resolved = False
            continue
        for skill in parsed:
            if skill not in skills:
                skills.append(skill)
    return SubagentLookup(True, skills, resolved)


def _definition_files(roots: List[str], *, max_entries: int, max_depth: int,
                      only_under_agents: bool) -> Tuple[List[str], bool]:
    """`*.md` files under `roots` (recursively; under a directory named
    `agents` when `only_under_agents`). Bounded; symlinks followed once each."""
    files: List[str] = []
    complete = True
    seen = 0
    visited = set()
    stack = [(root, 0, not only_under_agents) for root in roots]
    while stack:
        path, depth, in_agents = stack.pop()
        try:
            st = os.stat(path)
            key = (st.st_dev, st.st_ino)
            if key in visited:
                continue
            visited.add(key)
            it = os.scandir(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            complete = False
            continue
        with it:
            for entry in it:
                seen += 1
                if seen > max_entries:
                    return files, False
                try:
                    mode = entry.stat().st_mode     # follows a symlink, like the CLI
                except OSError:
                    continue
                if stat.S_ISDIR(mode):
                    if entry.name in (".git", "node_modules"):
                        continue
                    if depth + 1 > max_depth:
                        complete = False
                        continue
                    stack.append((entry.path, depth + 1, in_agents or entry.name == "agents"))
                elif in_agents and stat.S_ISREG(mode) and entry.name.endswith(".md"):
                    files.append(entry.path)
    return files, complete


# ---------------------------------------------------------------------------
# Who is asking — /proc, never our own environment
# ---------------------------------------------------------------------------

def read_environ(pid) -> Dict[str, str]:
    """A process's launch environment (NUL-separated, first wins); {} when it
    cannot be read."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "environ"), "rb") as f:
            raw = f.read(ENVIRON_MAX_BYTES)
    except OSError:
        return {}
    env: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env.setdefault(key.decode("utf-8", "replace"), value.decode("utf-8", "replace"))
    return env


def _project_dir() -> Optional[str]:
    """The claude process's working directory (its project)."""
    try:
        return os.readlink(os.path.join(PROC_ROOT, str(os.getppid()), "cwd"))
    except OSError:
        return None


def _user_home() -> Optional[str]:
    if USER_HOME:
        return USER_HOME
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return None


def _marker_present() -> bool:
    return os.path.lexists(MARKER)


# ---------------------------------------------------------------------------
# The question and the verdict
# ---------------------------------------------------------------------------

def _body(call: Call, execution_id: Optional[str], marker: bool) -> dict:
    """The request — within the route's own bounds (a 422 is "no answer")."""
    names = [n for n in call.names if isinstance(n, str) and 0 < len(n) <= MAX_NAME_LEN]
    resolved = bool(call.resolved) and len(names) == len(call.names) and len(names) <= MAX_NAMES
    invoked = call.invoked if call.invoked and len(call.invoked) <= MAX_NAME_LEN else None
    subagent = call.subagent if call.subagent and len(call.subagent) <= MAX_NAME_LEN else None
    return {"via": call.via, "invoked": invoked, "names": names[:MAX_NAMES], "resolved": resolved,
            "subagent": subagent, "execution_id": execution_id, "marker": marker}


def _ask(call: Call, claude_env: Dict[str, str]) -> Outcome:
    container = read_environ(1)
    key = container.get("TRINITY_MCP_API_KEY")
    if not key:
        return _no_verdict("no_identity", call)
    base = container.get("TRINITY_BACKEND_URL") or DEFAULT_BACKEND_URL
    run = claude_env.get("TRINITY_EXECUTION_ID") or None
    if run is not None and len(run) > MAX_EXECUTION_ID_LEN:
        run = None
    try:
        status, payload = _post(base, key, _body(call, run, _marker_present()))
    except Exception:  # noqa: BLE001 — refused, reset twice, timed out, unresolvable
        return _no_verdict("unreachable", call)
    return _verdict(status, payload, call)


def _post(base: str, key: str, body: dict) -> Tuple[int, bytes]:
    """One POST, retried once on a refused or dropped connection. `http.client`
    directly: no proxy, no redirect, nothing read from the environment."""
    import http.client
    from urllib.parse import urlsplit

    url = urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("bad backend url")
    port = url.port or (443 if url.scheme == "https" else 80)
    path = url.path.rstrip("/") + CHECK_PATH
    data = json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Content-Length": str(len(data))}
    connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    for attempt in (1, 2):
        conn = connection(url.hostname, port, timeout=HTTP_TIMEOUT_SECONDS)
        try:
            conn.request("POST", path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(RESPONSE_MAX_BYTES)
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError):
            if attempt == 2:
                raise
        finally:
            conn.close()
    raise RuntimeError("unreachable")


def _verdict(status: int, payload: bytes, call: Call) -> Outcome:
    if status != 200:
        return _no_verdict(f"status_{status}", call)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _no_verdict("malformed", call)
    if not isinstance(data, dict) or not isinstance(data.get("allowed"), bool):
        return _no_verdict("malformed", call)
    if data["allowed"] is True:
        return Outcome(0, None, "skill_gate_allow", _fields(call))
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        message = REFUSED_WITHOUT_WORDS
    return Outcome(2, message[:MESSAGE_MAX_CHARS], "skill_gate_deny", _fields(call))


def _no_verdict(reason: str, call) -> Outcome:
    """No answer from the platform: the marker decides. Present — this agent
    has gates — refuses; absent lets the call through."""
    marker = _marker_present()
    fields = dict(_fields(call), reason=reason, marker=marker)
    if marker:
        return Outcome(2, COULD_NOT_CHECK, "skill_gate_unknown", fields)
    return Outcome(0, None, "skill_gate_unknown", fields)


def _fields(call) -> dict:
    """What the log may carry: names and how they were asked — never the key,
    never an exception's text."""
    if not isinstance(call, Call):
        return {}
    return {"via": call.via, "invoked": call.invoked, "names": list(call.names)[:16],
            "subagent": call.subagent, "resolved": bool(call.resolved)}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _log(outcome: Outcome) -> None:
    """To /logs/guardrails.jsonl (Vector) through the guardrails' own helper,
    in a thread with a short join: a failing or blocking write never changes
    the exit code and never holds the tool."""
    if not outcome.event:
        return

    def write():
        try:
            import lib
            lib.log_event(outcome.event, **outcome.fields)
        except BaseException:  # noqa: BLE001
            pass

    t = threading.Thread(target=write, daemon=True)
    t.start()
    t.join(LOG_JOIN_SECONDS)


def _say(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The image build's smoke: `RUN … skill-gate.py --self-test`
# ---------------------------------------------------------------------------

def self_test() -> int:
    """Imports under `-I -S` and decides, with no network and no marker
    assumption. Returns 0; raises (failing the build) otherwise."""
    import tempfile

    def expect(condition, what):
        if not condition:
            raise RuntimeError(f"skill-gate self-test failed: {what}")

    call = Call("skill_tool", "pay-invoice", ["pay-invoice"], True, None)
    expect(_verdict(200, b'{"allowed": true}', call).code == 0, "an allow allows")
    deny = _verdict(200, json.dumps({"allowed": False, "message": "no"}).encode(), call)
    expect(deny.code == 2 and deny.message == "no", "a refusal refuses with its words")
    expect(_verdict(200, b"[]", call).code in (0, 2), "a malformed answer is not a verdict")
    expect(_verdict(503, b"{}", call).event == "skill_gate_unknown", "an error is not a verdict")
    expect(parse_skills_field("name: f\nskills: [a, b]") == ["a", "b"], "a skills list parses")
    expect(parse_skills_field("name: f\nskills: {a: 1}") is None, "a mapping is unreadable")
    with tempfile.TemporaryDirectory() as home:
        names, complete = resolve_skill_names("acme:pay-invoice", skill_roots(home, home))
        expect(complete and names == ["acme:pay-invoice", "pay-invoice"], "names resolve")
        expect(find_subagent("Explore", home, home) == SubagentLookup(False, [], True),
               "a built-in subagent preloads nothing")
    json.dumps(_body(call, "exec-1", False))
    import http.client  # noqa: F401 — the network module imports under -I -S
    return 0
