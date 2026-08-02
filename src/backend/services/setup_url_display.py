"""Safe display facts for an author-supplied `setup_url` (trinity-enterprise#127).

`credential_setup[].setup_url` is author-controlled and lands next to a "paste
your API key here" input, so the display/resolve split IS the attack.
`template_service._setup_url_error` already rejects the `user@host` form, a
non-https scheme, an over-long URL and non-printables — and its own docstring
records the residual it does NOT close:

    "IDN homographs and full-width characters SURVIVE. A lookalike host still
     gets through, which is why a consumer must render the parsed hostname
     beside the link rather than only the anchor text."

This module is that consumer's half. It answers ONE question — "what host will
a browser actually go to, and can we prove it?" — and it answers it the way a
browser does.

Three properties, each load-bearing:

1. **UTS-46 nontransitional, via the `idna` package — NOT `str.encode("idna")`.**
   The stdlib codec is IDNA2003 + nameprep and disagrees with every browser on
   exactly the deviation set that matters:

       host            stdlib codec      browser (UTS-46 nontransitional)
       faß.de          fass.de           xn--fa-hia.de     <- different registrable domain
       ς.example       xn--4xa.example   xn--3xa.example   <- displays != visits

   A "mitigation" that displays a different domain than the one the click
   resolves to manufactures the very split it exists to close.

2. **Fail CLOSED.** Every failure path returns ``display_host is None``, which
   the UI MUST render as inert text rather than an anchor. Falling back to the
   raw hostname (the obvious-looking alternative) makes a failed check
   byte-identical to a passed one — it ships the homograph under a clean flag.

3. **Mitigation, not closure.** Punycode canonicalisation closes confusable
   codepoints only. It does NOT catch subdomain deception
   (``accounts.google.com.evil.tld``), typosquats (``0penai.com``),
   percent-encoded authorities, or a correct host with a hostile destination
   (the path/query are not validated anywhere). That is why the result leads
   with the registrable domain: punycode is irrelevant to the ASCII-subdomain
   attack, which is the commonest one, and eTLD+1 is the part an operator can
   actually judge. A badge that is right *most* of the time trains the user to
   trust it, so the UI copy must claim mitigation, never safety.

Leaf module by construction: it imports nothing from `template_service`, so it
can be reused by any future consumer of the same field (catalog cards, the
create-time preview) without dragging the catalog in.
"""

from typing import Optional
from urllib.parse import urlsplit

import idna

# Multi-label public suffixes common enough to matter for a vendor console URL.
#
# DELIBERATELY a small embedded list and NOT the Public Suffix List: Trinity
# ships no PSL dependency, and the alternative — a network fetch or a vendored
# 15k-line table — buys accuracy on hosts that never appear in an API-key
# console link. The failure mode is bounded and one-directional: for a
# multi-label suffix we do not know, `registrable` is the last TWO labels, i.e.
# emphasis on a *narrower* string than the true registrant. The FULL host is
# always displayed regardless, so the security-load-bearing part of the render
# never depends on this table being complete.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "ac.uk", "co.uk", "gov.uk", "ltd.uk", "me.uk", "net.uk", "org.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
        "co.nz", "net.nz", "org.nz",
        "co.za", "org.za",
        "com.br", "com.mx", "com.ar", "com.co",
        "com.cn", "net.cn", "org.cn", "gov.cn",
        "co.in", "net.in", "org.in",
        "com.sg", "com.hk", "com.tw", "com.tr", "com.ua", "com.pl", "com.ru",
        "co.kr", "co.il", "co.id", "co.th",
        "github.io", "gitlab.io", "pages.dev", "workers.dev", "vercel.app",
        "herokuapp.com", "netlify.app", "web.app", "firebaseapp.com",
    }
)

_FAIL_CLOSED = {"display_host": None, "registrable": None, "verified": False, "idn": False}


def _registrable_domain(host: str) -> str:
    """eTLD+1 for `host`, heuristically (see `_MULTI_LABEL_SUFFIXES`).

    `host` is already punycode-canonical ASCII when this is called, so the
    label split is a plain `.` split with no encoding subtleties left.
    """
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def describe_setup_url(url) -> dict:
    """Display facts for one `setup_url`. Never raises.

    Returns ``{"display_host", "registrable", "verified", "idn"}``.

    ``display_host is None`` is the fail-closed signal and means **render the
    URL as inert text, not as a link** — the host could not be canonicalised,
    so we cannot say where a click would go.

    ``registrable`` is the eTLD+1 to emphasise inside ``display_host``
    (``accounts.google.com.``**``evil.tld``**). ``idn`` is True when
    canonicalisation actually changed the host, i.e. the link uses an
    internationalised domain — informational only; it is NOT a verdict, since
    the dangerous ``accounts.google.com.evil.tld`` shape is pure ASCII.
    """
    if not isinstance(url, str) or not url:
        return dict(_FAIL_CLOSED)

    try:
        # urlsplit is INSIDE the try: it raises ValueError on an NFKC-confusable
        # delimiter (`https://google.com／evil.tld`) and on a malformed IPv6
        # authority, and this function's whole contract is "never raises".
        parts = urlsplit(url)
        hostname = parts.hostname
    except (ValueError, AttributeError, UnicodeError):
        return dict(_FAIL_CLOSED)

    if not hostname:
        return dict(_FAIL_CLOSED)

    # Defence in depth. The backend validates https at declaration time
    # (`_setup_url_error`), but this module is a standalone leaf that other
    # consumers will reuse, and a describer that vouches for the host of a
    # `javascript:` or `http:` URL is a footgun waiting for its second caller.
    if (parts.scheme or "").lower() != "https":
        return dict(_FAIL_CLOSED)

    # A percent-encoded authority is not verifiable from Python: `urlsplit`
    # leaves `%D0%B0pple.com` undecoded while a browser decodes it and resolves
    # a completely different (homograph) host. `idna.encode` happens to reject
    # `%` today, but relying on that is relying on an implementation detail of
    # a third-party codepoint table — say it explicitly.
    if "%" in hostname:
        return dict(_FAIL_CLOSED)

    try:
        display_host = idna.encode(hostname, uts46=True, transitional=False).decode("ascii")
    except Exception:  # noqa: BLE001 — any codec failure is a FAILED check, never a pass
        return dict(_FAIL_CLOSED)

    return {
        "display_host": display_host,
        "registrable": _registrable_domain(display_host),
        "verified": True,
        "idn": display_host != hostname,
    }


def describe_setup_url_or_none(url) -> Optional[dict]:
    """`describe_setup_url` for an optional field: `None` in, `None` out."""
    if url is None:
        return None
    return describe_setup_url(url)
