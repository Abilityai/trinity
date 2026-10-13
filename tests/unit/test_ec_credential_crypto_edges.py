"""/edge-cases 2026-10-06 — credential crypto: parametrized boundary cases.

Targets
-------
* ``services/credential_encryption.py`` — the AES-256-GCM envelope every
  Invariant #12 secret rides (``CredentialEncryptionService.key`` /
  ``aesgcm_secondary`` / ``encrypt`` / ``decrypt`` / ``encrypt_files`` /
  ``decrypt_files`` / ``rewrap``) and the ``.credentials.enc`` import/export
  orchestration (``import_to_agent`` / ``export_to_agent`` and the three agent
  HTTP readers), mocked at the ``agent_httpx_client`` seam.
* ``services/secret_settings.py`` — the ent#435 policy leaf: the key classifier
  (``SECRET_SETTING_KEYS``, suffix heuristic, ``PUBLIC_CREDENTIAL_SHAPED_KEYS``,
  ``_encrypted`` exemption), the sink guard, the envelope helpers and the
  ``plan_migration`` decision shared by both migration tracks.

Companion to ``test_ec_credential_crypto_properties.py`` (Hypothesis invariants).
Matrix + coverage + mutation results: the 2026-10-06 /edge-cases matrix.
Every ``id`` below carries its matrix row number (``Mnn``).

Pure unit tests: no DB, no Docker, no network. Every key and secret here is
synthetic (``"a1" * 32`` etc.). The process-wide ``CREDENTIAL_ENCRYPTION_KEY``
is pinned per test via ``monkeypatch`` and the rotation secondary is always
cleared first, so an operator shell that exports either cannot leak in.

Two real bugs found here were fixed by #3325:
  * ``decrypt`` broke its "raises ValueError" contract on a non-object JSON
    document or a non-string nonce (AttributeError / TypeError), and
    ``.credentials.enc`` is agent-writable, so the import route answered 500
    where it promises 400 (M50a-g).
  * ``encrypt_secret_setting`` reported a lone-surrogate VALUE as a missing
    ``CREDENTIAL_ENCRYPTION_KEY`` — ``UnicodeEncodeError`` is a ``ValueError``
    (M58).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.credential_encryption import (  # noqa: E402
    DEFAULT_CREDENTIAL_FILES,
    ENCRYPTION_KEY_ENV,
    SECONDARY_ENCRYPTION_KEY_ENV,
    CredentialEncryptionService,
    CredentialsFileNotFoundError,
    get_credential_encryption_service,
)
from services.secret_settings import (  # noqa: E402
    ENCRYPTED_SETTING_KEYS,
    ENCRYPTED_SUFFIX,
    PUBLIC_CREDENTIAL_SHAPED_KEYS,
    SECRET_SETTING_KEYS,
    MissingEncryptionKeyError,
    SecretSettingWriteError,
    assert_plaintext_write_allowed,
    decrypt_secret_setting,
    encrypt_secret_setting,
    encrypted_key_for,
    is_credential_shaped,
    looks_like_envelope,
    plan_migration,
)

pytestmark = pytest.mark.unit

# Patch through the class's OWN module globals, never `import ... as ce`: some
# neighbouring tests swap `sys.modules` entries, after which a module alias can
# point at a different object than the one these methods resolve names in.
_CE_GLOBALS = CredentialEncryptionService.encrypt.__globals__

KEY_A = "a1" * 32  # synthetic 32-byte keys, hex
KEY_B = "b2" * 32
SECRET = {"bot_token": "synthetic-token-value"}


@pytest.fixture(autouse=True)
def _pinned_env(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, KEY_A)
    monkeypatch.delenv(SECONDARY_ENCRYPTION_KEY_ENV, raising=False)


def _svc(key=KEY_A):
    return CredentialEncryptionService(key=key)


def _envelope_with(**overrides):
    """A valid KEY_A envelope with fields overridden (``None`` deletes)."""
    data = json.loads(_svc().encrypt(SECRET))
    for k, v in overrides.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return json.dumps(data)


def _raw_envelope(plaintext: bytes, key=KEY_A, nonce=b"\x00" * 12):
    """An envelope around ARBITRARY plaintext bytes — only producible by a key holder."""
    ct = AESGCM(bytes.fromhex(key)).encrypt(nonce, plaintext, None)
    return json.dumps({
        "version": 1, "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ct).decode(),
    })


# =============================================================================
# Key loading (CREDENTIAL_ENCRYPTION_KEY)
# =============================================================================

@pytest.mark.parametrize("env_value, match", [
    pytest.param(None, "not configured", id="M01-env-unset"),
    pytest.param("", "not configured", id="M02-env-empty"),
    pytest.param("zz" * 32, "must be hex", id="M03-non-hex"),
    pytest.param("0x" + "a1" * 31, "must be hex", id="M04-0x-prefix"),
    pytest.param("a1" * 31, "got 31 bytes", id="M05-31-bytes"),
    pytest.param("a1" * 33, "got 33 bytes", id="M06-33-bytes"),
    pytest.param("a1" * 16, "got 16 bytes", id="M07-aes128-length"),
    pytest.param("   ", "got 0 bytes", id="M08-whitespace-only"),
    pytest.param("a1" * 31 + "a", "must be hex", id="M09-odd-length"),
])
def test_missing_or_malformed_primary_key_fails_closed(monkeypatch, env_value, match):
    if env_value is None:
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, env_value)
    svc = CredentialEncryptionService()
    with pytest.raises(ValueError, match=match):
        svc.encrypt(SECRET)


@pytest.mark.parametrize("variant", [
    pytest.param(KEY_A.upper(), id="M10-uppercase-hex"),
    pytest.param(KEY_A + "\n", id="M11-trailing-newline"),
    pytest.param(" " + KEY_A + " ", id="M12-surrounding-spaces"),
])
def test_equivalent_key_spellings_open_the_same_envelopes(monkeypatch, variant):
    """`bytes.fromhex` ignores ASCII whitespace and case: a `.env` line with a
    trailing newline or an upper-cased paste must still decrypt the fleet."""
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, variant)
    assert CredentialEncryptionService().decrypt(_svc().encrypt(SECRET)) == SECRET


def test_explicit_key_outranks_env(monkeypatch):  # M13
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, KEY_B)
    blob = CredentialEncryptionService(key=KEY_A).encrypt(SECRET)
    assert _svc(KEY_A).decrypt(blob) == SECRET
    with pytest.raises(ValueError):
        CredentialEncryptionService().decrypt(blob)  # env KEY_B cannot open it


def test_empty_explicit_key_falls_back_to_env():  # M14
    assert CredentialEncryptionService(key="").decrypt(_svc().encrypt(SECRET)) == SECRET


def test_cipher_is_cached_after_first_use(monkeypatch):  # M15
    """Characterization: the primary cipher is resolved once per instance, so a
    key change needs a new instance (the singleton ⇒ a restart)."""
    svc = CredentialEncryptionService()
    blob = svc.encrypt(SECRET)
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, KEY_B)
    assert svc.decrypt(blob) == SECRET
    with pytest.raises(ValueError):
        CredentialEncryptionService().decrypt(blob)


# =============================================================================
# Envelope round-trip
# =============================================================================

@pytest.mark.parametrize("files", [
    pytest.param({}, id="M16-empty-dict"),
    pytest.param({".env": ""}, id="M17-empty-value"),
    pytest.param({"": "x"}, id="M18-empty-path"),
    pytest.param({".env": "K=🔑 שלום é ​"}, id="M19-unicode-emoji-rtl-combining-zwsp"),
    pytest.param({".env": "A=\x00B\x01\x7f"}, id="M20-nul-and-control"),
    pytest.param({".env": "".join(chr(i) for i in range(256))}, id="M21-binaryish-latin1"),
    pytest.param({"ключ/路径.json": "v"}, id="M22-unicode-path"),
    pytest.param({".env": "x" * (2 * 1024 * 1024)}, id="M23-2MiB-value"),
    pytest.param({f"f{i}": str(i) for i in range(2000)}, id="M24-2000-files"),
])
def test_round_trip_is_exact(files):
    svc = _svc()
    assert svc.decrypt(svc.encrypt(files)) == files


def test_envelope_shape_is_the_documented_v1_format():  # M25
    plaintext = json.dumps(SECRET, ensure_ascii=False).encode()
    env = json.loads(_svc().encrypt(SECRET))
    assert set(env) == {"version", "algorithm", "nonce", "ciphertext"}
    assert env["version"] == 1 and env["algorithm"] == "AES-256-GCM"
    assert len(base64.b64decode(env["nonce"])) == 12
    # GCM: ciphertext = plaintext length + 16-byte tag (no padding, no compression).
    assert len(base64.b64decode(env["ciphertext"])) == len(plaintext) + 16


def test_every_encrypt_uses_a_fresh_nonce():  # M26
    svc = _svc()
    nonces = {json.loads(svc.encrypt(SECRET))["nonce"] for _ in range(50)}
    assert len(nonces) == 50


def test_lone_surrogate_is_refused_not_stored():  # M27
    """A JSON body can carry `\\ud800`; it has no UTF-8 form. Refusing is right
    (fail-closed) — the misattribution one layer up is the bug, see M58."""
    with pytest.raises(ValueError):
        _svc().encrypt({".env": "A=\ud800"})


# =============================================================================
# Decrypt: tampered / truncated / malformed envelopes
# =============================================================================

def _flip_ct_byte(i):
    env = json.loads(_svc().encrypt(SECRET))
    ct = bytearray(base64.b64decode(env["ciphertext"]))
    ct[i] ^= 0x01
    env["ciphertext"] = base64.b64encode(bytes(ct)).decode()
    return json.dumps(env)


def _truncate_ct(n):
    env = json.loads(_svc().encrypt(SECRET))
    ct = base64.b64decode(env["ciphertext"])
    env["ciphertext"] = base64.b64encode(ct[:n]).decode()
    return json.dumps(env)


@pytest.mark.parametrize("blob, match", [
    pytest.param("", "Invalid encrypted data format", id="M28-empty-string"),
    pytest.param("not json", "Invalid encrypted data format", id="M29-not-json"),
    pytest.param("{", "Invalid encrypted data format", id="M30-truncated-json"),
    pytest.param("{}", "Unsupported encryption version", id="M31-empty-object"),
    pytest.param(_envelope_with(version=2), "Unsupported encryption version", id="M32-version-2"),
    pytest.param(_envelope_with(version="1"), "Unsupported encryption version", id="M33-version-string"),
    pytest.param(_envelope_with(version=None), "Unsupported encryption version", id="M34-version-missing"),
    pytest.param(_envelope_with(algorithm="AES-128-GCM"), "Unsupported algorithm", id="M35-wrong-algorithm"),
    pytest.param(_envelope_with(algorithm="aes-256-gcm"), "Unsupported algorithm", id="M36-algorithm-case"),
    pytest.param(_envelope_with(nonce=None), "Invalid encrypted data structure", id="M37-nonce-missing"),
    pytest.param(_envelope_with(ciphertext=None), "Invalid encrypted data structure", id="M38-ct-missing"),
    pytest.param(_envelope_with(nonce="a"), "Invalid encrypted data structure", id="M39-nonce-bad-padding"),
    pytest.param(_envelope_with(nonce="!!!!"), "Decryption failed", id="M40-nonce-decodes-empty"),
    pytest.param(_envelope_with(nonce=base64.b64encode(b"\x00" * 12).decode()), "Decryption failed", id="M41-nonce-swapped"),
    pytest.param(_envelope_with(ciphertext=""), "Decryption failed", id="M42-ct-empty"),
    pytest.param(_flip_ct_byte(0), "Decryption failed", id="M43-ct-first-byte-flipped"),
    pytest.param(_flip_ct_byte(-1), "Decryption failed", id="M44-tag-last-byte-flipped"),
    pytest.param(_truncate_ct(-16), "Decryption failed", id="M45-tag-stripped"),
    pytest.param(_truncate_ct(-1), "Decryption failed", id="M46-truncated-by-one"),
    pytest.param(_raw_envelope(b"not json"), "not valid JSON", id="M47-plaintext-not-json"),
    pytest.param(_raw_envelope(b"\xff\xfe"), "not valid JSON", id="M48-plaintext-not-utf8"),
])
def test_bad_envelope_raises_valueerror(blob, match):
    with pytest.raises(ValueError, match=match):
        _svc().decrypt(blob)


def test_wrong_key_fails_closed_and_names_the_cause():  # M49
    blob = _svc(KEY_A).encrypt(SECRET)
    with pytest.raises(ValueError, match="wrong key or corrupted data"):
        _svc(KEY_B).decrypt(blob)


@pytest.mark.parametrize("blob", [
    pytest.param("[]", id="M50a-json-array"),
    pytest.param("null", id="M50b-json-null"),
    pytest.param("1", id="M50c-json-number"),
    pytest.param('"x"', id="M50d-json-string"),
    pytest.param(_envelope_with(nonce=12), id="M50e-nonce-int"),
    pytest.param(_envelope_with(ciphertext=["x"]), id="M50f-ct-list"),
])
def test_non_object_or_non_string_envelope_raises_valueerror(blob):
    with pytest.raises(ValueError):
        _svc().decrypt(blob)


def test_import_of_non_object_archive_is_a_valueerror():  # M50g
    svc = _svc()
    svc.read_agent_credential_files = AsyncMock(return_value={".credentials.enc": "[]"})
    svc.write_agent_credential_files = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(svc.import_to_agent("agent-x"))
    svc.write_agent_credential_files.assert_not_awaited()


# =============================================================================
# Key rotation (#267)
# =============================================================================

@pytest.mark.parametrize("secondary", [
    pytest.param("not-hex", id="M51-secondary-non-hex"),
    pytest.param("a1" * 16, id="M52-secondary-16-bytes"),
    pytest.param("", id="M53-secondary-empty"),
])
def test_invalid_secondary_is_ignored_never_breaks_primary(monkeypatch, secondary):
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, secondary)
    svc = _svc(KEY_A)
    assert svc.aesgcm_secondary is None
    assert svc.decrypt(svc.encrypt(SECRET)) == SECRET
    with pytest.raises(ValueError, match="wrong key"):
        svc.decrypt(_svc(KEY_B).encrypt(SECRET))


def test_both_keys_wrong_reports_failure(monkeypatch):  # M54
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_B)
    foreign = _svc("c3" * 32).encrypt(SECRET)
    with pytest.raises(ValueError, match="wrong key or corrupted data"):
        _svc(KEY_A).decrypt(foreign)


def test_secondary_only_decrypts_new_writes_use_primary(monkeypatch):  # M55
    old = _svc(KEY_A).encrypt(SECRET)
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_A)
    rotated = _svc(KEY_B)
    new = rotated.encrypt(SECRET)
    assert rotated.decrypt(old) == SECRET
    with pytest.raises(ValueError):
        _svc(KEY_A).decrypt(new)  # written under the primary, not the secondary
    # rewrap of an already-primary envelope is content-idempotent, fresh nonce
    again = rotated.rewrap(new)
    assert again != new and _svc(KEY_B).decrypt(again) == SECRET


def test_secondary_cipher_is_cached(monkeypatch):  # M56
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_B)
    svc = _svc(KEY_A)
    first = svc.aesgcm_secondary
    monkeypatch.delenv(SECONDARY_ENCRYPTION_KEY_ENV)
    assert svc.aesgcm_secondary is first


def test_rewrap_of_tampered_envelope_raises(monkeypatch):  # M57
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_A)
    with pytest.raises(ValueError):
        _svc(KEY_B).rewrap(_flip_ct_byte(3))


def test_primary_unset_secondary_set_still_decrypts(monkeypatch):  # M57b
    """Characterization (UNSPECIFIED in the report): with the primary missing the
    decrypt path silently runs on the decrypt-only secondary; writes fail closed."""
    blob = _svc(KEY_B).encrypt(SECRET)
    monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_B)
    svc = CredentialEncryptionService()
    assert svc.decrypt(blob) == SECRET
    with pytest.raises(ValueError, match="not configured"):
        svc.encrypt(SECRET)


# =============================================================================
# v2 archive / legacy flat archive
# =============================================================================

@pytest.mark.parametrize("inner, expected", [
    pytest.param({".env": "A=1"}, ({".env": "A=1"}, {}), id="M60-legacy-flat"),
    pytest.param({}, ({}, {}), id="M61-legacy-empty"),
    pytest.param({"__cred_archive_v2__": 0, "files": {"a": "b"}},
                 ({"__cred_archive_v2__": 0, "files": {"a": "b"}}, {}), id="M62-v2-marker-falsy"),
    pytest.param({"__cred_archive_v2__": 1}, ({}, {}), id="M63-v2-no-sections"),
])
def test_decrypt_files_dispatch(inner, expected):
    svc = _svc()
    assert svc.decrypt_files(svc.encrypt(inner)) == expected


def test_encrypt_files_none_sections_become_empty():  # M64
    svc = _svc()
    assert svc.decrypt_files(svc.encrypt_files(None, None)) == ({}, {})


# =============================================================================
# import_to_agent / export_to_agent (I/O mocked at the method seam)
# =============================================================================

def _io_svc(read=None, split=None, listed=None):
    svc = _svc()
    svc.read_agent_credential_files = AsyncMock(return_value=read or {})
    svc.read_agent_credential_files_split = AsyncMock(return_value=split or ({}, {}))
    svc.list_agent_credential_files = AsyncMock(return_value=listed or [])
    svc.write_agent_credential_files = AsyncMock(return_value={"ok": True})
    return svc


@pytest.mark.parametrize("read", [
    pytest.param({}, id="M65-file-absent"),
    pytest.param({".credentials.enc": ""}, id="M66-file-empty"),
])
def test_import_without_archive_is_the_skip_type(read):
    svc = _io_svc(read=read)
    with pytest.raises(CredentialsFileNotFoundError):
        asyncio.run(svc.import_to_agent("a"))
    assert issubclass(CredentialsFileNotFoundError, ValueError)  # 400 on the admin route


@pytest.mark.parametrize("files, files_b64, match", [
    pytest.param({}, {}, "contains no credential files", id="M67-empty-archive"),
    pytest.param({"../etc/passwd": "x"}, {}, "disallowed", id="M68-traversal-path"),
    pytest.param({}, {".mcp.json": "YQ=="}, "may not be binary", id="M69-binary-mcp-json"),
    pytest.param({".mcp.json": "not json"}, {}, "Invalid .mcp.json", id="M70-invalid-mcp-json"),
])
def test_import_refuses_before_writing(files, files_b64, match):
    blob = _svc().encrypt_files(files, files_b64)
    svc = _io_svc(read={".credentials.enc": blob})
    with pytest.raises(ValueError, match=match):
        asyncio.run(svc.import_to_agent("a"))
    svc.write_agent_credential_files.assert_not_awaited()


def test_import_happy_path_writes_text_and_binary():  # M71
    blob = _svc().encrypt_files({".env": "A=1\n"}, {"client.p12": "YWJj"})
    svc = _io_svc(read={".credentials.enc": blob})
    out = asyncio.run(svc.import_to_agent("a"))
    assert out == {".env": "A=1\n", "client.p12": "YWJj"}
    svc.write_agent_credential_files.assert_awaited_once_with("a", {".env": "A=1\n"}, {"client.p12": "YWJj"})


def test_import_under_rotated_key_uses_secondary(monkeypatch):  # M72
    blob = _svc(KEY_B).encrypt_files({".env": "A=1\n"})
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_B)
    svc = _io_svc(read={".credentials.enc": blob})
    assert asyncio.run(svc.import_to_agent("a")) == {".env": "A=1\n"}


def test_export_discovers_falls_back_and_never_nests_the_archive():  # M73
    svc = _io_svc(split=({".env": "A=1"}, {}), listed=[".env", ".credentials.enc"])
    path, total = asyncio.run(svc.export_to_agent("a"))
    assert (path, total) == (".credentials.enc", 1)
    svc.read_agent_credential_files_split.assert_awaited_once_with("a", [".env"])
    written = svc.write_agent_credential_files.await_args.args[1][".credentials.enc"]
    assert _svc().decrypt_files(written) == ({".env": "A=1"}, {})


def test_export_falls_back_to_defaults_on_old_images():  # M74
    svc = _io_svc(split=({".env": "A=1"}, {"k.p12": "YQ=="}), listed=[])
    _, total = asyncio.run(svc.export_to_agent("a"))
    assert total == 2
    svc.read_agent_credential_files_split.assert_awaited_once_with("a", list(DEFAULT_CREDENTIAL_FILES))


def test_export_with_nothing_to_export_raises_and_writes_nothing():  # M75
    svc = _io_svc(split=({}, {}))
    with pytest.raises(ValueError, match="No credential files"):
        asyncio.run(svc.export_to_agent("a", [".env"]))
    svc.write_agent_credential_files.assert_not_awaited()


# --- the agent HTTP readers, faked at `agent_httpx_client` -------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _fake_client(resp=None, exc=None):
    calls = []

    class _C:
        async def get(self, url, **kw):
            calls.append((url, kw))
            if exc:
                raise exc
            return resp

        async def post(self, url, **kw):
            calls.append((url, kw))
            return resp

    @asynccontextmanager
    async def factory(_name):
        yield _C()

    return factory, calls


@pytest.mark.parametrize("status, body, expected", [
    pytest.param(200, {"files": {".env": "A"}}, {".env": "A"}, id="M76-read-200"),
    pytest.param(500, {}, {}, id="M77-read-non-200"),
    pytest.param(200, {}, {}, id="M78-read-200-no-files-key"),
])
def test_read_agent_credential_files(status, body, expected):
    factory, calls = _fake_client(_Resp(status, body))
    with patch.dict(_CE_GLOBALS, {"agent_httpx_client": factory}):
        assert asyncio.run(_svc().read_agent_credential_files("a")) == expected
    assert calls[0][1]["params"] == {"paths": ".env,.mcp.json"}


@pytest.mark.parametrize("status, body, expected", [
    pytest.param(200, {"files": {"a": "b"}, "files_b64": {"c": "ZA=="}}, ({"a": "b"}, {"c": "ZA=="}), id="M79-split-200"),
    pytest.param(404, {}, ({}, {}), id="M80-split-non-200"),
])
def test_read_split(status, body, expected):
    factory, _ = _fake_client(_Resp(status, body))
    with patch.dict(_CE_GLOBALS, {"agent_httpx_client": factory}):
        assert asyncio.run(_svc().read_agent_credential_files_split("a", [".env"])) == expected


@pytest.mark.parametrize("resp, exc, expected", [
    pytest.param(_Resp(200, {"files": [{"path": ".env"}, {"path": "x.json"}]}), None, [".env", "x.json"], id="M81-list-200"),
    pytest.param(_Resp(404, {}), None, [], id="M82-list-old-image"),
    pytest.param(None, RuntimeError("down"), [], id="M83-list-transport-error"),
])
def test_list_agent_credential_files(resp, exc, expected):
    factory, _ = _fake_client(resp, exc)
    with patch.dict(_CE_GLOBALS, {"agent_httpx_client": factory}):
        assert asyncio.run(_svc().list_agent_credential_files("a")) == expected


def test_write_non_200_raises_with_agent_detail():  # M84
    factory, _ = _fake_client(_Resp(400, {"detail": "nope"}))
    with patch.dict(_CE_GLOBALS, {"agent_httpx_client": factory}):
        with pytest.raises(RuntimeError, match="nope"):
            asyncio.run(_svc().write_agent_credential_files("a", {".env": "x"}))


def test_write_200_sends_empty_b64_by_default():  # M85
    factory, calls = _fake_client(_Resp(200, {"written": [".env"]}))
    with patch.dict(_CE_GLOBALS, {"agent_httpx_client": factory}):
        assert asyncio.run(_svc().write_agent_credential_files("a", {".env": "x"})) == {"written": [".env"]}
    assert calls[0][1]["json"] == {"files": {".env": "x"}, "files_b64": {}}


def test_singleton_is_reused():  # M86
    with patch.dict(_CE_GLOBALS, {"_service": None}):
        first = get_credential_encryption_service()
        assert isinstance(first, CredentialEncryptionService)
        assert get_credential_encryption_service() is first


# =============================================================================
# secret_settings — key classifier + sink guard
# =============================================================================

@pytest.mark.parametrize("key", sorted(SECRET_SETTING_KEYS))
def test_every_registered_secret_is_also_credential_shaped(key):  # M87
    """Belt and braces: removing a key from the explicit set must still leave the
    heuristic refusing it."""
    assert is_credential_shaped(key)


@pytest.mark.parametrize("key", sorted(SECRET_SETTING_KEYS))
def test_known_secret_gets_the_route_message_not_the_heuristic_one(key):  # M88
    with pytest.raises(SecretSettingWriteError) as exc:
        assert_plaintext_write_allowed(key)
    assert exc.value.key == key
    assert "dedicated settings route" in str(exc.value)
    assert "PUBLIC_CREDENTIAL_SHAPED_KEYS" not in str(exc.value)


@pytest.mark.parametrize("key", [
    pytest.param("openai_api_key", id="M89-suffix-api_key"),
    pytest.param("discord_bot_token", id="M90-suffix-token"),
    pytest.param("oidc_client_secret", id="M91-suffix-secret"),
    pytest.param("gitlab_pat", id="M92-suffix-pat"),
    pytest.param("smtp_password", id="M93-suffix-password"),
    pytest.param("gcp_credentials", id="M94-suffix-credentials"),
    pytest.param("x_api_key", id="M95-minimal-prefix"),
    pytest.param("_token", id="M96-suffix-is-whole-key"),
    pytest.param("slack_client_id_token", id="M97-exempt-key-plus-suffix"),
])
def test_unregistered_credential_shaped_key_is_refused(key):
    assert is_credential_shaped(key)
    with pytest.raises(SecretSettingWriteError) as exc:
        assert_plaintext_write_allowed(key)
    msg = str(exc.value)
    assert "PUBLIC_CREDENTIAL_SHAPED_KEYS" in msg and encrypted_key_for(key) in msg


@pytest.mark.parametrize("key", [
    pytest.param("slack_client_id", id="M98-public-exemption"),
    pytest.param("api_key_rotation_days", id="M99-substring-not-suffix"),
    pytest.param("token_ttl_seconds", id="M100-prefix-not-suffix"),
    pytest.param("pat_enabled", id="M101-pat-prefix"),
    pytest.param("github_pat_encrypted", id="M102-encrypted-sibling"),
    pytest.param("github_pat_encrypted_encrypted", id="M103-suffix-doubled"),
    pytest.param("elevenlabs_api_key_encrypted", id="M104-preexisting-encrypted-row"),
    pytest.param("a2a_outbound_endpoints_encrypted", id="M105-a2a-encrypted-row"),
    pytest.param("session_tab_enabled", id="M106-ordinary"),
    pytest.param("", id="M107-empty-key"),
    pytest.param("max_tokens", id="M108-plural-tokens"),
    pytest.param("secret_settings_version", id="M109-secret-prefix"),
])
def test_non_credential_keys_pass_the_guard(key):
    assert not is_credential_shaped(key)
    assert assert_plaintext_write_allowed(key) is None


def test_public_exemption_is_genuinely_credential_shaped_or_documented():  # M110
    """Every exemption must carry a non-empty reason (reviewed, not overlooked)."""
    for key, reason in PUBLIC_CREDENTIAL_SHAPED_KEYS.items():
        assert reason.strip(), key
        assert key not in SECRET_SETTING_KEYS


def test_encrypted_key_set_is_exactly_the_suffixed_secrets():  # M111
    assert ENCRYPTED_SETTING_KEYS == {k + ENCRYPTED_SUFFIX for k in SECRET_SETTING_KEYS}
    assert all(not is_credential_shaped(k) for k in ENCRYPTED_SETTING_KEYS)
    assert all(assert_plaintext_write_allowed(k) is None for k in ENCRYPTED_SETTING_KEYS)


def test_secret_setting_write_error_is_a_valueerror():  # M112
    err = SecretSettingWriteError("k", "m")
    assert isinstance(err, ValueError) and err.key == "k" and str(err) == "m"


# =============================================================================
# secret_settings — envelope helpers
# =============================================================================

@pytest.mark.parametrize("value", [
    pytest.param("", id="M113-empty"),
    pytest.param("sk-ant-api03-synthetic", id="M114-ascii"),
    pytest.param("🔑שלום​\x00", id="M115-unicode-nul"),
    pytest.param("x" * 100_000, id="M116-100k"),
])
def test_secret_setting_round_trip(value):
    assert decrypt_secret_setting("github_pat", encrypt_secret_setting("github_pat", value)) == value


@pytest.mark.parametrize("env_value", [
    pytest.param(None, id="M117-key-unset"),
    pytest.param("nothex", id="M118-key-non-hex"),
    pytest.param("a1" * 8, id="M119-key-short"),
])
def test_encrypt_secret_setting_fails_closed_with_context(monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    else:
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, env_value)
    with pytest.raises(MissingEncryptionKeyError) as exc:
        encrypt_secret_setting("github_pat", "synthetic-pat-value")
    assert "synthetic-pat-value" not in str(exc.value)
    assert isinstance(exc.value.__cause__, ValueError)


@pytest.mark.parametrize("make_envelope", [
    pytest.param(lambda: _svc(KEY_B).encrypt({"github_pat": "v"}), id="M120-wrong-key"),
    pytest.param(lambda: "plain-cleartext", id="M121-not-an-envelope"),
    pytest.param(lambda: "", id="M122-empty"),
    pytest.param(lambda: _svc().encrypt({"anthropic_api_key": "v"}), id="M123-envelope-for-other-key"),
    pytest.param(lambda: _svc().encrypt({"github_pat": 123}), id="M124-non-str-value"),
    pytest.param(lambda: _svc().encrypt({"github_pat": None}), id="M125-null-value"),
    pytest.param(lambda: "[]", id="M126-json-array"),
    pytest.param(lambda: _flip_ct_byte(5), id="M127-tampered"),
])
def test_decrypt_secret_setting_returns_none_on_any_failure(make_envelope):
    assert decrypt_secret_setting("github_pat", make_envelope()) is None


def test_decrypt_secret_setting_honours_rotation_secondary(monkeypatch):  # M128
    old = encrypt_secret_setting("github_pat", "v")
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, KEY_B)
    assert decrypt_secret_setting("github_pat", old) is None
    monkeypatch.setenv(SECONDARY_ENCRYPTION_KEY_ENV, KEY_A)
    assert decrypt_secret_setting("github_pat", old) == "v"


def test_unencodable_value_is_not_misreported_as_missing_key():  # M58
    with pytest.raises(ValueError) as exc:
        encrypt_secret_setting("anthropic_api_key", "sk-ant-api03-\ud800")
    assert not isinstance(exc.value, MissingEncryptionKeyError)


@pytest.mark.parametrize("value, expected", [
    pytest.param("  \n" + json.dumps({"algorithm": "a", "nonce": "n", "ciphertext": "c"}), True, id="M129-leading-ws"),
    pytest.param(json.dumps({"algorithm": "a", "nonce": "n", "ciphertext": "c", "version": 9, "x": 1}), True, id="M130-extra-fields"),
    pytest.param(json.dumps({"nonce": "n", "ciphertext": "c"}), False, id="M131-missing-algorithm"),
    pytest.param(json.dumps({"algorithm": "a", "ciphertext": "c"}), False, id="M132-missing-nonce"),
    pytest.param(json.dumps([{"algorithm": "a", "nonce": "n", "ciphertext": "c"}]), False, id="M133-wrapped-in-array"),
    pytest.param('{"algorithm": "a", "nonce": "n", "ciphertext": "c"', False, id="M134-truncated-json"),
    pytest.param("   ", False, id="M135-whitespace"),
    pytest.param(None, False, id="M136-none"),
    pytest.param("ghp_synthetic{", False, id="M137-brace-not-first"),
])
def test_looks_like_envelope_edges(value, expected):
    assert looks_like_envelope(value) is expected


# =============================================================================
# secret_settings — plan_migration
# =============================================================================

def test_plan_envelopes_decrypt_back_to_the_exact_legacy_value():  # M138
    rows = [("github_pat", "ghp_synthetic"), ("slack_signing_secret", "sig-synthetic")]
    plan = plan_migration(rows)
    assert [p[0] for p in plan] == ["github_pat", "slack_signing_secret"]  # order preserved
    for (legacy, value), (k, enc_key, env) in zip(rows, plan):
        assert k == legacy and enc_key == legacy + ENCRYPTED_SUFFIX
        assert decrypt_secret_setting(legacy, env) == value


@pytest.mark.parametrize("rows", [
    pytest.param([], id="M139-no-rows"),
    pytest.param([("github_pat", ""), ("github_pat", None), ("anthropic_api_key", " \t\n")], id="M140-only-blanks"),
    pytest.param([("github_pat_encrypted", "x"), ("GITHUB_PAT", "x"), ("slack_client_id", "1.2")], id="M141-non-member-keys"),
    pytest.param([("openai_api_key", "sk-synthetic"), ("smtp_password", "pw")], id="M141b-shaped-but-unregistered"),
])
def test_plan_needs_no_key_when_nothing_to_encrypt(monkeypatch, rows):
    """A fresh install must boot without a key it does not yet need (#453 rule)."""
    monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    assert plan_migration(rows) == []


def test_plan_fails_closed_without_key_when_there_is_cleartext(monkeypatch):  # M142
    monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    with pytest.raises(MissingEncryptionKeyError):
        plan_migration([("session_tab_enabled", "true"), ("github_pat", "ghp_synthetic")])


def test_plan_accepts_a_one_shot_generator():  # M143
    plan = plan_migration(r for r in [("github_pat", "ghp_synthetic")])
    assert len(plan) == 1


def test_plan_skips_legacy_row_already_holding_an_envelope_from_another_key():  # M144
    """Structural skip: the plan does not decrypt, so ANY envelope-shaped legacy
    value is left for the read path (which normalises it) — characterization."""
    foreign = _svc(KEY_B).encrypt({"github_pat": "v"})
    assert plan_migration([("github_pat", foreign)]) == []
