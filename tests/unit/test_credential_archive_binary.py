"""Binary-safe `.credentials.enc` round-trip (#11).

The v2 archive carries text in ``files`` and base64 binary in ``files_b64`` so
cert/key material (.p12/.pfx/DER) survives export → import. Legacy flat
archives (and the single-secret SIEM/2FA/SSO callers) must keep working
unchanged through ``encrypt``/``decrypt``.
"""
from __future__ import annotations

import base64

from services.credential_encryption import CredentialEncryptionService

_KEY = "ab" * 32  # 64 hex chars → 32 bytes


def _svc():
    return CredentialEncryptionService(key=_KEY)


def test_text_and_binary_round_trip():
    svc = _svc()
    raw = bytes(range(256)) * 4  # non-UTF-8 binary blob
    text = {".env": "A=1\n", ".config/gcloud/sa.json": '{"type":"service_account"}'}
    binary = {"client.p12": base64.b64encode(raw).decode("ascii")}

    blob = svc.encrypt_files(text, binary)
    files, files_b64 = svc.decrypt_files(blob)

    assert files == text
    assert files_b64 == binary
    assert base64.b64decode(files_b64["client.p12"]) == raw  # bytes intact


def test_legacy_flat_archive_still_decrypts():
    """An archive written by the old `encrypt({path: text})` path reads back as
    all-text with no binary (back-compat)."""
    svc = _svc()
    legacy = svc.encrypt({".env": "A=1\n", ".mcp.json": "{}"})
    files, files_b64 = svc.decrypt_files(legacy)
    assert files == {".env": "A=1\n", ".mcp.json": "{}"}
    assert files_b64 == {}


def test_single_secret_callers_unaffected():
    """SIEM/2FA/SSO use encrypt({k: v}) / decrypt()[k] — must be untouched."""
    svc = _svc()
    assert svc.decrypt(svc.encrypt({"oidc_client_secret": "shh"})) == {"oidc_client_secret": "shh"}
