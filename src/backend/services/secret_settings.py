"""
Credential-bearing ``system_settings`` keys: encrypt at rest (trinity-enterprise#435).

Six settings rows held **live third-party credentials in cleartext**
(``anthropic_api_key``, ``github_pat``, ``google_api_key``, ``slack_app_token``,
``slack_client_secret``, ``slack_signing_secret``) while their siblings —
``elevenlabs_api_key_encrypted`` (ent#117), ``a2a_outbound_endpoints_encrypted``
(#736) and every AES-256-GCM token *column* under Invariant #12 — encrypted.
The platform's encryption-at-rest posture was therefore only partially true and
a single ``SELECT`` contradicted any statement of it (CWE-312).

This module is the **policy leaf** for that class: which keys are secret, what
the envelope looks like, and the sink guard that stops a new cleartext write.
It is deliberately dependency-light — ``db``/``services`` imports are lazy and
local — because ``db/settings.py`` calls into it and the reverse edge would
invert Invariant #1's layering.

Three properties are load-bearing:

1. **The key NAME moves, not just the value.** The encrypted form lives under a
   distinct ``<key>_encrypted`` row and the legacy row is DELETED. A same-named
   key that may hold either cleartext or an envelope leaves "is this install
   encrypted?" unanswerable by inspection — which is the reported defect, not a
   cosmetic detail. With the rename, the issue's own verification query
   (``SELECT key FROM system_settings WHERE key IN (...)`` returning nothing)
   *is* the proof, and it stays true forever because the guard below refuses to
   recreate the legacy row.

2. **The read path lazily migrates.** A migration converts what is on disk once,
   but the legacy row can come back — an operator restoring a pre-fix backup, a
   rollback-then-roll-forward, a direct DB write. ``resolve_secret_setting``
   therefore encrypts-and-deletes whatever legacy row it finds, so cleartext is
   transient rather than permanent no matter how it arrived. Steady state pays
   nothing: when the encrypted row is present the legacy key is never read.

3. **The guard is an explicit set PLUS a shape heuristic.** The explicit set is
   precise and refuses with a pointer to the encrypted route. The heuristic
   catches the *next* credential-shaped key somebody invents — including through
   the generic ``PUT /api/settings/{key}`` catch-all, which is how five previous
   settings-hardening issues (#506, #1609, ent#12, #1644, ent#14, ent#346) each
   found the same door standing open. Unknown-but-credential-shaped is refused,
   not silently stored; reviewed non-secrets are named in
   ``PUBLIC_CREDENTIAL_SHAPED_KEYS`` with a reason.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Suffix used for the encrypted sibling of a legacy cleartext key. Matches the
#: two rows that already shipped this way (``elevenlabs_api_key_encrypted``,
#: ``a2a_outbound_endpoints_encrypted``) — one pattern, not a second one.
ENCRYPTED_SUFFIX = "_encrypted"


def encrypted_key_for(key: str) -> str:
    """The ``system_settings`` key holding the encrypted form of ``key``."""
    return f"{key}{ENCRYPTED_SUFFIX}"


#: The credential-bearing settings migrated by ent#435. Each entry is a legacy
#: cleartext key; its envelope lives under ``encrypted_key_for(key)`` and the
#: envelope's single field is named after the key itself (self-describing, and
#: already the convention for ``github_pat`` in ``db/agent_settings/git_pat.py``
#: and ``db/users.py``).
SECRET_SETTING_KEYS = frozenset({
    "anthropic_api_key",
    "github_pat",
    "google_api_key",
    "slack_app_token",
    "slack_client_secret",
    "slack_signing_secret",
})

#: Encrypted counterparts — precomputed so callers (and the generic-PUT guard)
#: can test membership without recomputing the suffix.
ENCRYPTED_SETTING_KEYS = frozenset(encrypted_key_for(k) for k in SECRET_SETTING_KEYS)

#: Credential-*shaped* keys that are deliberately NOT secrets, each with the
#: reason it is safe in cleartext. Reviewed exemptions only — the point of the
#: heuristic below is that an unreviewed one fails loudly.
PUBLIC_CREDENTIAL_SHAPED_KEYS: Dict[str, str] = {
    # A Slack OAuth client_id is a PUBLIC identifier: `slack_service.get_oauth_url`
    # puts it verbatim in the browser-visible authorize URL query string. Encrypting
    # it would buy nothing and would imply the others are equally optional. Same
    # call as `whatsapp_bindings.account_sid`, which the schema marks "(public)"
    # beside its encrypted `auth_token_encrypted` sibling.
    "slack_client_id": "public OAuth identifier — emitted in the authorize URL",
}

#: Suffixes that make a key look like it holds a credential. Suffix-anchored, not
#: substring: ``elevenlabs_api_key_encrypted`` must NOT match (it ends in
#: ``_encrypted``, which is exempted below anyway, but a substring test would
#: also snare a hypothetical ``api_key_rotation_days``).
_CREDENTIAL_SUFFIXES = (
    "_api_key",
    "_token",
    "_secret",
    "_pat",
    "_password",
    "_credentials",
)


class SecretSettingWriteError(ValueError):
    """A cleartext write to a credential-bearing settings key was refused.

    Raised by :func:`assert_plaintext_write_allowed` at the DB sink. Deliberately
    a domain error, not an ``HTTPException`` — ``db/`` holds no HTTP concerns
    (Invariant #1); ``routers/settings.py`` maps it to 422.
    """

    def __init__(self, key: str, message: str):
        self.key = key
        super().__init__(message)


def is_credential_shaped(key: str) -> bool:
    """Whether ``key`` looks like it holds a credential.

    False for an already-encrypted key (``*_encrypted``) and for the reviewed
    public exemptions, so the guard cannot refuse its own encrypted write.
    """
    if key.endswith(ENCRYPTED_SUFFIX):
        return False
    if key in PUBLIC_CREDENTIAL_SHAPED_KEYS:
        return False
    return any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES)


def assert_plaintext_write_allowed(key: str) -> None:
    """Refuse a cleartext ``system_settings`` write to a credential-bearing key.

    The sink guard (issue item 2). Two tiers, deliberately different messages:

    * a **known** secret key names the route that writes it encrypted, because
      there always is one and the caller is simply using the wrong door;
    * an **unknown but credential-shaped** key cannot be pointed anywhere, so it
      names the two ways forward (encrypt via ``settings_service``, or record it
      in ``PUBLIC_CREDENTIAL_SHAPED_KEYS`` with a reason if it is not a secret).

    Raises ``SecretSettingWriteError``; returns None otherwise.
    """
    if key in SECRET_SETTING_KEYS:
        raise SecretSettingWriteError(
            key,
            f"'{key}' holds a live credential and may not be stored in cleartext "
            f"(ent#435, CWE-312). It is persisted AES-256-GCM encrypted under "
            f"'{encrypted_key_for(key)}' — write it via its dedicated settings "
            f"route, which encrypts on the way in.",
        )
    if is_credential_shaped(key):
        raise SecretSettingWriteError(
            key,
            f"'{key}' is credential-shaped and may not be stored in cleartext "
            f"(ent#435, CWE-312). Persist it via "
            f"services.settings_service.set_secret_setting() so it lands "
            f"AES-256-GCM encrypted under '{encrypted_key_for(key)}' — or, if it "
            f"is genuinely not a secret, record it in "
            f"services.secret_settings.PUBLIC_CREDENTIAL_SHAPED_KEYS with the "
            f"reason it is safe in cleartext.",
        )


# =============================================================================
# Envelope helpers
# =============================================================================

def encrypt_secret_setting(key: str, value: str) -> str:
    """Wrap ``value`` in an AES-256-GCM envelope keyed by the setting name.

    Raises whatever ``CredentialEncryptionService`` raises when
    ``CREDENTIAL_ENCRYPTION_KEY`` is unset or malformed — fail-CLOSED on the
    write path is the point: silently falling back to cleartext is the defect.
    (``scripts/deploy/start.sh`` auto-generates the key via
    ``ensure_hex32_secret``, so a supported deployment always has one.)
    """
    from services.credential_encryption import CredentialEncryptionService

    return CredentialEncryptionService().encrypt({key: value})


def decrypt_secret_setting(key: str, envelope: str) -> Optional[str]:
    """Unwrap an envelope written by :func:`encrypt_secret_setting`.

    Returns None on any failure (wrong key, corrupt row, not an envelope) so a
    single bad row degrades that one credential to its env fallback instead of
    500-ing every caller — the ``get_elevenlabs_api_key`` discipline. The caller
    logs; this stays quiet enough to be usable from a hot read path.
    """
    from services.credential_encryption import CredentialEncryptionService

    try:
        decrypted = CredentialEncryptionService().decrypt(envelope)
    except Exception as e:  # noqa: BLE001 — any failure is "unreadable"
        logger.error(f"Failed to decrypt secret setting '{key}': {e}")
        return None
    value = decrypted.get(key)
    return value if isinstance(value, str) else None


def looks_like_envelope(value: str) -> bool:
    """Whether a stored value is already an AES-256-GCM envelope.

    Structural, not a decrypt: the migration must be able to SKIP an
    already-encrypted row without holding the key, and must never mistake a
    cleartext credential for one. An envelope is the JSON document
    ``CredentialEncryptionService.encrypt`` emits — object-shaped with the
    ``algorithm``/``nonce``/``ciphertext`` triple. No real API key parses as
    that, and the check is cheap enough for the read path.
    """
    if not value or not value.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(parsed, dict)
        and "ciphertext" in parsed
        and "nonce" in parsed
        and "algorithm" in parsed
    )


# =============================================================================
# Migration planning (shared by BOTH tracks — Invariant #9)
# =============================================================================

def plan_migration(rows: Iterable[Tuple[str, Optional[str]]]) -> List[Tuple[str, str, str]]:
    """Decide what a one-shot cleartext→encrypted sweep should write.

    ``rows`` is ``[(key, value)]`` straight from ``system_settings``; the return
    is ``[(legacy_key, encrypted_key, envelope)]`` — for each, the caller UPSERTs
    the envelope under ``encrypted_key`` and DELETEs ``legacy_key``.

    Driver-free ON PURPOSE (it encrypts, so it is not side-effect-free, but it
    issues no SQL and knows no cursor). The SQLite runner holds a raw ``sqlite3``
    cursor and the Alembic revision holds a SQLAlchemy ``Connection``, so the two
    tracks cannot share SQL — but they MUST share the decision, or the dual-track
    requirement (Invariant #9) ships two subtly different sweeps. Only ~6 lines
    of plumbing are duplicated; none of the policy is.

    Skips: a blank value (nothing to protect, and an empty envelope would read as
    "configured"), and a value that is already an envelope (idempotent, so a
    re-run or a half-applied sweep converges).
    """
    plan: List[Tuple[str, str, str]] = []
    for key, value in rows:
        if key not in SECRET_SETTING_KEYS:
            continue
        if not value or not value.strip():
            continue
        if looks_like_envelope(value):
            continue
        plan.append((key, encrypted_key_for(key), encrypt_secret_setting(key, value)))
    return plan
