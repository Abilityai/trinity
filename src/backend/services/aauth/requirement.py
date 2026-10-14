"""Person-server response grammar: ``AAuth-Requirement``, ``Retry-After``,
interaction codes and URLs (ent#878).

Everything here parses strings a person server chose. The only consumers are
the person-token service, which fetches ``Location`` (same-origin checked by
the caller) and relays ``url`` + ``code`` to a human — so each parser is
strict about the shape it accepts and refuses the rest, the same posture as
``httpsig``'s structured-field subset.

``AAuth-Requirement`` (draft §Interaction Required, verified live against
Hellō 2026-10-13)::

    AAuth-Requirement: requirement=interaction; url="https://…"; code="XXXX-XXXX"

is an RFC 8941 dictionary whose ``requirement`` member carries the
requirement name (a token or sf-string) and the parameters the requirement
needs. Unknown members and parameters are ignored; a malformed field is an
error, never a partial read.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

#: Crockford base32 alphabet (no I, L, O, U), case-insensitive, hyphens cosmetic.
_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z*][A-Za-z0-9:/!#$%&'*+\-.^_`|~]*$")
_KEY_RE = re.compile(r"^[a-z*][a-z0-9_.*-]*$")

MAX_INTERACTION_URL_CHARS = 2048
MAX_CODE_CHARS = 64
MIN_CODE_SYMBOLS = 4
MAX_CODE_SYMBOLS = 32
DEFAULT_RETRY_AFTER = 5
MIN_RETRY_AFTER = 1
MAX_RETRY_AFTER = 60


class RequirementFormatError(ValueError):
    """A person-server header outside the grammar this prototype accepts."""


def _split_top_level(value: str, sep: str) -> List[str]:
    """Split on ``sep`` outside quoted strings (``\\"`` and ``\\\\`` honoured)."""
    parts: List[str] = []
    buf: List[str] = []
    in_str = escape = False
    for ch in value:
        if in_str:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if in_str:
        raise RequirementFormatError("unterminated quoted string")
    parts.append("".join(buf))
    return parts


def _bare_item(raw: str) -> str:
    """An sf-string (unescaped) or a token/integer, as text."""
    raw = raw.strip()
    if raw.startswith('"'):
        if not raw.endswith('"') or len(raw) < 2:
            raise RequirementFormatError("malformed sf-string")
        inner = raw[1:-1]
        out: List[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\":
                i += 1
                if i >= len(inner) or inner[i] not in '"\\':
                    raise RequirementFormatError("malformed sf-string escape")
                out.append(inner[i])
            elif ch == '"' or ord(ch) < 0x20 or ord(ch) > 0x7E:
                raise RequirementFormatError("malformed sf-string")
            else:
                out.append(ch)
            i += 1
        return "".join(out)
    if not raw or not (_TOKEN_RE.match(raw) or re.fullmatch(r"-?[0-9]{1,15}(\.[0-9]{1,3})?", raw)):
        raise RequirementFormatError("malformed bare item")
    return raw


def parse_sf_dictionary(value: Optional[str]) -> Dict[str, Tuple[Optional[str], Dict[str, str]]]:
    """``{key: (item or None for boolean-true, {param: value})}`` for one dictionary field.

    Inner lists are outside this profile and refused. Later duplicates win, as
    RFC 8941 prescribes.
    """
    if value is None or not value.strip():
        raise RequirementFormatError("empty structured field")
    out: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
    for member in _split_top_level(value, ","):
        member = member.strip()
        if not member:
            raise RequirementFormatError("empty dictionary member")
        pieces = _split_top_level(member, ";")
        head = pieces[0].strip()
        key, has_value, item_raw = head.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            raise RequirementFormatError("malformed dictionary key")
        if has_value and item_raw.strip().startswith("("):
            raise RequirementFormatError("inner lists are not accepted")
        item = _bare_item(item_raw) if has_value else None
        params: Dict[str, str] = {}
        for p in pieces[1:]:
            p = p.strip()
            if not p:
                raise RequirementFormatError("empty parameter")
            pk, has_pv, pv = p.partition("=")
            pk = pk.strip()
            if not _KEY_RE.match(pk):
                raise RequirementFormatError("malformed parameter key")
            params[pk] = _bare_item(pv) if has_pv else ""
        out[key] = (item, params)
    return out


def parse_aauth_requirement(value: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """``(requirement, params)`` from an ``AAuth-Requirement`` field.

    Raises :class:`RequirementFormatError` when the field is malformed or names
    no ``requirement`` member — the caller treats either as a protocol error,
    never as a requirement it can satisfy.
    """
    members = parse_sf_dictionary(value)
    entry = members.get("requirement")
    if entry is None or not entry[0]:
        raise RequirementFormatError("AAuth-Requirement names no requirement")
    requirement, params = entry
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", requirement):
        raise RequirementFormatError("AAuth-Requirement value is not a requirement name")
    return requirement, params


def parse_retry_after(value: Optional[str], default: int = DEFAULT_RETRY_AFTER) -> int:
    """Delay seconds from ``Retry-After``: integers clamped to [1, 60]; an
    HTTP-date or garbage yields ``default``."""
    if value is None:
        return default
    text = value.strip()
    if not re.fullmatch(r"[0-9]{1,9}", text):
        return default
    return max(MIN_RETRY_AFTER, min(MAX_RETRY_AFTER, int(text)))


def normalize_code(code: Optional[str]) -> Optional[str]:
    """Upper-cased Crockford base32 with cosmetic hyphens kept, or None if not a code."""
    if not isinstance(code, str) or not code or len(code) > MAX_CODE_CHARS:
        return None
    stripped = code.replace("-", "").upper()
    if not (MIN_CODE_SYMBOLS <= len(stripped) <= MAX_CODE_SYMBOLS) or not _CROCKFORD_RE.match(stripped):
        return None
    if code.startswith("-") or code.endswith("-") or "--" in code:
        return None
    return code.upper()


def interaction_code_ok(code: Optional[str]) -> bool:
    return normalize_code(code) is not None


def interaction_url_ok(url: Optional[str]) -> bool:
    """https, absolute, a host, no query, no fragment, no userinfo, bounded."""
    if not isinstance(url, str) or not url or len(url) > MAX_INTERACTION_URL_CHARS:
        return False
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not parts.query
        and not parts.fragment
        and parts.username is None
        and parts.password is None
    )


def interaction_link(url: str, code: str) -> str:
    """The composed ``{url}?code={code}`` a person opens (draft §Interaction Required)."""
    return f"{url}?code={code}"
