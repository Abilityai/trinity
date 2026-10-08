"""/edge-cases 2026-10-06 — credential crypto: Hypothesis invariants.

Companion to ``test_ec_credential_crypto_edges.py`` (named boundaries). This file
asserts the invariants that must hold over the whole input space of
``services/credential_encryption.py`` and ``services/secret_settings.py``:

* ROUND-TRIP — ``decrypt(encrypt(x)) == x`` for any JSON-able ``{str: str}``
  (full unicode minus lone surrogates, which have no UTF-8 form — see edge M27),
  and ``decrypt_secret_setting(k, encrypt_secret_setting(k, v)) == v``.
* ROTATION — an envelope written under the old key decrypts under
  (new primary, old secondary), and ``rewrap`` lands it on the new key alone.
* INTEGRITY — flipping ANY single bit of the nonce or ciphertext fails closed
  with ``ValueError`` (never a silent wrong plaintext).
* CLASSIFIER ORACLE — ``is_credential_shaped`` and the sink guard equal an
  independently-written spec over arbitrary keys; an encrypted sibling name is
  never refusable (the guard cannot block its own write).
* PLAN ORACLE — ``plan_migration`` equals a spec re-implementation, and every
  planned envelope decrypts back to the exact legacy value.

No fixtures are reused across Hypothesis examples: the env is pinned per example
with ``patch.dict`` (a function-scoped ``monkeypatch`` would be built once per
test function, not per example). Synthetic keys and values only.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("hypothesis")
from hypothesis import example, given, settings, strategies as st  # noqa: E402

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.credential_encryption import (  # noqa: E402
    ENCRYPTION_KEY_ENV,
    SECONDARY_ENCRYPTION_KEY_ENV,
    CredentialEncryptionService,
)
from services.secret_settings import (  # noqa: E402
    ENCRYPTED_SUFFIX,
    PUBLIC_CREDENTIAL_SHAPED_KEYS,
    SECRET_SETTING_KEYS,
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

KEY_A = "a1" * 32
KEY_B = "b2" * 32
PROPS = settings(max_examples=200, deadline=None)

# Full unicode except surrogates (no UTF-8 encoding exists for a lone one).
utf8_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200)
file_maps = st.dictionaries(utf8_text, utf8_text, max_size=8)
hex_keys = st.binary(min_size=32, max_size=32).map(bytes.hex)

_SPEC_SUFFIXES = ("_api_key", "_token", "_secret", "_pat", "_password", "_credentials")
# Keys built from the real vocabulary so the oracle is exercised near every branch,
# not just on random noise that never ends in a suffix.
_atoms = st.sampled_from(
    ["", "x", "slack", "github", "client_id", "api_key", "token", "secret", "pat",
     "password", "credentials", "encrypted", "_encrypted", "_api_key", "_token",
     "_secret", "_pat", "_password", "_credentials", "API_KEY", " ", "а"]
)
setting_keys = st.one_of(
    st.lists(_atoms, max_size=4).map("".join),
    st.sampled_from(sorted(SECRET_SETTING_KEYS | set(PUBLIC_CREDENTIAL_SHAPED_KEYS))),
    st.text(max_size=40),
)


def _env(primary=KEY_A, secondary=None):
    env = {ENCRYPTION_KEY_ENV: primary}
    ctx = patch.dict(os.environ, env)
    ctx.start()
    os.environ.pop(SECONDARY_ENCRYPTION_KEY_ENV, None)
    if secondary is not None:
        os.environ[SECONDARY_ENCRYPTION_KEY_ENV] = secondary
    return ctx


# =============================================================================
# CredentialEncryptionService
# =============================================================================

@PROPS
@given(file_maps)
@example({})
@example({".env": ""})
@example({".env": "".join(chr(i) for i in range(256))})
def test_round_trip(files):
    svc = CredentialEncryptionService(key=KEY_A)
    assert svc.decrypt(svc.encrypt(files)) == files


@PROPS
@given(file_maps, hex_keys, hex_keys)
def test_rotation_round_trip_and_rewrap(files, old_key, new_key):
    old_blob = CredentialEncryptionService(key=old_key).encrypt(files)
    ctx = _env(secondary=old_key)
    try:
        rotated = CredentialEncryptionService(key=new_key)
        assert rotated.decrypt(old_blob) == files
        moved = rotated.rewrap(old_blob)
    finally:
        ctx.stop()
    ctx = _env()  # secondary retired
    try:
        assert CredentialEncryptionService(key=new_key).decrypt(moved) == files
    finally:
        ctx.stop()


@settings(max_examples=150, deadline=None)
@given(st.sampled_from(["nonce", "ciphertext"]), st.integers(min_value=0), st.integers(0, 7))
def test_any_single_bit_flip_fails_closed(field, pos, bit):
    ctx = _env()
    try:
        svc = CredentialEncryptionService(key=KEY_A)
        env = json.loads(svc.encrypt({".env": "A=synthetic"}))
        raw = bytearray(base64.b64decode(env[field]))
        raw[pos % len(raw)] ^= 1 << bit
        env[field] = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(ValueError):
            svc.decrypt(json.dumps(env))
    finally:
        ctx.stop()


@PROPS
@given(file_maps, hex_keys)
def test_foreign_key_never_yields_plaintext(files, other):
    blob = CredentialEncryptionService(key=KEY_A).encrypt(files)
    if other == KEY_A:
        return
    ctx = _env()
    try:
        with pytest.raises(ValueError):
            CredentialEncryptionService(key=other).decrypt(blob)
    finally:
        ctx.stop()


# =============================================================================
# secret_settings — classifier oracle
# =============================================================================

def _spec_shaped(key: str) -> bool:
    if key.endswith("_encrypted") or key in PUBLIC_CREDENTIAL_SHAPED_KEYS:
        return False
    return any(key.endswith(s) for s in _SPEC_SUFFIXES)


@PROPS
@given(setting_keys)
@example("slack_client_id")
@example("github_pat_encrypted")
@example("api_key_rotation_days")
@example("_password")
def test_classifier_matches_spec(key):
    assert is_credential_shaped(key) is _spec_shaped(key)
    refused = key in SECRET_SETTING_KEYS or _spec_shaped(key)
    try:
        assert_plaintext_write_allowed(key)
    except SecretSettingWriteError as e:
        assert refused and e.key == key
    else:
        assert not refused


@PROPS
@given(setting_keys)
def test_encrypted_sibling_is_never_refused(key):
    """The guard must never block the write that stores a key's envelope."""
    enc = encrypted_key_for(key)
    assert enc == key + ENCRYPTED_SUFFIX
    assert not is_credential_shaped(enc)
    assert_plaintext_write_allowed(enc)


# =============================================================================
# secret_settings — envelope helpers + plan_migration
# =============================================================================

@PROPS
@given(st.sampled_from(sorted(SECRET_SETTING_KEYS)), utf8_text)
@example("github_pat", "")
def test_secret_setting_round_trip_and_shape(key, value):
    ctx = _env()
    try:
        env = encrypt_secret_setting(key, value)
        assert looks_like_envelope(env)
        assert decrypt_secret_setting(key, env) == value
        other = next(k for k in sorted(SECRET_SETTING_KEYS) if k != key)
        assert decrypt_secret_setting(other, env) is None  # field is the key name
    finally:
        ctx.stop()


@PROPS
@given(st.text(max_size=80).filter(lambda s: not s.lstrip().startswith("{")))
def test_non_object_text_is_never_an_envelope(value):
    assert looks_like_envelope(value) is False


_row_values = st.one_of(st.none(), st.just(""), st.just("  \t"), utf8_text)
_row_keys = st.one_of(
    st.sampled_from(sorted(SECRET_SETTING_KEYS)),
    st.sampled_from(["slack_client_id", "session_tab_enabled", "github_pat_encrypted", "GITHUB_PAT", "openai_api_key"]),
)


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(_row_keys, _row_values), max_size=8))
def test_plan_migration_matches_spec_and_round_trips(rows):
    ctx = _env()
    try:
        expected = [
            (k, v) for k, v in rows
            if k in SECRET_SETTING_KEYS and v and v.strip() and not looks_like_envelope(v)
        ]
        plan = plan_migration(rows)
        assert [(p[0], p[1]) for p in plan] == [(k, k + ENCRYPTED_SUFFIX) for k, _ in expected]
        for (k, v), (_, _, env) in zip(expected, plan):
            assert decrypt_secret_setting(k, env) == v
        # Idempotent: re-planning the post-sweep state converges to nothing.
        after = [(enc_key, env) for _, enc_key, env in plan] + [
            (k, v) for k, v in rows if (k, v) not in expected
        ]
        assert plan_migration(after) == []
    finally:
        ctx.stop()
