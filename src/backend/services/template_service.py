"""
Template service for processing agent templates.

Metadata for GitHub templates is fetched from each repo's template.yaml
via the GitHub API and cached in memory (10-minute TTL).
"""
import base64
import difflib
import json
import logging
import re
import subprocess
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import httpx
import yaml
from config import DEFAULT_GITHUB_TEMPLATE_REPOS, GITHUB_PAT_CREDENTIAL_ID
from services.credential_charset import (
    CREDENTIAL_DETECTOR_NAME_RE,
    CREDENTIAL_DETECTOR_REF_RE,
    is_credential_var_name,
)

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


# ============================================================================
# `credentials:` block — tolerant readers (trinity-enterprise#128)
# ============================================================================
#
# A template.yaml is untrusted input: bundled ones are hand-authored, `github:`
# ones come from arbitrary repos, and `local:` ones can be uploaded by any
# authenticated user via deploy_local_agent_logic. Every historical reader of
# this block assumed the happy shape and reached straight through it —
# `data.get("credentials", {}).get("mcp_servers", {}).keys()` — so ONE
# malformed template took down the whole catalog with an uncaught
# AttributeError, and a string `env_file:` was iterated character by character
# straight into the agent's `.env`.
#
# Two contracts, deliberately different:
#   * read paths (the catalog) NEVER raise — they degrade to a safe empty
#     value and collect named errors, so the other templates still list;
#   * the write path (`generate_credential_files`) raises, because writing an
#     agent's credential files from a declaration nobody can parse is exactly
#     the silent failure this closes.

class CredentialDeclarationError(ValueError):
    """A template's `credentials:` block is structurally invalid.

    HTTP-free by design (Invariant #1 — services hold no HTTP concerns):
    raised by `generate_credential_files` and mapped 1:1 to a 400 by its only
    caller, `agent_service.crud._stage_config_files`.
    """


_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "number",
    float: "number",
    str: "string",
    list: "list",
    dict: "mapping",
}


def _type_name(value) -> str:
    """YAML-flavoured type name for an error message ('mapping', not 'dict')."""
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _credentials_mapping(block) -> dict:
    """Return the `credentials:` block as a mapping, `{}` for anything else.

    `.get("credentials", {})` is NOT enough: the default only applies when the
    key is *absent*, so a present-but-null `credentials:` (a trailing colon
    with nothing under it — an ordinary authoring slip) yields `None` and every
    downstream `.get()` / `.keys()` raises.
    """
    return block if isinstance(block, dict) else {}


def credential_shape_errors(block) -> List[str]:
    """Named, operator-readable errors for a malformed `credentials:` block.

    Scope is *shape* — is each section the right kind of thing — not the
    per-variable descriptor validation. Never raises.

    An absent, null, or empty block all mean "this agent needs no credentials"
    and are NOT errors, so a template that comments its block out does not
    acquire a spurious warning.
    """
    if block is None or block == {}:
        return []
    if not isinstance(block, dict):
        return [
            f"credentials: expected a mapping of section name to declaration, "
            f"got {_type_name(block)}"
        ]

    errors: List[str] = []

    def _full() -> bool:
        """True once the error list is at its cap; appends one marker on arrival.

        The cap has to bound the WALK, not just the output. `credentials:` is
        author-controlled YAML, and a single alias reused across servers presents
        ~160k malformed `env_vars` elements from 13 KB of source — so a cap
        applied after the loops would still have built and joined every string.
        Measured before this bound: one `/api/templates` catalog entry serialized
        to 15.0 MB, and `generate_credential_files`' agent-creation 400 body to
        14.6 MB (~1100x amplification), reachable since ent#123 by any creator-role
        user pointing at an arbitrary public repo.

        `_MAX_CREDENTIAL_ERRORS` (defined below with the other ent#128 caps) is the
        SAME cap `normalize_credential_requirements` applies to its own error list.
        This function is the sibling producer feeding the same two surfaces, and
        capping only one of them leaves the amplification fully open. (ent#128)
        """
        if len(errors) < _MAX_CREDENTIAL_ERRORS:
            return False
        if len(errors) == _MAX_CREDENTIAL_ERRORS:
            errors.append(
                f"credentials: more than {_MAX_CREDENTIAL_ERRORS} problems found; "
                f"the rest are not shown. Fix these first."
            )
        return True

    mcp_servers = block.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        errors.append(
            f"credentials.mcp_servers: expected a mapping of server name to "
            f"config, got {_type_name(mcp_servers)}"
        )
    elif isinstance(mcp_servers, dict):
        # Per-server + per-ELEMENT rows, mirroring what `env_file` already gets
        # below. The element row is the one that matters: an `env_vars` entry
        # smuggled in as a mapping is what turns a downstream
        # `{r["name"] for r in ...}` into `TypeError: unhashable type: 'dict'`, and
        # before ent#128 this function checked only that `mcp_servers` was a dict —
        # so the single most dangerous shape in the block was unnamed. (ent#128)
        for server_name, server_config in mcp_servers.items():
            if _full():
                break
            label = f"credentials.mcp_servers.{_sanitize_for_warning(str(server_name))}"
            if not isinstance(server_config, dict):
                errors.append(
                    f"{label}: expected a mapping with an 'env_vars' list, got "
                    f"{_type_name(server_config)}"
                )
                continue
            env_vars = server_config.get("env_vars")
            if env_vars is None:
                continue
            if not isinstance(env_vars, list):
                errors.append(
                    f"{label}.env_vars: expected a list of variable names, got "
                    f"{_type_name(env_vars)}"
                )
                continue
            for i, entry in enumerate(env_vars):
                if _full():
                    break
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        f"{label}.env_vars[{i}]: expected a variable name "
                        f"(string), got {_type_name(entry)}"
                    )

    env_file = block.get("env_file")
    if env_file is not None and not isinstance(env_file, list):
        errors.append(
            f"credentials.env_file: expected a list of variable names, got "
            f"{_type_name(env_file)}. Give each name its own list item "
            f'("- OPENAI_API_KEY") — a bare string is read one character at a '
            f"time, which writes one variable per letter."
        )
    elif isinstance(env_file, list):
        for i, entry in enumerate(env_file):
            if _full():
                break
            if not isinstance(entry, str) or not entry.strip():
                errors.append(
                    f"credentials.env_file[{i}]: expected a variable name "
                    f"(string), got {_type_name(entry)}"
                )

    for message in _config_files_shape_errors(block.get("config_files")):
        if _full():
            break
        errors.append(message)
    return errors


def _config_files_shape_errors(config_files) -> List[str]:
    """Shape + path-containment errors for `credentials.config_files`.

    `path` is an author-controlled string that the creation path turns into
    `open(cred_files_dir / path, "w")`, so an absolute path or a `..` segment
    is an arbitrary-file-write primitive reachable from any authenticated user
    (deploy_local_agent_logic accepts an uploaded template archive). Rejected
    here at the boundary AND re-checked at the write sink in crud.py.
    """
    if config_files is None:
        return []
    if not isinstance(config_files, list):
        return [
            f"credentials.config_files: expected a list of "
            f"{{path, template}} entries, got {_type_name(config_files)}"
        ]

    errors: List[str] = []
    for i, entry in enumerate(config_files):
        if not isinstance(entry, dict):
            errors.append(
                f"credentials.config_files[{i}]: expected a mapping with "
                f"'path' and 'template', got {_type_name(entry)}"
            )
            continue

        path = entry.get("path", "")
        if not path:
            continue
        if not isinstance(path, str):
            errors.append(
                f"credentials.config_files[{i}].path: expected a string, got "
                f"{_type_name(path)}"
            )
            continue

        safe_path = _sanitize_for_warning(path)
        if PurePosixPath(path).is_absolute():
            errors.append(
                f"credentials.config_files[{i}].path: expected a path relative "
                f"to the agent's credential directory, got absolute path "
                f"{safe_path!r}"
            )
        elif ".." in PurePosixPath(path).parts:
            errors.append(
                f"credentials.config_files[{i}].path: '..' segments are not "
                f"allowed, got {safe_path!r}"
            )
    return errors


def credential_mcp_server_names(block) -> List[str]:
    """Server names under `credentials.mcp_servers`, tolerant of any shape.

    The read-path replacement for `credentials.mcp_servers.keys()`. Returns
    `[]` — never raises — for a null, list, string or scalar block at either
    level, so a malformed template cannot empty the catalog.
    """
    servers = _credentials_mapping(block).get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    return [str(name) for name in servers]


def credential_env_file_names(block) -> List[str]:
    """Variable names under `credentials.env_file`, tolerant of any shape.

    Backs the `.env` writer. Returns only non-empty strings, so a malformed
    entry can never reach the generated file — callers that must fail loud
    (the writer) check `credential_shape_errors()` first.
    """
    env_file = _credentials_mapping(block).get("env_file")
    if not isinstance(env_file, list):
        return []
    return [entry for entry in env_file if isinstance(entry, str) and entry.strip()]


def credential_mcp_env_vars(block) -> Dict[str, List[str]]:
    """`{server name: [declared variable names]}`, tolerant of any shape.

    The per-server companion to `credential_mcp_server_names`, tolerant at all
    THREE levels a template can break: the block, the `mcp_servers` mapping, and
    each server's `env_vars` list. Returns only non-empty strings, so a mapping
    or a list smuggled in as an `env_vars` element can never reach a consumer —
    that element is exactly the shape that turns a `{r["name"] for r in ...}`
    comprehension into `TypeError: unhashable type: 'dict'`, and a raise inside a
    compatibility check downgrades a HARD gate to `skipped`. Never raises.

    Deliberately does NOT validate names against the detector charset: this
    answers "what did the author DECLARE", which is compared against `${VAR}`
    references as-is. Charset validation belongs to the enriched
    `credential_setup:` descriptors, where a bad name is an authoring error worth
    naming.
    """
    servers = _credentials_mapping(block).get("mcp_servers")
    if not isinstance(servers, dict):
        return {}

    out: Dict[str, List[str]] = {}
    for name, server_config in servers.items():
        env_vars = server_config.get("env_vars") if isinstance(server_config, dict) else None
        if not isinstance(env_vars, list):
            out[str(name)] = []
            continue
        out[str(name)] = [
            entry for entry in env_vars if isinstance(entry, str) and entry.strip()
        ]
    return out


def declared_credential_names(block) -> List[str]:
    """Every variable name a `credentials:` block DECLARES, tolerant of any shape.

    The union of `credentials.mcp_servers.<server>.env_vars` and
    `credentials.env_file` — the two places a template names a credential. This
    is the "what did the author declare" universe: the cross-reference target for
    `credential_setup:` and the `listed` set the K-002/T-015 compatibility gate
    compares `${VAR}` references against.

    Deduplicated, order-preserving (first declaration wins) so a caller can build
    stable records. Every element is a non-empty `str` **structurally** — the
    tolerant readers below cannot emit anything else — so a caller's
    `{r["name"] for r in ...}` set comprehension can never meet the unhashable
    `dict` that turns a compatibility check into a `skipped` verdict. Callers on
    a security path defend anyway (`if isinstance(n, str)`): the gate must not
    depend on this contract holding.

    Path-free by construction — it takes a parsed block, never a filename — so
    the pending `template.yaml` → `trinity.yaml` rename (trinity#570) cannot
    reach it.
    """
    names: List[str] = []
    seen = set()
    for env_vars in credential_mcp_env_vars(block).values():
        for name in env_vars:
            if name not in seen:
                seen.add(name)
                names.append(name)
    for name in credential_env_file_names(block):
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def operator_supplied_credential_names(block) -> List[str]:
    """Declared names an OPERATOR must actually supply — the badge feed.

    `declared_credential_names` minus the platform-injected vars Trinity sets on
    the container itself at create time. That subtraction IS the badge's semantic:
    the catalog's "N credentials" chip is read as "how much work is this to set
    up", and counting `GEMINI_API_KEY` / `GITHUB_PAT` / `TRINITY_*` inflates it
    with rows nobody can fill. Measured on the real shipped catalog before this
    change, the count was correct on 1 of 7 repos — the ent#124 first-run agent
    read 5 when the operator supplies 2.

    Note this makes the badge a *count of work*, not a count of declarations. A
    consumer wanting every declared variable (including the injected ones) wants
    `declared_credential_names` or `credential_requirements`.
    """
    return [
        name for name in declared_credential_names(block)
        if not _is_platform_injected(name)
    ]


# ============================================================================
# `credential_setup:` — the enriched declaration standard (ent#128)
# ============================================================================
#
# `credentials:` is FROZEN as names-only, forever. Enrichment lives in a NEW
# sibling top-level key, and the two are joined by a mandatory validated
# cross-reference:
#
#   credentials:                      # names-only, valid forever
#     mcp_servers:
#       stripe:
#         env_vars: [STRIPE_API_KEY]
#     env_file: [VAULT_BASE_PATH]
#
#   credential_setup:                 # NEW — enriched, opt-in, DECORATES the above
#     - name: STRIPE_API_KEY
#       title: "Stripe secret key"
#       description: "Powers the stripe MCP server."
#       required: true
#       secret: true
#       setup_url: https://dashboard.stripe.com/apikeys
#
# Why a sibling key rather than enriching `credentials.env_file` in place: an
# already-deployed older Trinity reads `env_file` through
# `credential_env_file_names` and then does `agent_credentials.get(var_name, "")`.
# Hand it a list of mappings and that is `TypeError: unhashable type: 'dict'` at
# the moment it writes the agent's `.env`. A sibling key is STRUCTURALLY invisible
# to that binary, so there is no floor version and enrichment can be distributed
# immediately.
#
# Why base-set-plus-overlay rather than "credential_setup declares things": the
# sibling-key shape is normally where two places describe the same variable and
# drift. Here they cannot. One record per variable that `credentials:` declares,
# decorated by `credential_setup:` entries joined BY NAME; an entry naming nothing
# is a named error and is dropped, valid siblings survive. `credential_setup:` can
# only ever decorate — drift is impossible by construction, not by discipline.
#
# What drift DOES remain, stated honestly: for an external template a
# `credential_setup:` entry naming an undeclared variable is neither impossible
# nor visible in the UI — `credential_errors` has zero frontend and zero MCP
# consumers, so the only human channel is the backend WARNING. It is LOGGED, not
# surfaced. Say that rather than claiming visibility.

# One record per declared variable, hard-capped. The cap is on the INPUT, before
# any per-field work, because the cost this bounds is the walk itself, not the
# output: `str()` on an aliased YAML node EXPANDS it (443 B → 52 MB measured), so a
# cap applied after the loop bounds nothing. Both the enriched list AND the base
# set are capped — the base set comes from `credentials:`, which the enriched cap
# does not cover.
_MAX_CREDENTIAL_RECORDS = 100

# Errors are capped for the same reason and separately: 400k malformed entries
# yielded 1 capped record and 400,000 uncapped errors — a 35 MB response built out
# of the cap that was supposed to prevent exactly that.
_MAX_CREDENTIAL_ERRORS = 100

# Per-field, field-appropriate. `_sanitize_for_warning`'s 80-char default is for a
# terminal warning line, and reusing it here truncated a realistic 159-char
# `description` and made a real 90-char vendor console URL unusable.
_CREDENTIAL_FIELD_MAX = {
    "name": 128,
    "title": 120,
    "description": 500,
    "default": 200,
    "format": 40,
}
_MAX_SETUP_URL_LEN = 2048

# Open vocabulary — a hint for a future guided-setup UI, never an enforcement.
# `format` outside it is an error so a typo is caught, but the set is expected to
# grow; unknown values degrade the record, they never drop it.
_CREDENTIAL_FORMATS = frozenset(
    {
        "secret",
        "filepath",
        "dirpath",
        "url",
        "email",
        "number",
        "boolean",
        "text",
    }
)

# The authored top-level key. Named once so the error strings, the reader and the
# schema artifact cannot drift.
_CREDENTIAL_SETUP_KEY = "credential_setup"

# snake_case authored keys. Silent-ignore plus camelCase would turn an
# `is_required:` typo into a silent semantic flip, so unknown keys are named with a
# "did you mean" — and `x-` is the documented escape hatch for a vendor extension
# that must not become an error.
_CREDENTIAL_SETUP_FIELDS = frozenset(
    {
        "name",
        "title",
        "description",
        "required",
        "secret",
        "format",
        "setup_url",
        "default",
    }
)

_SOURCE_TRUST_LEVELS = frozenset({"bundled", "deployed", "github"})


def _did_you_mean(key: str, allowed) -> str:
    """Nearest allowed key, for an unknown-key error.

    `difflib` rather than prefix matching: the typos worth catching are
    transpositions and near-misses (`titel`, `descriptoin`), and a prefix test
    catches none of them. Also normalizes `-` to `_` so a camelCase-adjacent or
    kebab-case author is pointed at the snake_case key rather than told nothing.
    """
    lowered = key.lower().replace("-", "_")
    matches = difflib.get_close_matches(lowered, sorted(allowed), n=1, cutoff=0.6)
    return f" — did you mean '{matches[0]}'?" if matches else ""


def _clean_field(value, field: str) -> Optional[str]:
    """Sanitize one author-controlled string, or `None` if it is not a string.

    Returns `None` for a non-string so the CALLER emits the named type error. This
    never coerces: `str()` on a container from untrusted YAML expands a shared
    alias during the walk (443 B → 52 MB in 1.5 s, x10 per alias level), and both
    the sanitizer and the record cap act AFTER that cost is already paid. Type-guard
    first, always.
    """
    if not isinstance(value, str):
        return None
    return _sanitize_for_warning(value, max_len=_CREDENTIAL_FIELD_MAX[field])


def _setup_url_error(url) -> Optional[str]:
    """`None` if `url` is an acceptable setup link, else the reason it is not.

    Scheme-only is below the floor. `https://google.com@evil.tld/apikey` passes a
    scheme check and renders as a Google link while resolving to the attacker —
    and this string lands in an operator-facing "paste your API key here"
    checklist, so the display/resolve split IS the attack.

    Validate, THEN sanitize — and never run a URL through a truncator, which is
    what silently broke real 90-char vendor console links.

    Named residual: `isprintable()` is False for Cf/Cc, so RTL overrides and ANSI
    escapes are rejected, but IDN homographs and full-width characters SURVIVE.
    A lookalike host still gets through, which is why a consumer must render the
    parsed hostname beside the link rather than only the anchor text.
    """
    if not isinstance(url, str):
        return f"expected an https URL (string), got {_type_name(url)}"
    if len(url) > _MAX_SETUP_URL_LEN:
        return f"URL is {len(url)} characters (limit {_MAX_SETUP_URL_LEN})"
    if any(not ch.isprintable() for ch in url):
        return "URL contains non-printable characters"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL could not be parsed"
    # Case-insensitive: `HTTPS://` is a legitimate author today and lowercasing
    # the whole URL to test it would corrupt a case-sensitive path.
    if parts.scheme.lower() != "https":
        return f"expected an https URL, got scheme {parts.scheme.lower() or '(none)'!r}"
    if not parts.hostname:
        return "URL has no host"
    if parts.username or parts.password:
        return (
            "URL contains userinfo (the `user@host` form renders as one host and "
            "resolves to another)"
        )
    return None


def _base_credential_records(block) -> List[dict]:
    """One un-enriched record per variable `credentials:` declares, in order.

    This is the cross-reference universe: `credential_setup:` can only decorate a
    name that appears here. `source` keeps the `template:` prefix — an
    `extract_agent_credentials` consumer distinguishes a `template:`-declared
    variable from one merely observed in `.mcp.json`, and first declaration wins.
    """
    records: List[dict] = []
    seen: Dict[str, dict] = {}

    def add(name: str, source: str) -> None:
        if name in seen:
            return
        record = {
            "name": name,
            "title": name,
            "description": None,
            # Tri-state. A bare `- FOO` carries NO authorial intent, so it must not
            # read as `True`: an ent#127-style checklist that renders every legacy
            # name as a required field cries wolf on templates whose authors never
            # opted in. `"unknown"` is also the enriched/un-enriched discriminator —
            # `required == "unknown"` <=> this record was never decorated.
            "required": "unknown",
            # Fail-safe: assume a declared variable is sensitive until an author
            # says otherwise, so a consumer masks by default.
            "secret": True,
            "format": None,
            "setup_url": None,
            "default": None,
            "source": _sanitize_for_warning(source, max_len=160),
            "platform_injected": _is_platform_injected(name),
        }
        seen[name] = record
        records.append(record)

    for server_name, env_vars in credential_mcp_env_vars(block).items():
        # The server name is an arbitrary author-controlled mapping key and it goes
        # straight into `source`, which reaches an operator-facing surface — the
        # exact string `_sanitize_for_warning`'s own docstring names as the threat.
        # It was not on the sanitize list before ent#128.
        for name in env_vars:
            add(name, f"template:mcp:{server_name}")
    for name in credential_env_file_names(block):
        add(name, "template:env_file")
    return records


def normalize_credential_requirements(data, *, source_trust: str):
    """Normalized per-variable credential requirements for a parsed template.yaml.

    Returns `(records, errors)`. Base-set-plus-overlay: one record per variable
    `credentials:` declares, decorated by matching `credential_setup:` entries.

    **NEVER RAISES, and that is a load-bearing security property, not a nicety.**
    Two callers make it so: `_build_template` runs inside bare list comprehensions
    in `get_all_templates()`, where a raise is an HTTP 500 and an EMPTY CATALOG —
    the exact bug PR-A closed; and the K-002/T-015 HARD compatibility gate consumes
    the same readers, where a raise downgrades to a verdict indistinguishable from
    a pass. Callers on those paths ALSO wrap this, deliberately: the property must
    not rest on one function's discipline.

    **NEVER MUTATES `data`.** `_metadata_cache` holds the parsed dict for 600 s and
    YAML aliases genuinely share nodes, so one in-place normalize would rewrite
    both aliased fields and persist for ten minutes.

    `source_trust` (`bundled` | `deployed` | `github`) is a required kwarg with no
    default — fail-safe, and every level gets identical validation. It selects the
    LOG level only: a malformed bundled template is our own build bug and worth a
    WARNING, while an external one is routine untrusted input.

    Absent / `{}` / `null` `credentials:` means "this agent needs no credentials"
    and is not an error.
    """
    # `not in` on a frozenset raises TypeError for an unhashable value, i.e.
    # BEFORE the guard that exists precisely so a bad `source_trust` cannot raise.
    # Unreachable from parsed YAML (every call site passes a literal), but the
    # docstring's "NEVER RAISES" is load-bearing enough to be literally true.
    if not isinstance(source_trust, str) or source_trust not in _SOURCE_TRUST_LEVELS:
        # A coding error at the call site — but raising here would reopen exactly
        # the empty-catalog path this function exists to keep closed, so degrade to
        # the strictest posture and say so loudly instead.
        logger.warning(
            "normalize_credential_requirements: unknown source_trust %r; treating "
            "as 'github'",
            source_trust,
        )
        source_trust = "github"

    errors: List[str] = []

    def error(message: str) -> None:
        if len(errors) < _MAX_CREDENTIAL_ERRORS:
            errors.append(message)

    if not isinstance(data, dict):
        return [], errors

    records = _base_credential_records(data.get("credentials"))
    if len(records) > _MAX_CREDENTIAL_RECORDS:
        error(
            f"credentials: {len(records)} variables declared (limit "
            f"{_MAX_CREDENTIAL_RECORDS}); the rest are ignored"
        )
        records = records[:_MAX_CREDENTIAL_RECORDS]
    by_name = {record["name"]: record for record in records}

    setup = data.get(_CREDENTIAL_SETUP_KEY)
    if setup is None:
        return records, errors
    if not isinstance(setup, list):
        error(
            f"{_CREDENTIAL_SETUP_KEY}: expected a list of variable descriptors, "
            f"got {_type_name(setup)}"
        )
        return records, errors

    if len(setup) > _MAX_CREDENTIAL_RECORDS:
        error(
            f"{_CREDENTIAL_SETUP_KEY}: too many declarations "
            f"({len(setup)}, limit {_MAX_CREDENTIAL_RECORDS})"
        )
        setup = setup[:_MAX_CREDENTIAL_RECORDS]

    decorated: set = set()
    for i, entry in enumerate(setup):
        where = f"{_CREDENTIAL_SETUP_KEY}[{i}]"
        if not isinstance(entry, dict):
            error(
                f"{where}: expected a mapping with a 'name' key, got {_type_name(entry)}"
            )
            continue

        for key in entry:
            if not isinstance(key, str):
                error(f"{where}: key {_type_name(key)} is not a string")
            elif key.startswith("x-"):
                continue  # documented vendor-extension escape hatch
            elif key not in _CREDENTIAL_SETUP_FIELDS:
                error(
                    f"{where}: unknown key "
                    f"{_sanitize_for_warning(key, max_len=40)!r}"
                    f"{_did_you_mean(key, _CREDENTIAL_SETUP_FIELDS)}"
                )

        raw_name = entry.get("name")
        if raw_name is None:
            error(f"{where}: missing required key 'name'")
            continue
        name = _clean_field(raw_name, "name")
        if name is None:
            error(
                f"{where}.name: expected a variable name (string), got {_type_name(raw_name)}"
            )
            continue
        if not is_credential_var_name(name):
            error(f"{where}.name: invalid variable name {name!r}")
            continue
        if name in decorated:
            error(f"{where}.name: duplicate declaration of {name!r}")
            continue

        record = by_name.get(name)
        if record is None:
            # The mandatory cross-reference. Three lines on purpose — problem,
            # cause, FIX — because the whole no-drift guarantee rests on an author
            # understanding that this key decorates and does not declare.
            error(
                f"{where}.name: {name!r} is not declared in `credentials:`. "
                f"Add it to `credentials.env_file` or "
                f"`credentials.mcp_servers.<server>.env_vars` — `credential_setup:` "
                f"adds setup guidance for variables `credentials:` declares, it does "
                f"not declare them."
            )
            continue
        decorated.add(name)

        for field in ("title", "description", "default", "format"):
            if field not in entry:
                continue
            cleaned = _clean_field(entry[field], field)
            if cleaned is None:
                error(
                    f"{where}.{field}: expected a string, got {_type_name(entry[field])}"
                )
                continue
            if field == "format" and cleaned not in _CREDENTIAL_FORMATS:
                error(f"{where}.format: unknown format {cleaned!r}")
                continue
            record[field] = cleaned

        for field in ("required", "secret"):
            if field not in entry:
                continue
            value = entry[field]
            if not isinstance(value, bool):
                error(
                    f"{where}.{field}: expected true or false, got {_type_name(value)}"
                )
                continue
            record[field] = value

        if "setup_url" in entry:
            reason = _setup_url_error(entry["setup_url"])
            if reason:
                error(f"{where}.setup_url: {reason}")
            else:
                # Validated, THEN sanitized — and never truncated.
                record["setup_url"] = "".join(
                    ch for ch in entry["setup_url"] if ch.isprintable()
                )

        # Enriched-and-omitted means REQUIRED: this cohort is "a form a human
        # completes", and an author who described a variable but left `required`
        # off meant it. Only an un-decorated legacy name stays `"unknown"`.
        if "required" not in entry:
            record["required"] = True

    if errors:
        level = logger.warning if source_trust == "bundled" else logger.info
        level(
            "`%s` in a %s template has %d problem(s): %s",
            _CREDENTIAL_SETUP_KEY,
            source_trust,
            len(errors),
            "; ".join(errors),
        )
    return records, errors


def _template_credential_errors(block, template_id: str) -> List[str]:
    """`credential_shape_errors()` for a catalog entry, logged once.

    Exactly one WARNING per malformed template, naming the id, so an operator
    can find the offender without diffing the catalog. The entry still lists —
    a broken `credentials:` block costs that template its credential metadata,
    not its place in the catalog.
    """
    errors = credential_shape_errors(block)
    if errors:
        logger.warning(
            "Template %s has a malformed `credentials:` block (%d problem(s)): %s",
            _sanitize_for_warning(template_id),
            len(errors),
            "; ".join(errors),
        )
    return errors


def _catalog_credential_metadata(data, template_id: str, source_trust: str):
    """`(requirements, errors)` for one catalog entry. Cannot raise. (ent#128)

    The belt over `normalize_credential_requirements`' own "never raises", and it
    is not redundant. `_build_template` (the GitHub builder — the untrusted path
    since ent#123) is called in BARE list comprehensions inside
    `get_all_templates()`, outside PR-A's per-template fence, which covers
    `_build_local_template` only. A raise there propagates to
    `routers/templates.py` as HTTP 500 with an EMPTY CATALOG: PR-A's exact bug,
    reopened by the change that surfaces the new metadata.

    Degrading here rather than fencing the comprehensions is deliberate — it keeps
    the NAMED error the resilience contract promises, instead of dropping the
    template silently.
    """
    try:
        requirements, errors = normalize_credential_requirements(
            data, source_trust=source_trust
        )
    except Exception:  # noqa: BLE001 — a 500 here is an empty catalog
        logger.exception(
            "Template %s: credential requirements could not be normalized",
            _sanitize_for_warning(template_id),
        )
        return [], [
            "credential_setup: could not be read; this template's credential "
            "setup metadata is unavailable"
        ]
    return requirements, errors


def _build_template(repo: str, metadata: dict, admin_override: dict = None) -> dict:
    """Build a full template dict from repo + fetched metadata + optional admin overrides.

    Priority for display_name / description:
      1. Admin-configured value (from Settings DB entry) — if non-empty
      2. template.yaml value (from GitHub) — if available
      3. Repo name fallback
    """
    # A GitHub repo's template.yaml is untrusted, and `_fetch_template_yaml`
    # returns `yaml.safe_load(content) or {}` — a top-level list or scalar is
    # truthy, so it comes back as a non-mapping and every `metadata.get()`
    # below raises AttributeError, 500ing the whole catalog for one bad repo.
    # `_build_local_template` has always had this guard; this path had none.
    # (trinity-enterprise#128)
    if not isinstance(metadata, dict):
        logger.warning(
            "Template metadata for %s is a %s, not a mapping — ignoring it",
            _sanitize_for_warning(repo),
            _type_name(metadata),
        )
        metadata = {}

    override = admin_override if isinstance(admin_override, dict) else {}

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

    # Computed BEFORE the dict literal, and through the non-raising wrapper: this
    # builder runs in bare list comprehensions in `get_all_templates()`, so a raise
    # inside the literal is an HTTP 500 and an empty catalog.
    credential_requirements, setup_errors = _catalog_credential_metadata(
        metadata, f"github:{repo}", "github"
    )

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
        # The template's own `mcp_servers:` is authoritative; `credentials:` is the
        # legacy fallback. W14: this builder had NO fallback at all, so a GitHub
        # template declaring only `credentials.mcp_servers` showed an empty list in
        # the catalog while its own Info tab listed them. All three surfaces
        # (catalog-local, catalog-github, agent `/api/template/info`) now agree.
        "mcp_servers": metadata.get("mcp_servers")
            or credential_mcp_server_names(metadata.get("credentials")),
        # Derived, never read from a top-level `required_credentials:` key. That
        # key is declared by ZERO templates anywhere — 25 bundled and all 7
        # configured GitHub repos — so the catalog's "N credentials" badge rendered
        # 0 for everything (Defect C). Derived unconditionally rather than
        # "explicit key wins, else derive": one code path, and the override branch
        # was dead. Platform-injected vars are excluded — see the accessor.
        "required_credentials": operator_supplied_credential_names(
            metadata.get("credentials")
        ),
        # Named errors for a malformed `credentials:` block, so a broken
        # declaration is reported against its own template instead of taking
        # the catalog down. Empty list = nothing wrong. Shape mirrors
        # `_build_local_template`. (trinity-enterprise#128)
        "credential_errors": _template_credential_errors(
            metadata.get("credentials"), f"github:{repo}"
        ) + setup_errors,
        # Per-variable setup metadata (ent#128 AC #2). Empty list when the template
        # declares nothing; `required == "unknown"` marks a record carrying no
        # authorial intent.
        "credential_requirements": credential_requirements,
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


#: Allowed shape for a local-template directory name — the `<name>` in a
#: `local:<name>` id, and the `name:` a template.yaml declares. A curated or
#: deploy-local template is always a single directory DIRECTLY under its root,
#: so one plain path segment is the entire legitimate space.
#:
#: Duplicated verbatim from `agent_service.crud._LOCAL_TEMPLATE_NAME_RE` (#950)
#: rather than imported, in BOTH directions:
#:   * crud → template_service is forbidden *for a security gate* (crud already
#:     imports this module for `generate_credential_files`, so the ban is about
#:     what may be *gated* on it, not about the import edge): the #1484
#:     characterization harness MagicMocks `services.template_service`, so a
#:     gate calling into it would be satisfied by a truthy mock and those tests
#:     would stay green on the OLD behaviour (the same reason
#:     `crud._repo_local_templates_dir` is hand-rolled, crud.py:73-87);
#:   * template_service → crud would close an import cycle (crud imports this
#:     module).
#: `tests/unit/test_1759_template_root_parity.py` pins the two patterns equal.
_LOCAL_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def contained_template_dir(name, root: Path) -> Optional[Path]:
    """Resolve `name` as a direct child of `root`, or `None` if it escapes.

    The two-step barrier the CREATE path has had since #950
    (`crud._safe_local_template_path`), brought to the READ path (#1900). It is
    two steps on purpose:

    1. **Name allowlist.** Rejects `..`, `/`, `\\`, a leading dot, an absolute
       path, an empty string and a non-string outright — before any path math.
       This is also what CodeQL recognises as a `py/path-injection` barrier;
       resolve + `is_relative_to` ALONE was flagged high-severity twice on this
       codebase (crud.py:206-210).
    2. **Containment.** `resolve()` BOTH sides, then `is_relative_to`. A plain
       `str(candidate).startswith(str(root))` is NOT equivalent — it passes the
       sibling escape `<root>-evil`. Resolving also closes the symlink escape,
       which step 1 cannot see.

    Both sides are resolved because a mismatched pair is wrong in *both*
    directions: an unresolved root with a resolved candidate rejects every
    LEGITIMATE name whenever the root sits under a symlinked prefix, and neither
    the prod bind mount nor the repo path is guaranteed symlink-free. Callers
    pass whatever `_local_templates_dir()` returns — unresolved. Defence in
    depth, and `crud._safe_cred_file_path` already resolves its root for the
    same reason.

    Returns the RESOLVED path, so the value used downstream is the value that
    was checked. Step 1 guarantees a single component, so the basename
    `_build_local_template` turns back into the `local:<name>` id is unchanged.

    This is deliberately PUBLIC: it is the one containment primitive for
    `local:` names in this module, and the remote-template-registry work
    (trinity-enterprise#14) edits this same resolver family — it should import
    this rather than copy it.
    """
    if not isinstance(name, str) or not name or ".." in name:
        return None
    if not _LOCAL_TEMPLATE_NAME_RE.match(name):
        return None
    root = root.resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


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
    except Exception as e:  # noqa: BLE001 — one bad file must not empty the catalog
        # Deliberately broader than `(OSError, yaml.YAMLError)`: deeply nested
        # YAML raises RecursionError, which is a RuntimeError and therefore
        # escapes a `yaml.YAMLError` handler entirely, 500ing the catalog.
        # (trinity-enterprise#128)
        logger.warning("Failed to parse local template %s: %s", template_dir.name, e)
        return None

    if not isinstance(data, dict):
        return None

    name = template_dir.name
    credentials_block = data.get("credentials")
    # `bundled` only when this really is the curated catalog root. The deploy-local
    # writable store (#950) is operator-uploaded and must not inherit our own
    # templates' trust label — trust selects the log level only today, but a wrong
    # label is exactly what gets read as a security boundary later.
    try:
        is_bundled = template_dir.resolve().parent == _local_templates_dir().resolve()
    except OSError:
        is_bundled = False
    credential_requirements, setup_errors = _catalog_credential_metadata(
        data, f"local:{name}", "bundled" if is_bundled else "deployed"
    )
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
        # Defect D: the operands were the other way round, so a `credentials:`
        # block silently OUTRANKED the template's own `mcp_servers:` declaration.
        # `agent_server/routers/info.py` has always read them in THIS order, so the
        # catalog and the agent's own Info tab disagreed for any template declaring
        # both.
        "mcp_servers": data.get("mcp_servers")
            or credential_mcp_server_names(credentials_block),
        "required_credentials": operator_supplied_credential_names(credentials_block),
        # Named errors for a malformed `credentials:` block. The template still
        # lists — it just loses its credential metadata, instead of taking
        # every other template down with an uncaught AttributeError.
        # (trinity-enterprise#128)
        "credential_errors": _template_credential_errors(
            credentials_block, f"local:{name}"
        ) + setup_errors,
        # Per-variable setup metadata (ent#128 AC #2).
        "credential_requirements": credential_requirements,
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
        # Last-resort per-template fence (trinity-enterprise#128). The tolerant
        # readers above are the real fix; this guarantees the property they buy
        # — one malformed template costs *itself*, never the catalog — survives
        # a future field being reached through without a guard.
        try:
            entry = _build_local_template(child)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Skipping local template %s: failed to build entry", child.name
            )
            continue
        if entry is not None and not entry.get("hidden"):
            out.append(entry)
    return out


def get_local_template(template_id: str) -> Optional[dict]:
    """Get a single local template by `local:<name>` id.

    `<name>` is caller-controlled and arrives from
    `GET /api/templates/{template_id:path}`, whose `:path` converter permits
    `/`. Unvalidated, the join escaped the templates root and disclosed any
    parseable `template.yaml` on the backend filesystem to any authenticated
    caller (#1900) — including other tenants' uploads under
    `/data/deployed-templates`.

    An invalid or escaping id returns `None`, so the router's 404 stays
    byte-identical to an unknown template: no error code, no path, no root name
    (the #1759 non-disclosure rule — this endpoint is the same enumeration
    oracle that rule exists to close).
    """
    if not template_id.startswith("local:"):
        return None
    name = template_id[len("local:"):]
    template_dir = contained_template_dir(name, _local_templates_dir())
    if template_dir is None:
        # DEBUG, not WARNING: this endpoint carries no rate limit, so a
        # per-rejection warning is an authenticated log-flood primitive, and the
        # id is attacker-supplied text landing in `platform.json`. Louder
        # security telemetry belongs in the audit log or behind a rate limit.
        logger.debug(
            "Rejected out-of-root local template id %s",
            _sanitize_for_warning(template_id),
        )
        return None
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

    # The shared detector charset, NOT uppercase-only: `${my_var}` is substituted
    # at runtime, so an uppercase-only finder here left a real reference invisible
    # to the deploy-time credential-gap warning while K-001/K-002 HARD-failed on
    # the same variable. See services/credential_charset.py. (ent#128)
    pattern = CREDENTIAL_DETECTOR_REF_RE
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

    # `yaml.safe_load("")` returns None and a scalar/list document returns a
    # non-mapping, so a bare `.get()` raises AttributeError. Route both levels
    # through the tolerant reader: this always answers with a mapping, which is
    # what its caller immediately `.get("mcp_servers", {})`s. (ent#128)
    return _credentials_mapping(
        (data if isinstance(data, dict) else {}).get("credentials")
    )


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
                    # `.strip()` before the anchored match is load-bearing — the
                    # validator uses `\Z`, so it would reject a trailing newline.
                    var_name = line.split('=')[0].strip()
                    if var_name and CREDENTIAL_DETECTOR_NAME_RE.match(var_name):
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

        for server_name, env_vars in credential_mcp_env_vars(template_creds).items():
            for var in env_vars:
                if var not in all_vars:
                    all_vars[var] = []
                # Test BOTH source spellings. The guard used to check `mcp:<s>`
                # while appending `template:mcp:<s>`, so it could only ever
                # suppress the cross-source duplicate and never its own —
                # harmless today because the loop visits each server once, but a
                # guard that does not cover what it appends is a trap for the
                # next caller. (ent#128)
                if (
                    f"mcp:{server_name}" not in all_vars[var]
                    and f"template:mcp:{server_name}" not in all_vars[var]
                ):
                    all_vars[var].append(f"template:mcp:{server_name}")

        env_file_vars = credential_env_file_names(template_creds)
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

    Raises `CredentialDeclarationError` when the template's `credentials:`
    block is structurally invalid. This path WRITES the agent's credential
    files, so — unlike the catalog readers, which degrade and keep listing —
    it must fail loud: silently emitting a `.env` built from a declaration
    nobody could parse is the failure mode this closes.
    (trinity-enterprise#128)
    """
    files = {}
    if not isinstance(template_data, dict):
        # A non-mapping template.yaml declares no credentials. Whether such a
        # template may create an agent at all is the resolver's call, not the
        # credential writer's — but it must not AttributeError here.
        logger.warning(
            "Template data for agent %s is a %s, not a mapping — generating no "
            "credential files",
            _sanitize_for_warning(str(agent_name)),
            _type_name(template_data),
        )
        return files

    credentials_block = template_data.get("credentials")
    errors = credential_shape_errors(credentials_block)
    if errors:
        raise CredentialDeclarationError(
            "Template `credentials:` block is invalid: " + "; ".join(errors)
        )

    creds_schema = _credentials_mapping(credentials_block)

    # Generate .mcp.json with real credentials
    mcp_servers_schema = creds_schema.get("mcp_servers", {})
    if mcp_servers_schema:
        if template_base_path:
            mcp_template_path = Path(template_base_path) / ".mcp.json"
        else:
            # #1900: this arm used to derive the directory by joining the
            # template.yaml's own `name:` field onto a hardcoded root, then read
            # the resulting `.mcp.json` INTO the new agent's credential files.
            # Two things were wrong with that. `name:` is untrusted — any
            # `creator` supplies one through `deploy_local_agent_logic` — so
            # `name: ../../data/deployed-templates/<victim>` read another
            # tenant's credential-bearing `.mcp.json` into the attacker's own
            # agent. And it is not a directory name at all: 5 shipped templates
            # declare a display string there ("Test Echo Agent").
            #
            # `crud` now threads the directory it already validated through
            # `_safe_local_template_path`, so the live path never derives one.
            # No caller supplies `template_base_path=None` with a real template
            # today (the `github:` branch never reaches this function — its
            # `template_data` stays empty and `_stage_config_files` guards on
            # it), so this arm exists for a future caller of a public function:
            # fail-closed and contained rather than trusting `name:`. The
            # containment helper also absorbs a non-string `name:`, which used
            # to raise TypeError out of agent creation as an uncaught 500.
            mcp_template_path = None
            fallback_root = contained_template_dir(
                template_data.get("name", ""), _local_templates_dir()
            )
            if fallback_root is not None:
                mcp_template_path = fallback_root / ".mcp.json"

        if mcp_template_path is not None and mcp_template_path.exists():
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

    # Generate .env file. Read through the tolerant accessor rather than
    # iterating the raw value: `for var_name in "OPENAI_API_KEY"` iterates a
    # bare string CHARACTER BY CHARACTER, writing `O=`, `P=`, `E=` ... and never
    # the real credential — with no error, no warning and no crash. The shape
    # check above now rejects that outright; this keeps the writer safe by
    # construction. (trinity-enterprise#128)
    env_vars = credential_env_file_names(creds_schema)
    if env_vars:
        env_lines = ["# Generated by Trinity - Agent credentials", ""]
        for var_name in env_vars:
            value = agent_credentials.get(var_name, "")
            env_lines.append(f"{var_name}={value}")
        files[".env"] = "\n".join(env_lines)

    # Generate config files from templates. `path` is validated by the shape
    # check above (relative, no `..`); crud.py re-checks containment at the
    # write sink.
    config_files = creds_schema.get("config_files") or []
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
