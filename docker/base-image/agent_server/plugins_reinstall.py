"""Re-install declared Claude Code plugins at container startup (#1704).

Trinity persists an agent's plugin selection as a committed, secret-free
`~/.trinity/plugins.yaml` manifest (written at creation by the backend from a
`template.yaml plugins:` block). A plain container recreate is already
volume-safe, but a **git-based reconstitution** onto a fresh/empty volume or a
new host drops the gitignored `~/.claude.json` + `~/.claude/plugins/` cache —
so the installed plugins are gone. This module is the self-healing half: on
start it reads the manifest and re-installs anything declared-but-missing, so
the declaration is the durability mechanism (the agent-local half of the
incubating global plugin-management model, trinity-enterprise#192).

Runs as `python3 -m agent_server.plugins_reinstall` from `startup.sh`, AFTER
credential injection (a private marketplace needs a git credential at install
time; it is resolved from the agent's `GITHUB_PAT` env here, NEVER from the
manifest). Mirrors `mcp_template.py`.

## Contracts

* **Zero subprocesses when satisfied.** Current state is read via
  `claude plugin marketplace list --json` and `claude plugin list --json`; a
  marketplace/plugin already present is skipped, so a volume-persisting restart
  runs no installs at all.
* **Untrusted manifest.** `~/.trinity/plugins.yaml` is on the agent-writable
  volume, so it is parsed with the ent#314 hardened loader (size cap +
  `AliasPolicy.REJECT`) and every marketplace/plugin name AND the marketplace
  `source` are re-charset-validated here (a second defense layer). Names/sources
  are passed as subprocess **arg lists** — never a shell string — and a
  `source`/ref beginning with `-` (argument injection) is refused.
* **Never hangs, never fatal.** A no-TTY prompt hangs, so every subprocess has a
  hard `timeout` and `stdin=DEVNULL`, and `install` passes `--yes`. Any failure
  is logged as `withheld:<reason>` (the #1929 "withhold with a reason, don't
  blank" contract) and startup continues.

## Trust model (supply chain)

`plugin@marketplace` pins identity, not a commit — a re-install re-fetches the
marketplace's *current* content (contrast ent#237 tag-pinning for skills). This
matches the incubating #192 `auto_update: on` behaviour; a pinned mode is a
documented follow-up (see the #1704 deferred-work note).
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 — arg-list only, never shell=True
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:  # package import (normal) …
    from .safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml
except ImportError:  # pragma: no cover — … or standalone, for a script run
    from safe_yaml import AliasPolicy, HardenedYamlError, load_hardened_yaml  # type: ignore

AGENT_HOME = Path("/home/developer")
MANIFEST_FILE = AGENT_HOME / ".trinity" / "plugins.yaml"
# Fallback for a source-mode / tokenless agent (Cornelius) whose committed
# `.trinity/plugins.yaml` never materialized: a git-reconstituted clone still
# carries `template.yaml`, whose `plugins:` block is the SAME nested shape.
TEMPLATE_FILE = AGENT_HOME / "template.yaml"

# The manifest is agent-writable; cap the read.
_MAX_BYTES = 256 * 1024

# Hard per-subprocess timeout. A no-TTY prompt hangs, so this is a correctness
# bound, not a nicety. `list` reads are quick; `add`/`install` fetch over the
# network, so they get a longer budget.
_READ_TIMEOUT = 30
_INSTALL_TIMEOUT = 180

# Re-validation (defense in depth) — the SAME shell-safe, traversal-free charset
# the backend normalizer (`services.template_plugins`) enforces. Also refuses a
# leading '-' so a value can never be parsed by the CLI as a flag.
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SOURCE_RE = re.compile(r"(?:https://)?[A-Za-z0-9._:/~-]+")


def _log(message: str) -> None:
    """stdout, so it lands in the container log Vector captures."""
    print(f"[plugins-reinstall] {message}", flush=True)


def _is_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 100
        and ".." not in value
        and not value.startswith("-")
        and _NAME_RE.fullmatch(value) is not None
    )


def _is_source(value: object) -> bool:
    """A marketplace source: `owner/repo` shorthand or an `https://` URL.

    No userinfo, no traversal, no leading '-' (flag injection), charset-limited.
    This is the dangerous argument — it points where `marketplace add` fetches
    from — so it is validated the most strictly.
    """
    if not isinstance(value, str) or not value or len(value) > 300:
        return False
    if value.startswith("-") or ".." in value or "@" in value:
        return False
    if _SOURCE_RE.fullmatch(value) is None:
        return False
    if "://" in value and not value.startswith("https://"):
        # Any URL-form must be https:// — reject ftp://, file://, ssh://, data://,
        # etc. Mirrors the backend `template_plugins._validate_source` exactly; the
        # two must not diverge, because this copy is the SOLE gate for the untrusted
        # `template.yaml` fallback path (a source-mode agent's clone).
        return False
    if "://" not in value:
        # `owner/repo` shorthand: exactly two plain-name segments.
        parts = value.split("/")
        return len(parts) == 2 and all(_is_name(p) for p in parts)
    return True


def _read_plugins_block(path: Path, alias_policy: "AliasPolicy") -> Optional[dict]:
    """Read `path`, hardened-parse it, and return its `plugins:` block (or None).

    Never raises. `alias_policy` differs by source: `.trinity/plugins.yaml` is a
    simple platform-written manifest (REJECT — no legitimate anchor), while
    `template.yaml` is a full author document that may legitimately anchor a
    repeated block (BUDGET), so REJECTing it would drop a valid template.
    """
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        _log(f"could not read {path}: {e}")
        return None
    try:
        data = load_hardened_yaml(raw, kind="plugins", alias_policy=alias_policy)
    except HardenedYamlError as e:
        _log(f"withheld all: {path.name} is not safe to parse ({e})")
        return None
    except Exception as e:  # noqa: BLE001 — one bad file must never cost the boot
        _log(f"withheld all: {path.name} could not be parsed ({type(e).__name__}: {e})")
        return None
    block = data.get("plugins") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else None


def load_manifest(
    path: Path = MANIFEST_FILE, template_path: Path = TEMPLATE_FILE
) -> Dict[str, object]:
    """Read + validate the declared plugins. Returns `{marketplaces, installed}` or `{}`.

    Prefers the committed `.trinity/plugins.yaml` manifest; if it is absent or
    empty, falls back to `template.yaml`'s `plugins:` block (the same nested
    shape) so a source-mode / tokenless agent — whose manifest may never have
    materialized — still self-heals from the template the clone carries.

    Never raises. Any invalid marketplace/plugin/source is dropped with a logged
    reason rather than trusted (the block is on the agent-writable volume).
    """
    block = _read_plugins_block(path, AliasPolicy.REJECT)
    if block is None:
        block = _read_plugins_block(template_path, AliasPolicy.BUDGET)
    if not isinstance(block, dict):
        return {}

    marketplaces: Dict[str, str] = {}
    raw_markets = block.get("marketplaces")
    if isinstance(raw_markets, list):
        for entry in raw_markets:
            if not isinstance(entry, dict):
                continue
            name, source = entry.get("name"), entry.get("source")
            if _is_name(name) and _is_source(source):
                marketplaces[name] = source
            else:
                _log(f"withheld marketplace {name!r}: invalid name or source")

    installed: List[str] = []
    raw_installed = block.get("installed")
    if isinstance(raw_installed, list):
        for ref in raw_installed:
            if not isinstance(ref, str) or "@" not in ref:
                continue
            plugin, _, marketplace = ref.rpartition("@")
            if not _is_name(plugin) or not _is_name(marketplace):
                _log(f"withheld plugin {ref!r}: invalid plugin or marketplace name")
                continue
            if marketplace not in marketplaces:
                _log(
                    f"withheld plugin {ref!r}: marketplace '{marketplace}' is not "
                    f"declared, so it cannot be added"
                )
                continue
            installed.append(f"{plugin}@{marketplace}")

    if not marketplaces and not installed:
        return {}
    return {"marketplaces": marketplaces, "installed": installed}


def _run(args: List[str], *, timeout: int) -> Tuple[int, str, str]:
    """Run `args` (arg list, never a shell string). Returns `(rc, stdout, stderr)`.

    `stdin=DEVNULL` so a prompt gets EOF instead of hanging; the timeout is the
    backstop. `GH_TOKEN` is seeded from `GITHUB_PAT` so a private marketplace can
    be fetched — the token comes from the agent env, never the manifest.
    """
    env = dict(os.environ)
    pat = env.get("GITHUB_PAT")
    if pat and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = pat
    try:
        proc = subprocess.run(  # noqa: S603 — arg list, validated, no shell
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "claude CLI not found on PATH"
    except Exception as e:  # noqa: BLE001 — never fatal
        return 1, "", f"{type(e).__name__}: {e}"


def _extract_strings(data: object, keys: Tuple[str, ...]) -> Set[str]:
    """Best-effort: pull identifiers out of a `--json` payload of unknown shape.

    The `claude plugin ... --json` shapes are undocumented and version-drifting
    (#1704 Step 0), so this tolerates a bare list, a `{key: list}` wrapper, and
    items that are either strings or objects carrying one of `keys`. The exact
    shape MUST be pinned to a real capture in the E2E (a wrong shape reads as
    "already installed" and silently installs nothing).
    """
    found: Set[str] = set()
    items: List[object] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                items = value
                break
    for item in items:
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if isinstance(value, str):
                    found.add(value)
                    break
    return found


def _read_marketplaces() -> Tuple[Set[str], bool]:
    """Currently-registered marketplace names, and whether the read succeeded."""
    rc, out, err = _run(
        ["claude", "plugin", "marketplace", "list", "--json"], timeout=_READ_TIMEOUT
    )
    if rc != 0:
        _log(f"could not read marketplace list (rc={rc}): {err.strip()[:200]}")
        return set(), False
    try:
        return _extract_strings(json.loads(out or "null"), ("name", "id")), True
    except ValueError:
        return set(), False


def _read_installed() -> Tuple[Set[str], bool]:
    """Currently-installed `plugin@marketplace` refs, and whether the read succeeded.

    Handles both a `"plugin@marketplace"` string form and an object carrying
    separate name/marketplace fields.
    """
    rc, out, err = _run(["claude", "plugin", "list", "--json"], timeout=_READ_TIMEOUT)
    if rc != 0:
        _log(f"could not read plugin list (rc={rc}): {err.strip()[:200]}")
        return set(), False
    try:
        data = json.loads(out or "null")
    except ValueError:
        return set(), False

    refs: Set[str] = set()
    items: List[object] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                items = value
                break
    for item in items:
        if isinstance(item, str) and "@" in item:
            refs.add(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("plugin")
            mkt = item.get("marketplace") or item.get("source") or item.get("mkt")
            if isinstance(name, str) and isinstance(mkt, str):
                refs.add(f"{name}@{mkt}")
            elif isinstance(name, str) and "@" in name:
                refs.add(name)
    return refs, True


def reinstall(manifest: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Reconcile declared plugins against current state. Never raises.

    Returns a summary `{added_marketplaces, installed, skipped, withheld,
    status}` for observability and the tests. Emits one INFO-style summary line
    so an inert reconcile (declared N, installed 0) is visible, not silent.
    """
    result: Dict[str, object] = {
        "added_marketplaces": [],
        "installed": [],
        "skipped": [],
        "withheld": {},
        "status": "ok",
    }
    if manifest is None:
        manifest = load_manifest()
    if not manifest:
        result["status"] = "no_manifest"
        return result

    marketplaces: Dict[str, str] = manifest.get("marketplaces") or {}
    installed_decl: List[str] = manifest.get("installed") or []

    known_markets, markets_ok = _read_marketplaces()
    known_plugins, plugins_ok = _read_installed()

    added_markets: List[str] = list(result["added_marketplaces"])
    installed_done: List[str] = list(result["installed"])
    skipped: List[str] = list(result["skipped"])
    withheld: Dict[str, str] = dict(result["withheld"])

    # Marketplaces first — a plugin install needs its marketplace registered.
    for name, source in sorted(marketplaces.items()):
        if markets_ok and name in known_markets:
            skipped.append(f"marketplace:{name}")
            continue
        rc, _out, err = _run(
            ["claude", "plugin", "marketplace", "add", source],
            timeout=_INSTALL_TIMEOUT,
        )
        if rc == 0:
            added_markets.append(name)
            known_markets.add(name)
        else:
            withheld[f"marketplace:{name}"] = err.strip()[:200] or f"rc={rc}"

    for ref in sorted(installed_decl):
        if plugins_ok and ref in known_plugins:
            skipped.append(f"plugin:{ref}")
            continue
        _plugin, _, marketplace = ref.rpartition("@")
        if marketplace not in known_markets:
            # Its marketplace could not be added — installing would prompt/fail.
            withheld[f"plugin:{ref}"] = f"marketplace '{marketplace}' unavailable"
            continue
        rc, _out, err = _run(
            ["claude", "plugin", "install", ref, "--yes"],
            timeout=_INSTALL_TIMEOUT,
        )
        if rc == 0:
            installed_done.append(ref)
        else:
            withheld[f"plugin:{ref}"] = err.strip()[:200] or f"rc={rc}"

    for key, reason in sorted(withheld.items()):
        _log(f"withheld {key}: {reason}")

    result.update(
        added_marketplaces=added_markets,
        installed=installed_done,
        skipped=skipped,
        withheld=withheld,
    )
    _log(
        f"declared {len(marketplaces)} marketplace(s) / {len(installed_decl)} "
        f"plugin(s); added {len(added_markets)} marketplace(s), installed "
        f"{len(installed_done)} plugin(s), skipped {len(skipped)} present, "
        f"withheld {len(withheld)}"
    )
    return result


def main() -> int:
    reinstall()
    return 0  # startup must continue regardless


if __name__ == "__main__":  # pragma: no cover — exercised via startup.sh
    sys.exit(main())
