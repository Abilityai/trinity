"""Shared helpers for the Brain Orb convention hooks (trinity-enterprise#76).

The four executable hooks (scopes / scope / search / action) are the agent-side
contract Trinity's agent-server brokers (docker/base-image/agent_server/routers/
brain_orb.py). This module holds what they share: root resolution, the scope
discovery + state file, and the canonical scope-primitive imports.

Canonical source: cornelius-internal (synced to the public abilityai/cornelius
template via /sync-to-public). Contract version: 1.
"""
import json
import os
import sys
from pathlib import Path

CONTRACT_VERSION = 1

# <root>/.trinity/brain-orb/_common.py → root two levels up. Never assume a
# home path — the same file runs in an agent container (/home/developer) and a
# local checkout (tests).
ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "Brain"
VIZ_DIR = ROOT / "resources" / "agent-visualization"
DATA_PATH = VIZ_DIR / "data.json"
EXPORTER = VIZ_DIR / "export_data.py"
STATE_PATH = Path(__file__).resolve().parent / "state.json"

# Canonical scope primitives (registry, family scopes, resolver). Pure stdlib —
# the import is cheap and safe. Fail open to name-only discovery if the module
# is missing (a stripped clone): the orb still mounts/unmounts real folders.
sys.path.insert(0, str(ROOT / "resources" / "local-brain-search"))
try:
    from memory_config import (  # noqa: F401  (re-exported for hooks)
        CORE_FOLDERS, SCOPE_FAMILIES, in_read_scope, resolve_read_scope,
        scope_kind,
    )
    HAVE_PRIMITIVES = True
except Exception:
    HAVE_PRIMITIVES = False
    CORE_FOLDERS = frozenset({"02-Permanent", "03-MOCs", "AI Extracted Notes", "01-Sources"})
    SCOPE_FAMILIES = frozenset({"Books"})

    def scope_kind(folder):
        return "cognitive"

    def in_read_scope(note_id, read_scope):
        parts = str(note_id).replace("\\", "/").split("/")
        fine = "/".join(parts[:2]) if parts[0] in SCOPE_FAMILIES and len(parts) >= 2 else parts[0]
        return fine in read_scope or parts[0] in read_scope

    def resolve_read_scope(raw=None):
        toks = [t.strip() for t in str(raw or "").split(",") if t.strip()]
        out = set()
        for t in toks:
            out |= set(CORE_FOLDERS) if t.lower() == "core" else {t}
        return frozenset(out) if out else CORE_FOLDERS


def discover_scopes():
    """The mountable scope tokens, from the vault's ACTUAL top-level dirs.

    Returns an ordered list of entries shaped for the orb's scope panel
    ({token, label, core?, kind?, family?, parent?}). Family folders (Books)
    yield the shelf entry plus one child entry per book dir. Only directories
    that exist on disk are offered — the registry may know scopes this clone
    doesn't carry (e.g. a reference scope), and offering an empty mount is
    noise.
    """
    entries = []
    if not VAULT.is_dir():
        return entries
    for d in sorted(p for p in VAULT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        name = d.name
        base = {"token": name, "label": name}
        if name in CORE_FOLDERS:
            base["core"] = True
        kind = scope_kind(name)
        if kind == "reference":
            base["kind"] = "reference"
        if name in SCOPE_FAMILIES:
            base["family"] = True
            entries.append(base)
            for child in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")):
                entries.append({"token": f"{name}/{child.name}", "label": child.name,
                                "parent": name})
        else:
            entries.append(base)
    return entries


def default_active(entries=None):
    """Default mounted set = every top-level scope (family shelf covers its
    children). Matches the committed seed export and the exporter's own
    "no surprise narrowing" default, so the first-paint canvas, the scope
    panel, and a capture-then-refresh all agree (review H2/H3)."""
    entries = discover_scopes() if entries is None else entries
    return [e["token"] for e in entries if "parent" not in e]


def read_active():
    """The persisted active scope set; falls back to default_active()."""
    try:
        active = json.loads(STATE_PATH.read_text()).get("active")
        if isinstance(active, list) and active:
            return [str(t) for t in active]
    except Exception:
        pass
    return default_active()


def write_active(tokens):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"active": list(tokens)}))
    os.replace(tmp, STATE_PATH)


def resolved_scope(tokens):
    """Token list → the folder-name set the exporter/search filter on."""
    return resolve_read_scope(",".join(tokens))


# Below the agent-server's 180s kill (scope + refresh routes) so the failure
# mode is a clean JSON error, not a 504.
EXPORT_TIMEOUT = 150


def run_export(active_tokens):
    """Re-run export_data.py under the active scope. Returns (ok, error).

    Output is CAPTURED — the exporter prints progress lines, and a hook's
    stdout must be exactly one JSON document. On failure the previous
    data.json is untouched (the exporter writes tmp+rename)."""
    import subprocess
    env = dict(os.environ)
    env["BRAIN_READ_SCOPE"] = ",".join(active_tokens)
    try:
        proc = subprocess.run(
            [sys.executable, str(EXPORTER)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=EXPORT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"export timed out after {EXPORT_TIMEOUT}s"
    except Exception as e:  # exporter missing / not runnable
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "export failed")[-500:]
    return True, None


def graph_counts():
    try:
        data = json.loads(DATA_PATH.read_text())
        return len(data.get("nodes", [])), len(data.get("edges", []))
    except Exception:
        return 0, 0


def emit(payload):
    """Print the hook's single JSON document (the agent-server json-parses the
    ENTIRE stdout — nothing else may be printed to stdout)."""
    payload.setdefault("contract_version", CONTRACT_VERSION)
    print(json.dumps(payload))


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return None
