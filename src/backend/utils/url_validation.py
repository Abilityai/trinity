"""
URL validation utilities for SSRF prevention.

Provides strict validation for URLs that the backend will connect to,
ensuring they point to allowed external hosts and not internal services.

Related: SEC-179 (pentest finding 3.2.2)
"""

import ipaddress
import re
import socket
from typing import List
from urllib.parse import urlparse

# Allowed hostnames for skills library URLs
ALLOWED_SKILLS_LIBRARY_HOSTS = {"github.com", "www.github.com"}

# Scheme assumed when the caller's URL has none (`owner/repo`,
# `tok@github.com/o/r`). Only ever used to make `urlparse` see an authority —
# never emitted into the return value.
_ASSUMED_SCHEME = "https://"

# --- Shared authority grammar (#2052) --------------------------------------
# ONE definition of where an authority's userinfo ends, used by the single-URL
# fallback below AND by the free-text scrubber `scrub_url_credentials_in_text`.
#
# #2052 was exactly these two drifting: the free-text pattern was anchored on a
# literal `https://`, so the protocol-relative, scheme-less and alternate-scheme
# shapes `strip_url_credentials` handles slipped past it and leaked a token into
# `system_settings['skills_library_last_error']` — durable, admin-rendered.
# Those shapes are reachable because `_adopt_legacy_clone` writes source rows
# with no validation at all and `validate_skills_library_url` accepts the
# scheme-less shorthand, so a *stored* URL is legitimately allowed to take them.

# RFC 3986 §3.1 scheme, plus the `//` that opens an authority. Optional scheme
# so a protocol-relative `//tok@host` is covered by the same fragment.
_AUTHORITY_PREFIX = r"(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//"

# Fallback for input `urlparse` refuses (see `strip_url_credentials`). Anchored
# at the authority so a `@` in a path or query is out of reach, and `[^/]*` is
# greedy so the LAST `@` before the path wins — the double-`@` case.
_AUTHORITY_USERINFO_RE = re.compile(rf"^({_AUTHORITY_PREFIX})[^/]*@")

# A maximal run of characters that could sit inside an authority. `\s/?#` end a
# run because none is legal unencoded in userinfo (RFC 3986) — which is also
# what stops one run bridging two URLs in the same stderr blob.
#
# The free-text scrub walks these runs rather than matching userinfo directly,
# and that is a PERFORMANCE contract, not a style choice. The obvious pattern —
# `(?:scheme:)?//[^\s/?#]*@` with a bare alternative — has no literal for the
# engine to skip to, so on a long run containing no `@` it rescans from every
# offset: 200 KB of one run took 24 SECONDS, 500 KB took 151. The input here is
# raw `git stderr`, unbounded and attacker-influenced (a remote controls branch
# and path names it echoes), and the old pattern only escaped this by being
# anchored on a literal `https://` that `str.find` could jump to — the very
# anchor #2052 removes. `finditer` over this run pattern consumes each character
# once, so the scrub is linear no matter what the blob contains.
_AUTHORITY_RUN_RE = re.compile(r"[^\s/?#]+")

# Characters that may precede a BARE `tok@host` run (no scheme, no `//`) for it
# to still be an authority. Without this the scrub reads the `@` in
# `https://github.com/o/r?ref=a@b` as userinfo and mangles a legitimate URL —
# the removed `https://` anchor was also what kept the old pattern off a query
# `@`. Quotes/parens/angles/brackets count because git stderr habitually wraps a
# URL in them (`for 'https://…'`); start-of-string counts too.
_BARE_RUN_BOUNDARY = frozenset("'\"(<[")

# What replaces the userinfo in free text. Unlike `strip_url_credentials`, which
# DROPS userinfo for display, the free-text scrub leaves a marker: an operator
# reading a scrubbed git error needs to see that a credential was present and
# redacted, not a URL that silently never had one.
_USERINFO_PLACEHOLDER = "***"


class EmbeddedCredentialError(ValueError):
    """A URL carries userinfo (`https://<token>@host/...`)."""


def reject_embedded_credentials(url: str) -> None:
    """Refuse a URL embedding a token or password.

    `validate_skills_library_url` checks `parsed.hostname`, which IGNORES
    userinfo, and returns the URL unchanged — so a tokenized clone URL passes
    SSRF validation and is then persisted verbatim wherever the caller stores
    it. Pasting one is an easy mistake: it is the form GitHub hands you for
    scripted clones.

    Deliberately NOT folded into `validate_skills_library_url`: that helper
    serves the pre-ent#237 `skills_library_url` setting too, and an install
    relying on an embedded token for private-repo access would break on
    upgrade. Callers opt in on new surfaces.

    Lives here rather than in the router so it is importable without pulling in
    the whole `routers` package (which drags in the agent-service chain).
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.username or parsed.password:
        raise EmbeddedCredentialError(
            "Repository URL must not embed a token or password. It is stored "
            "and displayed in plain text. Configure a GitHub PAT in Settings "
            "for private repositories instead."
        )


def strip_url_credentials(url: str) -> str:
    """Return `url` with any userinfo removed from its authority. Never raises.

    The display counterpart of `reject_embedded_credentials`, which lives here
    for the same reason: this module already owns what "userinfo" means for
    Trinity's repo URLs, and both answers must move together. Rejection only
    guards NEW writes — rows persisted before it existed, and rows written by
    paths that do not validate at all, still carry `https://<token>@host/...`.
    Anything that renders or returns a stored URL has to strip.

    **It never raises**, by contract, not by accident. `urlparse` itself
    raises `ValueError: Invalid IPv6 URL` on an unbalanced bracket
    (`https://[oops/repo`), and malformed URLs are genuinely reachable — the
    legacy-adoption path writes a source row with no validation at all. The
    caller is `get_library_status`, whose own comments say status must never
    500 the panel; a scrubber that can throw would turn a cosmetic bad row
    into an outage, and the tempting local `try/except` around each call site
    is exactly how one gets forgotten. Any parse failure falls back to an
    authority-anchored textual scrub and, failing even that, returns the input.

    **It is parse-based, not a regex**, following the house rule set by
    `skill_service._authenticated_url`: the host is decided by PARSING, never
    by substring. Splitting the authority off textually is how the two
    pre-existing scrubbers got `https://a@b@github.com/o/r` wrong (`[^@]+@`
    cannot cross the first `@`, so the second credential survived), and a
    looser pattern mangles a legitimate `https://github.com/o/r?ref=a@b`,
    where the `@` is in the query and means nothing. `urlparse` puts the
    authority in `netloc`, so both questions answer themselves.

    The caller's shape is preserved: a scheme-less input (`tok@github.com/o/r`,
    the shorthand `validate_skills_library_url` accepts) is parsed with an
    assumed scheme so `netloc` is populated, but no scheme is invented into the
    output.
    """
    try:
        text = url if isinstance(url, str) else str(url or "")
        stripped = text.strip()
        if not stripped:
            return text

        # A leading `//` is protocol-relative: RFC 3986 says the authority is
        # already there, so it must NOT get the assumed scheme prepended.
        # Doing so yields `https:////tok@host`, whose netloc parses EMPTY —
        # the `@`-in-netloc guard below then reads it as credential-free and
        # returns the token verbatim. This is one of the shapes the frontend
        # `stripUserinfo` enumerates; the two must agree on it.
        had_authority = "://" in stripped or stripped.startswith("//")
        parsed = urlparse(stripped if had_authority else _ASSUMED_SCHEME + stripped)

        # `@` in the authority IS userinfo (RFC 3986 `[userinfo@]host[:port]`),
        # and this catches the empty-username shape `https://@host/x` that a
        # `parsed.username` test reads as falsy and leaves alone.
        if "@" not in parsed.netloc:
            return text

        # rsplit, not split: the authority ends at the LAST `@`, which is also
        # how `urlparse` itself resolves `.hostname` (it rpartitions). Keeps
        # `https://PAT@LEGACYTOK@github.com/o/r` from leaking `LEGACYTOK`.
        host_only = parsed.netloc.rsplit("@", 1)[-1]

        # `_replace(netloc=None)` would blow up in `geturl()`; a userinfo with
        # no host (`https://tok@/x`) legitimately reduces to the empty string.
        cleaned = parsed._replace(netloc=host_only).geturl()

        if not had_authority and cleaned.startswith(_ASSUMED_SCHEME):
            cleaned = cleaned[len(_ASSUMED_SCHEME):]
        return cleaned
    except Exception:  # noqa: BLE001 — see the never-raises contract above
        try:
            return _AUTHORITY_USERINFO_RE.sub(r"\1", url or "")
        except Exception:  # noqa: BLE001
            return ""


def scrub_url_credentials_in_text(text: str) -> str:
    """Replace the userinfo of every URL in free text with `***`. Never raises.

    The free-text counterpart of `strip_url_credentials`, and it lives beside it
    on purpose (#2052): this module already owns what "userinfo" means for
    Trinity's repo URLs, and the two answers have to move together. They had
    already drifted once — the free-text pattern was anchored on a literal
    `https://` while the parser accepted protocol-relative, scheme-less and
    alternate-scheme authorities, so three shapes a stored source URL may
    legitimately take were echoed verbatim into durable admin-rendered state.
    Both now agree on where an authority's userinfo ends, and
    `tests/unit/test_2052_scrubber_authority_parity.py` pins them equal over a
    shared corpus.

    It does NOT call the parser, and that is not a shortcut: the input is git
    stderr, which carries prose plus several URLs at once, so there is no single
    URL for `urlparse` to be handed. What the two share is the *rule*, not the
    call.

    Structure: walk `_AUTHORITY_RUN_RE` runs and rewrite the ones that are an
    authority, rather than pattern-matching userinfo directly. That keeps the
    scan LINEAR — see `_AUTHORITY_RUN_RE` for why a direct pattern is quadratic
    here and why it matters on this input. A run qualifies when it sits right
    after the `//` that opens an authority, or at the start of a non-whitespace
    run (the scheme-less shorthand `validate_skills_library_url` accepts).
    Everything up to the run's LAST `@` goes, which is how `urlparse` itself
    resolves `.hostname` and what keeps `https://PAT@LEGACY@host` from leaking
    `LEGACY`.

    Deliberate over-match: a bare `user@host` in prose (an email, or the scp-like
    `git@github.com:o/r`) is treated as userinfo and redacted. That is the same
    reading `strip_url_credentials` gives those strings — the two agreeing is the
    property under test — and the asymmetry justifies it: a false positive costs
    a mangled word in a diagnostic, a false negative costs a PAT persisted in
    `system_settings` and rendered to admins.
    """
    try:
        source = text or ""
        out: List[str] = []
        cursor = 0
        for run in _AUTHORITY_RUN_RE.finditer(source):
            at = run.group(0).rfind("@")
            if at < 0:
                continue
            start = run.start()
            # Right after the `//` of `scheme://host` or a protocol-relative
            # `//host`, or opening a fresh run — otherwise this `@` is data
            # (a path segment, a query value) and must survive.
            after_slashes = start >= 2 and source[start - 2:start] == "//"
            prev = source[start - 1] if start else ""
            if not (
                after_slashes
                or start == 0
                or prev.isspace()
                or prev in _BARE_RUN_BOUNDARY
            ):
                continue
            out.append(source[cursor:start])
            out.append(f"{_USERINFO_PLACEHOLDER}@")
            cursor = start + at + 1
        if not out:
            return source
        out.append(source[cursor:])
        return "".join(out)
    except Exception:  # noqa: BLE001 — mirrors the never-raises contract above
        return ""


def validate_skills_library_url(url: str) -> str:
    """
    Validate that a skills library URL points to github.com.

    Prevents SSRF by ensuring the URL:
    1. Uses https:// scheme
    2. Hostname is exactly github.com (no subdomains, no lookalikes)
    3. Does not resolve to internal/private IP ranges

    Args:
        url: The URL to validate

    Returns:
        The validated URL

    Raises:
        ValueError: If the URL is not a valid github.com URL
    """
    if not url or not url.strip():
        raise ValueError("Skills library URL cannot be empty")

    url = url.strip()

    # Reject non-http(s) schemes early
    if "://" in url and not url.startswith("https://") and not url.startswith("http://"):
        raise ValueError("Skills library URL must use HTTPS")

    # Handle shorthand format: "owner/repo" or "github.com/owner/repo"
    if not url.startswith("https://") and not url.startswith("http://"):
        # Could be "github.com/owner/repo" or "owner/repo"
        if url.startswith("github.com/"):
            url = f"https://{url}"
        elif "/" in url and not url.startswith(".") and not url.startswith("localhost"):
            # Assume "owner/repo" format — this is fine, will be prefixed with github.com later
            return url
        else:
            raise ValueError("Skills library URL must be a github.com repository URL")

    # Parse the URL
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format")

    # Enforce HTTPS
    if parsed.scheme != "https":
        raise ValueError("Skills library URL must use HTTPS")

    # Extract hostname (strip port if present)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Skills library URL must have a valid hostname")

    # Strict hostname check — must be exactly github.com
    if hostname.lower() not in ALLOWED_SKILLS_LIBRARY_HOSTS:
        raise ValueError(
            f"Skills library URL must point to github.com (got: {hostname}). "
            "Only GitHub repositories are supported."
        )

    # Defense-in-depth: resolve hostname and reject private/internal IPs
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for family, type_, proto, canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                raise ValueError(
                    f"Skills library URL resolved to internal address ({ip}). "
                    "This is not allowed."
                )
    except socket.gaierror:
        # DNS resolution failed — allow it to fail later during git clone
        pass
    except ValueError:
        raise  # Re-raise our own ValueError

    return url
