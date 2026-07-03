"""
Credential encryption key rotation (#267).

Before #267 the AES-256-GCM master key (`CREDENTIAL_ENCRYPTION_KEY`) had no
rotation path: changing it bricked every `.credentials.enc` and DB token. #267
adds a decrypt-only secondary key + a `rewrap()` primitive so an operator can
rotate the key online (old key decrypts, new key encrypts) and then sweep
persisted secrets onto the new key.

These pin the service primitive (the sweep script composes it):
  - single-key behavior is unchanged when no secondary is set (back-compat);
  - secondary key decrypts ciphertext written under the previous key;
  - `rewrap` migrates an old-key envelope onto the primary;
  - a wrong/absent key still fails closed;
  - an invalid secondary is ignored, never breaking the primary path.
"""
import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from services.credential_encryption import (  # noqa: E402
    CredentialEncryptionService,
    SECONDARY_ENCRYPTION_KEY_ENV,
)

pytestmark = pytest.mark.unit

KEY_A = "11" * 32  # 64 hex chars = 32 bytes
KEY_B = "22" * 32
SECRET = {"bot_token": "xoxb-super-secret"}


@pytest.fixture(autouse=True)
def _clear_secondary(monkeypatch):
    monkeypatch.delenv(SECONDARY_ENCRYPTION_KEY_ENV, raising=False)


def test_single_key_roundtrip_unchanged():
    svc = CredentialEncryptionService(key=KEY_A)
    assert svc.decrypt(svc.encrypt(SECRET)) == SECRET
    assert svc.aesgcm_secondary is None  # no fallback configured


def test_secondary_key_decrypts_old_ciphertext(monkeypatch):
    old = CredentialEncryptionService(key=KEY_A).encrypt(SECRET)
    # Rotate: primary = new key B, secondary = old key A.
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_A)
    svc_b = CredentialEncryptionService(key=KEY_B)
    assert svc_b.decrypt(old) == SECRET  # opened via the fallback


def test_rewrap_migrates_onto_primary(monkeypatch):
    old = CredentialEncryptionService(key=KEY_A).encrypt(SECRET)
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_A)
    migrated = CredentialEncryptionService(key=KEY_B).rewrap(old)

    # After rewrap the new envelope opens under the primary key ALONE — proving
    # it no longer depends on the old key (so the secondary can be retired).
    monkeypatch.delenv(SECONDARY_ENCRYPTION_KEY_ENV, raising=False)
    assert CredentialEncryptionService(key=KEY_B).decrypt(migrated) == SECRET
    # ...and the ORIGINAL ciphertext does NOT open under B alone.
    with pytest.raises(ValueError):
        CredentialEncryptionService(key=KEY_B).decrypt(old)


def test_wrong_key_without_secondary_fails_closed():
    old = CredentialEncryptionService(key=KEY_A).encrypt(SECRET)
    with pytest.raises(ValueError, match="Decryption failed"):
        CredentialEncryptionService(key=KEY_B).decrypt(old)


def test_invalid_secondary_is_ignored(monkeypatch):
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, "not-hex-garbage")
    svc = CredentialEncryptionService(key=KEY_A)
    # invalid secondary disabled (not raised), primary still works
    assert svc.aesgcm_secondary is None
    assert svc.decrypt(svc.encrypt(SECRET)) == SECRET


def test_rewrap_idempotent_on_primary():
    svc = CredentialEncryptionService(key=KEY_A)
    enc = svc.encrypt(SECRET)
    assert svc.decrypt(svc.rewrap(enc)) == SECRET
