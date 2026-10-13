"""RFC 9421 HTTP Message Signatures — the AAuth profile, and nothing more.

Covers exactly what the agent-identity-only mode uses:

* derived components ``@method``, ``@authority``, ``@path``;
* the ``signature-key`` field (the key-conveyance draft) plus, on POST,
  ``content-digest`` (RFC 9530, sha-256) and ``content-type``;
* one signature labelled ``sig`` with the ``created`` parameter only — agents
  "MUST NOT include" ``alg`` and "SHOULD NOT" include ``keyid``;
* ``Signature`` values in **standard** base64 (an RFC 8941 byte sequence);
  base64url is rejected by whoami.aauth.dev and by the JS reference.

The RFC 8941 parsing below is a strict subset: it accepts the shapes this
profile produces and refuses everything else, which is the right failure for a
security boundary (``invalid_input``), not a general-purpose parser.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

LABEL = "sig"
REQUIRED_COMPONENTS = ("@method", "@authority", "@path", "signature-key")
BODY_COMPONENTS = ("content-digest", "content-type")
_DEFAULT_PORTS = {"https": 443, "http": 80}

_TOKEN_CHARS = re.compile(r'^[A-Za-z0-9._~+/=-]+$')


class SignatureFormatError(ValueError):
    """A signature header that is absent, malformed, or outside the profile."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

def authority(host: str, port: Optional[int], scheme: str = "https") -> str:
    """``@authority`` — lowercase host, default port omitted (RFC 9421 §2.2.3)."""
    host = host.lower()
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return host
    return f"{host}:{port}"


def content_digest(body: bytes) -> str:
    """RFC 9530 ``Content-Digest`` value for ``body`` (sha-256)."""
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')}:"


def components_for(method: str) -> Tuple[str, ...]:
    return REQUIRED_COMPONENTS + (BODY_COMPONENTS if method.upper() == "POST" else ())


def _params(components: Tuple[str, ...], created: int) -> str:
    inner = " ".join(f'"{c}"' for c in components)
    return f"({inner});created={created}"


def signature_base(values: Dict[str, str], components: Tuple[str, ...], params: str) -> bytes:
    """The RFC 9421 §2.5 signature base. ``values`` maps component → value."""
    lines = [f'"{c}": {values[c]}' for c in components]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode("utf-8")


# --------------------------------------------------------------------------- #
# Serialise (signer)
# --------------------------------------------------------------------------- #

def signature_key_header(agent_token: str) -> str:
    return f'{LABEL}=jwt;jwt="{agent_token}"'


@dataclass(frozen=True)
class SignedHeaders:
    signature_input: str
    signature: str
    signature_key: str
    content_digest: Optional[str]

    def as_dict(self) -> Dict[str, str]:
        out = {
            "Signature-Input": self.signature_input,
            "Signature": self.signature,
            "Signature-Key": self.signature_key,
        }
        if self.content_digest is not None:
            out["Content-Digest"] = self.content_digest
        return out


def sign(
    *,
    method: str,
    authority_value: str,
    path: str,
    agent_token: str,
    body: bytes,
    content_type: Optional[str],
    created: int,
    private_key,
) -> SignedHeaders:
    """Produce the three (four on POST) headers for one request."""
    method = method.upper()
    components = components_for(method)
    sig_key = signature_key_header(agent_token)
    values = {"@method": method, "@authority": authority_value, "@path": path, "signature-key": sig_key}
    digest = None
    if method == "POST":
        digest = content_digest(body)
        values["content-digest"] = digest
        values["content-type"] = content_type or ""
    params = _params(components, created)
    raw = private_key.sign(signature_base(values, components, params))
    return SignedHeaders(
        signature_input=f"{LABEL}={params}",
        signature=f"{LABEL}=:{base64.b64encode(raw).decode('ascii')}:",
        signature_key=sig_key,
        content_digest=digest,
    )


# --------------------------------------------------------------------------- #
# Parse (verifier)
# --------------------------------------------------------------------------- #

def _split_members(value: str) -> List[Tuple[str, str]]:
    """Split an RFC 8941 dictionary into ``(key, raw member)`` pairs.

    Commas inside quoted strings, byte sequences and inner lists do not split.
    """
    members: List[Tuple[str, str]] = []
    buf: List[str] = []
    in_str = in_bytes = False
    depth = 0
    escape = False
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
        elif ch == ":" and depth == 0:
            # Outside a string, a colon only ever delimits a byte sequence.
            in_bytes = not in_bytes
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0 and not in_bytes:
            members.append(_key_split("".join(buf)))
            buf = []
            continue
        buf.append(ch)
    if in_str or depth or in_bytes:
        raise SignatureFormatError("invalid_input", "unterminated structured field")
    if "".join(buf).strip():
        members.append(_key_split("".join(buf)))
    return members


def _key_split(member: str) -> Tuple[str, str]:
    member = member.strip()
    key, sep, rest = member.partition("=")
    if not sep or not re.match(r"^[a-z*][a-z0-9_.*-]*$", key):
        raise SignatureFormatError("invalid_input", "malformed structured field member")
    return key, rest


def _only_label(value: Optional[str], header: str) -> str:
    """Return the ``sig`` member of a dictionary header (other labels ignored)."""
    if not value:
        raise SignatureFormatError("invalid_signature", f"missing {header} header")
    found = [raw for key, raw in _split_members(value) if key == LABEL]
    if len(found) != 1:
        raise SignatureFormatError("invalid_signature", f"{header} has no single '{LABEL}' member")
    return found[0]


def parse_signature_key(value: Optional[str]) -> str:
    """``sig=jwt;jwt="<token>"`` → token. Any other scheme → ``unsupported_scheme``."""
    member = _only_label(value, "Signature-Key")
    scheme, _, params = member.partition(";")
    if scheme != "jwt":
        raise SignatureFormatError("unsupported_scheme", f"Signature-Key scheme '{scheme}' is not accepted")
    m = re.fullmatch(r'jwt="([^"\\]*)"', params.strip())
    if not m or not _TOKEN_CHARS.match(m.group(1)):
        raise SignatureFormatError("invalid_input", "Signature-Key jwt parameter is malformed")
    return m.group(1)


@dataclass(frozen=True)
class SignatureInput:
    components: Tuple[str, ...]
    params: str           # the exact serialized inner list + params, for the base
    created: int
    expires: Optional[int] = None


def parse_signature_input(value: Optional[str]) -> SignatureInput:
    member = _only_label(value, "Signature-Input").strip()
    m = re.fullmatch(r'\(((?:"[a-z@][a-z0-9-]*"(?: "[a-z@][a-z0-9-]*")*)?)\)((?:;[a-z][a-z0-9-]*=[^;]*)*)', member)
    if not m:
        raise SignatureFormatError("invalid_input", "Signature-Input is malformed")
    components = tuple(c.strip('"') for c in m.group(1).split(" ")) if m.group(1) else ()
    if len(set(components)) != len(components):
        raise SignatureFormatError("invalid_input", "Signature-Input repeats a component")
    params: Dict[str, str] = {}
    for part in filter(None, m.group(2).split(";")):
        k, _, v = part.partition("=")
        if k in params:
            raise SignatureFormatError("invalid_input", f"Signature-Input repeats parameter '{k}'")
        params[k] = v
    if "alg" in params:
        raise SignatureFormatError("invalid_input", "Signature-Input must not carry alg")
    created = params.get("created")
    if created is None or not re.fullmatch(r"-?[0-9]{1,15}", created):
        raise SignatureFormatError("invalid_input", "Signature-Input created must be an integer")
    expires = params.get("expires")
    if expires is not None and not re.fullmatch(r"-?[0-9]{1,15}", expires):
        raise SignatureFormatError("invalid_input", "Signature-Input expires must be an integer")
    return SignatureInput(
        components=components,
        params=member,
        created=int(created),
        expires=int(expires) if expires is not None else None,
    )


def parse_content_digest_sha256(value: Optional[str]) -> Optional[str]:
    """The ``sha-256`` member of a ``Content-Digest`` header, re-serialised, or None."""
    if not value:
        return None
    for key, raw in _split_members(value):
        if key == "sha-256":
            m = re.fullmatch(r":([A-Za-z0-9+/]*={0,2}):", raw.strip())
            return f"sha-256=:{m.group(1)}:" if m else None
    return None


def parse_signature(value: Optional[str]) -> bytes:
    member = _only_label(value, "Signature").strip()
    m = re.fullmatch(r":([A-Za-z0-9+/]*={0,2}):", member)
    if not m:
        raise SignatureFormatError("invalid_signature", "Signature is not a standard-base64 byte sequence")
    try:
        return base64.b64decode(m.group(1), validate=True)
    except Exception:  # noqa: BLE001
        raise SignatureFormatError("invalid_signature", "Signature is not valid base64") from None
