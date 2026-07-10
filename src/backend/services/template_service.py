"""
Template service for processing agent templates.

Metadata for GitHub templates is fetched from each repo's template.yaml
via the GitHub API and cached in memory (10-minute TTL).
"""
import base64
import json
import logging
import re
import subprocess
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from pathlib import Path
import httpx
import yaml
from config import DEFAULT_GITHUB_TEMPLATE_REPOS, GITHUB_PAT_CREDENTIAL_ID

logger = logging.getLogger(__name__)

# ============================================================================
# GitHub Metadata Fetching & Caching
# ============================================================================

_metadata_cache: Dict[str, tuple] = {}  # repo -> (timestamp, metadata_dict)
_CACHE_TTL = 600  # 10 minutes


def _fetch_template_yaml(repo: str, pat: str) -> dict:
    """Fetch and parse template.yaml from a GitHub repo via the API.

    Returns parsed YAML dict, or empty dict if not found / error.
    """
    try:
        headers = {"Accept": "application/vnd.github+json"}
        if pat:
            headers["Authorization"] = f"Bearer {pat}"

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{repo}/contents/template.yaml",
                headers=headers,
            )

        if resp.status_code != 200:
            logger.debug("template.yaml not found for %s (HTTP %s)", repo, resp.status_code)
            return {}

        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return yaml.safe_load(content) or {}
    except Exception as e:
        logger.warning("Failed to fetch template.yaml for %s: %s", repo, e)
        return {}


def _get_github_pat() -> str:
    """Get GitHub PAT (avoids circular import)."""
    from services.settings_service import get_github_pat
    return get_github_pat()


def _get_cached_metadata(repo: str) -> dict:
    """Return cached metadata for a repo, fetching if stale or missing."""
    cached = _metadata_cache.get(repo)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    pat = _get_github_pat()
    metadata = _fetch_template_yaml(repo, pat)
    _metadata_cache[repo] = (time.time(), metadata)
    return metadata


def _fetch_all_metadata(repos: List[str]) -> Dict[str, dict]:
    """Fetch template.yaml for multiple repos, using cache and concurrency."""
    results = {}
    to_fetch = []

    for repo in repos:
        cached = _metadata_cache.get(repo)
        if cached and time.time() - cached[0] < _CACHE_TTL:
            results[repo] = cached[1]
        else:
            to_fetch.append(repo)

    if to_fetch:
        pat = _get_github_pat()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_fetch_template_yaml, repo, pat): repo
                for repo in to_fetch
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    metadata = future.result()
                except Exception:
                    metadata = {}
                _metadata_cache[repo] = (time.time(), metadata)
                results[repo] = metadata

    return results


# ============================================================================
# Template Expansion
# ============================================================================

# Default sort weight for a template that declares no `priority:` (lower =
# earlier). The router sorts by `(priority, display_name)`.
_DEFAULT_TEMPLATE_PRIORITY = 100


def _coerce_priority(value) -> int:
    """Return a sortable int priority from a raw template.yaml value.

    The router sorts on `(priority, display_name)`, so a non-int priority
    would raise `TypeError: '<' not supported between 'NoneType' and 'int'`
    (a missing key defaults via `.get(..., 100)`, but a present-yet-null or
    string value slips through and 500s the whole endpoint). Coerce here so
    every surfaced template carries a real int. `bool` is excluded because
    `isinstance(True, int)` is True in Python — a `priority: true` typo must
    not sort as `1`.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _DEFAULT_TEMPLATE_PRIORITY


def _build_template(repo: str, metadata: dict, admin_override: dict = None) -> dict:
    """Build a full template dict from repo + fetched metadata + optional admin overrides.

    Priority for display_name / description:
      1. Admin-configured value (from Settings DB entry) — if non-empty
      2. template.yaml value (from GitHub) — if available
      3. Repo name fallback
    """
    override = admin_override or {}

    display_name = (
        override.get("display_name")
        or metadata.get("display_name")
        or metadata.get("name")
        or repo.split("/")[-1]
    )
    description = (
        override.get("description")
        or metadata.get("description", "")
    )

    # S4 (#383): import lazily to avoid any circular-import risk with
    # git_service, which imports database/docker modules at module load.
    from services.git_service import DEFAULT_PERSISTENT_STATE

    return {
        "id": f"github:{repo}",
        "display_name": display_name,
        "description": description,
        "github_repo": repo,
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        # Surface `priority` so the router's `(priority, display_name)` sort is
        # honored. Coerced to a real int — a GitHub repo's template.yaml is
        # untrusted and could set `priority: "high"`, which would 500 the sort.
        "priority": _coerce_priority(metadata.get("priority")),
        "resources": metadata.get("resources", {"cpu": "2", "memory": "4g"}),
        "skills": metadata.get("skills", []),
        "mcp_servers": metadata.get("mcp_servers", []),
        "required_credentials": metadata.get("required_credentials", []),
        # Surface `persistent_state` from template.yaml so crud.py can
        # materialize `.trinity/persistent-state.yaml` at creation. Falls
        # back to the global default list when the template omits the key.
        "persistent_state": metadata.get(
            "persistent_state", list(DEFAULT_PERSISTENT_STATE)
        ),
        # Surface `data_paths` (#1169) so crud.py can materialize
        # `.trinity/data-paths.yaml` + the per-agent .gitignore at creation.
        # Opt-in — defaults to an empty list when the template omits the key.
        "data_paths": metadata.get("data_paths", []),
        # trinity-enterprise#93: fork-to-own declaration. 'required' makes
        # creation demand a user-owned destination repo (crud enforces it —
        # the copy lands there and origin points at it). Also drives the
        # featured card treatment in CreateAgentModal, with `tagline` as the
        # card subtitle.
        "fork_to_own": metadata.get("fork_to_own"),
        "tagline": metadata.get("tagline", ""),
    }


# ============================================================================
# Public API
# ============================================================================

def _local_templates_dir() -> Path:
    """Return the canonical local-templates directory.

    Production path is the read-only bind mount at
    `/agent-configs/templates` (set up by docker-compose). When running
    outside the container, fall back to the in-repo path so the function
    still works in tests and dev shells. (#843)
    """
    inside_container = Path("/agent-configs/templates")
    if inside_container.exists():
        return inside_container
    return Path(__file__).resolve().parent.parent.parent.parent / "config" / "agent-templates"


def _build_local_template(template_dir: Path) -> Optional[dict]:
    """Build a template-list entry from a local-template directory.

    Returns None if the directory doesn't contain a readable
    `template.yaml`. Shape mirrors `_build_template` so the frontend's
    rendering code (CreateAgentModal.vue:117) works without a
    per-source branch.
    """
    template_yaml = template_dir / "template.yaml"
    if not template_yaml.exists():
        return None

    try:
        with open(template_yaml) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Failed to parse local template %s: %s", template_dir.name, e)
        return None

    if not isinstance(data, dict):
        return None

    name = template_dir.name
    return {
        "id": f"local:{name}",
        "display_name": data.get("display_name") or data.get("name") or name,
        "description": data.get("description") or data.get("tagline") or "",
        "source": "local",
        # Surface `priority` (coerced int) so the router's
        # `(priority, display_name)` sort orders real starters ahead of the
        # rest (scout/sage/scribe declare `priority: 20`; most others omit it
        # and fall to the default). (#1513)
        "priority": _coerce_priority(data.get("priority")),
        # `hidden: true` marks internal test/canary/demo fixtures that must not
        # appear in the user-facing catalog. Surfaced here (not filtered) so a
        # single-id resolve — `get_local_template()` / creation-by-id — still
        # works for the test harness; `get_local_templates()` does the actual
        # exclusion. (#1513)
        "hidden": bool(data.get("hidden", False)),
        "resources": data.get("resources", {"cpu": "2", "memory": "4g"}),
        "skills": data.get("skills", []),
        "mcp_servers": list(data.get("credentials", {}).get("mcp_servers", {}).keys())
            or data.get("mcp_servers", []),
        "required_credentials": data.get("required_credentials", []),
        # Local templates surface their full capabilities/use-cases so the
        # frontend can preview them without a second round-trip.
        "capabilities": data.get("capabilities", []),
        "use_cases": data.get("use_cases", []),
        # Opt-in runtime-data declaration (#1169); defaults to empty.
        "data_paths": data.get("data_paths", []),
    }


def get_local_templates() -> List[dict]:
    """Scan the local-templates directory and return entries for every
    directory containing a parseable `template.yaml`.

    Each entry has `id` prefixed `local:<dirname>` and shape matching
    `_build_template` (the GitHub-template builder) so the frontend
    handles both sources identically. (#843)

    Templates marked `hidden: true` in their `template.yaml` — internal
    test/canary fixtures and demo agents — are excluded from this
    user-facing catalog. They remain fully resolvable by id via
    `get_local_template()` and creatable by id (the create path resolves
    by directory name), so the test/canary harness is unaffected. (#1513)
    """
    templates_dir = _local_templates_dir()
    if not templates_dir.exists():
        return []

    out: List[dict] = []
    for child in sorted(templates_dir.iterdir()):
        if not child.is_dir():
            continue
        entry = _build_local_template(child)
        if entry is not None and not entry.get("hidden"):
            out.append(entry)
    return out


def get_local_template(template_id: str) -> Optional[dict]:
    """Get a single local template by `local:<name>` id."""
    if not template_id.startswith("local:"):
        return None
    name = template_id[len("local:"):]
    template_dir = _local_templates_dir() / name
    if not template_dir.is_dir():
        return None
    return _build_local_template(template_dir)


def get_all_templates() -> List[dict]:
    """Return the full resolved template list — local + GitHub-configured.

    Local templates (under `config/agent-templates/`) come first; they
    don't require network access and are always available. GitHub
    metadata is fetched per repo (cached, 10-min TTL).

    Issue #843: local templates were silently omitted before this PR,
    so the frontend's "Local templates" section in CreateAgentModal
    rendered empty even when local templates existed on disk.
    """
    from services.settings_service import get_github_templates

    local = get_local_templates()

    db_entries = get_github_templates()

    if db_entries is not None:
        # Admin-configured list
        repos = [e["github_repo"] for e in db_entries]
        all_metadata = _fetch_all_metadata(repos)
        github = [
            _build_template(e["github_repo"], all_metadata.get(e["github_repo"], {}), e)
            for e in db_entries
        ]
    else:
        # Defaults
        all_metadata = _fetch_all_metadata(DEFAULT_GITHUB_TEMPLATE_REPOS)
        github = [
            _build_template(repo, all_metadata.get(repo, {}))
            for repo in DEFAULT_GITHUB_TEMPLATE_REPOS
        ]

    return local + github


def get_github_template(template_id: str) -> Optional[dict]:
    """Get a single GitHub template by ID (e.g., 'github:owner/repo').

    Resolves metadata from GitHub (cached).
    """
    if not template_id.startswith("github:"):
        return None

    repo = template_id[len("github:"):]

    # Check if it's in the configured list (DB or defaults)
    from services.settings_service import get_github_templates
    db_entries = get_github_templates()

    if db_entries is not None:
        for entry in db_entries:
            if entry["github_repo"] == repo:
                metadata = _get_cached_metadata(repo)
                return _build_template(repo, metadata, entry)

    # Check defaults
    if repo in DEFAULT_GITHUB_TEMPLATE_REPOS:
        metadata = _get_cached_metadata(repo)
        return _build_template(repo, metadata)

    # Dynamic: repo not in any configured list but still a valid github: ID
    metadata = _get_cached_metadata(repo)
    return _build_template(repo, metadata)


def clone_github_repo(github_repo: str, github_pat: str, dest_path: Path, branch: str = None) -> bool:
    """
    Clone a GitHub repository using a Personal Access Token.

    Args:
        github_repo: Repository in format 'org/repo' (e.g., 'Abilityai/agent-ruby')
        github_pat: GitHub Personal Access Token
        dest_path: Destination path to clone to
        branch: Optional branch to clone (default: repo's default branch)

    Returns:
        True if successful, False otherwise
    """
    clone_url = f"https://oauth2:{github_pat}@github.com/{github_repo}.git"

    # Build git clone command
    clone_cmd = ["git", "clone", "--depth", "1"]
    if branch:
        clone_cmd.extend(["-b", branch])
    clone_cmd.extend([clone_url, str(dest_path)])

    try:
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            print(f"Git clone failed: {result.stderr}")
            return False

        # Remove .git directory to prevent accidental pushes from container
        git_dir = dest_path / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)

        print(f"Successfully cloned {github_repo} to {dest_path}")
        return True

    except subprocess.TimeoutExpired:
        print(f"Git clone timed out for {github_repo}")
        return False
    except Exception as e:
        print(f"Error cloning {github_repo}: {e}")
        return False


def extract_env_vars_from_mcp_json(file_path: Path) -> Dict[str, List[str]]:
    """
    Extract ${VAR_NAME} patterns from .mcp.json or .mcp.json.template

    Returns dict mapping MCP server name to list of env vars it requires
    """
    if not file_path.exists():
        return {}

    try:
        with open(file_path) as f:
            content = f.read()
            data = json.loads(content)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not parse {file_path}: {e}")
        return {}

    pattern = r'\$\{([A-Z][A-Z0-9_]*)\}'
    result = {}
    mcp_servers = data.get("mcpServers", {})

    for server_name, server_config in mcp_servers.items():
        vars_for_server = set()

        if "env" in server_config:
            for key, value in server_config["env"].items():
                if isinstance(value, str):
                    matches = re.findall(pattern, value)
                    vars_for_server.update(matches)

        if "args" in server_config:
            for arg in server_config["args"]:
                if isinstance(arg, str):
                    matches = re.findall(pattern, arg)
                    vars_for_server.update(matches)

        if vars_for_server:
            result[server_name] = sorted(vars_for_server)

    return result


def extract_credentials_from_template_yaml(file_path: Path) -> Dict:
    """Extract credentials section from template.yaml."""
    if not file_path.exists():
        return {}

    try:
        with open(file_path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"Warning: Could not parse {file_path}: {e}")
        return {}

    return data.get("credentials", {})


def extract_credentials_from_env_example(file_path: Path) -> List[str]:
    """Extract variable names from .env.example."""
    if not file_path.exists():
        return []

    vars = []
    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    var_name = line.split('=')[0].strip()
                    if var_name and re.match(r'^[A-Z][A-Z0-9_]*$', var_name):
                        vars.append(var_name)
    except IOError as e:
        print(f"Warning: Could not read {file_path}: {e}")

    return vars


def extract_agent_credentials(repo_path: Path) -> Dict:
    """
    Extract all credential requirements from an agent repository.

    Returns:
        {
            "required_credentials": [
                {"name": "HEYGEN_API_KEY", "source": "mcp:heygen"},
                ...
            ],
            "mcp_servers": {
                "heygen": ["HEYGEN_API_KEY"],
                ...
            },
            "env_file_vars": ["BLOTATO_API_KEY", ...]
        }
    """
    result = {
        "required_credentials": [],
        "mcp_servers": {},
        "env_file_vars": []
    }

    all_vars = {}

    # Check .mcp.json or .mcp.json.template
    mcp_json = repo_path / ".mcp.json"
    mcp_template = repo_path / ".mcp.json.template"

    if mcp_json.exists():
        mcp_servers = extract_env_vars_from_mcp_json(mcp_json)
    elif mcp_template.exists():
        mcp_servers = extract_env_vars_from_mcp_json(mcp_template)
    else:
        mcp_servers = {}

    result["mcp_servers"] = mcp_servers

    for server_name, vars in mcp_servers.items():
        for var in vars:
            if var not in all_vars:
                all_vars[var] = []
            all_vars[var].append(f"mcp:{server_name}")

    # Check template.yaml
    template_yaml = repo_path / "template.yaml"
    if template_yaml.exists():
        template_creds = extract_credentials_from_template_yaml(template_yaml)

        for server_name, server_config in template_creds.get("mcp_servers", {}).items():
            env_vars = server_config.get("env_vars", [])
            for var in env_vars:
                if var not in all_vars:
                    all_vars[var] = []
                if f"mcp:{server_name}" not in all_vars[var]:
                    all_vars[var].append(f"template:mcp:{server_name}")

        env_file_vars = template_creds.get("env_file", [])
        result["env_file_vars"] = env_file_vars
        for var in env_file_vars:
            if var not in all_vars:
                all_vars[var] = []
            all_vars[var].append("template:env_file")

    # Check .env.example
    env_example = repo_path / ".env.example"
    if env_example.exists():
        env_vars = extract_credentials_from_env_example(env_example)
        for var in env_vars:
            if var not in all_vars:
                all_vars[var] = []
            all_vars[var].append(".env.example")

    # Build consolidated list
    for var_name in sorted(all_vars.keys()):
        sources = all_vars[var_name]
        primary_source = sources[0] if sources else "unknown"
        result["required_credentials"].append({
            "name": var_name,
            "source": primary_source
        })

    return result


def generate_credential_files(
    template_data: dict,
    agent_credentials: dict,
    agent_name: str,
    template_base_path: Optional[Path] = None
) -> dict:
    """
    Generate credential files (.mcp.json, .env, config files) with real values.
    Returns dict of {filepath: content} to write into container.
    """
    files = {}
    creds_schema = template_data.get("credentials", {})

    # Generate .mcp.json with real credentials
    mcp_servers_schema = creds_schema.get("mcp_servers", {})
    if mcp_servers_schema:
        if template_base_path:
            mcp_template_path = template_base_path / ".mcp.json"
        else:
            templates_dir = Path("/agent-configs/templates")
            if not templates_dir.exists():
                templates_dir = Path("./config/agent-templates")
            template_name = template_data.get("name", "")
            mcp_template_path = templates_dir / template_name / ".mcp.json"

        if mcp_template_path.exists():
            with open(mcp_template_path) as f:
                mcp_config = json.load(f)

            for server_name, server_config in mcp_config.get("mcpServers", {}).items():
                if "env" in server_config:
                    for env_key, env_val in server_config["env"].items():
                        if isinstance(env_val, str) and env_val.startswith("${") and env_val.endswith("}"):
                            var_name = env_val[2:-1]
                            real_value = agent_credentials.get(var_name, "")
                            server_config["env"][env_key] = real_value

                if "args" in server_config:
                    new_args = []
                    for arg in server_config["args"]:
                        if isinstance(arg, str) and arg.startswith("${") and arg.endswith("}"):
                            var_name = arg[2:-1]
                            real_value = agent_credentials.get(var_name, "")
                            new_args.append(real_value)
                        else:
                            new_args.append(arg)
                    server_config["args"] = new_args

            files[".mcp.json"] = json.dumps(mcp_config, indent=2)

    # Generate .env file
    env_vars = creds_schema.get("env_file", [])
    if env_vars:
        env_lines = ["# Generated by Trinity - Agent credentials", ""]
        for var_name in env_vars:
            value = agent_credentials.get(var_name, "")
            env_lines.append(f"{var_name}={value}")
        files[".env"] = "\n".join(env_lines)

    # Generate config files from templates
    config_files = creds_schema.get("config_files", [])
    for config_file in config_files:
        file_path = config_file.get("path", "")
        template_content = config_file.get("template", "")

        if file_path and template_content:
            content = template_content
            for var_name, value in agent_credentials.items():
                content = content.replace(f"{{{var_name}}}", str(value))
            files[file_path] = content

    return files


# ============================================================================
# Trinity-Compatible Validation (Local Agent Deployment)
# ============================================================================

from typing import Tuple


def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if a directory contains a Trinity-compatible agent.

    A Trinity-compatible agent must have:
    1. template.yaml file
    2. name field in template.yaml
    3. resources field in template.yaml
    4. a non-empty CLAUDE.md (agent instructions)

    Args:
        path: Path to the agent directory

    Returns:
        Tuple of (is_compatible, error_message, template_data)
        - is_compatible: True if the agent is Trinity-compatible
        - error_message: Description of why validation failed (None if valid)
        - template_data: Parsed template.yaml data (None if invalid)
    """
    template_path = path / "template.yaml"

    if not template_path.exists():
        return (False, "Missing template.yaml", None)

    try:
        with open(template_path) as f:
            template_data = yaml.safe_load(f)
    except Exception as e:
        return (False, f"Invalid template.yaml: {e}", None)

    if not template_data:
        return (False, "template.yaml is empty", None)

    if not template_data.get("name"):
        return (False, "template.yaml missing required field: name", None)

    if not template_data.get("resources"):
        return (False, "template.yaml missing required field: resources", None)

    # Validate resources has expected structure
    resources = template_data.get("resources", {})
    if not isinstance(resources, dict):
        return (False, "template.yaml resources must be a dictionary", None)

    # Require a non-empty, UTF-8-readable CLAUDE.md. Without it the agent
    # deploys with no usable instructions and comes up effectively empty
    # (#950). Decode strictly and catch UnicodeDecodeError so a binary /
    # non-UTF-8 CLAUDE.md yields a clean 400 here rather than falling through
    # to the generic 500 handler in deploy.py.
    claude_md = path / "CLAUDE.md"
    missing_claude_md = (
        False,
        "Missing or empty CLAUDE.md — agent would deploy with no instructions",
        None,
    )
    if not claude_md.exists():
        return missing_claude_md
    try:
        claude_md_content = claude_md.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (
            False,
            "CLAUDE.md is not valid UTF-8 text — agent would deploy with no usable instructions",
            None,
        )
    except OSError as e:
        return (False, f"Could not read CLAUDE.md: {e}", None)
    if claude_md_content.strip() == "":
        return missing_claude_md

    return (True, None, template_data)


def get_name_from_template(path: Path) -> Optional[str]:
    """
    Extract agent name from template.yaml.

    Args:
        path: Path to the agent directory

    Returns:
        Agent name from template.yaml, or None if not found
    """
    template_path = path / "template.yaml"
    if not template_path.exists():
        return None

    try:
        with open(template_path) as f:
            template_data = yaml.safe_load(f)
            return template_data.get("name") if template_data else None
    except Exception:
        return None


# Platform-injected environment variables — credentials/config Trinity sets on
# the agent container itself at create time, so a template's MCP config that
# references one of these does NOT need the operator to supply a matching
# value. Used only to suppress false-positive credential-gap warnings.
#
# Keep in sync with crud.py:470-559 (the env_vars dict assembled in
# create_agent_internal). A static mirror is deliberate (D3): sharing the
# live allowlist would couple this advisory check to the hot create path.
_PLATFORM_INJECTED_EXACT = frozenset({
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GEMINI_API_KEY",
    "GITHUB_PAT",
    "GITHUB_REPO",
})
# Prefixes cover the family-namespaced vars: TRINITY_MCP_API_KEY/URL/
# GIT_BASE_URL, GIT_SYNC_*/SOURCE_*/WORKING_BRANCH, OTEL_*, and
# CLAUDE_CODE_ENABLE_TELEMETRY.
_PLATFORM_INJECTED_PREFIXES = ("TRINITY_", "GIT_", "OTEL_", "CLAUDE_CODE_")


def _is_platform_injected(var: str) -> bool:
    """True if Trinity injects `var` into the container at create time."""
    if var in _PLATFORM_INJECTED_EXACT:
        return True
    return any(var.startswith(prefix) for prefix in _PLATFORM_INJECTED_PREFIXES)


def _sanitize_for_warning(text: str, max_len: int = 80) -> str:
    """Make an operator-supplied string safe to echo in a deploy warning.

    An MCP server name is an arbitrary JSON key controlled by whoever authored
    the template. Strip non-printable characters (ANSI escapes, newlines, C0/C1
    control bytes) so a crafted name cannot hijack the operator's terminal when
    the warning is rendered, and bound the length so a hostile name cannot flood
    the output. (#950 L1)
    """
    cleaned = "".join(ch for ch in text if ch.isprintable())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


def collect_mcp_credential_warnings(template_dir: Path) -> List[str]:
    """Advisory warnings for MCP servers with unsatisfied ${VAR} references.

    For each `${VAR}` referenced by an MCP server in `.mcp.json.template`
    (or `.mcp.json`) that is neither present in the deployed `.env` nor
    platform-injected, emit a non-fatal warning. This surfaces a missing
    credential at deploy time rather than as a silently broken MCP server on
    first use (#950 deferred hardening).

    Args:
        template_dir: The deployed template directory. The `.env` read here
            must be the post-merge copy (the operator's `credentials` already
            folded in) — see deploy.py.

    Returns:
        A list of human-readable warning strings (empty when nothing is
        missing or no `.mcp` config exists).
    """
    mcp_template = template_dir / ".mcp.json.template"
    mcp_json = template_dir / ".mcp.json"
    if mcp_template.exists():
        mcp_vars = extract_env_vars_from_mcp_json(mcp_template)
    elif mcp_json.exists():
        mcp_vars = extract_env_vars_from_mcp_json(mcp_json)
    else:
        return []

    provided = set(extract_credentials_from_env_example(template_dir / ".env"))

    warnings: List[str] = []
    for server_name in sorted(mcp_vars):
        for var in mcp_vars[server_name]:
            if var in provided or _is_platform_injected(var):
                continue
            warnings.append(
                f"MCP server '{_sanitize_for_warning(server_name)}' references "
                f"${{{var}}} but no matching credential was provided"
            )
    return warnings
