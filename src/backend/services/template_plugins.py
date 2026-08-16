"""`plugins:` block — tolerant reader for template.yaml (#1704).

Declares which Claude Code marketplace plugins an agent should have, so the
selection is a first-class, committed, portable piece of agent config rather
than surviving only by grace of the durable workspace volume (see the #1704
reframe: a plain container recreate is already volume-safe; the real gap is a
**git-based reconstitution** into a fresh/empty volume or a new host, where the
gitignored `~/.claude.json` + `~/.claude/plugins/` cache are exactly what a
clone drops). This module is the agent-local half of the incubating global
plugin-management model (trinity-enterprise#192): the same normalized shape a
future per-agent assignment surface would materialize, reconciled on start.

A `template.yaml` is untrusted input: bundled ones are hand-authored, `github:`
ones come from arbitrary repos, and `local:` ones can be uploaded by any
authenticated user. `yaml.safe_load(...) or {}` can yield a scalar, a list, or a
mapping at any level, so every function here is **total** — it degrades to a
safe empty value and collects named errors, and it NEVER raises. Three consumers
depend on that (identical to the ent#89 `schedules:` reader):

  * `template_service._build_template` / `_build_local_template` — the catalog
    read path, where one raise empties the whole template list (#1835);
  * `agent_service.crud._materialize_agent_files` — creation, where a raise
    would enter the destructive rollback fence;
  * the plugin re-install boot hook reads the *materialized* file, not this.

This is a **leaf**: stdlib + `utils.credential_sanitizer` (a stdlib-only
sanitizer) only. `template_service` imports *this* module, so an import back
would close a cycle — hence the duplicated `_type_name`/`_safe_echo` below,
mirroring `template_schedules.py`.

Two public functions over one private `_parse`, mirroring the sibling
`schedule_shape_errors` / `normalize_declared_schedules` convention (ent#89).
Sharing `_parse` is what makes the reported errors and the accepted entries
structurally unable to disagree.

## Security (autoplan security phase)

Every value in the normalized output is written into the agent container via the
injection-safe single-quoted heredoc (`git_service.materialize_plugins`) AND is
later fed to `claude plugin marketplace add <source>` / `install <plugin>@<mkt>`
as a subprocess argument by the boot hook. So both the marketplace `source` (the
dangerous one — it points where `marketplace add` fetches from) and the
plugin/marketplace names are charset-validated here to a shell-safe, traversal-
free set, and any URL-form source is refused if it carries `user:token@`
userinfo (a marketplace URL is a plausible place to smuggle a credential into a
committed, world-readable manifest).
"""

from typing import Any, Dict, List, Optional, Tuple

from utils.credential_sanitizer import redact_url_userinfo

# A template declaring hundreds of plugins/marketplaces would mint hundreds of
# boot-time subprocesses. The caps live HERE so the catalog surface, creation,
# and the boot hook inherit one bound.
MAX_MARKETPLACES = 20
MAX_PLUGINS = 50

# Marketplace/plugin ids and sources are written into the manifest and passed to
# the CLI; bound their length so a runaway value cannot flood the manifest, the
# catalog response, or the logs.
MAX_NAME_LEN = 100
MAX_SOURCE_LEN = 300

# Marketplace name / plugin name charset. Deliberately the SAME shell-safe,
# traversal-free set the CLI arg and the heredoc body both require: no `..`, no
# `/`, no `@`, no shell metacharacters. Matches the spirit of
# `git_service._SAFE_DATA_PATH_RE`, tightened (a name is one token, not a glob).
_NAME_RE = "[A-Za-z0-9_.-]+"

# YAML-flavoured type names ("mapping", not "dict"). Duplicated from
# `template_service._type_name` deliberately — importing it would close the
# cycle described in the module docstring.
_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "number",
    float: "number",
    str: "string",
    list: "list",
    dict: "mapping",
}


def _type_name(value: Any) -> str:
    """YAML-flavoured type name for an error message."""
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _safe_echo(text: Any, max_len: int = 80) -> str:
    """Make an author-supplied string safe to echo in an error message.

    Twin of `template_schedules._safe_echo` — strip non-printable characters so
    a crafted value cannot hijack a terminal rendering the error, and bound the
    length. Duplicated rather than imported to keep this a leaf (see docstring).
    """
    cleaned = "".join(ch for ch in str(text) if ch.isprintable())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


def _is_name(value: Any, max_len: int = MAX_NAME_LEN) -> bool:
    """True if `value` is a shell-safe, traversal-free single-token name."""
    import re

    return (
        isinstance(value, str)
        and 0 < len(value) <= max_len
        and ".." not in value
        and re.fullmatch(_NAME_RE, value) is not None
    )


def _validate_source(source: Any) -> Tuple[Optional[str], Optional[str]]:
    """Validate a marketplace `source`. Returns `(normalized, None)` or `(None, reason)`.

    Two accepted forms, both shell-safe and free of userinfo:

    * `owner/repo` — exactly one `/`, each segment a plain name (GitHub shorthand);
    * `https://…` — HTTPS only, NO `user:token@` userinfo, charset-restricted.

    Anything else (a bare local path, an `http://` URL, an `ssh`/`git@` remote,
    an embedded credential) is refused by name rather than committed. This is the
    ONE dangerous argument — it decides where `claude plugin marketplace add`
    fetches from — so it is validated the most strictly.
    """
    import re

    if not isinstance(source, str):
        return None, f"source: expected a string, got {_type_name(source)}"
    source = source.strip()
    if not source:
        return None, "source: must not be empty"
    if len(source) > MAX_SOURCE_LEN:
        return None, f"source: exceeds the {MAX_SOURCE_LEN}-character limit"
    if ".." in source:
        return None, f"source: path traversal is not permitted ('{_safe_echo(source)}')"

    # HTTPS URL form.
    if "://" in source or source.lower().startswith("http"):
        if not source.startswith("https://"):
            return None, (
                f"source: only https:// URLs or 'owner/repo' shorthand are "
                f"accepted ('{_safe_echo(source)}')"
            )
        # A credential in the userinfo is the leak this refuses. `@` in an https
        # URL only appears as userinfo, so its presence — or a redaction that
        # changes the string — means credentials.
        if "@" in source or redact_url_userinfo(source) != source:
            return None, (
                "source: a marketplace URL must not embed credentials "
                "(user:token@…) — store the token in the agent env, not the "
                "manifest"
            )
        # Restrict to a shell-safe URL charset (no metacharacters that would
        # break the heredoc write or the subprocess arg).
        if re.fullmatch(r"https://[A-Za-z0-9._:/~-]+", source) is None:
            return None, (
                f"source: URL contains disallowed characters "
                f"('{_safe_echo(source)}')"
            )
        return source, None

    # `owner/repo` shorthand form.
    parts = source.split("/")
    if len(parts) != 2 or not all(_is_name(p) for p in parts):
        return None, (
            f"source: expected 'owner/repo' or an https:// URL, got "
            f"'{_safe_echo(source)}'"
        )
    return source, None


def _parse_marketplaces(
    block: Any, errors: List[str]
) -> Tuple[List[Dict[str, str]], set]:
    """Normalize the `marketplaces:` list. Returns `(entries, declared_names)`."""
    if block is None or block == []:
        return [], set()
    if not isinstance(block, list):
        errors.append(
            f"plugins.marketplaces: expected a list of {{name, source}} entries, "
            f"got {_type_name(block)}"
        )
        return [], set()

    entries = block
    if len(entries) > MAX_MARKETPLACES:
        errors.append(
            f"plugins.marketplaces: {len(entries)} declared, only the first "
            f"{MAX_MARKETPLACES} are materialized"
        )
        entries = entries[:MAX_MARKETPLACES]

    by_name: Dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(
                f"plugins.marketplaces[{index}]: expected a mapping with 'name' "
                f"and 'source', got {_type_name(entry)}"
            )
            continue
        name = entry.get("name")
        if not _is_name(name):
            errors.append(
                f"plugins.marketplaces[{index}].name: expected a plain name "
                f"([A-Za-z0-9._-], no '..'), got '{_safe_echo(name)}'"
            )
            continue
        normalized_source, reason = _validate_source(entry.get("source"))
        if reason is not None:
            errors.append(f"plugins.marketplaces[{index}].{reason}")
            continue
        if name in by_name:
            errors.append(
                f"plugins.marketplaces[{index}].name: duplicate of an earlier "
                f"entry's name — only the first is materialized"
            )
            continue
        by_name[name] = normalized_source

    out = [{"name": n, "source": s} for n, s in by_name.items()]
    return out, set(by_name)


def _collect_installed_refs(block: Any, errors: List[str]) -> List[str]:
    """Collect raw `plugin@marketplace` refs from `installed:` and/or `enabledPlugins:`.

    Two accepted spellings, unioned:

    * `installed:` — a plain list of `plugin@marketplace` strings;
    * `enabledPlugins:` — a mapping of `plugin@marketplace: bool`, mirroring
      Claude Code's own `settings.json` shape (a future runtime distill / the
      #192 assignment surface can write this form directly). Only entries whose
      value is exactly `True` are kept — a `false` entry means an explicitly
      DISABLED plugin and is dropped.
    """
    installed = block.get("installed")
    enabled = block.get("enabledPlugins")
    refs: List[str] = []

    if installed is not None:
        if isinstance(installed, list):
            for index, ref in enumerate(installed):
                if isinstance(ref, str):
                    refs.append(ref)
                else:
                    errors.append(
                        f"plugins.installed[{index}]: expected a "
                        f"'plugin@marketplace' string, got {_type_name(ref)}"
                    )
        else:
            errors.append(
                f"plugins.installed: expected a list of 'plugin@marketplace' "
                f"strings, got {_type_name(installed)}"
            )

    if enabled is not None:
        if isinstance(enabled, dict):
            for ref, value in enabled.items():
                if value is True:
                    refs.append(ref if isinstance(ref, str) else str(ref))
                elif value is not False:
                    # A non-bool value is neither a clean enable nor disable.
                    errors.append(
                        f"plugins.enabledPlugins['{_safe_echo(ref)}']: expected "
                        f"true or false, got {_type_name(value)} — dropped"
                    )
        else:
            errors.append(
                f"plugins.enabledPlugins: expected a mapping of "
                f"'plugin@marketplace': bool, got {_type_name(enabled)}"
            )

    return refs


def _parse(block: Any) -> Tuple[Dict[str, List], List[str]]:
    """The single implementation behind both public functions.

    Returns `(normalized, errors)`. `normalized` is `{}` (a full no-op) when the
    declaration is absent/empty or yields nothing installable; otherwise
    `{"marketplaces": [...], "installed": [...]}` with sorted, de-duplicated
    entries so a stable declaration produces a byte-identical manifest. Total:
    any input shape yields a value, never an exception.
    """
    if block is None or block == {} or block == []:
        return {}, []

    if not isinstance(block, dict):
        return {}, [
            f"plugins: expected a mapping with 'marketplaces' and 'installed'/"
            f"'enabledPlugins', got {_type_name(block)}"
        ]

    errors: List[str] = []
    marketplaces, declared_names = _parse_marketplaces(
        block.get("marketplaces"), errors
    )

    installed: List[str] = []
    seen_refs = set()
    for ref in _collect_installed_refs(block, errors):
        if not isinstance(ref, str):
            continue
        ref = ref.strip()
        if "@" not in ref:
            errors.append(
                f"plugins.installed: '{_safe_echo(ref)}' must be "
                f"'plugin@marketplace'"
            )
            continue
        plugin, _, marketplace = ref.rpartition("@")
        if not _is_name(plugin) or not _is_name(marketplace):
            errors.append(
                f"plugins.installed: '{_safe_echo(ref)}' has an invalid plugin "
                f"or marketplace name ([A-Za-z0-9._-], no '..')"
            )
            continue
        if marketplace not in declared_names:
            # An install we could never satisfy — the marketplace it names is
            # not declared, so the boot hook has no `source` to add.
            errors.append(
                f"plugins.installed: '{_safe_echo(ref)}' references marketplace "
                f"'{_safe_echo(marketplace)}', which is not declared under "
                f"'marketplaces' — dropped"
            )
            continue
        canonical = f"{plugin}@{marketplace}"
        if canonical in seen_refs:
            continue
        seen_refs.add(canonical)
        if len(seen_refs) > MAX_PLUGINS:
            errors.append(
                f"plugins.installed: more than {MAX_PLUGINS} plugins declared — "
                f"only the first {MAX_PLUGINS} are materialized"
            )
            seen_refs.discard(canonical)
            break
        installed.append(canonical)

    # Deterministic, de-duplicated ordering — a stable set never churns the
    # committed manifest across the 15-min auto-sync loop (#1704 determinism).
    marketplaces = sorted(marketplaces, key=lambda m: m["name"])
    installed = sorted(installed)

    if not marketplaces and not installed:
        return {}, errors
    return {"marketplaces": marketplaces, "installed": installed}, errors


def plugin_shape_errors(block: Any) -> List[str]:
    """Named, operator-readable errors for a malformed `plugins:` block.

    An absent, null, or empty block all mean "this agent declares no plugins"
    and are NOT errors. Never raises.
    """
    return _parse(block)[1]


def normalize_declared_plugins(block: Any) -> Dict[str, List]:
    """Well-formed declared plugins, tolerant of any input shape.

    Returns `{}` (opt-in no-op) when nothing installable is declared, else
    `{"marketplaces": [{name, source}], "installed": ["plugin@marketplace"]}`
    with every value charset-validated and both lists sorted+de-duplicated —
    safe to hand straight to `git_service.materialize_plugins`. Never raises.
    """
    return _parse(block)[0]
