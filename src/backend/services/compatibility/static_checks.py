"""
Deterministic (STATIC) compatibility checks (#668).

Each check is a PURE function ``(snapshot) -> (status, message, detail)`` over the
collector's workspace-snapshot dict — no Docker, no network — so the whole
catalog is unit-testable with fixture dicts. Functions are registered in
``STATIC_CHECKS`` keyed by check id; a consistency test asserts this registry
matches ``spec.STATIC_IDS`` exactly.

status ∈ {"pass", "fail", "skipped"}. A check returns "skipped" (with a reason in
detail) when its precondition isn't met (e.g. a template.yaml field check when
template.yaml is missing — F-001 already flags that), so we never double-count a
single root cause as several HARD failures.

Secret-bearing values are NEVER echoed: S-003 / S-009 / K-004 report the file and
line and a pattern label, never the matched secret.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from services.template_service import _is_platform_injected
from services.git_service import _GITIGNORE_PATTERNS

# A check result: (status, message, detail)
Result = Tuple[str, str, Optional[Dict[str, Any]]]

PASS = "pass"
FAIL = "fail"
SKIP = "skipped"


# ---------------------------------------------------------------------------
# Snapshot accessors
# ---------------------------------------------------------------------------

def _file(snap: Dict[str, Any], path: str) -> Dict[str, Any]:
    return (snap.get("files") or {}).get(path) or {}


def _exists(snap: Dict[str, Any], path: str) -> bool:
    return bool(_file(snap, path).get("exists"))


def _content(snap: Dict[str, Any], path: str) -> Optional[str]:
    return _file(snap, path).get("content")


def _dir_list(snap: Dict[str, Any], path: str) -> Optional[List[str]]:
    return (snap.get("dirs") or {}).get(path)


def _skill_files(snap: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return snap.get("skills") or {}


def _ok(msg: str, detail: Optional[Dict] = None) -> Result:
    return (PASS, msg, detail)


def _fail(msg: str, detail: Optional[Dict] = None) -> Result:
    return (FAIL, msg, detail)


def _skip(msg: str, reason: str) -> Result:
    return (SKIP, msg, {"skip_reason": reason})


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_yaml(content: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    if content is None:
        return None, "content unavailable"
    try:
        return yaml.safe_load(content), None
    except yaml.YAMLError as e:
        return None, str(e).splitlines()[0] if str(e) else "invalid YAML"


def _template(snap: Dict[str, Any]) -> Tuple[Optional[dict], Optional[str]]:
    """Parsed template.yaml as a dict, or (None, error)."""
    content = _content(snap, "template.yaml")
    if content is None:
        return None, "missing"
    data, err = _parse_yaml(content)
    if err:
        return None, err
    return (data if isinstance(data, dict) else {}), None


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _mcp_vars(snap: Dict[str, Any]) -> List[str]:
    """All ${VAR} names referenced in .mcp.json.template."""
    content = _content(snap, ".mcp.json.template")
    if not content:
        return []
    return sorted(set(_VAR_RE.findall(content)))


def _mcp_server_names(snap: Dict[str, Any]) -> List[str]:
    content = _content(snap, ".mcp.json.template")
    if not content:
        return []
    try:
        data = __import__("json").loads(content)
    except (ValueError, TypeError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def _env_example_vars(snap: Dict[str, Any]) -> List[str]:
    content = _content(snap, ".env.example")
    if not content:
        return []
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if re.match(r"^[A-Z][A-Z0-9_]*$", name):
            out.append(name)
    return out


def _gitignore_lines(snap: Dict[str, Any]) -> List[str]:
    """Trimmed, CRLF-normalized, non-comment .gitignore lines."""
    content = _content(snap, ".gitignore")
    if not content:
        return []
    out = []
    for raw in content.splitlines():
        line = raw.strip().rstrip("\r").strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _has_ignore(snap: Dict[str, Any], *patterns: str) -> bool:
    lines = set(_gitignore_lines(snap))
    return any(p in lines for p in patterns)


# Secret detection (shared by S-003, S-009, K-004). NEVER returns the value.
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "openai-style key (sk-)"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "anthropic key (sk-ant-)"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github pat (ghp_)"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github fine-grained pat"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack token (xox*-)"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{30,}"), "google api key (AIza)"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "aws access key (AKIA)"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
]
# NOTE: the value is captured greedily to end-of-line (`(.*)$`) and stripped by
# the caller — NOT `[ \t]*(.+?)[ \t]*$`. The trailing-whitespace trim around a
# lazy `.+?` made `.+?` and `[ \t]*` both able to match a tab, giving polynomial
# backtracking on agent-supplied text with long `\t` runs (py/polynomial-redos).
_ASSIGN_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD))"
    r"[ \t]*[:=](.*)$"
)
_PLACEHOLDER_RE = re.compile(
    r"(?i)(your[-_ ]|placeholder|changeme|change[-_ ]me|example|xxxx|<[^>]+>|\.\.\.|todo|fixme|dummy|sample)"
)


def _looks_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'")
    if not v:
        return True
    if v.startswith("${") or v.startswith("$"):
        return True
    if _PLACEHOLDER_RE.search(v):
        return True
    # Very short values are unlikely to be real secrets.
    return len(v) < 8


def _scan_secret_values(text: str) -> List[Dict[str, Any]]:
    """Return [{line, pattern}] hits — never the secret value itself."""
    hits: List[Dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for rx, label in _SECRET_PATTERNS:
            if rx.search(line):
                hits.append({"line": i, "pattern": label})
                break
        else:
            m = _ASSIGN_RE.search(line)
            if m and not _looks_placeholder(m.group(2)):
                hits.append({"line": i, "pattern": f"{m.group(1)}=<non-placeholder value>"})
    return hits


# Files that should be committed (scanned for hardcoded secrets) — never .env
# or the generated .mcp.json (existence-only in the snapshot anyway).
_COMMITTED_SCAN_PATHS = (
    "template.yaml", "CLAUDE.md", "AGENTS.md", ".mcp.json.template",
    "README.md", "dashboard.yaml", "ARCHITECTURE.md", "docs/architecture.md",
    "CHANGELOG.md", "docs/memory/requirements.md", "REQUIREMENTS.md",
)


# ===========================================================================
# F — File Structure
# ===========================================================================

def c_f001(snap):  # template.yaml exists
    return _ok("template.yaml present") if _exists(snap, "template.yaml") \
        else _fail("template.yaml is missing — required for Trinity")


def c_f002(snap):  # CLAUDE.md exists
    return _ok("CLAUDE.md present") if _exists(snap, "CLAUDE.md") \
        else _fail("CLAUDE.md is missing — agent would deploy with no instructions")


def c_f003(snap):  # .gitignore exists
    return _ok(".gitignore present") if _exists(snap, ".gitignore") \
        else _fail(".gitignore is missing — secrets may be committed on first sync")


def c_f004(snap):  # .env.example exists
    return _ok(".env.example present") if _exists(snap, ".env.example") \
        else _fail(".env.example is missing — users can't tell what credentials to provide")


def c_f005(snap):  # .mcp.json.template exists if MCP servers declared
    data, err = _template(snap)
    declares_mcp = bool(data and data.get("mcp_servers"))
    if not declares_mcp:
        return _ok("no MCP servers declared")
    return _ok(".mcp.json.template present") if _exists(snap, ".mcp.json.template") \
        else _fail("template.yaml declares mcp_servers but .mcp.json.template is missing")


def c_f006(snap):  # README.md
    return _ok("README.md present") if _exists(snap, "README.md") \
        else _fail("README.md is missing")


def c_f007(snap):  # .trinity/setup.sh when system packages referenced
    claude = _content(snap, "CLAUDE.md") or ""
    refs_pkgs = bool(re.search(r"(?i)(apt-get install|apt install|npm install -g|npm i -g|pip install)", claude))
    if not refs_pkgs:
        return _ok("no system-package installs referenced")
    return _ok(".trinity/setup.sh present") if _exists(snap, ".trinity/setup.sh") \
        else _fail("CLAUDE.md references system packages but .trinity/setup.sh is missing "
                   "(installs won't persist across restarts)")


def c_f008(snap):  # .claude/commands/ exists
    return _ok(".claude/commands/ present") if _dir_list(snap, ".claude/commands") is not None \
        else _fail(".claude/commands/ directory is missing")


def c_f009(snap):  # at least one skill or command file
    return _ok("skills/commands present") if _skill_files(snap) \
        else _fail("no skill or command files found")


def c_f010(snap):  # dashboard.yaml
    return _ok("dashboard.yaml present") if _exists(snap, "dashboard.yaml") \
        else _fail("dashboard.yaml is missing — Dashboard tab will be empty")


def c_f011(snap):  # ARCHITECTURE.md
    return _ok("architecture doc present") if (_exists(snap, "ARCHITECTURE.md") or _exists(snap, "docs/architecture.md")) \
        else _fail("ARCHITECTURE.md (or docs/architecture.md) is missing")


def c_f012(snap):  # requirements doc
    return _ok("requirements doc present") if (_exists(snap, "docs/memory/requirements.md") or _exists(snap, "REQUIREMENTS.md")) \
        else _fail("docs/memory/requirements.md (or REQUIREMENTS.md) is missing")


def c_f013(snap):  # CHANGELOG.md
    return _ok("CHANGELOG.md present") if _exists(snap, "CHANGELOG.md") \
        else _fail("CHANGELOG.md is missing")


# ===========================================================================
# S — Security
# ===========================================================================

def c_s001(snap):  # .env ignored
    return _ok(".env is gitignored") if _has_ignore(snap, ".env", ".env.*") \
        else _fail(".env is not excluded in .gitignore — credentials may be committed")


def c_s002(snap):  # .mcp.json ignored
    return _ok(".mcp.json is gitignored") if _has_ignore(snap, ".mcp.json") \
        else _fail(".mcp.json is not excluded in .gitignore — injected credentials may be committed")


def c_s003(snap):  # no hardcoded secrets in committed files
    hits: List[Dict[str, Any]] = []
    for path in _COMMITTED_SCAN_PATHS:
        content = _content(snap, path)
        if not content:
            continue
        for h in _scan_secret_values(content):
            hits.append({"file": path, **h})
    if hits:
        return _fail(f"possible hardcoded secret(s) in {len(hits)} location(s) — review and remove",
                     {"matches": hits[:25]})
    return _ok("no hardcoded secrets detected in committed files")


def c_s004(snap):  # .claude/projects/ ignored
    return _ok(".claude/projects/ is gitignored") if _has_ignore(snap, ".claude/projects/", ".claude/projects") \
        else _fail(".claude/projects/ is not excluded — Claude Code session history would be committed")


def c_s005(snap):  # .trinity/ ignored
    return _ok(".trinity/ is gitignored") if _has_ignore(snap, ".trinity/", ".trinity") \
        else _fail(".trinity/ is not excluded — platform runtime state would be committed")


def c_s006(snap):  # claude runtime dirs ignored
    needed = [".claude/statsig/", ".claude/todos/", ".claude/debug/",
              ".claude/sessions/", ".claude/shell-snapshots/"]
    lines = set(_gitignore_lines(snap))
    missing = [p for p in needed if p not in lines and p.rstrip("/") not in lines]
    if missing:
        return _fail("Claude Code runtime dirs not all excluded in .gitignore", {"missing": missing})
    return _ok("Claude Code runtime dirs are gitignored")


def c_s007(snap):  # content/ ignored
    return _ok("content/ is gitignored") if _has_ignore(snap, "content/", "content") \
        else _fail("content/ is not excluded — generated assets would bloat the repo")


def c_s008(snap):  # wildcard secret file patterns
    needed = ["*.pem", "*.key", "credentials.json"]
    lines = set(_gitignore_lines(snap))
    missing = [p for p in needed if p not in lines]
    if missing:
        return _fail("wildcard secret-file patterns missing from .gitignore", {"missing": missing})
    return _ok("wildcard secret-file patterns present")


def c_s009(snap):  # .mcp.json.template has no literal secrets
    content = _content(snap, ".mcp.json.template")
    if not content:
        return _ok(".mcp.json.template absent or empty")
    hits = _scan_secret_values(content)
    if hits:
        return _fail("possible literal secret(s) in .mcp.json.template — use ${VAR} placeholders",
                     {"matches": hits[:25]})
    return _ok(".mcp.json.template uses placeholders")


def c_s010(snap):  # credential var names service-specific
    generic = {"API_KEY", "SECRET", "TOKEN", "PASSWORD", "KEY", "KEY1", "KEY2", "APIKEY"}
    names = set(_mcp_vars(snap)) | set(_env_example_vars(snap))
    flagged = sorted(n for n in names if n in generic)
    if flagged:
        return _fail("generic credential variable names (add a service prefix)", {"names": flagged})
    return _ok("credential variable names are service-specific")


# ===========================================================================
# T — template.yaml
# ===========================================================================

def c_t001(snap):  # valid YAML
    if not _exists(snap, "template.yaml"):
        return _skip("template.yaml missing (see F-001)", "no_template")
    content = _content(snap, "template.yaml")
    if content is None:
        return _skip("template.yaml unreadable", "unreadable")
    _data, err = _parse_yaml(content)
    return _fail(f"template.yaml is not valid YAML: {err}") if err else _ok("template.yaml parses")


def _with_template(snap, fn):
    data, err = _template(snap)
    if err == "missing":
        return _skip("template.yaml missing (see F-001)", "no_template")
    if err:
        return _skip("template.yaml invalid (see T-001)", "invalid_template")
    return fn(data or {})


def c_t002(snap):
    def f(d):
        name = d.get("name")
        if not name or not isinstance(name, str):
            return _fail("template.yaml is missing a 'name' field")
        if not re.match(r"^[a-z0-9][a-z0-9\-]*$", name) or len(name) > 64:
            return _fail("template.yaml 'name' must be lowercase alphanumeric + hyphens, ≤64 chars",
                         {"name": name[:80]})
        return _ok("name is valid")
    return _with_template(snap, f)


def c_t003(snap):
    return _with_template(snap, lambda d: _ok("description present")
                          if (d.get("description") or "").strip()
                          else _fail("template.yaml 'description' is missing or empty"))


def c_t004(snap):
    def f(d):
        cpu = ((d.get("resources") or {}).get("cpu"))
        if cpu is None:
            return _fail("template.yaml resources.cpu is missing")
        if str(cpu) not in {"1", "2", "4", "8", "16"}:
            return _fail("resources.cpu must be one of 1/2/4/8/16", {"cpu": str(cpu)})
        return _ok("resources.cpu is valid")
    return _with_template(snap, f)


def c_t005(snap):
    def f(d):
        mem = ((d.get("resources") or {}).get("memory"))
        if mem is None:
            return _fail("template.yaml resources.memory is missing")
        if not re.match(r"^\d+[gm]$", str(mem)):
            return _fail("resources.memory must match <number>g|m (e.g. 2g, 512m)", {"memory": str(mem)})
        return _ok("resources.memory is valid")
    return _with_template(snap, f)


def c_t006(snap):
    return _with_template(snap, lambda d: _ok("display_name present")
                          if d.get("display_name") else _fail("template.yaml 'display_name' is missing"))


def c_t007(snap):
    def f(d):
        v = d.get("version")
        if not v:
            return _fail("template.yaml 'version' is missing")
        if not re.match(r"^\d+\.\d+(\.\d+)?$", str(v)):
            return _fail("version must be semantic (e.g. 1.0 or 1.0.0)", {"version": str(v)})
        return _ok("version is valid")
    return _with_template(snap, f)


def c_t008(snap):
    return _with_template(snap, lambda d: _ok("author present")
                          if d.get("author") else _fail("template.yaml 'author' is missing"))


def c_t010(snap):
    def f(d):
        uc = d.get("use_cases")
        if not isinstance(uc, list) or not uc:
            return _fail("template.yaml 'use_cases' is missing or empty")
        if not (3 <= len(uc) <= 7):
            return _fail(f"use_cases should have 3–7 entries (has {len(uc)})", {"count": len(uc)})
        return _ok("use_cases count is in range")
    return _with_template(snap, f)


def c_t011(snap):
    return _with_template(snap, lambda d: _ok("capabilities present")
                          if isinstance(d.get("capabilities"), list) and d.get("capabilities")
                          else _fail("template.yaml 'capabilities' array is missing"))


def c_t012(snap):
    def f(d):
        declared = d.get("mcp_servers")
        actual = set(_mcp_server_names(snap))
        if not actual:
            return _ok("no MCP servers in .mcp.json.template")
        declared_names = set()
        if isinstance(declared, list):
            for s in declared:
                if isinstance(s, dict) and s.get("name"):
                    declared_names.add(s["name"])
                elif isinstance(s, str):
                    declared_names.add(s)
        missing = sorted(actual - declared_names)
        if missing:
            return _fail("MCP servers in .mcp.json.template not described in template.yaml",
                         {"missing": missing})
        return _ok("mcp_servers match .mcp.json.template")
    return _with_template(snap, f)


def c_t015(snap):
    def f(d):
        mcp_vars = set(_mcp_vars(snap))
        if not mcp_vars:
            return _ok("no MCP credential variables")
        creds = d.get("credentials") or {}
        listed = set()
        if isinstance(creds, dict):
            listed = set(creds.keys())
        elif isinstance(creds, list):
            for c in creds:
                if isinstance(c, dict) and c.get("name"):
                    listed.add(c["name"])
                elif isinstance(c, str):
                    listed.add(c)
        missing = sorted(v for v in mcp_vars if v not in listed and not _is_platform_injected(v))
        if missing:
            return _fail("MCP ${VAR}s not listed in template.yaml credentials", {"missing": missing})
        return _ok("credentials schema lists all MCP variables")
    return _with_template(snap, f)


def c_t016(snap):
    def f(d):
        schedules = d.get("schedules") or []
        cmds = set(_command_names(snap))
        missing = []
        for s in schedules if isinstance(schedules, list) else []:
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            name = _slash_command(msg)
            if name and name not in cmds:
                missing.append(name)
        if missing:
            return _fail("schedule messages reference missing commands", {"missing": sorted(set(missing))})
        return _ok("schedule messages reference existing commands")
    return _with_template(snap, f)


def c_t017(snap):
    def f(d):
        git = d.get("git") or {}
        paths = git.get("commit_paths") or []
        bad = [p for p in paths if p in (".env", ".mcp.json", ".mcp.json.template")]
        if bad:
            return _fail("git.commit_paths includes Trinity-injected files", {"paths": bad})
        return _ok("commit_paths do not overwrite injected files")
    return _with_template(snap, f)


# ===========================================================================
# C — CLAUDE.md (static parts)
# ===========================================================================

def c_c001(snap):
    f = _file(snap, "CLAUDE.md")
    if not f.get("exists"):
        return _fail("CLAUDE.md is missing")
    content = f.get("content")
    if f.get("binary"):
        return _fail("CLAUDE.md is not valid UTF-8 text")
    if content is None or not content.strip():
        return _fail("CLAUDE.md is empty")
    return _ok("CLAUDE.md is valid and non-empty")


def c_c007(snap):
    content = _content(snap, "CLAUDE.md")
    if content is None:
        return _skip("CLAUDE.md unreadable", "unreadable")
    n = content.count("\n") + 1
    if n > 2000:
        return _fail(f"CLAUDE.md is {n} lines (>2000) — trailing instructions may be ignored", {"lines": n})
    return _ok(f"CLAUDE.md is {n} lines")


# ===========================================================================
# K — Credentials
# ===========================================================================

def c_k001(snap):
    mcp_vars = _mcp_vars(snap)
    if not mcp_vars:
        return _ok("no MCP credential variables")
    if not _exists(snap, ".env.example"):
        return _fail(".env.example missing but .mcp.json.template references ${VAR}s", {"vars": mcp_vars})
    provided = set(_env_example_vars(snap))
    missing = sorted(v for v in mcp_vars if v not in provided and not _is_platform_injected(v))
    if missing:
        return _fail("${VAR}s in .mcp.json.template missing from .env.example", {"missing": missing})
    return _ok("all MCP variables documented in .env.example")


def c_k002(snap):
    # Same documentation gap but against template.yaml credentials — reuse T-015's logic.
    return c_t015(snap) if _mcp_vars(snap) else _ok("no MCP credential variables")


def c_k003(snap):
    content = _content(snap, ".env.example")
    if not content:
        return _skip(".env.example missing (see F-004)", "no_env_example")
    lines = content.splitlines()
    commented = any(l.strip().startswith("#") for l in lines)
    has_vars = bool(_env_example_vars(snap))
    if has_vars and not commented:
        return _fail(".env.example has no explanatory comments")
    return _ok(".env.example documents its variables")


def c_k004(snap):
    content = _content(snap, ".env.example")
    if not content:
        return _skip(".env.example missing (see F-004)", "no_env_example")
    hits = []
    for i, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        if val.strip() and not _looks_placeholder(val):
            for rx, label in _SECRET_PATTERNS:
                if rx.search(val):
                    hits.append({"line": i, "var": name.strip(), "pattern": label})
                    break
    if hits:
        return _fail(".env.example appears to contain real values, not placeholders", {"matches": hits[:25]})
    return _ok(".env.example uses placeholder values")


# ===========================================================================
# G — Git Config
# ===========================================================================

def c_g001(snap):
    lines = _gitignore_lines(snap)
    blanket = [l for l in lines if l in (".claude/", ".claude")]
    if blanket:
        return _fail(".claude/ is excluded wholesale — commits/skills/agents won't reach Trinity",
                     {"lines": blanket})
    return _ok(".claude/ is not wholesale excluded")


def c_g002(snap):
    if not _exists(snap, ".gitignore"):
        return _skip(".gitignore missing (see F-003)", "no_gitignore")
    lines = set(_gitignore_lines(snap))
    missing = [p for p in _GITIGNORE_PATTERNS if p not in lines]
    if missing:
        return _fail(f".gitignore is missing {len(missing)} canonical pattern(s)", {"missing": missing[:40]})
    return _ok(".gitignore follows the canonical pattern list")


def c_g003(snap):
    def f(d):
        git = d.get("git") or {}
        paths = git.get("commit_paths") or []
        bad = [p for p in paths if p in (".env", ".mcp.json") or str(p).startswith("content")]
        if bad:
            return _fail("git.commit_paths includes secrets or content/", {"paths": bad})
        return _ok("commit_paths exclude secrets and content/")
    return _with_template(snap, f)


def c_g004(snap):
    def f(d):
        git = d.get("git") or {}
        if "ignore_paths" not in git:
            return _ok("no git.ignore_paths declared")
        ignore = git.get("ignore_paths") or []
        missing = [p for p in (".env", ".mcp.json") if p not in ignore]
        if missing:
            return _fail("git.ignore_paths should include .env and .mcp.json", {"missing": missing})
        return _ok("git.ignore_paths include credential files")
    return _with_template(snap, f)


def c_g005(snap):
    def f(d):
        git = d.get("git") or {}
        if "push_enabled" in git:
            return _ok("git.push_enabled is explicit")
        if not git:
            return _ok("no git config declared")
        return _fail("git.push_enabled is left to the platform default — declare it explicitly")
    return _with_template(snap, f)


# ===========================================================================
# P — Skills & Playbooks (static parts)
# ===========================================================================

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _is_skill_md(rel: str) -> bool:
    return rel.endswith("SKILL.md") or rel.endswith(".md")


def c_p001(snap):
    bad = []
    for rel, info in _skill_files(snap).items():
        if not rel.endswith("SKILL.md"):
            continue
        content = info.get("content")
        if content is None:
            continue
        m = _FRONTMATTER_RE.match(content)
        if not m:
            bad.append(rel)
            continue
        _data, err = _parse_yaml(m.group(1))
        if err:
            bad.append(rel)
    if bad:
        return _fail("skill files with missing/invalid YAML frontmatter", {"files": bad[:25]})
    return _ok("skill frontmatter is valid")


def c_p002(snap):
    bad = []
    for rel, info in _skill_files(snap).items():
        if not rel.endswith("SKILL.md"):
            continue
        content = info.get("content")
        if content is None:
            continue
        m = _FRONTMATTER_RE.match(content)
        if not m:
            bad.append(rel)
            continue
        data, err = _parse_yaml(m.group(1))
        if err or not isinstance(data, dict) or not data.get("name") or not data.get("description"):
            bad.append(rel)
    if bad:
        return _fail("skill frontmatter missing name/description", {"files": bad[:25]})
    return _ok("skill frontmatter has name and description")


def c_p004(snap):
    big = []
    for rel, info in _skill_files(snap).items():
        content = info.get("content")
        if content is None:
            if info.get("truncated"):
                big.append(rel)  # truncated → certainly large
            continue
        if content.count("\n") + 1 > 500:
            big.append(rel)
    if big:
        return _fail("skill files exceed 500 lines", {"files": big[:25]})
    return _ok("all skill files are under 500 lines")


_APPROVAL_PATTERNS = [
    r"\[approval gate\]", r"wait for (?:the )?(?:user|human|approval|confirmation)",
    r"ask (?:the )?user", r"confirm with (?:the )?(?:user|human)",
    r"present options to", r"get user input", r"human decision",
    r"await (?:user|human) (?:input|approval)",
]
_APPROVAL_RE = re.compile("(?i)(" + "|".join(_APPROVAL_PATTERNS) + ")")


def c_p006(snap):
    """STATIC (deviation from doc AI): scan autonomous/scheduled skills for
    approval gates that would hang a scheduled run. Targets the command files
    referenced by template.yaml schedules — the actual autonomous path."""
    data, _err = _template(snap)
    scheduled_cmds = set()
    if isinstance(data, dict):
        for s in (data.get("schedules") or []):
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            name = _slash_command(msg)
            if name:
                scheduled_cmds.add(name)
    if not scheduled_cmds:
        return _ok("no scheduled/autonomous skills declared")
    hits = []
    skills = _skill_files(snap)
    for name in scheduled_cmds:
        for rel, info in skills.items():
            if rel.endswith(f"commands/{name}.md") or rel.endswith(f"skills/{name}/SKILL.md"):
                content = info.get("content") or ""
                for i, line in enumerate(content.splitlines(), start=1):
                    if _APPROVAL_RE.search(line):
                        hits.append({"file": rel, "line": i})
                        break
    if hits:
        return _fail("autonomous/scheduled skill contains an approval gate (would hang the run)",
                     {"matches": hits[:25]})
    return _ok("autonomous skills contain no approval gates")


# ===========================================================================
# A — Autonomy Design (static parts)
# ===========================================================================

def _slash_command(message: str) -> Optional[str]:
    m = re.match(r"^\s*/([A-Za-z0-9][A-Za-z0-9_\-]*)", message or "")
    return m.group(1) if m else None


def _command_names(snap) -> List[str]:
    out = []
    for rel in _skill_files(snap):
        if "/commands/" in rel or rel.startswith(".claude/commands/"):
            base = rel.rsplit("/", 1)[-1]
            if base.endswith(".md"):
                out.append(base[:-3])
    listing = _dir_list(snap, ".claude/commands") or []
    for fn in listing:
        if fn.endswith(".md"):
            out.append(fn[:-3])
    return sorted(set(out))


_CRON_FIELD = re.compile(r"^[\d*/,\-]+$")


def _valid_cron(expr: str) -> bool:
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    return all(_CRON_FIELD.match(p) for p in parts)


def c_a001(snap):
    def f(d):
        schedules = d.get("schedules") or []
        prose = []
        for s in schedules if isinstance(schedules, list) else []:
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            if msg and not msg.strip().startswith("/"):
                prose.append((s.get("name") if isinstance(s, dict) else None) or msg[:40])
        if prose:
            return _fail("scheduled messages use raw prose instead of slash commands", {"schedules": prose[:25]})
        return _ok("scheduled messages reference slash commands")
    return _with_template(snap, f)


def c_a002(snap):
    def f(d):
        schedules = d.get("schedules") or []
        bad = []
        for s in schedules if isinstance(schedules, list) else []:
            cron = (s.get("cron") or s.get("cron_expression") or s.get("schedule")) if isinstance(s, dict) else None
            if cron and not _valid_cron(str(cron)):
                bad.append(str(cron))
        if bad:
            return _fail("invalid cron expression(s)", {"crons": bad[:25]})
        return _ok("cron expressions are valid")
    return _with_template(snap, f)


def c_a004(snap):
    f = _file(snap, ".trinity/pre-check")
    if not f.get("exists"):
        return _ok("no .trinity/pre-check present")
    content = _content(snap, ".trinity/pre-check") or ""
    if not content.startswith("#!"):
        return _fail(".trinity/pre-check has no shebang on line 1 — docker exec can't run it")
    if not f.get("mode_exec"):
        return _fail(".trinity/pre-check is not executable")
    return _ok(".trinity/pre-check is executable with a shebang")


# ===========================================================================
# D — Dashboard & Metrics (static parts)
# ===========================================================================

_WIDGET_TYPES = {"metric", "status", "progress", "text", "markdown", "table",
                 "list", "link", "image", "divider", "spacer"}
_WIDGET_COLORS = {"green", "red", "yellow", "gray", "blue", "orange", "purple"}


def _dashboard(snap):
    content = _content(snap, "dashboard.yaml")
    if content is None:
        return None, None, "missing"
    data, err = _parse_yaml(content)
    if err:
        return None, None, err
    widgets = []
    if isinstance(data, dict):
        widgets = data.get("widgets") or []
        # widgets may be nested under sections
        if not widgets and isinstance(data.get("sections"), list):
            for sec in data["sections"]:
                if isinstance(sec, dict):
                    widgets += sec.get("widgets") or []
    return data, [w for w in widgets if isinstance(w, dict)], None


def c_d001(snap):
    if not _exists(snap, "dashboard.yaml"):
        return _skip("dashboard.yaml missing (see F-010)", "no_dashboard")
    _d, _w, err = _dashboard(snap)
    return _fail(f"dashboard.yaml is not valid YAML: {err}") if err and err != "missing" else _ok("dashboard.yaml parses")


def _with_dashboard(snap, fn):
    if not _exists(snap, "dashboard.yaml"):
        return _skip("dashboard.yaml missing (see F-010)", "no_dashboard")
    _d, widgets, err = _dashboard(snap)
    if err:
        return _skip("dashboard.yaml invalid (see D-001)", "invalid_dashboard")
    return fn(widgets or [])


def c_d002(snap):
    def f(widgets):
        bad = sorted({w.get("type") for w in widgets if w.get("type") not in _WIDGET_TYPES and w.get("type")})
        if bad:
            return _fail("unsupported dashboard widget type(s)", {"types": bad})
        return _ok("all widget types are supported")
    return _with_dashboard(snap, f)


def c_d003(snap):
    req = {
        "text": ["content"], "markdown": ["content"], "list": ["items"],
        "link": ["url"], "metric": ["label", "value"],
        "status": ["label", "value", "color"], "progress": ["label", "value"],
    }
    def f(widgets):
        bad = []
        for w in widgets:
            t = w.get("type")
            for field in req.get(t, []):
                if field not in w:
                    bad.append({"type": t, "missing": field})
        if bad:
            return _fail("dashboard widgets missing required fields (won't render)", {"widgets": bad[:25]})
        return _ok("widget required fields are present")
    return _with_dashboard(snap, f)


def c_d004(snap):
    def f(widgets):
        bad = []
        for w in widgets:
            if w.get("type") == "progress":
                v = w.get("value")
                if isinstance(v, (int, float)) and not (0 <= v <= 100):
                    bad.append(w.get("label") or v)
        if bad:
            return _fail("progress widget values outside 0–100", {"widgets": bad[:25]})
        return _ok("progress values are in range")
    return _with_dashboard(snap, f)


def c_d005(snap):
    def f(widgets):
        bad = []
        for w in widgets:
            if w.get("type") == "status":
                color = w.get("color")
                if color and color not in _WIDGET_COLORS:
                    bad.append(color)
        if bad:
            return _fail("status widget colors not in the allowed palette", {"colors": sorted(set(bad))})
        return _ok("status colors are valid")
    return _with_dashboard(snap, f)


def c_d006(snap):
    def f(d):
        metrics = d.get("metrics")
        if not metrics:
            return _ok("no metrics declared")
        names = []
        if isinstance(metrics, dict):
            names = list(metrics.keys())
        elif isinstance(metrics, list):
            names = [m.get("name") for m in metrics if isinstance(m, dict) and m.get("name")]
        bad = [n for n in names if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", str(n))]
        if bad:
            return _fail("metric names are not valid keys", {"names": bad})
        return _ok("metric names are valid")
    return _with_template(snap, f)


def c_d008(snap):
    def f(widgets):
        _d, _w, err = _dashboard(snap)
        data = _d or {}
        interval = data.get("refresh_interval") if isinstance(data, dict) else None
        if isinstance(interval, (int, float)) and interval < 5:
            return _fail(f"dashboard refresh_interval is {interval}s (<5s)", {"refresh_interval": interval})
        return _ok("refresh interval is acceptable")
    return _with_dashboard(snap, f)


# ===========================================================================
# X — Cross-File Consistency (static parts)
# ===========================================================================

def c_x003(snap):
    def f(d):
        declared = d.get("skills")
        if not declared:
            return _ok("no skills declared in template.yaml")
        names = []
        if isinstance(declared, list):
            for s in declared:
                names.append(s.get("name") if isinstance(s, dict) else s)
        existing = set()
        for rel in _skill_files(snap):
            if rel.endswith("SKILL.md"):
                # .claude/skills/<name>/SKILL.md
                parts = rel.split("/")
                if len(parts) >= 2:
                    existing.add(parts[-2])
        missing = [n for n in names if n and n not in existing]
        if missing:
            return _fail("template.yaml declares skills with no SKILL.md", {"missing": missing})
        return _ok("declared skills exist in .claude/skills/")
    return _with_template(snap, f)


def c_x004(snap):
    def f(d):
        declared = d.get("mcp_servers")
        declared_names = set()
        if isinstance(declared, list):
            for s in declared:
                if isinstance(s, dict) and s.get("name"):
                    declared_names.add(s["name"])
                elif isinstance(s, str):
                    declared_names.add(s)
        actual = set(_mcp_server_names(snap))
        if not declared_names and not actual:
            return _ok("no MCP servers declared")
        only_template = sorted(declared_names - actual)
        only_mcp = sorted(actual - declared_names)
        if only_template or only_mcp:
            return _fail("MCP servers differ between template.yaml and .mcp.json.template",
                         {"only_in_template_yaml": only_template, "only_in_mcp_json": only_mcp})
        return _ok("MCP servers are consistent across files")
    return _with_template(snap, f)


def c_x007(snap):
    def f(d):
        schedules = d.get("schedules") or []
        cmds = set(_command_names(snap))
        missing = []
        for s in schedules if isinstance(schedules, list) else []:
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            name = _slash_command(msg)
            if name and name not in cmds:
                missing.append(name)
        if missing:
            return _fail("scheduled messages reference commands that don't exist",
                         {"missing": sorted(set(missing))})
        return _ok("scheduled messages match existing commands")
    return _with_template(snap, f)


# ===========================================================================
# I — Composability (static parts)
# ===========================================================================

def c_i005(snap):
    data, _err = _template(snap)
    declares_contract = False
    if isinstance(data, dict) and ("output_format" in data or "output_schema" in data):
        declares_contract = True
    if _dir_list(snap, "schemas") is not None:
        declares_contract = True
    claude = (_content(snap, "CLAUDE.md") or "")
    if re.search(r"(?i)output (?:format|contract|schema)", claude):
        declares_contract = True
    if not declares_contract:
        return _ok("no output contract declared")
    return _ok(".trinity/post-check present") if _exists(snap, ".trinity/post-check") \
        else _fail("an output contract is declared but no .trinity/post-check validates it")


# ---------------------------------------------------------------------------
# Registry — keyed by check id (consistency-tested against spec.STATIC_IDS).
# ---------------------------------------------------------------------------
STATIC_CHECKS = {
    "F-001": c_f001, "F-002": c_f002, "F-003": c_f003, "F-004": c_f004,
    "F-005": c_f005, "F-006": c_f006, "F-007": c_f007, "F-008": c_f008,
    "F-009": c_f009, "F-010": c_f010, "F-011": c_f011, "F-012": c_f012,
    "F-013": c_f013,
    "S-001": c_s001, "S-002": c_s002, "S-003": c_s003, "S-004": c_s004,
    "S-005": c_s005, "S-006": c_s006, "S-007": c_s007, "S-008": c_s008,
    "S-009": c_s009, "S-010": c_s010,
    "T-001": c_t001, "T-002": c_t002, "T-003": c_t003, "T-004": c_t004,
    "T-005": c_t005, "T-006": c_t006, "T-007": c_t007, "T-008": c_t008,
    "T-010": c_t010, "T-011": c_t011, "T-012": c_t012, "T-015": c_t015,
    "T-016": c_t016, "T-017": c_t017,
    "C-001": c_c001, "C-007": c_c007,
    "K-001": c_k001, "K-002": c_k002, "K-003": c_k003, "K-004": c_k004,
    "G-001": c_g001, "G-002": c_g002, "G-003": c_g003, "G-004": c_g004,
    "G-005": c_g005,
    "P-001": c_p001, "P-002": c_p002, "P-004": c_p004, "P-006": c_p006,
    "A-001": c_a001, "A-002": c_a002, "A-004": c_a004,
    "D-001": c_d001, "D-002": c_d002, "D-003": c_d003, "D-004": c_d004,
    "D-005": c_d005, "D-006": c_d006, "D-008": c_d008,
    "X-003": c_x003, "X-004": c_x004, "X-007": c_x007,
    "I-005": c_i005,
}


def run_static(snapshot: Dict[str, Any], check_ids: List[str]) -> Dict[str, Result]:
    """Run the requested static checks against a snapshot. Returns {id: result}.

    A check that raises is captured as a "skipped" result with the error reason
    rather than failing the whole report.
    """
    out: Dict[str, Result] = {}
    for cid in check_ids:
        fn = STATIC_CHECKS.get(cid)
        if fn is None:
            out[cid] = _skip("no static implementation", "not_implemented")
            continue
        try:
            out[cid] = fn(snapshot)
        except Exception as e:  # noqa: BLE001 — one bad check never breaks the report
            out[cid] = _skip(f"check error: {e}", "check_error")
    return out
