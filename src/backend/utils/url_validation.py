"""
URL validation utilities for SSRF prevention.

Provides strict validation for URLs that the backend will connect to,
ensuring they point to allowed external hosts and not internal services.

Related: SEC-179 (pentest finding 3.2.2)
"""

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
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

#: RFC 6598 shared address space (CGNAT). Python's `ipaddress` reports it as
#: NEITHER `is_private` NOR `is_reserved`, so the standard predicate stack below
#: admits it (trinity-enterprise#14 S3). Not reachable inside Trinity's own
#: topology — both Docker networks are 172.28/16 and 172.29/16, which ARE
#: `is_private` — but several cloud providers address internal endpoints out of
#: this range, so a Trinity deployed there would have a hole the same shape as
#: 10.0.0.0/8. Parsed once, at import.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


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


# ============================================================================
# Remote template registry (TMPL-002, trinity-enterprise#14)
# ============================================================================

#: Fixed refusal reasons. The registry settings endpoint echoes the message of
#: a rejected URL back to an admin, so the text must never be built from a
#: resolved address or a server-supplied string.
_REGISTRY_URL_MAX_LEN = 2048


#: The predicate stack that decides "is this address outside the perimeter?".
#: One definition, consulted by every validator in this module that resolves a
#: host, so a future range (the CGNAT clause was the last one) is added once.
def _is_internal_address(ip: "ipaddress._BaseAddress") -> bool:
    # The v4 view of `ip`: itself when it IS v4, its payload when it is the
    # IPv4-MAPPED form `::ffff:a.b.c.d`, else None. The CGNAT clause below must
    # ask THIS, never `ip.version == 4` (ent#393): `::ffff:100.64.0.1` reports
    # version 6, so a version gate never reaches it — and the stdlib answers
    # False to all six predicates above for it, which is the exact gap the
    # clause exists to close. Every OTHER internal range survives its mapped
    # form for free, because CPython's `is_private`/`is_reserved`/... delegate
    # through `ipv4_mapped` before consulting their IPv6 tables; CGNAT is the
    # one range whose refusal is Trinity's own code, so it is the one that has
    # to do the delegation itself.
    #
    # Mapped is the ONLY embedding needing this. The other v4-in-v6 shapes are
    # refused wholesale by prefix regardless of the address they carry —
    # `::/8` (IPv4-compatible RFC 4291, IPv4-translated RFC 2765), `64:ff9b::/96`
    # + `64:ff9b:1::/48` (NAT64), `2002::/16` (6to4), `2001::/32` (Teredo) — and
    # none of them populates `ipv4_mapped`. Pinned by
    # `test_736_a2a_outbound_edges.py::test_C1_ipv6_transition_forms_*`, because
    # that refusal is the interpreter's, not ours (the #1891 class).
    v4 = ip if ip.version == 4 else ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or (v4 is not None and v4 in _SHARED_ADDRESS_SPACE)
    )


@dataclass(frozen=True)
class ValidatedPublicUrl:
    """The result of `_validate_public_https_url` — the URL AND what it resolved to.

    `addresses` is the part callers other than the template registry need: a
    validator that resolves a host and then throws the answer away forces the
    HTTP client to resolve it a second time, which is precisely the TOCTOU
    window a rebinding attack lives in (#736 FR-4). Returning the validated set
    lets a caller pin the connection to an address this function approved.
    """

    url: str
    hostname: str          # IDNA A-label, lowercased, trailing dot stripped
    port: int              # `effective_port` of the authority — never 0, never None
    addresses: Tuple[str, ...]


#: The port a scheme addresses when the authority names none.
SCHEME_DEFAULT_PORTS = {"https": 443, "http": 80}


def effective_port(port: Optional[int], scheme: str) -> Optional[int]:
    """The port a URL actually addresses. `None` when the scheme has no default.

    ONE normalisation for a field that had three (ent#398). Along a single call
    path, `:0` used to mean three different things: the validator coalesced it to
    443 (`parsed.port or 443` — `0` is falsy), `_pinned_url` dropped it and
    therefore connected to 443, and `_same_origin` compared it literally as port
    0. So a `:0` endpoint validated, would have connected correctly, and was then
    permanently refused `card_origin_mismatch` against any card declaring the
    ordinary form — fail-closed, but permanently broken with a reason code that
    points at the wrong thing.

    Three independent normalisations of one field is the actual hazard: today
    they disagree harmlessly, and it is an edit to any one of them that turns
    that into something else. Validation, connection pinning and origin
    comparison now all consume this.

    `0` is treated as "no port given", matching the validator's shipped
    behaviour (and browsers, which refuse `:0` outright rather than dialing it):
    port 0 is not a connectable destination, so the alternative reading — dial
    port 0 — is not a reading anyone wants. An unknown scheme yields `None`; the
    caller decides what that means, because "no default port" is not the same
    answer for a comparison as it is for a connection.
    """
    if port:
        return port
    return SCHEME_DEFAULT_PORTS.get((scheme or "").lower())


def canonical_host(hostname: str) -> Optional[str]:
    """Canonicalise a hostname the way a browser resolves it, or `None`.

    UTS-46 nontransitional via the `idna` package, matching
    `services/setup_url_display.py` — NOT `str.encode("idna")`, which is
    IDNA2003 and disagrees with every browser on the deviation set that matters
    (`fass.de` vs `xn--fa-hia.de`). The point here is narrower than display
    safety: the parser and the resolver must agree on **which host was
    approved**, so the name is canonicalised ONCE and that one form is what gets
    validated, resolved, and connected to.

    Two deliberate asymmetries:

    * A trailing dot is stripped first (`host.` and `host` resolve identically,
      and the dot survives `idna.encode`).
    * A codec failure on a **pure-ASCII** host is NOT fatal, and the host is
      returned lowercased unchanged. Canonicalisation of an ASCII host is a
      no-op by definition — there is no confusable to fold — so refusing
      `my_registry.example.com` (underscores are illegal in IDNA) would break a
      legitimate operator URL for no security gain. A codec failure on a
      **non-ASCII** host is fatal, because that is exactly where the homograph
      lives.
    """
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        return None
    # A percent-encoded authority is not verifiable from Python: `urlparse`
    # leaves `%D0%B0pple.com` undecoded while a browser decodes it and resolves a
    # different host. Refuse rather than rely on the codec happening to reject it
    # (the `setup_url_display` rule).
    if "%" in host:
        return None
    try:
        import idna

        return idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except Exception:  # noqa: BLE001 — see the ASCII asymmetry above
        try:
            host.encode("ascii")
        except UnicodeEncodeError:
            return None
        return host


#: Private alias retained so existing readers of the old name keep working.
_canonical_host = canonical_host


def _validate_public_https_url(
    url: str,
    *,
    label: str,
    host_label: str,
    credential_advice: str,
    internal_advice: str,
    max_len: int = _REGISTRY_URL_MAX_LEN,
    resolver: Optional[Callable[..., list]] = None,
) -> ValidatedPublicUrl:
    """ONE public-HTTPS-destination gate, shared by every validator that needs it.

    Extracted (#736) rather than cloned a third time: this module exists to
    centralise "where may the backend connect?", and a third ~90%-identical copy
    of the predicate stack inside it would be the Invariant #5 failure happening
    *in the file whose job is preventing it*. The messages are parameterised so
    each caller keeps its own operator-readable wording verbatim.

    What it enforces, in order:

    1. Non-empty, `<= max_len`.
    2. **HTTPS only.** Not `http:`, not `file:`, not anything else.
    3. **No userinfo**, refused outright rather than stripped — a stripped
       credential is a credential the operator believes is still in use.
    4. A hostname that canonicalises (see `_canonical_host`).
    5. **Every** address the resolver returns is public. One internal record
       among public ones refuses the whole URL: which record a resolver hands
       out is not our choice, so a host that resolves to both is a DNS-level
       smuggle.
    6. **DNS failure is fatal**, deliberately unlike `validate_skills_library_url`
       (whose target is a later `git clone` that fails loudly on its own).

    Refusal messages are fixed strings and are **never** built from a resolved
    address: they are shown to an operator, and echoing an internal IP back
    turns a refusal into a topology oracle.

    `resolver` is a seam for tests only — production always uses
    `socket.getaddrinfo`.
    """
    if not url or not url.strip():
        raise ValueError(f"{label} cannot be empty")

    url = url.strip()

    if len(url) > max_len:
        raise ValueError(f"{label} is too long (max {max_len} characters)")

    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format")

    if parsed.scheme != "https":
        raise ValueError(f"{label} must use HTTPS")

    # `username`/`password` are None when absent. Check the raw netloc too: a
    # malformed authority can carry an `@` that urlparse folds into the host.
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError(credential_advice)

    hostname = canonical_host(parsed.hostname or "")
    if not hostname:
        raise ValueError(f"{label} must have a valid hostname")

    try:
        # ent#398: the ONE normalisation, shared with `_pinned_url` and
        # `_same_origin`. The scheme is https by construction here (checked
        # above), so the default is never absent.
        port = effective_port(parsed.port, parsed.scheme) or 443
    except ValueError:
        # urlparse raises on a non-numeric / out-of-range port only when `.port`
        # is read, so this is the first place a junk authority surfaces.
        raise ValueError(f"{label} must have a valid hostname")

    resolve = resolver or socket.getaddrinfo
    try:
        resolved = resolve(hostname, port)
    except socket.gaierror:
        raise ValueError(f"{host_label} could not be resolved ({hostname})")

    addresses: List[str] = []
    for entry in resolved:
        sockaddr = entry[4]
        raw = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            # An address the stdlib itself cannot parse is not one we are going
            # to vet — refuse rather than skip (skipping is how a bad record
            # passes through unexamined).
            raise ValueError(internal_advice)
        if _is_internal_address(ip):
            raise ValueError(internal_advice)
        addresses.append(str(ip))

    if not addresses:
        # An empty resolver result is indistinguishable from "nothing to check"
        # and would sail through the loop above. Treat it as unresolvable.
        raise ValueError(f"{host_label} could not be resolved ({hostname})")

    return ValidatedPublicUrl(
        url=url, hostname=hostname, port=port, addresses=tuple(addresses)
    )


def validate_template_registry_url(url: str) -> str:
    """Validate a remote template-registry URL, SSRF-gated.

    Sibling of `validate_skills_library_url`, deliberately NOT a reuse of it:
    that one is `github.com`-only, which is right for a git clone target and
    wrong here — an operator may self-host a registry on their own domain. The
    rules that stay are the ones that bound where the backend will connect, and
    since #736 they live in `_validate_public_https_url`, which this function
    parameterises with its own operator-readable wording:

    1. **HTTPS only.** No `http:`, no `file:`, no `gopher:`, nothing else. An
       unencrypted registry is a trivially-tampered catalog of repos users are
       invited to trust.
    2. **No userinfo** (`https://user:token@host/...`). Rejected outright rather
       than stripped, so a credential can never be persisted into a
       `system_settings` row or echoed back through the status payload. Silently
       dropping it would be worse: the operator would think it was in use.
    3. **Resolve and reject private / loopback / link-local / reserved
       destinations**, so the registry URL cannot be turned into a probe of the
       platform network (Redis, the Docker socket proxy, cloud metadata).

    Returns the normalized URL. Raises `ValueError` with an operator-readable
    message otherwise.

    Known residual, accepted for v1 and recorded in requirements §4.2.2: this
    pre-resolves, so it does not close DNS rebinding (a TOCTOU between validate
    and connect). It is tolerable *here* because the URL is admin-AND-human-set,
    the response is parsed into a display-only allowlisted record, and the body
    never reaches an eval/exec/deserialize sink. The fetcher's
    `follow_redirects=False` closes the adjacent bypass — a validated URL that
    redirects is a fetch failure, not a hop to re-validate. It is NOT tolerable
    on the A2A outbound path, which sends a credential — see
    `validate_a2a_endpoint_url`, which returns the resolved addresses so the
    caller can pin the connection to one this function approved (#736 FR-4).
    """
    return _validate_public_https_url(
        url,
        label="Template registry URL",
        host_label="Template registry hostname",
        credential_advice=(
            "Template registry URL must not embed credentials "
            "(user:password@host). Host a public document, or put the "
            "registry behind a network boundary instead."
        ),
        internal_advice=(
            "Template registry URL resolves to an internal address. "
            "The registry must be reachable on the public internet."
        ),
    ).url


# ============================================================================
# Outbound A2A endpoints (#736)
# ============================================================================

#: Machine-readable refusal codes the A2A route maps 1:1 to HTTP. Distinct from
#: the human message so the tool can branch without string-matching.
A2A_URL_REASONS = {
    "endpoint_not_https": "endpoint_not_https",
    "endpoint_private_address": "endpoint_private_address",
    "endpoint_dns_failure": "endpoint_dns_failure",
    "endpoint_invalid": "endpoint_invalid",
}


class A2AEndpointUrlError(ValueError):
    """A registered A2A endpoint URL that must not be fetched.

    Carries a stable `reason` code alongside the operator message, because the
    caller maps refusals to HTTP status + a machine-readable field and
    string-matching a human message is how those two drift apart.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def validate_a2a_endpoint_url(url: str) -> ValidatedPublicUrl:
    """Validate an outbound A2A endpoint URL at CALL time (#736 FR-3).

    Runs on **every** call, on a URL that came out of the endpoint registry —
    because "it is in the registry" does not mean "it is safe to fetch". The
    registration surface validates a URL with `startswith("http://") or
    startswith("https://")` plus a length cap: no SSRF check at all, and plain
    `http://` accepted. Without this gate an operator (or a compromised
    management surface) could register `http://169.254.169.254/...` and have a
    credentialed server-side fetch aimed at cloud metadata.

    Two things it does that the template-registry sibling does not:

    * **Refuses `http://` at USE rather than silently upgrading it.** An
      operator who typed `http://` has to find out; a client that quietly
      rewrites the scheme is a client that will one day quietly rewrite it back.
    * **Returns the resolved, validated addresses.** This request carries a
      credential, so the rebinding residual the registry validator documents is
      not acceptable here: the caller pins the connection to an address this
      function approved (`services/a2a_client.py`).

    Raises `A2AEndpointUrlError` (a `ValueError`) carrying a stable `reason`.
    """
    try:
        return _validate_public_https_url(
            url,
            label="A2A endpoint URL",
            host_label="A2A endpoint hostname",
            credential_advice=(
                "A2A endpoint URL must not embed credentials (user:password@host). "
                "Register the secret as the endpoint's credential instead — it is "
                "stored encrypted and never echoed back."
            ),
            internal_advice=(
                "A2A endpoint URL resolves to an internal address. An outbound "
                "A2A endpoint must be reachable on the public internet."
            ),
        )
    except ValueError as exc:
        message = str(exc)
        if "must use HTTPS" in message:
            raise A2AEndpointUrlError(
                "endpoint_not_https",
                "A2A endpoint URL must use HTTPS. Re-register this endpoint with "
                "an https:// URL — the call is refused rather than silently "
                "upgraded, because a credential would otherwise travel in clear.",
            ) from None
        if "internal address" in message:
            raise A2AEndpointUrlError("endpoint_private_address", message) from None
        if "could not be resolved" in message:
            raise A2AEndpointUrlError("endpoint_dns_failure", message) from None
        raise A2AEndpointUrlError("endpoint_invalid", message) from None
