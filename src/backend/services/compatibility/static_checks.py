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

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from utils.safe_yaml import (
    AliasPolicy,
    HardenedYamlError,
    load_hardened_yaml,
)

from services.credential_charset import (
    CREDENTIAL_DETECTOR_NAME_RE,
    CREDENTIAL_DETECTOR_REF_RE,
)
from services.template_service import _is_platform_injected, declared_credential_names

logger = logging.getLogger(__name__)

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
        # ent#314: the snapshot is agent-authored workspace content. REJECT,
        # because every check here walks the parsed structure and no legitimate
        # workspace file needs an alias.
        return load_hardened_yaml(
            content, kind="workspace_yaml", alias_policy=AliasPolicy.REJECT
        ), None
    except (yaml.YAMLError, HardenedYamlError) as e:
        # HardenedYamlError is a ValueError: without it a refused document would
        # escape this helper instead of becoming the (None, reason) it promises.
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


# The shared detector charset (services/credential_charset.py). This finder was
# already the wide one; naming the constant is what stops a future "align the
# regexes" pass from narrowing it to match `_env_example_vars` — the wrong
# direction, because the substitution engines accept the wide form. Read that
# module's NON-MEMBERS list before touching any sibling pattern.
_VAR_RE = CREDENTIAL_DETECTOR_REF_RE


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
        # The shared detector charset, NOT uppercase-only. This reader is the
        # `provided` side of K-001 (HARD) and the `has_vars` precondition of
        # K-003 (SOFT): an uppercase-only view made a correctly documented
        # `my_var=` invisible, so K-001 HARD-failed a template that documents
        # every variable it references. `.strip()` above is load-bearing — the
        # validator anchors with `\Z`. (ent#128)
        name = line.split("=", 1)[0].strip()
        if CREDENTIAL_DETECTOR_NAME_RE.match(name):
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


def c_f004(snap):  # .env.example exists when credentials are declared
    """SOFT, conditional (#2137).

    Unconditionally demanding `.env.example` SOFT-failed every credential-free
    agent for a file that would document nothing. K-001 already covers the case
    that matters at HARD (a `${VAR}` in `.mcp.json.template` with no
    `.env.example` entry); this now fires only when the agent declares
    credentials somewhere and still ships no example file.
    """
    if _exists(snap, ".env.example"):
        return _ok(".env.example present")
    data, _err = _template(snap)
    declares_creds = bool(
        _mcp_vars(snap)
        or (isinstance(data, dict) and data.get("credentials"))
        or (isinstance(data, dict) and data.get("mcp_servers"))
    )
    if not declares_creds:
        return _ok("no credentials declared — .env.example not required")
    return _fail(".env.example is missing — users can't tell what credentials to provide")


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


def c_f009(snap):  # at least one skill or command file
    return _ok("skills/commands present") if _skill_files(snap) \
        else _fail("no skill or command files found")


def c_f010(snap):  # dashboard.yaml
    return _ok("dashboard.yaml present") if _exists(snap, "dashboard.yaml") \
        else _fail("dashboard.yaml is missing — Dashboard tab will be empty")


def c_f011(snap):  # ARCHITECTURE.md
    return _ok("architecture doc present") if (_exists(snap, "ARCHITECTURE.md") or _exists(snap, "docs/architecture.md")) \
        else _fail("ARCHITECTURE.md (or docs/architecture.md) is missing")


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


def c_s005(snap):  # .trinity/ runtime state ignored
    # ".trinity/*" is the CANONICAL shape since #2070: git cannot re-include a
    # path under a dir-form exclusion (it never descends into the directory),
    # so the star form is the only one under which the authored hooks
    # — pre-check, post-check, setup.sh, brain-orb/, pipelines/ — can stay
    # tracked while runtime state stays ignored.
    #
    # The dir-forms are still ACCEPTED rather than failed: they do exclude the
    # runtime state this check is about, which is what it grades. A template on
    # the dir-form that also commits a hook is the #2070 case, and the fleet
    # merge repairs it on the next Push by replacing that exact line — grading
    # it a failure here would report a problem the platform fixes itself.
    return _ok(".trinity/ runtime state is gitignored") \
        if _has_ignore(snap, ".trinity/*", ".trinity/", ".trinity") \
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


_CREDENTIAL_SECTION_KEYS = frozenset({"mcp_servers", "env_file", "config_files"})


def c_t015(snap):
    def f(d):
        mcp_vars = set(_mcp_vars(snap))
        if not mcp_vars:
            return _ok("no MCP credential variables")
        creds = d.get("credentials") or {}
        listed = set()
        if isinstance(creds, dict):
            listed = set(creds.keys()) - _CREDENTIAL_SECTION_KEYS
        elif isinstance(creds, list):
            for c in creds:
                if isinstance(c, dict) and c.get("name"):
                    listed.add(c["name"])
                elif isinstance(c, str):
                    listed.add(c)
        # The STRUCTURED form is the documented one, and it declares variables one
        # level deeper than `creds.keys()` ever looked — so
        # `credentials.mcp_servers.stripe.env_vars: [STRIPE_API_KEY]` satisfied
        # nothing and this HARD gate failed a correctly-declared template.
        #
        # Fails CLOSED, and only this term is wrapped. A raise degrades to the
        # narrower set above — which makes `missing` LARGER, i.e. errs toward
        # failing — and never to `skipped`. That direction is the whole point: a
        # HARD gate that cannot evaluate must not become indistinguishable from a
        # HARD gate that passed, and `c_k002` delegates here, so one raise would
        # otherwise take BOTH gates dark together on four lines of untrusted YAML.
        # The `isinstance` filter is redundant against
        # `declared_credential_names`' own contract, deliberately: the gate must
        # not depend on that contract holding.
        try:
            listed |= {
                name for name in declared_credential_names(creds) if isinstance(name, str)
            }
        except Exception as e:  # noqa: BLE001 — fail CLOSED, keep the narrow verdict
            logger.warning(
                "T-015: credential declaration unreadable (%s); comparing against "
                "section names only",
                e,
            )
        missing = sorted(v for v in mcp_vars if v not in listed and not _is_platform_injected(v))
        if missing:
            return _fail("MCP ${VAR}s not listed in template.yaml credentials", {"missing": missing})
        return _ok("credentials schema lists all MCP variables")
    return _with_template(snap, f)


def c_t018(snap):
    """STATIC: the `schedules:` block is STRUCTURALLY well-formed (ent#89).

    Structure only — presence and type of `name`/`cron`/`message`, entry shape,
    block shape, and the declared-schedule cap. It deliberately does NOT report
    cron syntax: A-002 already ships as "cron expressions are valid", and two
    contradicting cron authorities in the same report on the same field is
    worse than either alone. (The *reader* validates cron strictly, but that is
    a materialization gate — drop the entry — not a report verdict.)
    """
    def f(d):
        try:
            from services.template_schedules import schedule_shape_errors
            errors = schedule_shape_errors(d.get("schedules"))
        except Exception as e:  # noqa: BLE001
            # Fail CLOSED, deliberately, and against the grain of every other
            # check here. `run_static` converts a raise into `skipped`, and
            # `_counts` (compatibility/__init__.py) counts only `status ==
            # "fail"` — so a raising SOFT check drops soft_count 1->0 and, since
            # `overall` is a bare `> 0` test, flips the whole report from
            # `issues` to `compatible` exactly when this check's finding was the
            # only failure. That is the entire population T-018 exists to serve.
            # Worse, `build_report` persists `checks_json` and
            # `_report_from_persisted` recomputes counts from it, so one
            # transient raise is replayed as a clean bill of health on every
            # stopped-agent read. A check whose whole purpose is
            # malformed-input tolerance must not rely on that fail-open net.
            #
            # Type name ONLY: `detail` is persisted to
            # agent_compatibility_results.checks_json and rendered in the UI,
            # and `str(e)` can embed untrusted template content.
            return _fail("schedules block could not be evaluated",
                         {"error_type": type(e).__name__})
        if errors:
            return _fail("template.yaml `schedules:` entries are malformed",
                         {"errors": errors[:25]})
        return _ok("schedules block entries are well-formed")
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
    """SOFT: each SKILL.md is under 500 lines.

    #2137: this walked EVERY `.md` under `.claude/skills/` (that is what
    `_skill_files` collects), so it flagged the very `reference.md` /
    `examples.md` companions that P-009 tells authors to create — the catalog
    contradicting itself. Scoped to `SKILL.md`, matching P-002's existing
    scope, the two form a ladder: P-009 suggests splitting a SKILL.md past
    ~200 lines, P-004 fails one past 500.
    """
    big = []
    for rel, info in _skill_files(snap).items():
        if not rel.endswith("SKILL.md") and "/commands/" not in rel:
            continue  # companion reference/examples file — P-009's output, not a defect
        content = info.get("content")
        if content is None:
            if info.get("truncated"):
                big.append(rel)  # truncated → certainly large
            continue
        if content.count("\n") + 1 > 500:
            big.append(rel)
    if big:
        return _fail("SKILL.md files exceed 500 lines", {"files": big[:25]})
    return _ok("all SKILL.md files are under 500 lines")


_APPROVAL_PATTERNS = [
    r"\[approval gate\]", r"wait for (?:the )?(?:user|human|approval|confirmation)",
    r"ask (?:the )?user", r"confirm with (?:the )?(?:user|human)",
    r"present options to", r"get user input", r"human decision",
    r"await (?:user|human) (?:input|approval)",
]
_APPROVAL_RE = re.compile("(?i)(" + "|".join(_APPROVAL_PATTERNS) + ")")


def _automation_mode(content: str) -> Optional[str]:
    """The skill's declared `automation:` mode, or None.

    The marketplace convention (`autonomous | gated | manual`) is the author's
    own statement about whether a human is expected in the loop. Parsed off the
    YAML frontmatter via the same hardened loader every other frontmatter read
    uses — never a bare regex over the body, which would let a mention of the
    word in prose flip a HARD check off.
    """
    m = _FRONTMATTER_RE.match(content or "")
    if not m:
        return None
    data, err = _parse_yaml(m.group(1))
    if err or not isinstance(data, dict):
        return None
    mode = data.get("automation")
    return str(mode).strip().lower() if isinstance(mode, str) else None


def c_p006(snap):
    """STATIC (deviation from doc AI): scan autonomous/scheduled skills for
    approval gates that would hang a scheduled run. Targets the command files
    referenced by template.yaml schedules — the actual autonomous path."""
    data, _err = _template(snap)
    scheduled_cmds = set()
    if isinstance(data, dict):
        # ent#89: `isinstance(..., list)` was MISSING here, unlike all four
        # sibling readers of this field (c_t016, c_a001, c_a002, c_x007). A
        # template with `schedules: 5` raised `TypeError: 'int' object is not
        # iterable`, `run_static` swallowed it into `skipped`, and `_counts`
        # ignores `skipped` — so this HARD check silently vanished from
        # hard_count. A live instance of the exact fail-open class T-018 above
        # guards against.
        schedules = data.get("schedules")
        for s in (schedules if isinstance(schedules, list) else []):
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            name = _slash_command(msg)
            if name:
                scheduled_cmds.add(name)
    if not scheduled_cmds:
        return _ok("no scheduled/autonomous skills declared")
    hits = []
    gated = []
    skills = _skill_files(snap)
    for name in scheduled_cmds:
        for rel, info in skills.items():
            if rel.endswith(f"commands/{name}.md") or rel.endswith(f"skills/{name}/SKILL.md"):
                content = info.get("content") or ""
                # #2137: a skill that gates BY DESIGN is not a defect. The
                # marketplace already carries an `automation:` frontmatter
                # convention (`autonomous | gated | manual`); an explicit
                # `gated`/`manual` is the author telling us the human pause is
                # intentional, so reporting it HARD is reporting their design
                # back at them. Only the DEFAULT (absent, or an explicit
                # `autonomous`) is held to "must not hang".
                if _automation_mode(content) in ("gated", "manual"):
                    gated.append({"file": rel, "mode": _automation_mode(content)})
                    continue
                for i, line in enumerate(content.splitlines(), start=1):
                    if _APPROVAL_RE.search(line):
                        hits.append({"file": rel, "line": i})
                        break
    if hits:
        return _fail("autonomous/scheduled skill contains an approval gate (would hang the run)",
                     {"matches": hits[:25], "opt_out": "declare `automation: gated` in the skill "
                                                       "frontmatter if the pause is intentional"})
    if gated:
        return _ok("scheduled skills that gate do so by declaration", {"declared_gated": gated[:25]})
    return _ok("autonomous skills contain no approval gates")


# ===========================================================================
# A — Autonomy Design (static parts)
# ===========================================================================

# A slash command may appear ANYWHERE in a schedule message, not only at
# position 0 (#2137). The marketplace's own generated schedules are the proof:
# `add-pipeline` writes `message: "Run /pipeline-tick"`, `add-orchestrator`
# writes `"Run /project-steward"`, `trinity:onboard` writes `"Run /weekly-report
# and post the summary"`. The old `re.match(r"^\s*/...")` anchored at position 0
# and saw none of them — so P-006 (HARD: approval gates would hang an autonomous
# run) resolved zero command names and returned "no scheduled/autonomous skills
# declared" on exactly the agents it exists to guard, and X-007/T-016 passed
# vacuously.
#
# Two guards keep a filesystem path from being read as a command — X-007 turns a
# resolved name into a SOFT "references a command that doesn't exist", so a false
# positive here manufactures exactly the unactionable finding #2137 removes:
#
#   1. `(?<![^\s(\[])` requires the `/` to START a token (preceded by
#      start-of-string, whitespace, `(` or `[`), so `reports/daily` and the
#      `//` and `/status` in `https://x/status` are all skipped.
#   2. The matched token must not be followed by `/` — a command is a single
#      segment, a path is not. Checked in CODE rather than with a `(?!/)`
#      lookahead, because the regex engine would simply backtrack the greedy
#      name class and match `/et` out of `/etc/passwd`.
#
# So `"Check /var/log/app.log for errors"` yields no command at all, while
# `"Run /weekly-report and post the summary"` yields `weekly-report`. The first
# qualifying match wins: a message naming two commands is ambiguous, and the
# single-valued callers (P-006's `scheduled_cmds`, X-007's `missing`) expect one.
_SLASH_COMMAND_RE = re.compile(r"(?<![^\s(\[])/([A-Za-z0-9][A-Za-z0-9_\-]*)")


def _slash_command(message: str) -> Optional[str]:
    text = message or ""
    for m in _SLASH_COMMAND_RE.finditer(text):
        if m.end() < len(text) and text[m.end()] == "/":
            continue  # `/etc/passwd` — a path segment, not a command
        return m.group(1)
    return None


def _command_names(snap) -> List[str]:
    """Every name an agent's schedule message could invoke as `/name`.

    #2137: this globbed ONLY `.claude/commands/`, so it was blind to the single
    layout the `create-agent` wizards produce — `.claude/skills/<name>/SKILL.md`.
    X-007 ("scheduled messages match existing skills/commands") therefore
    reported a false failure for every skill-based agent that named a real
    skill. Both layouts now resolve, so X-007 passes on a skill-based agent and
    still fails on a genuinely missing target.
    """
    out = []
    for rel in _skill_files(snap):
        base = rel.rsplit("/", 1)[-1]
        if "/commands/" in rel or rel.startswith(".claude/commands/"):
            if base.endswith(".md"):
                out.append(base[:-3])
        elif base == "SKILL.md":
            # `.claude/skills/<name>/SKILL.md` -> `<name>`; a nested
            # `<name>/<sub>/SKILL.md` is invoked as `<sub>`, so take the parent
            # directory in both cases.
            parts = rel.split("/")
            if len(parts) >= 2:
                out.append(parts[-2])
    for base_dir in (".claude/commands", ".claude/skills"):
        for fn in _dir_list(snap, base_dir) or []:
            if base_dir.endswith("commands"):
                if fn.endswith(".md"):
                    out.append(fn[:-3])
            elif not fn.endswith(".md"):
                # A skills/ listing entry is a directory name == the skill name.
                out.append(fn)
    return sorted(set(out))


def _valid_cron(expr: str) -> bool:
    r"""True if the dedicated scheduler would accept this cron expression.

    ent#89: this was a per-field `^[\d*/,\-]+$` regex, which was wrong in BOTH
    directions — it rejected `0 9 * * MON` (valid; the scheduler translates
    named days) and accepted `99 99 * * *` (invalid; no range check). So A-002
    reported "cron expressions are valid" for an expression creation silently
    drops, and flagged one that works. Delegate to the same validator the
    scheduler registers with (#1472): *validate a config with the SAME parser
    the executor uses.*

    Imported inside the function so `apscheduler`/`pytz` stay off this module's
    load path — `static_checks` is imported by the templates router on every
    catalog request.
    """
    from services.schedule_validation import (
        ScheduleValidationError,
        validate_cron_expression,
    )
    try:
        validate_cron_expression(expr)
    except ScheduleValidationError:
        return False
    except Exception:  # noqa: BLE001 — an unexpected shape is still "not valid"
        return False
    return True


def c_a001(snap):
    def f(d):
        schedules = d.get("schedules") or []
        prose = []
        for s in schedules if isinstance(schedules, list) else []:
            msg = (s.get("message") if isinstance(s, dict) else "") or ""
            if msg and not _slash_command(msg):
                prose.append((s.get("name") if isinstance(s, dict) else None) or msg[:40])
        if prose:
            return _fail("scheduled messages name no slash command — a named skill "
                         "(e.g. \"Run /weekly-report\") dispatches more deterministically "
                         "than prose", {"schedules": prose[:25]})
        return _ok("scheduled messages reference a slash command")
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
# (I-005 retired in #2137 — `.trinity/post-check` has no executor anywhere in
# the platform; its only other mention was a git_service comment pointing back
# at this check.)


# ===========================================================================
# DP — Runtime Data Paths (#1169, implemented #2137)
# ===========================================================================

# Paths that are materialized and managed by their OWN mechanism. A data_paths
# entry overlapping one of these creates ambiguous ownership and double-handling.
_DP_MANAGED_PREFIXES = (".trinity/", ".claude/", ".env", ".mcp.json")


def _dp_norm(path: str) -> str:
    """Normalize a data_paths entry for prefix comparison.

    Strips a leading `./` — as a PREFIX. `str.lstrip("./")` strips a character
    SET, which turns `.trinity/state.json` into `trinity/state.json` and makes
    every `.`-prefixed managed path unmatchable.
    """
    p = str(path).strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _declared_data_paths(d: dict) -> Tuple[List[str], bool]:
    """(entries, declared). `declared` distinguishes "absent" from "empty list"."""
    raw = d.get("data_paths")
    if raw is None:
        return [], False
    if not isinstance(raw, list):
        return [], True  # declared but malformed — DP-001 reports the shape
    return [str(x).strip() for x in raw if str(x).strip()], True


def c_dp001(snap):
    """HARD: every data_paths entry resolves under `data/`.

    An entry that escapes the data root is never snapshotted by
    export/import (#1169), so the author believes their data is covered and it
    silently is not — the reason this is the one HARD check in the category.

    Shell-safety is checked with `git_service._is_safe_data_path`, the SAME
    predicate `materialize_data_paths` uses to decide what to drop — imported
    rather than reimplemented, for the reason A-002 delegates its cron parsing:
    a checker that reimplements its executor's rule eventually disagrees with it.
    Containment is checked HERE because the materializer deliberately does not
    (its regex admits `..` and `/`), so this is new coverage, not a mirror.
    """
    def f(d):
        entries, declared = _declared_data_paths(d)
        if not declared:
            return _ok("no data_paths declared")
        raw = d.get("data_paths")
        if not isinstance(raw, list):
            return _fail("template.yaml `data_paths:` must be a list",
                         {"found_type": type(raw).__name__})
        if not entries:
            return _ok("data_paths declared but empty")
        from services.git_service import _is_safe_data_path
        bad = []
        for e in entries:
            norm = _dp_norm(e)
            if e.startswith("/") or e.startswith("~"):
                bad.append({"path": e, "reason": "absolute"})
            elif ".." in norm.split("/"):
                bad.append({"path": e, "reason": "escapes_data_root"})
            elif not (norm == "data" or norm.startswith("data/")):
                # The check has to mean what its name says. `POST
                # /api/agents/{name}/data/export` archives `/home/developer/data`
                # and nothing else, so a plain relative entry like `outputs/*.csv`
                # is just as unsnapshotted as `../escape` — it simply fails
                # quietly instead of loudly. Absolute/`..` are only two of the
                # ways to be outside the root; being outside it is the defect.
                bad.append({"path": e, "reason": "outside_data_root"})
            elif not _is_safe_data_path(e):
                # Dropped verbatim by materialize_data_paths (#1169 L1).
                bad.append({"path": e, "reason": "shell_metacharacters"})
        if bad:
            return _fail("data_paths entries do not resolve under data/", {"entries": bad[:25]})
        return _ok(f"{len(entries)} data_paths entr{'y' if len(entries) == 1 else 'ies'} resolve under data/")
    return _with_template(snap, f)


def c_dp002(snap):
    """SOFT: the `data/` root is gitignored when data_paths is declared.

    SOFT, not the doc's HARD (#2137): `materialize_data_paths` appends `data/`
    to the agent's own `.gitignore` at creation, so a violation is a platform
    anomaly rather than an author defect — and the consequence (runtime data
    committed to git) is bloat/leak, not the runtime breakage HARD denotes.
    """
    def f(d):
        entries, declared = _declared_data_paths(d)
        if not declared or not entries:
            return _ok("no data_paths declared")
        if not _exists(snap, ".gitignore"):
            return _skip(".gitignore missing (see F-003)", "no_gitignore")
        lines = set(_gitignore_lines(snap))
        if lines & {"data/", "data", "/data/", "/data"}:
            return _ok("data/ root is gitignored")
        return _fail("data_paths is declared but the data/ root is not gitignored — "
                     "runtime data will be committed on sync")
    return _with_template(snap, f)


def c_dp003(snap):
    """SOFT: data_paths do not overlap separately-managed surfaces."""
    def f(d):
        entries, declared = _declared_data_paths(d)
        if not declared or not entries:
            return _ok("no data_paths declared")
        managed = set(d.get("persistent_state") or []) if isinstance(d.get("persistent_state"), list) else set()
        bad = []
        managed_norm = {_dp_norm(m) for m in managed}
        for e in entries:
            norm = _dp_norm(e)
            if norm.startswith(_DP_MANAGED_PREFIXES):
                bad.append({"path": e, "conflicts_with": "platform-managed path"})
            elif norm in managed_norm:
                bad.append({"path": e, "conflicts_with": "persistent_state"})
        if bad:
            return _fail("data_paths overlap separately-managed paths", {"entries": bad[:25]})
        return _ok("data_paths do not overlap managed paths")
    return _with_template(snap, f)


def c_dp004(snap):
    """INFO: declaring data_paths makes the agent instance-local.

    INFO, not the doc's SOFT (#2137): this reports a PROPERTY of the agent, not
    a defect — there is no edit that "fixes" it, and an unactionable SOFT is
    exactly the finding class this issue removes.
    """
    def f(d):
        entries, declared = _declared_data_paths(d)
        if not declared or not entries:
            return _ok("no data_paths declared — agent is replica-safe")
        return _fail("data_paths make this agent instance-local: its runtime data must "
                     "travel via export/import, not template clone",
                     {"data_paths": entries[:25]})
    return _with_template(snap, f)


PLATFORM_PLUGIN_REF = "trinity@abilityai"


def c_i006(snap):
    """INFO: is the Trinity plugin present, and if not, why (ent#411).

    The plugin is what lets a deployed agent make ITSELF compatible
    (`/trinity:onboard` in place), so its absence is the difference between an
    agent that can fix its own findings and one that needs a human with a local
    checkout. INFO, never a defect tier: an operator may legitimately switch the
    platform set off, and a bare repo is not at fault for what the platform
    failed to install.

    Reads `.trinity/plugins-state.json`, written by the boot reconciler. That
    file is on the agent-writable volume, so every field is treated as
    agent-supplied: only known keys are read, the presence claim is cross-checked
    against the recorded lists rather than a free-text status, and a withheld
    REASON is reported as the reconciler's own string, truncated.

    A missing file is not a failure — it means an image or a boot that predates
    this mechanism, which is a different statement from "the install failed".
    """
    raw = _content(snap, ".trinity/plugins-state.json")
    if raw is None:
        return _skip("plugin state not reported",
                     "no .trinity/plugins-state.json — base image or boot predates ent#411")
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return _fail("plugin state file is present but unreadable",
                     {"path": ".trinity/plugins-state.json"})
    if not isinstance(state, dict):
        return _fail("plugin state file is not an object",
                     {"path": ".trinity/plugins-state.json"})

    present = {str(x) for x in (state.get("installed") or []) if isinstance(x, str)}
    # `skipped` entries are recorded as "plugin:<ref>" / "marketplace:<name>".
    present |= {
        str(x).split(":", 1)[1]
        for x in (state.get("skipped") or [])
        if isinstance(x, str) and x.startswith("plugin:")
    }
    if PLATFORM_PLUGIN_REF in present:
        return _ok(f"{PLATFORM_PLUGIN_REF} is installed — this agent can onboard itself in place")

    withheld = state.get("withheld") if isinstance(state.get("withheld"), dict) else {}
    reason = withheld.get(f"plugin:{PLATFORM_PLUGIN_REF}") or withheld.get(
        "marketplace:abilityai"
    )
    if isinstance(reason, str) and reason.strip():
        return _fail(f"{PLATFORM_PLUGIN_REF} could not be installed",
                     {"withheld": reason.strip()[:200]})
    if state.get("platform_defaults_enabled") is False:
        return _skip("platform plugins are switched off for this agent",
                     "TRINITY_PLATFORM_PLUGINS is disabled — the plugin was never wanted")
    return _fail(f"{PLATFORM_PLUGIN_REF} is not installed",
                 {"reported_status": str(state.get("status"))[:80]})


STATIC_CHECKS = {
    "F-001": c_f001, "F-002": c_f002, "F-003": c_f003, "F-004": c_f004,
    "F-005": c_f005, "F-006": c_f006, "F-007": c_f007,
    "F-009": c_f009, "F-010": c_f010, "F-011": c_f011,
    "S-001": c_s001, "S-002": c_s002, "S-003": c_s003, "S-004": c_s004,
    "S-005": c_s005, "S-006": c_s006, "S-007": c_s007, "S-008": c_s008,
    "S-009": c_s009, "S-010": c_s010,
    "T-001": c_t001, "T-002": c_t002, "T-003": c_t003, "T-004": c_t004,
    "T-005": c_t005, "T-006": c_t006, "T-007": c_t007, "T-008": c_t008,
    "T-010": c_t010, "T-011": c_t011, "T-015": c_t015, "T-018": c_t018,
    "C-001": c_c001, "C-007": c_c007,
    "K-001": c_k001, "K-003": c_k003, "K-004": c_k004,
    "G-001": c_g001,
    "P-001": c_p001, "P-002": c_p002, "P-004": c_p004, "P-006": c_p006,
    "A-001": c_a001, "A-002": c_a002, "A-004": c_a004,
    "D-001": c_d001, "D-002": c_d002, "D-003": c_d003, "D-004": c_d004,
    "D-005": c_d005, "D-008": c_d008,
    "X-003": c_x003, "X-004": c_x004, "X-007": c_x007,
    "I-006": c_i006,
    "DP-001": c_dp001, "DP-002": c_dp002, "DP-003": c_dp003,
    "DP-004": c_dp004,
}


def run_static(snapshot: Dict[str, Any], check_ids: List[str]) -> Dict[str, Result]:
    """Run the requested static checks against a snapshot. Returns {id: result}.

    A check that raises is captured rather than propagated — one bad check must
    never break the whole report — but it is captured as a **FAIL**, not a skip.

    It used to be a skip, and `_counts` counts only `status == "fail"`, so a raise
    inside a HARD check DROPPED `hard_count` and could flip `overall_status` from
    `issues` to `compatible` on an agent with a real problem. Four lines of
    untrusted `template.yaml` were enough (`env_vars: [{K: v}]` → the gate's set
    comprehension raises `TypeError: unhashable type: 'dict'`), and because
    `c_k002` delegates to `c_t015` one raise took both HARD gates dark together —
    a result indistinguishable from a clean pass. `template.yaml` is read from the
    agent's own workspace, so that is a self-attestation bypass on the surface
    whose job is to police it.

    A check that could not evaluate is not a check that passed. Individual checks
    that CAN degrade meaningfully do so themselves and fail closed (see `c_t015`);
    reaching this handler means an unanticipated shape, which is exactly what
    should be visible.
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
            # This swallow previously left NO trace anywhere AND dropped out of
            # `_counts` (which counted only `status == "fail"`), so a broken
            # validator reported "healthy" with nothing in the logs saying
            # otherwise. Both halves are now closed: ent#128 flipped the result
            # from `_skip` to `_fail` so a crashed check is counted, and ent#89
            # added the log so all ~100 checks' failures are observable.
            #
            # ERROR with `exc_info`, not WARNING: the status alone says a check
            # crashed but not where, and this handler is only reached by an
            # unanticipated shape — the traceback is the whole diagnostic value.
            logger.error("Static compatibility check %s raised: %s", cid, e,
                         exc_info=True)
            out[cid] = _fail(
                f"check could not be evaluated: {e}", {"check_error": str(e)}
            )
    return out
