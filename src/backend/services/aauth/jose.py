"""The JOSE subset AAuth needs: compact JWS with the ``Ed25519`` algorithm.

Hand-rolled rather than PyJWT because PyJWT 2.14 only registers the polymorphic
``EdDSA`` identifier, which the AAuth draft forbids verifiers to accept
("MUST NOT accept `none`, the polymorphic `EdDSA` identifier, or any symmetric
algorithm") — the fully-specified ``Ed25519`` id is RFC 9864. The whole format
is three base64url segments and one signature, so owning it is cheaper than a
new dependency (and an image rebuild).
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ALG = "Ed25519"

#: JOSE header parameters that would let a token name its own verification key
#: or add semantics we do not implement. Refused outright (RFC 7515 §4.1).
FORBIDDEN_HEADER_PARAMS = frozenset({"jku", "x5u", "jwk", "x5c", "crit"})


class JoseError(ValueError):
    """A token or key that is malformed or does not verify."""


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or any(c in text for c in "=+/ \t\r\n"):
        raise JoseError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:  # noqa: BLE001
        raise JoseError("not base64url") from None


def _json_segment(obj: Dict[str, Any]) -> str:
    return b64u_encode(json.dumps(obj, separators=(",", ":"), sort_keys=False).encode("utf-8"))


def public_jwk(key: Ed25519PublicKey, **extra: Any) -> Dict[str, Any]:
    """An OKP/Ed25519 public JWK carrying the fully-specified ``alg`` (required)."""
    jwk: Dict[str, Any] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64u_encode(key.public_bytes_raw()),
        "alg": ALG,
    }
    jwk.update(extra)
    return jwk


def jwk_to_public_key(jwk: Any) -> Ed25519PublicKey:
    """Parse a public Ed25519 JWK. Refuses private material and alg/kty/crv drift."""
    if not isinstance(jwk, dict):
        raise JoseError("jwk is not an object")
    if "d" in jwk:
        raise JoseError("jwk carries private key material")
    if jwk.get("alg") != ALG:
        raise JoseError("jwk alg must be Ed25519")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise JoseError("jwk kty/crv do not match alg Ed25519")
    raw = b64u_decode(jwk.get("x") or "")
    if len(raw) != 32:
        raise JoseError("jwk x is not a 32-byte Ed25519 key")
    return Ed25519PublicKey.from_public_bytes(raw)


def thumbprint(jwk: Dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) over the OKP required members."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return b64u_encode(hashlib.sha256(canonical.encode("utf-8")).digest())


def sign_jws(header: Dict[str, Any], payload: Dict[str, Any], key: Ed25519PrivateKey) -> str:
    signing_input = f"{_json_segment(header)}.{_json_segment(payload)}"
    return f"{signing_input}.{b64u_encode(key.sign(signing_input.encode('ascii')))}"


def decode_unverified(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
    """Split and parse a compact JWS WITHOUT verifying it.

    Returns ``(header, claims, signing_input, signature)``. Everything returned
    is attacker-controlled until :func:`verify_ed25519` has passed.
    """
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16384:
        raise JoseError("not a compact JWS")
    h64, p64, s64 = token.split(".")
    if len(h64) > 4096 or len(p64) > 12288:
        raise JoseError("JWS segment too large")
    try:
        header = json.loads(b64u_decode(h64))
        claims = json.loads(b64u_decode(p64))
    except Exception:  # noqa: BLE001 — including RecursionError on a deeply
        # nested document: `json` has no depth limit, so a ~12 KB segment of
        # nested arrays raises RuntimeError, and an escaping RuntimeError would
        # be a 500 an unauthenticated caller can trigger at will.
        raise JoseError("JWS segments are not JSON") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise JoseError("JWS segments are not objects")
    return header, claims, f"{h64}.{p64}".encode("ascii"), b64u_decode(s64)


def verify_ed25519(signing_input: bytes, signature: bytes, key: Ed25519PublicKey) -> None:
    try:
        key.verify(signature, signing_input)
    except InvalidSignature:
        raise JoseError("Ed25519 signature does not verify") from None
