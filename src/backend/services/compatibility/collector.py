"""
Workspace snapshot collector for compatibility checks (#668).

ONE docker exec into the running agent container runs a self-contained
in-container Python script (injected injection-safe via base64) that walks a
FIXED path allowlist and emits ONE JSON blob describing the workspace. The
backend then `json.loads` it once and hands the dict to the pure check
functions — so the checks are unit-testable with fixture dicts and never touch
Docker.

Robustness is concentrated here: the in-container script handles binary,
missing, huge, and bad-encoding files (utf-8 errors='replace', size caps), so
the backend only has to guard the single `json.loads`. Any failure (container
stopped, exec error, unparseable output) degrades to a structured status, never
a 500.

Security: the secret-bearing files (`.env`, generated `.mcp.json`) are read for
EXISTENCE ONLY — their content is never placed in the snapshot, so it can never
leak to the AI payload or logs.
"""

import base64
import json
import logging
from typing import Any, Dict, Optional

from services.docker_service import (
    execute_command_in_container,
    get_agent_container,
    get_agent_runtime,
)
from services.git_service import _detect_git_dir

logger = logging.getLogger(__name__)

# Per-file and aggregate caps applied INSIDE the container so a runaway repo
# can never balloon the exec output.
_FILE_CAP = 256 * 1024
_TOTAL_CAP = 2 * 1024 * 1024
_MAX_SKILL_FILES = 80
_EXEC_TIMEOUT = 20  # seconds — keep the Overview request snappy

# (relative path, read_content). Secret-bearing files are existence-only.
_FIXED_FILES = [
    ("template.yaml", True),
    ("CLAUDE.md", True),
    ("AGENTS.md", True),  # Codex instruction file mirror
    (".gitignore", True),
    (".env.example", True),
    (".mcp.json.template", True),
    (".mcp.json", False),   # generated — holds live secrets, existence only
    (".env", False),        # holds live secrets, existence only
    ("dashboard.yaml", True),
    ("README.md", True),
    ("ARCHITECTURE.md", True),
    ("docs/architecture.md", True),
    ("docs/memory/requirements.md", True),
    ("REQUIREMENTS.md", True),
    ("CHANGELOG.md", True),
    (".trinity/setup.sh", True),
    (".trinity/pre-check", True),
    (".trinity/post-check", True),
]

# The in-container script body. ROOT and the config are prepended at runtime.
_SCRIPT_BODY = r'''
import os, json, sys

_state = {"total": 0}

def read_file(rel, read_content):
    p = os.path.join(ROOT, rel)
    info = {"exists": False}
    try:
        st = os.stat(p)
    except OSError:
        return info
    info["exists"] = True
    info["is_file"] = os.path.isfile(p)
    if not info["is_file"]:
        return info
    info["size"] = st.st_size
    info["mode_exec"] = bool(st.st_mode & 0o111)
    if not read_content:
        return info
    truncated = False
    try:
        with open(p, "rb") as f:
            raw = f.read(FILE_CAP + 1)
    except OSError:
        info["content"] = None
        return info
    if len(raw) > FILE_CAP:
        raw = raw[:FILE_CAP]
        truncated = True
    binary = b"\x00" in raw
    info["binary"] = binary
    info["truncated"] = truncated
    if binary or _state["total"] >= TOTAL_CAP:
        info["content"] = None
    else:
        info["content"] = raw.decode("utf-8", "replace")
        _state["total"] += len(raw)
    return info

def list_dir(rel):
    try:
        return sorted(os.listdir(os.path.join(ROOT, rel)))
    except OSError:
        return None

def walk_md(base, limit):
    out = []
    bp = os.path.join(ROOT, base)
    for dirpath, _dn, fnames in os.walk(bp):
        for fn in sorted(fnames):
            if fn.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), ROOT))
                if len(out) >= limit:
                    return out
    return out

files = {rel: read_file(rel, rc) for rel, rc in FIXED_FILES}

dirs = {
    ".claude/commands": list_dir(".claude/commands"),
    ".claude/skills": list_dir(".claude/skills"),
    ".claude/agents": list_dir(".claude/agents"),
    "schemas": list_dir("schemas"),
}

skill_rels = []
if os.path.isdir(os.path.join(ROOT, ".claude/skills")):
    skill_rels += walk_md(".claude/skills", MAX_SKILL_FILES)
if os.path.isdir(os.path.join(ROOT, ".claude/commands")):
    skill_rels += walk_md(".claude/commands", MAX_SKILL_FILES)
skills = {rel: read_file(rel, True) for rel in skill_rels[:MAX_SKILL_FILES]}

out = {
    "schema": 1,
    "root": ROOT,
    "files": files,
    "dirs": dirs,
    "skills": skills,
    "hit_total_cap": _state["total"] >= TOTAL_CAP,
}
sys.stdout.write(json.dumps(out))
'''


def _build_script(root: str) -> str:
    header = (
        f"ROOT = {root!r}\n"
        f"FILE_CAP = {_FILE_CAP}\n"
        f"TOTAL_CAP = {_TOTAL_CAP}\n"
        f"MAX_SKILL_FILES = {_MAX_SKILL_FILES}\n"
        f"FIXED_FILES = {_FIXED_FILES!r}\n"
    )
    return header + _SCRIPT_BODY


async def collect(agent_name: str) -> Dict[str, Any]:
    """Collect a workspace snapshot for an agent.

    Returns ``{"status", "snapshot", "runtime"}`` where status is:
      * ``"ok"``           — snapshot collected
      * ``"not_running"``  — container missing/stopped (can't exec)
      * ``"unavailable"``  — exec ran but output couldn't be parsed
    """
    runtime = get_agent_runtime(agent_name)
    container = get_agent_container(agent_name)
    if container is None or getattr(container, "status", None) != "running":
        return {"status": "not_running", "snapshot": None, "runtime": runtime}

    try:
        root = await _detect_git_dir(f"agent-{agent_name}")
        script = _build_script(root)
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # b64 charset is [A-Za-z0-9+/=] — shell-safe inside the double quotes.
        # 2>/dev/null drops any interpreter noise so stdout is pure JSON.
        command = f'bash -c "echo {b64} | base64 -d | python3 - 2>/dev/null"'
        result = await execute_command_in_container(
            container_name=f"agent-{agent_name}",
            command=command,
            timeout=_EXEC_TIMEOUT,
        )
        raw = (result.get("output") or "").strip()
        if result.get("exit_code") != 0 or not raw:
            logger.warning(
                "[compatibility] collector exec failed for %s (exit=%s)",
                agent_name, result.get("exit_code"),
            )
            return {"status": "unavailable", "snapshot": None, "runtime": runtime}
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or "files" not in snapshot:
            return {"status": "unavailable", "snapshot": None, "runtime": runtime}
        return {"status": "ok", "snapshot": snapshot, "runtime": runtime}
    except (ValueError, TypeError) as e:
        logger.warning("[compatibility] collector JSON parse failed for %s: %s", agent_name, e)
        return {"status": "unavailable", "snapshot": None, "runtime": runtime}
    except Exception as e:  # noqa: BLE001 — never let collection 500 the request
        logger.warning("[compatibility] collector error for %s: %s", agent_name, e)
        return {"status": "unavailable", "snapshot": None, "runtime": runtime}
