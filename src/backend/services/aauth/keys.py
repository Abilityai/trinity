"""The instance's Agent Provider signing key (ent#623).

One Ed25519 key per instance, the key its JWKS publishes and every agent token
is signed with. Persisted as an AES-256-GCM envelope in
``system_settings.aauth_signing_key_encrypted`` (Invariant #12 — the
``elevenlabs_api_key_encrypted`` location), never in plaintext, never logged.

Two workers must never fork the key: the first use writes through
``insert_setting_if_absent`` and then RE-READS the row, so the loser of a
first-use race adopts the winner's key. The row is read on every call rather
than cached by position, so a worker can never keep signing with a key the
published JWKS no longer carries.

An envelope that no longer decrypts (a ``CREDENTIAL_ENCRYPTION_KEY`` rotation
that skipped this row, a corrupted value) makes AAuth **fail closed** with
:class:`SigningKeyUnavailable`. It is deliberately not regenerated: a silent new
key would change this instance's identity underneath every peer that trusts it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.aauth import jose

logger = logging.getLogger(__name__)

SIGNING_KEY_SETTING = "aauth_signing_key_encrypted"
_ENVELOPE_FIELD = "aauth_ed25519_seed"


class SigningKeyUnavailable(RuntimeError):
    """The persisted signing key exists but cannot be used. AAuth is inert."""


@dataclass(frozen=True)
class InstanceKey:
    kid: str
    public_jwk: Dict[str, Any]
    private_key: Ed25519PrivateKey = field(repr=False)


_cache: Optional[tuple] = None   # (envelope, InstanceKey)


def _encrypt(seed: bytes) -> str:
    from services.credential_encryption import CredentialEncryptionService

    return CredentialEncryptionService().encrypt({_ENVELOPE_FIELD: jose.b64u_encode(seed)})


def _decrypt(envelope: str) -> Ed25519PrivateKey:
    from services.credential_encryption import CredentialEncryptionService

    payload = CredentialEncryptionService().decrypt(envelope)
    seed = jose.b64u_decode(str(payload[_ENVELOPE_FIELD]))
    return Ed25519PrivateKey.from_private_bytes(seed)


def _build(private_key: Ed25519PrivateKey) -> InstanceKey:
    jwk = jose.public_jwk(private_key.public_key())
    kid = jose.thumbprint(jwk)
    return InstanceKey(kid=kid, public_jwk={**jwk, "kid": kid, "use": "sig"}, private_key=private_key)


def get_instance_key() -> InstanceKey:
    """Load (creating on first use) the instance signing key."""
    global _cache
    from database import db

    envelope = db.get_setting_value(SIGNING_KEY_SETTING, None)
    if _cache is not None and _cache[0] == envelope:
        return _cache[1]
    if not envelope:
        seed = Ed25519PrivateKey.generate().private_bytes_raw()
        db.insert_setting_if_absent(SIGNING_KEY_SETTING, _encrypt(seed))
        envelope = db.get_setting_value(SIGNING_KEY_SETTING, None)
        if not envelope:
            raise SigningKeyUnavailable("AAuth signing key could not be persisted")
        logger.info("[aauth] instance signing key initialised")
    try:
        key = _build(_decrypt(envelope))
    except Exception:  # noqa: BLE001 — any failure here means "cannot sign"
        logger.error(
            "[aauth] %s exists but cannot be decrypted; AAuth is disabled until the "
            "row is restored or deliberately deleted", SIGNING_KEY_SETTING,
        )
        raise SigningKeyUnavailable("AAuth signing key is unreadable") from None
    _cache = (envelope, key)
    return key


def jwks() -> Dict[str, Any]:
    return {"keys": [get_instance_key().public_jwk]}


def reset_cache() -> None:
    """Tests only."""
    global _cache
    _cache = None
