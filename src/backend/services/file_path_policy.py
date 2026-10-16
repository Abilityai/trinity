"""One path policy for the agent Files surface (trinity-enterprise#820).

Which file classes a path falls in, and which tier may use which verb on each
class. The backend decides the tier (only it can tell callers apart); the agent
server applies the platform floor and checks that a link does not change a
path's class.

VENDORED, BYTE-IDENTICAL (architecture.md Invariant #5):
  src/backend/services/file_path_policy.py
  docker/base-image/agent_server/file_path_policy.py
The agent server ships as its own image and cannot import the backend. Edit
one, copy it to the other; tests/unit/test_ent820_path_policy_parity.py fails
when they differ. Pure stdlib, no imports from either tree.

Paths are POSIX strings. A relative path is read under `home`, as the agent
server reads it. fnmatch's `*` crosses `/`, so `.trinity/backup/*` covers any
depth.
"""
import fnmatch
import posixpath
import re
from typing import NamedTuple, Optional

HOME = "/home/developer"

# --- file classes -----------------------------------------------------------

CREDENTIAL = "credential"
RUNTIME = "runtime"
ALIAS = "alias"
TRINITY_COPY = "trinity_copy"
SECRET_CLASS = "secret_class"
TRINITY = "trinity"
GIT_METADATA = "git_metadata"
SYSTEM = "system"
INSTRUCTIONS = "instructions"
SKILLS = "skills"
RUNS_CODE = "runs_code"
HIDDEN = "hidden"

# Each runtime's login and MCP config (trinity-enterprise#823). Codex keeps
# them in CODEX_HOME = $TMPDIR/codex = ~/.tmp/codex.
RUNTIME_CONFIG_PATTERNS = (
    ".claude.json",
    ".claude/.credentials.json",
    ".gemini/settings.json",
    ".tmp/codex/*",
)
# Never writable through the file routes, owner tier to read (ent#819).
CREDENTIAL_PATH_PATTERNS = (
    ".env",
    ".env.*",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".ssh/*",
    ".aws/*",
    ".gcp/*",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".git/config",
)
# Other spellings of a home-dir path: /proc/self/{cwd,root}/... and /dev/fd/N
# lead back into the home dir.
PATH_ALIAS_PATTERNS = (
    "/proc/*",
    "/dev/*",
)
# Trinity-managed copies that may hold credentials (write-denied as TRINITY).
TRINITY_CREDENTIAL_COPY_PATTERNS = (
    ".trinity/git-credential",
    ".trinity/backup/*",
)
# Secret file classes: owner tier to read, writable like any file.
SECRET_FILE_CLASS_PATTERNS = (
    ".kube/config",
    ".config/gcloud/*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
)
TRINITY_PATTERNS = (
    ".trinity",
    ".trinity/*",
)
SYSTEM_PATTERNS = (
    "/opt/trinity/*",
    "/etc/claude-code/*",
    "/etc/*",
    "/sys/*",
)
# The agent's standing instructions, per runtime (ent#164, ent#820). Only the
# agent's own top-level files count: a `docs/CLAUDE.md` is an ordinary file.
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md")
INSTRUCTION_DIRS = (".claude",)
INSTRUCTION_NESTED_FILES = (".gemini/GEMINI.md",)
SKILLS_DIR = ".claude/skills"
# The agent's published read surface (CLAUDE.md rule 8, #919): the MCP pipeline
# tools list these for anyone with access, so a share holder may list them
# although they sit under the hidden `.trinity`.
PUBLISHED_LIST_DIRS = (".trinity/pipelines", ".trinity/pipeline-state")
# Files the shell, git or PATH run on the agent's behalf (ent#820): editing one
# runs code outside the file guardrails, on the platform's own git sync too.
RUNS_CODE_FILES = (".gitconfig", ".bashrc", ".bash_profile", ".profile", ".zshrc")
RUNS_CODE_DIRS = (".config/git", ".local/bin")

# The agent server's platform floor: what its routes never write or delete,
# for every caller, the platform included (it cannot tell callers apart). By
# NAME, on the path or any parent. A deliberate SUBSET of the backend route
# floor: platform writers reach the agent server directly and write paths the
# user routes refuse (read-only mode writes `.claude/settings.local.json`).
AGENT_SERVER_EDIT_PROTECTED_NAMES = (
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".claude.json",
    ".credentials.json",
)
# `CLAUDE.md` is deletable by no one through these routes; it stays editable
# (the owner edits instructions, the backend tiers that).
AGENT_SERVER_DELETE_PROTECTED_NAMES = (
    "CLAUDE.md",
    ".trinity",
    ".git",
    ".gitignore",
    ".env",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".claude.json",
    ".credentials.json",
)
# Runtime config matched on the RESOLVED path by the agent server (#823).
AGENT_SERVER_RUNTIME_CONFIG_RELPATHS = (
    ".claude.json",
    ".claude/.credentials.json",
    ".gemini/settings.json",
    ".tmp/codex",
)

_CLASS_PATTERNS = (
    (CREDENTIAL, CREDENTIAL_PATH_PATTERNS),
    (RUNTIME, RUNTIME_CONFIG_PATTERNS),
    (ALIAS, PATH_ALIAS_PATTERNS),
    (TRINITY_COPY, TRINITY_CREDENTIAL_COPY_PATTERNS),
    (SECRET_CLASS, SECRET_FILE_CLASS_PATTERNS),
    (TRINITY, TRINITY_PATTERNS),
    (SYSTEM, SYSTEM_PATTERNS),
)

# The backend route floors.
WRITE_DENY_CLASSES = frozenset({CREDENTIAL, RUNTIME, ALIAS, TRINITY, GIT_METADATA, SYSTEM})
OWNER_TIER_READ_CLASSES = frozenset({CREDENTIAL, RUNTIME, ALIAS, TRINITY_COPY, SECRET_CLASS})

# The pattern lists the write floor is built from, for the anchor set below and
# for the guardrails-baseline.json parity test.
FILE_WRITE_DENY_PATTERNS = (
    CREDENTIAL_PATH_PATTERNS + RUNTIME_CONFIG_PATTERNS + TRINITY_PATTERNS
    + (".git/*", ".gitignore") + SYSTEM_PATTERNS + PATH_ALIAS_PATTERNS
)


# --- normalisation and classification --------------------------------------

def normalize(raw: str, home: str = HOME) -> str:
    """A user path reduced to a stable absolute form, lexically (no FS access).

    Relative paths are anchored at `home`. Leading slashes are collapsed first:
    posixpath.normpath keeps exactly two, Linux reads them as one (ent#792).
    An empty path normalises to "" (callers fail closed on it).
    """
    if not raw:
        return ""
    if raw.startswith("/"):
        return posixpath.normpath("/" + raw.lstrip("/"))
    return posixpath.normpath(posixpath.join(home, raw))


def _rel_to_home(normalized: str, home: str) -> Optional[str]:
    """`normalized` relative to `home`; "" for home itself; None outside it."""
    if normalized == home:
        return ""
    if normalized.startswith(home + "/"):
        return normalized[len(home) + 1:]
    return None


_COMPILED: dict = {}


def _compiled(patterns) -> "re.Pattern":
    """One regex per pattern tuple (fnmatch semantics), built once."""
    key = tuple(patterns)
    rx = _COMPILED.get(key)
    if rx is None:
        rx = _COMPILED[key] = re.compile("|".join(fnmatch.translate(p) for p in key))
    return rx


def matches_any(normalized: str, patterns, home: str = HOME) -> bool:
    """Basename, full-path or home-relative glob match (mirrors the file
    guardrail hook). `normalized` is the output of `normalize`."""
    if not normalized or not patterns:
        return False
    rx = _compiled(patterns)
    rel = _rel_to_home(normalized, home) or ""
    return bool(rx.match(posixpath.basename(normalized)) or rx.match(normalized)
                or (rel and rx.match(rel)))


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def _is_git_metadata(normalized: str) -> bool:
    """`.git` or `.gitignore` at any depth, and a bare repository's `config` /
    `hooks/**` (a directory whose name ends in `.git`)."""
    parts = [p for p in normalized.split("/") if p]
    if any(p == ".git" for p in parts) or (parts and parts[-1] == ".gitignore"):
        return True
    for i, part in enumerate(parts[:-1]):
        if part.endswith(".git"):
            rest = parts[i + 1:]
            if rest[0] == "hooks" or rest == ["config"]:
                return True
    return False


def classes(path: str, home: str = HOME, *, include_ancestors: bool = False) -> frozenset:
    """Every class `path` falls in. With `include_ancestors` (DELETE), a
    directory ABOVE an instruction, skills or runs-code location counts as that
    class too: deleting `.claude` removes the instructions as surely."""
    normalized = normalize(path, home)
    if not normalized:
        return frozenset()
    found = {name for name, patterns in _CLASS_PATTERNS if matches_any(normalized, patterns, home)}
    if _is_git_metadata(normalized):
        found.add(GIT_METADATA)
    rel = _rel_to_home(normalized, home)
    if rel is None:
        # A directory above the home (`/`, `/home`) holds all of it.
        if include_ancestors and home.startswith(normalized.rstrip("/") + "/"):
            found |= {INSTRUCTIONS, SKILLS, RUNS_CODE}
        return frozenset(found)
    if rel and any(p.startswith(".") for p in rel.split("/")):
        found.add(HIDDEN)
    instruction_roots = INSTRUCTION_FILES + INSTRUCTION_NESTED_FILES
    if (rel in instruction_roots
            or any(_under(rel, d) and not _under(rel, SKILLS_DIR) for d in INSTRUCTION_DIRS)):
        found.add(INSTRUCTIONS)
    if _under(rel, SKILLS_DIR):
        found.add(SKILLS)
    if rel in RUNS_CODE_FILES or any(_under(rel, d) for d in RUNS_CODE_DIRS):
        found.add(RUNS_CODE)
    if include_ancestors:
        above = (lambda target: rel == "" or target.startswith(rel + "/"))
        if any(above(t) for t in instruction_roots + INSTRUCTION_DIRS):
            found.add(INSTRUCTIONS)
        if above(SKILLS_DIR):
            found.add(SKILLS)
        if any(above(t) for t in RUNS_CODE_FILES + RUNS_CODE_DIRS):
            found.add(RUNS_CODE)
    return frozenset(found)


# --- the backend route floors ----------------------------------------------

def deny_anchor(pattern: str, home: str = HOME) -> str:
    """The fixed directory (or file) a path-anchored deny pattern lives under:
    its literal prefix, cut back to the last `/` before the first glob char.
    Relative patterns are anchored at `home`."""
    glob_at = min((i for i, c in enumerate(pattern) if c in "*?["), default=None)
    head = pattern if glob_at is None else pattern[:pattern.rfind("/", 0, glob_at) + 1]
    if not head.startswith("/"):
        head = posixpath.join(home, head)
    return head.rstrip("/") or "/"


DENY_ANCHORS = tuple(deny_anchor(p) for p in FILE_WRITE_DENY_PATTERNS if "/" in p)


def write_denied(path: str, home: str = HOME) -> bool:
    """The route write floor: refused for every caller. Fails closed on ""."""
    if not normalize(path, home):
        return True
    return bool(classes(path, home) & WRITE_DENY_CLASSES)


def _holds_anchor(normalized: str, home: str) -> bool:
    head = normalized.rstrip("/")
    anchors = DENY_ANCHORS if home == HOME else tuple(
        deny_anchor(p, home) for p in FILE_WRITE_DENY_PATTERNS if "/" in p)
    return any(a == head or a.startswith(head + "/") for a in anchors)


# Deletable by no one, though writable (the agent server refuses these by name).
DELETE_DENY_NAMES = ("CLAUDE.md",)


def delete_denied(path: str, home: str = HOME) -> bool:
    """The route delete floor: the write floor, a name no one deletes, plus any
    directory that holds a path-anchored protected location (ent#792). `/`
    holds every anchor."""
    normalized = normalize(path, home)
    return (write_denied(path, home) or posixpath.basename(normalized) in DELETE_DENY_NAMES
            or _holds_anchor(normalized, home))


def owner_tier_read(path: str, home: str = HOME) -> bool:
    """Reading `path` takes the owner tier (ent#819). Fails closed on ""."""
    if not normalize(path, home):
        return True
    return bool(classes(path, home) & OWNER_TIER_READ_CLASSES)


# --- tier x verb -------------------------------------------------------------

OWNER = "owner"   # a person who is the agent's owner, or an admin
SHARE = "share"   # a person the agent is shared with
KEY = "key"       # an agent, system, connector or ops key

LIST, READ, WRITE, DELETE = "list", "read", "write", "delete"
VERBS = (LIST, READ, WRITE, DELETE)
TIERS = (OWNER, SHARE, KEY)

PROTECTED = "protected_path"
OWNER_TIER = "owner_tier_path"
PERSON_REQUIRED = "person_required"

class Verdict(NamedTuple):
    allowed: bool
    code: Optional[str] = None


ALLOW = Verdict(True)


def decide(tier: str, verb: str, path: str, home: str = HOME) -> Verdict:
    """May `tier` use `verb` on `path`? The one table the Files routes follow.

    Capabilities are not decided here: an agent key that clears this still
    needs `instructions.manage` / `skills.manage` for those classes (ent#164,
    ent#596), which the backend enforces with its own audit row.
    """
    if tier not in TIERS or verb not in VERBS:
        raise ValueError(f"unknown tier/verb: {tier!r}/{verb!r}")
    normalized = normalize(path, home)
    found = classes(normalized, home) if normalized else frozenset()
    above = classes(normalized, home, include_ancestors=True) if (normalized and verb == DELETE) else found
    return _decide(tier, verb, normalized, found, above, home)


def _decide(tier, verb, normalized, found, above, home) -> Verdict:
    if verb == READ:
        if (normalized and not found & OWNER_TIER_READ_CLASSES) or tier == OWNER:
            return ALLOW
        return Verdict(False, PERSON_REQUIRED if tier == KEY else OWNER_TIER)
    if verb == LIST:
        if tier == SHARE and (not normalized or found & OWNER_TIER_READ_CLASSES
                              or (HIDDEN in found and not _published(normalized, home))):
            return Verdict(False, OWNER_TIER)
        return ALLOW
    if not normalized or found & WRITE_DENY_CLASSES:
        return Verdict(False, PROTECTED)
    if verb == DELETE and (posixpath.basename(normalized) in DELETE_DENY_NAMES
                           or _holds_anchor(normalized, home)):
        return Verdict(False, PROTECTED)
    scoped = above if verb == DELETE else found
    if tier == OWNER:
        return ALLOW
    if tier == SHARE and scoped & {INSTRUCTIONS, SKILLS, RUNS_CODE}:
        return Verdict(False, OWNER_TIER)
    if tier == KEY and RUNS_CODE in scoped:
        return Verdict(False, PROTECTED)
    return ALLOW


def _published(normalized: str, home: str) -> bool:
    rel = _rel_to_home(normalized, home)
    return rel is not None and any(_under(rel, d) for d in PUBLISHED_LIST_DIRS)


def restricted_verbs(tier: str, path: str, home: str = HOME, *, is_dir: bool = False) -> list:
    """The verbs `tier` may NOT use on a listed node, for the Files tab.
    Classifies the path once (a listing can hold thousands of nodes)."""
    normalized = normalize(path, home)
    found = classes(normalized, home) if normalized else frozenset()
    above = classes(normalized, home, include_ancestors=True) if (normalized and is_dir) else found
    verbs = (WRITE, DELETE) if is_dir else (READ, WRITE, DELETE)
    return [v for v in verbs if not _decide(tier, v, normalized, found, above, home).allowed]


# --- the agent server's platform floor --------------------------------------

def agent_server_edit_protected(rel: str) -> bool:
    """By name, on the path or any parent: the agent server never writes it
    (the `.trinity` platform exception is the caller's)."""
    parts = [p for p in rel.split("/") if p]
    return bool(set(parts) & set(AGENT_SERVER_EDIT_PROTECTED_NAMES)) or _is_git_metadata("/" + rel)


def agent_server_delete_protected(rel: str) -> bool:
    parts = [p for p in rel.split("/") if p]
    return bool(set(parts) & set(AGENT_SERVER_DELETE_PROTECTED_NAMES)) or _is_git_metadata("/" + rel)


def agent_server_touches_runtime_config(rel: str, *, include_ancestors: bool) -> bool:
    """`rel` (home-relative, resolved) is runtime config or under it; with
    `include_ancestors` (DELETE), also a directory above one."""
    rel = rel.strip("/")
    for target in AGENT_SERVER_RUNTIME_CONFIG_RELPATHS:
        if _under(rel, target):
            return True
        if include_ancestors and (rel == "" or target.startswith(rel + "/")):
            return True
    return False


def same_class(lexical: str, resolved: str, home: str = HOME, *, verb: Optional[str] = None) -> bool:
    """A link may not change what a path is (ent#820). The class set of the
    path the caller named must equal that of the path it resolves to, and with
    `verb`, so must every tier's verdict for that verb — the backend decided on
    the named path. Comparing the verdicts, not only the classes, is what
    catches a link to the home itself: `ws/l/.ssh` and `~/.ssh` are both just
    "hidden", but only the second holds a protected anchor."""
    if classes(lexical, home) != classes(resolved, home):
        return False
    if verb is None:
        return True
    return all(decide(t, verb, lexical, home) == decide(t, verb, resolved, home) for t in TIERS)
