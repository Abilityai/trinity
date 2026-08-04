"""Remote template registry — the runtime source for the GitHub half of the
catalog (TMPL-002, trinity-enterprise#14).

WHAT THIS IS
------------
A `registry.yaml` fetched over HTTPS from a configurable endpoint, listing which
GitHub repos an install offers as agent templates. Curating the starter fleet
becomes a vendor file edit instead of a Trinity release.

WHY EVERY FAILURE IS SAFE (the structural argument, not the `except` branches)
-----------------------------------------------------------------------------
`template_service.get_all_templates()` returns `local + github`. `local` is read
from disk with no network and no registry involvement. `github` is EMPTY by
default (`DEFAULT_GITHUB_TEMPLATE_REPOS`, #1931). The registry can only ever
*add* to `github`, so every failure mode here — unreachable, 5xx, timeout,
malformed, alias-bombed, oversize, redirected, empty — reduces `github` toward
`[]`, which is the already-shipping default state of the product. There is no
path by which this module can make the catalog worse than a default install.

The belts below are a second layer, deliberately:
  * `get_registry_templates()` / `get_registry_overrides()` never raise;
  * their call site in `template_service` is ADDITIONALLY fenced, because
    fail-open must not depend on this module being bug-free;
  * each entry still passes through `_safe_build_github_template`, so #1835's
    "one bad entry costs itself, never the catalog" extends to registry entries.

THE ALLOWLIST IS THE BLAST-RADIUS BOUND
---------------------------------------
A registry entry is parsed into `RegistryTemplate` — exactly four fields — and
NEVER splatted into the template dict. Unknown keys are ignored, not merged, so
a registry cannot assert `fork_to_own`, `credentials`, `schedules`,
`data_paths`, `persistent_state`, `resources`, `skills`, `hidden` or `id`. Every
one of those is a claim about a repo the registry does not own and every one has
a creation-path consequence.

Say the next part plainly, because the comfortable version is false: `repo` is a
**capability pointer**. By choosing it, the registry chooses which
`template.yaml` Trinity fetches and trusts — and that document declares all the
fields the allowlist just refused. The allowlist bounds the DIRECT blast radius
to display and order. It does not bound the indirect one. (Same distinction
ent#123 draws for tokenless public clones: the platform trusts a repo it did not
author.)

IMPORT DISCIPLINE
-----------------
Module level: `config`, `utils.safe_yaml`, `utils.url_validation`, `httpx`.
`settings_service` is imported lazily inside functions because it imports
`database`. Nothing here imports `template_service` — the dependency runs one
way only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from utils.safe_yaml import (
    HardenedYamlError,
    TEMPLATE_REGISTRY_MAX_BYTES,
    load_template_registry_yaml,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Bounds
# ============================================================================

#: Schema major version this parser understands. An unknown/greater version
#: REFUSES the whole document rather than mis-reading a future shape — the same
#: direction-of-failure choice the tolerant readers make everywhere else.
SUPPORTED_SCHEMA_VERSION = 1

#: Bumped when the parse semantics change, so a durable last-known-good written
#: by an older parser is discarded rather than reinterpreted.
PARSER_VERSION = 1

#: Semantic bound. NOT a target — a real vendor registry holds 3–12. This is the
#: guard against a hostile or typo'd registry minting a 5000-entry catalog, and
#: it is sized against the GitHub metadata budget: each listed repo costs one
#: `template.yaml` fetch per per-repo cache window per worker, and GitHub's
#: ANONYMOUS limit is 60 req/hr per IP. See requirements §4.2.2.
MAX_REGISTRY_TEMPLATES = 25

#: DoS bound on the document itself. Enforced twice — at the transport, where it
#: is load-bearing, and again at the parser as a belt (see `_fetch_registry_text`).
REGISTRY_MAX_BYTES = TEMPLATE_REGISTRY_MAX_BYTES

#: These strings land in the catalog response, the platform log and the DOM.
#: Truncated rather than rejected, so an over-long field costs itself and the
#: entry stays useful. XSS is covered by Vue interpolation (never `v-html`);
#: these caps cover "a 10 MB description in every catalog response".
MAX_DISPLAY_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 1000

#: Bounds the status payload an operator reads. A hostile registry must not be
#: able to make `GET /api/settings/template-registry` enormous by shipping
#: 25 000 malformed entries.
MAX_REPORTED_ERRORS = 50

#: Cap on any foreign string interpolated into an error message. The errors are
#: persisted in memory and serialized to an admin; an unbounded `repo:` value
#: would otherwise ride along.
_ERROR_VALUE_SNIPPET = 80

#: One hour, DELIBERATELY NOT aligned with `template_service._CACHE_TTL` (600 s).
#: Aligning them is not a shared rhythm, it is a correlated thundering herd: on
#: expiry a worker fires the registry fetch AND N per-repo GitHub fetches in the
#: same instant, and with `--workers 2` both workers drift into phase. A registry
#: is an index that changes on a human's git commit; it does not need 10-minute
#: propagation. The longer TTL is also the cheapest lever on the GitHub budget
#: above — it is doing real security work here, not tidiness.
REGISTRY_CACHE_TTL = 3600.0
#: Decorrelates worker refresh instants (the #1085 jitter precedent).
REGISTRY_CACHE_JITTER = 300.0

#: A failed fetch with no prior good parse is remembered this long, so a dead URL
#: costs one bounded request per minute per worker rather than one per catalog load.
REGISTRY_NEGATIVE_TTL = 60.0

#: Serve-stale ceiling. Unbounded stale is NOT safe: the registry is a trust
#: pointer to repos, so an unbounded stale copy keeps a de-curated, renamed or
#: compromised repo in the product indefinitely while the operator sees nothing
#: wrong — the catalog still renders. Past this we drop to the bundled floor,
#: which is the documented contract.
REGISTRY_MAX_STALE_SECONDS = 7 * 24 * 3600.0

#: `operator_intake_service`'s value. `get_all_templates()` is sync and called
#: from an async route, so this blocks the event loop; the negative cache bounds
#: the damage to one 5 s worst case per minute per worker.
REGISTRY_FETCH_TIMEOUT = 5.0

#: Fourth copy of the owner/repo gate (`routers/settings._REPO_PATTERN`,
#: `agent_service.crud._GITHUB_REPO_PATH_RE`, `Settings.vue REPO_PATTERN`).
#: Duplicated rather than imported, which is this codebase's documented
#: convention for a shared gate that must not create an import edge (a service
#: importing a router violates Invariant #1) — and it carries that convention's
#: obligation: `tests/unit/test_ent14_repo_pattern_parity.py` compares BEHAVIOUR
#: on a fixture corpus, not source strings, because the existing copies already
#: differ in character-class ordering while denoting the same set.
#:
#: It matters because `repo` is interpolated into
#: `https://api.github.com/repos/{repo}/contents/template.yaml` AND into the
#: template id `github:{repo}` a user can hand to agent creation.
_REGISTRY_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: Path segments the pattern above admits and a URL parser does NOT treat as
#: literal. `.` is inside the character class, so `../evil` MATCHES all four
#: copies of this regex — verified, not assumed. That breaks the one invariant
#: the catalog card rests on: `github:../evil` renders "../evil" under the
#: display name while `https://api.github.com/repos/../evil/contents/…` and
#: `https://github.com/../evil.git` both normalize to a DIFFERENT repo, so the
#: card would advertise one path and clone another.
#:
#: Exactly `.` and `..` — those are the only two segments URL normalization
#: rewrites; `...` is a literal directory and needs no special case.
#:
#: The registry gate is therefore deliberately STRICTER than its three siblings.
#: They share the hole (it predates this work and is reachable only by an
#: authenticated `creator` typing the id by hand, so it is filed rather than
#: fixed here); this copy guards an untrusted document fetched from the network,
#: which is a different threat model. The parity test asserts the containment
#: that actually matters — everything this gate ACCEPTS, the siblings accept —
#: and pins the divergence explicitly rather than letting it drift.
_TRAVERSAL_SEGMENTS = frozenset({".", ".."})


def _is_valid_repo(repo: str) -> bool:
    """Charset gate plus an explicit dot-segment refusal."""
    if not _REGISTRY_REPO_RE.match(repo):
        return False
    owner, _, name = repo.partition("/")
    return owner not in _TRAVERSAL_SEGMENTS and name not in _TRAVERSAL_SEGMENTS

#: Fixed status vocabulary. NEVER a raw exception string — a hostile server's
#: response text must not reach the operator's settings panel.
ERROR_DISABLED = "disabled"
ERROR_INVALID_URL = "invalid_url"
ERROR_UNREACHABLE = "unreachable"
ERROR_TIMEOUT = "timeout"
ERROR_HTTP = "http_error"
ERROR_REDIRECT = "redirect"
ERROR_TOO_LARGE = "too_large"
ERROR_PARSE_REFUSED = "parse_refused"
ERROR_UNSUPPORTED_VERSION = "unsupported_version"
ERROR_BAD_SHAPE = "bad_shape"


# ============================================================================
# Records
# ============================================================================

@dataclass(frozen=True)
class RegistryTemplate:
    """One registry entry, allowlisted to exactly four fields.

    Frozen so a downstream consumer cannot smuggle a fifth field back in by
    mutating a shared instance out of the process-wide cache.
    """

    repo: str
    display_name: str
    description: str
    priority: Optional[int]

    def as_override(self) -> dict:
        """The `admin_override` dict `_build_template` already understands.

        Reusing that shape is why the registry needs no new builder, no new
        template `source`, and no catalog payload change — registry templates
        ARE `github:` templates. It also hardens the existing path: a
        registry-supplied display name renders the card correctly even when the
        per-repo GitHub metadata fetch is rate-limited.
        """
        return {
            "github_repo": self.repo,
            "display_name": self.display_name,
            "description": self.description,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class ParsedRegistry:
    """Outcome of parsing one document. Never raised — always returned.

    `ok=False` means the WHOLE document was refused (`error_code` says why) and
    the caller degrades to the floor. `ok=True` with `entries=()` is a
    SUCCESS — an empty registry is a legitimate, deliberate state (it is what
    the ship prerequisite publishes on day one), and conflating it with failure
    would make an operator's intentional empty catalog look like an outage.
    """

    ok: bool
    entries: tuple[RegistryTemplate, ...] = ()
    error_code: Optional[str] = None
    errors: tuple[str, ...] = ()


@dataclass
class _CacheEntry:
    entries: tuple[RegistryTemplate, ...]
    good: bool
    fetched_at: float           # epoch of the last SUCCESSFUL parse (0 = never)
    checked_at: float           # epoch of the last attempt, successful or not
    next_check_at: float
    source_url: str
    generation: int
    status: dict


_cache: Optional[_CacheEntry] = None
#: `get_all_templates()` is sync, so FastAPI runs it in a threadpool — several
#: threads of ONE worker can reach this concurrently. The lock makes cache reads
#: and writes atomic and collapses a concurrent miss into a single fetch. Held
#: across the network call on purpose: blocking a sibling thread for one bounded
#: 5 s fetch is strictly better than N simultaneous fetches of the same document.
_cache_lock = threading.RLock()


# ============================================================================
# Parsing — pure, tolerant, network-free
# ============================================================================

def _short(value: Any) -> str:
    """A bounded, type-safe rendering of untrusted input for an error message.

    NEVER `str()` on the raw container. `template_service._clean_field`
    documents why: `str()` on a value from untrusted YAML walks the graph and
    pays the amplification cost BEFORE any cap can act. Moot under
    `AliasPolicy.REJECT` — kept as discipline, because the next document may not
    get REJECT.
    """
    if isinstance(value, str):
        text = value
    else:
        return type(value).__name__
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > _ERROR_VALUE_SNIPPET:
        return text[:_ERROR_VALUE_SNIPPET] + "…"
    return text


def _clean_text(value: Any, cap: int) -> Optional[str]:
    """Type-guard first, then truncate. Returns None for a non-string."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:cap]


def parse_registry_document(text: str) -> ParsedRegistry:
    """Parse a registry document. Pure, never raises, no network.

    Document-level refusals (`ok=False`) vs per-entry drops is the ent#128 /
    ent#89 tolerant-reader contract: a structurally wrong document tells us
    nothing trustworthy, so nothing from it is used; a single malformed entry
    costs only itself.
    """
    errors: list[str] = []

    try:
        data = load_template_registry_yaml(text)
    except HardenedYamlError as e:
        code = (
            ERROR_TOO_LARGE
            if e.code == "template_registry_too_large"
            else ERROR_PARSE_REFUSED
        )
        # `e.code`, never `str(e)`: the hardened loader's message can quote the
        # document, and this string reaches an operator panel.
        return ParsedRegistry(ok=False, error_code=code, errors=(e.code,))
    except Exception:  # noqa: BLE001 — a parser bug must still fail open
        logger.exception("[template-registry] unexpected parse failure")
        return ParsedRegistry(ok=False, error_code=ERROR_PARSE_REFUSED)

    if not isinstance(data, dict):
        return ParsedRegistry(
            ok=False,
            error_code=ERROR_BAD_SHAPE,
            errors=(f"registry document is a {type(data).__name__}, not a mapping",),
        )

    # Absent ⇒ 1. Anything unknown, non-int, or boolean refuses the document.
    raw_version = data.get("version", SUPPORTED_SCHEMA_VERSION)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        return ParsedRegistry(
            ok=False,
            error_code=ERROR_UNSUPPORTED_VERSION,
            errors=(f"version must be an integer, got {type(raw_version).__name__}",),
        )
    if raw_version != SUPPORTED_SCHEMA_VERSION:
        return ParsedRegistry(
            ok=False,
            error_code=ERROR_UNSUPPORTED_VERSION,
            errors=(
                f"registry schema version {raw_version} is not supported by this "
                f"Trinity (expected {SUPPORTED_SCHEMA_VERSION})",
            ),
        )

    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list):
        return ParsedRegistry(
            ok=False,
            error_code=ERROR_BAD_SHAPE,
            errors=(
                "templates must be a list, got "
                f"{type(raw_templates).__name__}",
            ),
        )

    considered = raw_templates
    if len(raw_templates) > MAX_REGISTRY_TEMPLATES:
        # Truncate + name it, the `MAX_DECLARED_SCHEDULES` precedent — a
        # too-long registry is still mostly usable, unlike a malformed one.
        errors.append(
            f"registry declares {len(raw_templates)} templates; only the first "
            f"{MAX_REGISTRY_TEMPLATES} are used"
        )
        considered = raw_templates[:MAX_REGISTRY_TEMPLATES]

    entries: list[RegistryTemplate] = []
    seen: set[str] = set()

    for index, raw_entry in enumerate(considered):
        if not isinstance(raw_entry, dict):
            errors.append(
                f"entry {index}: expected a mapping, got {type(raw_entry).__name__}"
            )
            continue

        raw_repo = raw_entry.get("repo")
        if not isinstance(raw_repo, str):
            errors.append(
                f"entry {index}: `repo` must be a string, got "
                f"{type(raw_repo).__name__}"
            )
            continue
        repo = raw_repo.strip()
        if not _is_valid_repo(repo):
            errors.append(
                f"entry {index}: `repo` {_short(repo)!r} is not a valid owner/repo"
            )
            continue

        # Case-insensitive: `Owner/Repo` and `owner/repo` are one GitHub repo but
        # two different template ids and two different metadata-cache keys, so
        # admitting both would show one repo as two cards.
        key = repo.lower()
        if key in seen:
            errors.append(f"entry {index}: duplicate repo {_short(repo)!r}; ignored")
            continue
        seen.add(key)

        display_name = _clean_text(raw_entry.get("display_name"), MAX_DISPLAY_NAME_LEN)
        if display_name is None and raw_entry.get("display_name") is not None:
            errors.append(f"entry {index} ({repo}): `display_name` is not a string")
        description = _clean_text(raw_entry.get("description"), MAX_DESCRIPTION_LEN)
        if description is None and raw_entry.get("description") is not None:
            errors.append(f"entry {index} ({repo}): `description` is not a string")

        raw_priority = raw_entry.get("priority")
        if isinstance(raw_priority, bool) or not isinstance(raw_priority, int):
            # `bool` excluded for `_coerce_priority`'s own reason:
            # `isinstance(True, int)` is True, so `priority: true` would sort as 1.
            if raw_priority is not None:
                errors.append(f"entry {index} ({repo}): `priority` is not an integer")
            priority = None
        else:
            priority = raw_priority

        entries.append(
            RegistryTemplate(
                repo=repo,
                display_name=display_name or "",
                description=description or "",
                priority=priority,
            )
        )

    if len(errors) > MAX_REPORTED_ERRORS:
        extra = len(errors) - MAX_REPORTED_ERRORS
        errors = errors[:MAX_REPORTED_ERRORS] + [f"… and {extra} more problems"]

    return ParsedRegistry(ok=True, entries=tuple(entries), errors=tuple(errors))


# ============================================================================
# Transport
# ============================================================================

def _http_client(timeout: float) -> httpx.Client:
    """Registry HTTP client. Seam for tests (`httpx.MockTransport`).

    `follow_redirects=False` is a security control, not a default: a URL that
    passed the SSRF gate and then redirects is an SSRF bypass, and
    `raw.githubusercontent.com` does not redirect for a valid path. A redirect is
    therefore a fetch FAILURE that degrades to the floor. Re-validating every hop
    (the #1932/#1951 two-tier shape) is more machinery than this needs.
    """
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _fetch_registry_text(url: str) -> tuple[Optional[str], Optional[str]]:
    """Stream the document under a hard byte ceiling. Returns `(text, error_code)`.

    The ceiling is applied to bytes ACTUALLY RECEIVED, not to `Content-Length`.
    A declared length is checked first purely as an early abort — it is absent on
    chunked responses and trivially lied about, so it can never be the gate.
    `resp.text` on a 10 GB body OOMs the worker before any parse-time cap could
    act; that is why the transport layer is the load-bearing one and the parser's
    `max_bytes` is only a belt.
    """
    try:
        with _http_client(REGISTRY_FETCH_TIMEOUT) as client:
            with client.stream(
                "GET",
                url,
                headers={
                    "Accept": "text/yaml, application/yaml, text/plain;q=0.9, */*;q=0.1",
                    "User-Agent": "Trinity-Template-Registry/1",
                },
            ) as resp:
                if 300 <= resp.status_code < 400:
                    return None, ERROR_REDIRECT
                if resp.status_code != 200:
                    return None, ERROR_HTTP

                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > REGISTRY_MAX_BYTES:
                    return None, ERROR_TOO_LARGE

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > REGISTRY_MAX_BYTES:
                        # Abort mid-stream: do not finish reading a body we have
                        # already decided to refuse.
                        return None, ERROR_TOO_LARGE
                    chunks.append(chunk)

        raw = b"".join(chunks)
    except httpx.TimeoutException:
        return None, ERROR_TIMEOUT
    except httpx.HTTPError:
        return None, ERROR_UNREACHABLE
    except Exception:  # noqa: BLE001 — the fetch may never take the catalog down
        logger.warning("[template-registry] unexpected fetch failure", exc_info=True)
        return None, ERROR_UNREACHABLE

    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        # A binary body (or an HTML captive-portal page in a foreign encoding) is
        # not a registry. `bad_shape`, not `parse_refused`: nothing was parsed.
        return None, ERROR_BAD_SHAPE


# ============================================================================
# Durable last-known-good
# ============================================================================

def _entries_fingerprint(entries: tuple[RegistryTemplate, ...]) -> str:
    payload = [
        [e.repo, e.display_name, e.description, e.priority] for e in entries
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _persist_lkg(url: str, entries: tuple[RegistryTemplate, ...]) -> None:
    """Store the sanitized PARSED registry — never the raw YAML.

    Raw YAML in the database would mean re-parsing an untrusted document out of
    our own storage on a path where the network guards no longer apply.

    Written ONLY when the normalized content changes, so a steady-state install
    that refetches an unchanged registry every hour writes no rows at all.
    """
    from services.settings_service import settings_service

    fingerprint = _entries_fingerprint(entries)
    try:
        existing = settings_service.get_template_registry_lkg() or {}
        if (
            existing.get("sha256") == fingerprint
            and existing.get("source_url") == url
            and existing.get("parser_version") == PARSER_VERSION
        ):
            return
        settings_service.set_template_registry_lkg(
            {
                "source_url": url,
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "fetched_at": time.time(),
                "sha256": fingerprint,
                "entries": [
                    {
                        "repo": e.repo,
                        "display_name": e.display_name,
                        "description": e.description,
                        "priority": e.priority,
                    }
                    for e in entries
                ],
            }
        )
    except Exception:  # noqa: BLE001 — persistence is an optimisation, never a gate
        logger.warning("[template-registry] could not persist last-known-good", exc_info=True)


def _load_lkg(url: str, now: float) -> Optional[tuple[tuple[RegistryTemplate, ...], float]]:
    """Load a still-valid durable last-known-good for `url`, or None.

    Invalidated by a URL change, a parser-version bump, or the same max-stale cap
    the in-memory copy obeys. Entries are re-validated on load rather than
    trusted: the row is ours, but it is reachable through the database and a
    validate-on-write-only contract is one bad migration away from being wrong.
    """
    from services.settings_service import settings_service

    stored = settings_service.get_template_registry_lkg()
    if not stored:
        return None
    if stored.get("source_url") != url:
        return None
    if stored.get("parser_version") != PARSER_VERSION:
        return None
    fetched_at = stored.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if now - float(fetched_at) > REGISTRY_MAX_STALE_SECONDS:
        return None

    raw_entries = stored.get("entries")
    if not isinstance(raw_entries, list):
        return None

    entries: list[RegistryTemplate] = []
    for raw in raw_entries[:MAX_REGISTRY_TEMPLATES]:
        if not isinstance(raw, dict):
            continue
        repo = raw.get("repo")
        if not isinstance(repo, str) or not _is_valid_repo(repo):
            continue
        priority = raw.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = None
        entries.append(
            RegistryTemplate(
                repo=repo,
                display_name=_clean_text(raw.get("display_name"), MAX_DISPLAY_NAME_LEN) or "",
                description=_clean_text(raw.get("description"), MAX_DESCRIPTION_LEN) or "",
                priority=priority,
            )
        )
    return tuple(entries), float(fetched_at)


# ============================================================================
# Resolution
# ============================================================================

def _iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _status(
    *,
    last_status: str,
    fetched_at: Optional[float],
    error_code: Optional[str],
    template_count: int,
    stale: bool,
    errors: tuple[str, ...] = (),
) -> dict:
    return {
        "last_fetch_at": _iso(fetched_at),
        "last_status": last_status,
        "last_error_code": error_code,
        "template_count": template_count,
        "stale": stale,
        "errors": list(errors),
    }


def _next_check(now: float) -> float:
    return now + REGISTRY_CACHE_TTL + random.uniform(0.0, REGISTRY_CACHE_JITTER)


def _resolve() -> _CacheEntry:
    """Return the current registry state, fetching only when due. Never raises."""
    global _cache

    from services.settings_service import settings_service

    now = time.time()

    with _cache_lock:
        try:
            enabled = settings_service.is_template_registry_enabled()
        except Exception:  # noqa: BLE001
            enabled = True
        if not enabled:
            # Do NOT clear the cache: re-enabling should not force a cold fetch,
            # and the stale cap still governs whatever was held.
            return _CacheEntry(
                entries=(),
                good=False,
                fetched_at=0.0,
                checked_at=now,
                next_check_at=now,
                source_url="",
                generation=0,
                status=_status(
                    last_status="disabled",
                    fetched_at=None,
                    error_code=ERROR_DISABLED,
                    template_count=0,
                    stale=False,
                ),
            )

        try:
            url = settings_service.get_template_registry_url()
            generation = settings_service.get_template_registry_generation()
        except Exception:  # noqa: BLE001
            logger.warning("[template-registry] settings unreadable", exc_info=True)
            return _CacheEntry(
                entries=(), good=False, fetched_at=0.0, checked_at=now,
                next_check_at=now + REGISTRY_NEGATIVE_TTL, source_url="",
                generation=0,
                status=_status(
                    last_status="failed", fetched_at=None,
                    error_code=ERROR_INVALID_URL, template_count=0, stale=False,
                ),
            )

        cached = _cache
        # A cached entry is only usable if it was built from the SAME url under
        # the SAME generation. The generation is what makes invalidation reach
        # the other uvicorn worker, which a per-process clear cannot.
        if cached and (cached.source_url != url or cached.generation != generation):
            cached = None

        if cached and now < cached.next_check_at:
            return cached

        # --- refetch -------------------------------------------------------
        from utils.url_validation import validate_template_registry_url

        try:
            validated = validate_template_registry_url(url)
            text, error_code = _fetch_registry_text(validated)
        except ValueError:
            # The stored URL no longer validates (or never did — an operator can
            # set TEMPLATE_REGISTRY_URL in env, which bypasses the endpoint).
            text, error_code = None, ERROR_INVALID_URL
        except Exception:  # noqa: BLE001
            logger.warning("[template-registry] fetch aborted", exc_info=True)
            text, error_code = None, ERROR_UNREACHABLE

        if text is not None:
            parsed = parse_registry_document(text)
            if parsed.ok:
                entry = _CacheEntry(
                    entries=parsed.entries,
                    good=True,
                    fetched_at=now,
                    checked_at=now,
                    next_check_at=_next_check(now),
                    source_url=url,
                    generation=generation,
                    status=_status(
                        last_status="ok",
                        fetched_at=now,
                        error_code=None,
                        template_count=len(parsed.entries),
                        stale=False,
                        errors=parsed.errors,
                    ),
                )
                _cache = entry
                _persist_lkg(url, parsed.entries)
                if parsed.errors:
                    logger.warning(
                        "[template-registry] %d entries accepted with %d problems",
                        len(parsed.entries),
                        len(parsed.errors),
                    )
                return entry
            error_code = parsed.error_code or ERROR_BAD_SHAPE
            document_errors = parsed.errors
        else:
            document_errors = ()

        # --- failure: serve stale, then durable LKG, then the floor ---------
        logger.warning(
            "[template-registry] fetch/parse failed (%s); the template catalog "
            "degrades to its bundled floor",
            error_code,
        )

        if (
            cached
            and cached.good
            and (now - cached.fetched_at) <= REGISTRY_MAX_STALE_SECONDS
        ):
            stale_entry = replace(
                cached,
                checked_at=now,
                next_check_at=now + REGISTRY_NEGATIVE_TTL,
                status=_status(
                    last_status="failed",
                    fetched_at=cached.fetched_at,
                    error_code=error_code,
                    template_count=len(cached.entries),
                    stale=True,
                    errors=document_errors,
                ),
            )
            _cache = stale_entry
            return stale_entry

        try:
            lkg = _load_lkg(url, now)
        except Exception:  # noqa: BLE001
            lkg = None
        if lkg is not None:
            lkg_entries, lkg_fetched_at = lkg
            entry = _CacheEntry(
                entries=lkg_entries,
                good=True,
                fetched_at=lkg_fetched_at,
                checked_at=now,
                next_check_at=now + REGISTRY_NEGATIVE_TTL,
                source_url=url,
                generation=generation,
                status=_status(
                    last_status="failed",
                    fetched_at=lkg_fetched_at,
                    error_code=error_code,
                    template_count=len(lkg_entries),
                    stale=True,
                    errors=document_errors,
                ),
            )
            _cache = entry
            return entry

        entry = _CacheEntry(
            entries=(),
            good=False,
            fetched_at=0.0,
            checked_at=now,
            next_check_at=now + REGISTRY_NEGATIVE_TTL,
            source_url=url,
            generation=generation,
            status=_status(
                last_status="failed",
                fetched_at=None,
                error_code=error_code,
                template_count=0,
                stale=False,
                errors=document_errors,
            ),
        )
        _cache = entry
        return entry


# ============================================================================
# Public API — none of these raise
# ============================================================================

def get_registry_templates() -> list[RegistryTemplate]:
    """Registry entries, or `[]` for every failure mode. Never raises."""
    try:
        return list(_resolve().entries)
    except Exception:  # noqa: BLE001 — the seam's own contract
        logger.exception("[template-registry] resolution failed; degrading to floor")
        return []


def get_registry_overrides() -> list[dict]:
    """Registry entries as `admin_override` dicts, ready for `_build_template`."""
    return [t.as_override() for t in get_registry_templates()]


def get_registry_status() -> dict:
    """Operator-facing status. Resolves through the same cache as the catalog.

    It can therefore trigger a fetch — but only when one was due anyway, so an
    admin opening the panel costs no more than a user listing templates. That is
    deliberate: a panel that reports `never` until somebody else happens to load
    the catalog is a panel an operator cannot use to debug their registry, which
    is the whole reason this status exists (ent#236's "the panel must be able to
    show a *failing* auto-sync").
    """
    try:
        return dict(_resolve().status)
    except Exception:  # noqa: BLE001
        logger.exception("[template-registry] status unavailable")
        return _status(
            last_status="failed",
            fetched_at=None,
            error_code=ERROR_UNREACHABLE,
            template_count=0,
            stale=False,
        )


def invalidate_registry_cache() -> None:
    """Drop this process's cached entry.

    A convenience for the calling worker only — cross-worker invalidation is the
    generation counter's job, and this is NOT a substitute for bumping it.
    """
    global _cache
    with _cache_lock:
        _cache = None
