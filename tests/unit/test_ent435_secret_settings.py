"""ent#435 — credential-bearing ``system_settings`` rows are encrypted at rest.

Six settings held LIVE third-party credentials in cleartext (CWE-312), so every
DB dump, backup, replica and snapshot carried usable tokens and any read path to
the DB yielded them without needing ``CREDENTIAL_ENCRYPTION_KEY``.

Behavioural coverage of the four halves of the fix:

* the SINK refuses a cleartext write (and refuses the *next* credential-shaped
  key nobody has registered yet);
* the WRITE path stores an envelope under ``<key>_encrypted`` and leaves no
  cleartext row behind;
* the READ path resolves encrypted → legacy-with-lazy-migration → env, and the
  lazy migration is what makes cleartext transient rather than merely absent
  right now — a restored pre-fix backup is converted on first read;
* the one-shot MIGRATION plan is idempotent and never mistakes an envelope for
  a credential.

DB-level unit test against the conftest's throwaway SQLite DB — no live backend.
The static/AST half lives in ``test_ent435_settings_sink_guard.py``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
# The envelope helpers fail CLOSED without a key; give this process one. 32 bytes.
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from database import db  # noqa: E402
from services import secret_settings  # noqa: E402
from services.secret_settings import (  # noqa: E402
    ENCRYPTED_SETTING_KEYS,
    SECRET_SETTING_KEYS,
    SecretSettingWriteError,
    assert_plaintext_write_allowed,
    encrypt_secret_setting,
    encrypted_key_for,
    is_credential_shaped,
    looks_like_envelope,
    plan_migration,
)
from services.settings_service import settings_service  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_settings():
    """Every test starts with no rows for the keys it touches, in either form."""
    def purge():
        for key in SECRET_SETTING_KEYS:
            db.delete_setting(key)
            db.delete_setting(encrypted_key_for(key))
    purge()
    yield
    purge()


def _raw_write(key: str, value: str) -> None:
    """Write a row BYPASSING the guard — how a pre-fix install, a restored
    backup, or a direct DB write puts cleartext on disk."""
    from db.engine import get_engine, make_insert
    from db.tables import system_settings
    from utils.helpers import utc_now_iso

    now = utc_now_iso()
    stmt = (
        make_insert(system_settings)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[system_settings.c.key],
            set_={"value": value, "updated_at": now},
        )
    )
    with get_engine().begin() as conn:
        conn.execute(stmt)


# =============================================================================
# The sink guard
# =============================================================================

@pytest.mark.parametrize("key", sorted(SECRET_SETTING_KEYS))
def test_cleartext_write_to_a_known_secret_key_is_refused(key):
    with pytest.raises(SecretSettingWriteError) as exc:
        db.set_setting(key, "sk-ant-totally-real")
    # The message must name the encrypted destination — an operator hitting this
    # needs to know where the value is supposed to go, not just that it was
    # rejected.
    assert encrypted_key_for(key) in str(exc.value)
    assert db.get_setting_value(key, None) is None


def test_refusal_leaves_no_partial_row():
    """The guard runs BEFORE the upsert, so a refused write must not have
    created or clobbered anything — including an existing encrypted row."""
    settings_service.set_secret_setting("github_pat", "ghp_original")
    before = db.get_setting_value(encrypted_key_for("github_pat"), None)

    with pytest.raises(SecretSettingWriteError):
        db.set_setting("github_pat", "ghp_attacker_cleartext")

    assert db.get_setting_value("github_pat", None) is None
    assert db.get_setting_value(encrypted_key_for("github_pat"), None) == before
    assert settings_service.get_github_pat() == "ghp_original"


def test_unregistered_credential_shaped_key_is_refused():
    """The heuristic half: the NEXT secret somebody invents is caught even
    though nobody registered it. This is what makes the generic
    `PUT /api/settings/{key}` catch-all safe by construction rather than by
    remembering to add a special case."""
    with pytest.raises(SecretSettingWriteError):
        db.set_setting("stripe_secret", "sk_live_xxx")
    with pytest.raises(SecretSettingWriteError):
        db.set_setting("acme_api_key", "whatever")
    with pytest.raises(SecretSettingWriteError):
        db.set_setting("vendor_token", "whatever")


def test_ordinary_settings_are_unaffected():
    """The guard must not become a tax on normal config — a false positive here
    breaks unrelated admin surfaces at runtime."""
    for key, value in [
        ("session_tab_enabled", "true"),
        ("platform_default_model", "claude-opus-5"),
        ("execution_log_retention_days", "30"),
        ("public_chat_url", "https://example.com"),
        ("slack_transport_mode", "socket"),
        # Credential-shaped but PUBLIC: a Slack client_id goes verbatim into the
        # browser-visible OAuth authorize URL.
        ("slack_client_id", "123.456"),
    ]:
        db.set_setting(key, value)
        assert db.get_setting_value(key, None) == value
        db.delete_setting(key)


def test_encrypted_keys_are_writable_so_the_guard_cannot_block_itself():
    """`*_encrypted` must never be credential-shaped, or the fix could not
    persist its own output."""
    for key in ENCRYPTED_SETTING_KEYS:
        assert not is_credential_shaped(key)
    assert not is_credential_shaped("elevenlabs_api_key_encrypted")
    assert not is_credential_shaped("a2a_outbound_endpoints_encrypted")
    assert_plaintext_write_allowed("elevenlabs_api_key_encrypted")


# =============================================================================
# Write + read round trip
# =============================================================================

def test_set_secret_setting_stores_an_envelope_and_no_cleartext():
    settings_service.set_secret_setting("anthropic_api_key", "sk-ant-secret-value")

    stored = db.get_setting_value(encrypted_key_for("anthropic_api_key"), None)
    assert stored is not None
    assert looks_like_envelope(stored)
    # The reported defect, restated as an assertion: the plaintext must not be
    # recoverable by reading the row.
    assert "sk-ant-secret-value" not in stored
    assert db.get_setting_value("anthropic_api_key", None) is None

    assert settings_service.get_anthropic_api_key() == "sk-ant-secret-value"


def test_the_reporters_verification_query_returns_nothing():
    """The issue's own repro: the legacy key names must be absent from
    `system_settings` after credentials are configured the supported way."""
    settings_service.set_secret_setting("anthropic_api_key", "sk-ant-x")
    settings_service.set_secret_setting("github_pat", "ghp_x")
    settings_service.set_secret_setting("slack_app_token", "xapp-x")
    settings_service.set_secret_setting("slack_client_secret", "cs-x")
    settings_service.set_secret_setting("slack_signing_secret", "ss-x")

    present = [k for k in SECRET_SETTING_KEYS if db.get_setting_value(k, None) is not None]
    assert present == []


def test_clear_removes_both_forms():
    _raw_write("github_pat", "ghp_legacy")
    settings_service.set_secret_setting("github_pat", "ghp_new")
    # set_secret_setting already drops the legacy row; put one back to prove
    # clear() does not depend on that.
    _raw_write("github_pat", "ghp_legacy_again")

    assert settings_service.clear_secret_setting("github_pat") is True
    assert db.get_setting_value("github_pat", None) is None
    assert db.get_setting_value(encrypted_key_for("github_pat"), None) is None


def test_has_secret_setting_sees_either_form():
    assert settings_service.has_secret_setting("github_pat") is False
    _raw_write("github_pat", "ghp_legacy")
    assert settings_service.has_secret_setting("github_pat") is True
    settings_service.set_secret_setting("github_pat", "ghp_new")
    assert settings_service.has_secret_setting("github_pat") is True


def test_blank_write_unsets_rather_than_storing_an_empty_envelope():
    """Pre-ent#435 a blank write stored `''`, which every reader treated as
    falsy and fell through to the env var. Storing an envelope OF an empty
    string would resolve the same way but make `has_secret_setting` — and the
    `source: settings|env` field it backs — claim the credential is configured
    in settings when it actually comes from the environment."""
    settings_service.set_secret_setting("github_pat", "ghp_real")
    assert settings_service.has_secret_setting("github_pat") is True

    settings_service.set_secret_setting("github_pat", "   ")

    assert db.get_setting_value(encrypted_key_for("github_pat"), None) is None
    assert db.get_setting_value("github_pat", None) is None
    assert settings_service.has_secret_setting("github_pat") is False


def test_set_secret_setting_rejects_an_unregistered_key():
    with pytest.raises(ValueError):
        settings_service.set_secret_setting("not_a_registered_key", "x")


# =============================================================================
# Read-path resolution + lazy migration
# =============================================================================

def test_legacy_cleartext_row_is_migrated_on_read():
    """A restored pre-fix backup (or any direct DB write) puts cleartext back.
    The read path converts it, which is what makes cleartext TRANSIENT rather
    than merely absent at this instant."""
    _raw_write("anthropic_api_key", "sk-ant-from-old-backup")

    assert settings_service.get_anthropic_api_key() == "sk-ant-from-old-backup"

    assert db.get_setting_value("anthropic_api_key", None) is None
    envelope = db.get_setting_value(encrypted_key_for("anthropic_api_key"), None)
    assert looks_like_envelope(envelope)
    # Still correct on the next read, now via the encrypted row.
    assert settings_service.get_anthropic_api_key() == "sk-ant-from-old-backup"


def test_encrypted_row_wins_over_a_stale_legacy_row():
    """Both rows present (a half-applied migration, or a partial restore). The
    encrypted one is current; a stale cleartext value silently outranking it
    would hand callers a revoked credential."""
    settings_service.set_secret_setting("github_pat", "ghp_current")
    _raw_write("github_pat", "ghp_stale")

    assert settings_service.get_github_pat() == "ghp_current"


def test_env_fallback_when_nothing_is_stored(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "ghp_from_env")
    assert settings_service.get_github_pat() == "ghp_from_env"


def test_unreadable_envelope_degrades_to_env_not_to_the_legacy_row(monkeypatch):
    """Fail-OPEN on read (never 500 the agent-start path) but NOT all the way
    down to cleartext: an envelope written under a rotated key means "configured,
    currently unreadable", and falling back to a stale plaintext row would
    resurrect a credential the operator replaced."""
    _raw_write(encrypted_key_for("github_pat"), "not-an-envelope-at-all")
    _raw_write("github_pat", "ghp_stale_cleartext")
    monkeypatch.setenv("GITHUB_PAT", "ghp_from_env")

    assert settings_service.get_github_pat() == "ghp_from_env"


def test_reads_do_not_rewrite_when_already_encrypted():
    """Steady state must cost exactly one read and zero writes — this resolver
    sits on the agent create/start path."""
    settings_service.set_secret_setting("github_pat", "ghp_x")
    envelope = db.get_setting_value(encrypted_key_for("github_pat"), None)

    for _ in range(3):
        assert settings_service.get_github_pat() == "ghp_x"

    # A rewrite would produce a fresh nonce, so byte-equality proves no write.
    assert db.get_setting_value(encrypted_key_for("github_pat"), None) == envelope


def test_slack_getters_resolve_through_the_encrypted_path():
    settings_service.set_secret_setting("slack_app_token", "xapp-1-abc")
    settings_service.set_secret_setting("slack_signing_secret", "sign-me")
    settings_service.set_secret_setting("slack_client_secret", "shhh")

    assert settings_service.get_slack_app_token() == "xapp-1-abc"
    assert settings_service.get_slack_signing_secret() == "sign-me"
    assert settings_service.get_slack_client_secret() == "shhh"


def test_migration_failure_on_read_still_returns_the_credential(monkeypatch):
    """The lazy migration runs on a READ path, so a write failure must not break
    agent startup — the value is returned and the conversion retried later."""
    _raw_write("github_pat", "ghp_legacy")

    def boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(secret_settings, "encrypt_secret_setting", boom)
    assert settings_service.get_github_pat() == "ghp_legacy"


# =============================================================================
# One-shot migration plan (shared by both DB tracks)
# =============================================================================

def test_missing_encryption_key_explains_why_the_upgrade_needs_one(monkeypatch):
    """The rare install this can bite — provisioned by a bare `docker compose up`,
    which defaults the key to empty, and configured credentials through the UI —
    has been running fine for months. A bare "encryption key not configured"
    gives that operator no clue an upgrade is what changed, so the message names
    ent#435, why refusing to boot is deliberate, the exact fix, and the runbook.
    """
    from services.secret_settings import MissingEncryptionKeyError

    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(MissingEncryptionKeyError) as exc:
        encrypt_secret_setting("github_pat", "ghp_x")

    msg = str(exc.value)
    assert "ent#435" in msg
    assert "CREDENTIAL_ENCRYPTION_KEY" in msg
    assert "SECRET_SETTINGS_ENCRYPTION_2026-08.md" in msg
    # It must not leak the credential it failed to encrypt.
    assert "ghp_x" not in msg
    # Still a ValueError, so the #453-shaped `except ValueError` callers and the
    # migration runner's failure path keep working unchanged.
    assert isinstance(exc.value, ValueError)


def test_plan_migration_converts_cleartext_rows():
    plan = plan_migration([
        ("anthropic_api_key", "sk-ant-abc"),
        ("github_pat", "ghp_abc"),
    ])
    assert {p[0] for p in plan} == {"anthropic_api_key", "github_pat"}
    for legacy, encrypted, envelope in plan:
        assert encrypted == encrypted_key_for(legacy)
        assert looks_like_envelope(envelope)


def test_plan_migration_is_idempotent_over_already_encrypted_values():
    """A re-run, or a sweep that crashed halfway, must converge — not double-wrap
    an envelope into an unreadable one."""
    envelope = encrypt_secret_setting("github_pat", "ghp_abc")
    assert plan_migration([("github_pat", envelope)]) == []


def test_plan_migration_skips_blanks_and_unrelated_keys():
    assert plan_migration([("github_pat", "")]) == []
    assert plan_migration([("github_pat", "   ")]) == []
    assert plan_migration([("github_pat", None)]) == []
    assert plan_migration([("session_tab_enabled", "true")]) == []
    # `slack_client_id` is public and deliberately out of scope.
    assert plan_migration([("slack_client_id", "123.456")]) == []


def test_looks_like_envelope_never_mistakes_a_credential_for_one():
    """The skip test is structural, not a decrypt (the migration must be able to
    skip without holding the key). It must not classify a real token as already
    encrypted, or the sweep would silently leave it in cleartext."""
    for value in [
        "sk-ant-api03-abcdef",
        "ghp_abcdefghijklmnop",
        "xapp-1-A01-2-abcdef",
        "AIzaSyAbcdef",
        "{}",
        '{"not": "an envelope"}',
        json.dumps({"ciphertext": "x"}),  # partial — missing nonce/algorithm
        "",
    ]:
        assert looks_like_envelope(value) is False, value

    assert looks_like_envelope(encrypt_secret_setting("github_pat", "x")) is True


# =============================================================================
# The SQLite one-shot sweep, end to end
# =============================================================================

def _sqlite_settings_db(tmp_path, rows):
    import sqlite3

    path = tmp_path / "sweep.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, '2026-08-20T00:00:00Z')",
        rows,
    )
    conn.commit()
    return conn


def _sweep(conn):
    from db.migrations import _migrate_secret_settings_encryption

    _migrate_secret_settings_encryption(conn.cursor(), conn)


def _dump(conn):
    return dict(conn.execute("SELECT key, value FROM system_settings").fetchall())


def test_sqlite_sweep_encrypts_and_removes_the_cleartext_rows(tmp_path):
    conn = _sqlite_settings_db(tmp_path, [
        ("anthropic_api_key", "sk-ant-live"),
        ("github_pat", "ghp_live"),
        ("slack_signing_secret", "sign-live"),
        ("slack_client_id", "123.456"),        # public — must be untouched
        ("session_tab_enabled", "true"),        # ordinary config — untouched
    ])
    _sweep(conn)
    after = _dump(conn)

    for key in ("anthropic_api_key", "github_pat", "slack_signing_secret"):
        assert key not in after
        assert looks_like_envelope(after[encrypted_key_for(key)])
    # And the plaintext is genuinely gone from the file, not merely re-keyed.
    assert "sk-ant-live" not in json.dumps(after)
    assert "ghp_live" not in json.dumps(after)

    assert after["slack_client_id"] == "123.456"
    assert after["session_tab_enabled"] == "true"


def test_sqlite_sweep_is_idempotent(tmp_path):
    conn = _sqlite_settings_db(tmp_path, [("github_pat", "ghp_live")])
    _sweep(conn)
    once = _dump(conn)
    _sweep(conn)
    assert _dump(conn) == once


def test_sqlite_sweep_on_a_clean_install_needs_no_encryption_key(tmp_path, monkeypatch):
    """A fresh install has no credential rows. It must not be blocked from
    booting by a key it does not yet need — the sweep only reaches the
    fail-closed encryptor when there is something to protect."""
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    conn = _sqlite_settings_db(tmp_path, [("session_tab_enabled", "true")])
    _sweep(conn)
    assert _dump(conn) == {"session_tab_enabled": "true"}


def test_sqlite_sweep_refuses_rather_than_leaving_cleartext(tmp_path, monkeypatch):
    """With credentials present and no encryption key, the migration RAISES —
    the backend fails to start rather than booting with the secrets still in
    cleartext. Same choice #453 made for the Slack sweep."""
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    conn = _sqlite_settings_db(tmp_path, [("github_pat", "ghp_live")])
    with pytest.raises(ValueError):
        _sweep(conn)
    assert _dump(conn)["github_pat"] == "ghp_live"


def test_sqlite_sweep_is_atomic_across_rows(tmp_path, monkeypatch):
    """The sweep is ONE transaction, not a commit per row.

    Documented in the migration and worth pinning: a crash partway must roll
    back every row rather than leave an install with three of six credentials
    converted. Simulated by failing the DELETE of the second row — the first
    row's already-executed INSERT must not survive.
    """
    import sqlite3

    from db.migrations import _migrate_secret_settings_encryption

    conn = _sqlite_settings_db(tmp_path, [
        ("anthropic_api_key", "sk-ant-one"),
        ("github_pat", "ghp_two"),
        ("google_api_key", "AIza-three"),
    ])
    real_cursor = conn.cursor()

    class _FailingCursor:
        """Passes everything through, then raises on the 2nd DELETE."""

        def __init__(self, inner):
            self._inner = inner
            self._deletes = 0

        def execute(self, sql, *a, **kw):
            if sql.lstrip().upper().startswith("DELETE"):
                self._deletes += 1
                if self._deletes == 2:
                    raise sqlite3.OperationalError("simulated crash mid-sweep")
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.raises(sqlite3.OperationalError):
        _migrate_secret_settings_encryption(_FailingCursor(real_cursor), conn)

    conn.rollback()
    after = _dump(conn)

    # Nothing converted: all three cleartext rows intact, no encrypted rows.
    assert after["anthropic_api_key"] == "sk-ant-one"
    assert after["github_pat"] == "ghp_two"
    assert after["google_api_key"] == "AIza-three"
    assert [k for k in after if k.endswith("_encrypted")] == []

    # And a clean re-run converges completely — no credential was lost.
    _sweep(conn)
    final = _dump(conn)
    assert [k for k in final if not k.endswith("_encrypted")] == []
    assert len([k for k in final if k.endswith("_encrypted")]) == 3


def test_sqlite_sweep_is_registered_in_the_runner():
    from db.migrations import MIGRATIONS

    assert "secret_settings_encryption" in [name for name, _ in MIGRATIONS]


def test_postgres_track_ships_the_same_sweep():
    """Invariant #9: the reporter observed this on PostgreSQL, so the Alembic
    revision is the primary track, not an afterthought — and it must share the
    policy rather than re-implement it."""
    import pathlib

    rev = (
        pathlib.Path(_BACKEND)
        / "migrations"
        / "versions"
        / "0041_secret_settings_encryption.py"
    ).read_text()
    assert "plan_migration" in rev, "PG track must reuse the shared policy"
    assert "DELETE FROM system_settings" in rev, "PG track must drop the cleartext row"
