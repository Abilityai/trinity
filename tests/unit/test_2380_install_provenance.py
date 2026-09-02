"""#2380 — install provenance + the first-run hardening guide gate.

A marketplace image boots into a public droplet with a default password and
plain HTTP, so it needs a one-time hardening prompt. Every *managed* instance
serves the same plain HTTP over a Tailscale CGNAT address — encrypted transport
that is indistinguishable, from inside the container, from an unhardened
droplet. So the gate cannot key on observed TLS state without firing forever on
every paying client. It keys on PROVENANCE: how the box was installed, recorded
once at first boot from ``TRINITY_INSTALL_SOURCE`` and read from
``system_settings`` for the rest of the instance's life.

That reframing puts the whole weight on one property, and these tests exist to
pin it:

  **provenance must not be re-assertable.**

Three independent doors have to stay shut for that to hold, and each is a
separate test group below:

  * the RECORDER is write-once — an existing row is never overwritten, no
    matter what the env now declares (`TestRecorderSqlite`);
  * the RESOLVER reads the ROW ONLY — the env var is not a fallback, so
    editing `.env` on a running box changes nothing (`TestResolver`);
  * the ROUTER blocks both `PUT` **and** `DELETE` on the catch-all — the
    delete is the sharper of the two, since write-once means clearing the row
    is precisely the move that unlocks a rewrite (`TestRouterGuards`).

Two more properties are load-bearing and easy to get wrong:

  * an unrecognised marker records **nothing** — not the bogus value and not
    `unknown`. Recording `unknown` would combine with write-once to freeze one
    typo permanently (`test_a_corrected_marker_lands_on_a_later_boot` is the
    reason that design exists);
  * the recorder never raises. ``init_database`` runs at import time, so a
    raise here crash-loops the backend permanently — the #1638/#2216 seed
    contract. The cost of a skip is a hidden guide; the cost of a raise is an
    instance that does not start.

``classify_advertised_url`` gets its own thorough group because it is the
honesty-critical piece: nothing in this feature probes a socket or reads a
certificate (TLS terminates outside the backend, HOST-010), so the verdict is
only ever a claim about the URL the instance ADVERTISES — and the copy above it
must never assert a cert it does not hold.

Imports are lazy inside helpers: ``database`` / ``routers.settings`` pull the
backend chain, and ``services/__init__`` eagerly imports ``docker_service``,
a known pytest-randomly stub-leak target (the ``test_2381`` / ``test_2217``
convention). Sync throughout — ``tests/unit/pytest.ini`` overrides
``asyncio_mode = auto``, so handlers are driven with ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

MARKETPLACE = "do-marketplace"
OTHER_MARKETPLACE = "vultr-marketplace"
SCRIPT = "script"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# lazy imports (collection stays backend-free)
# ---------------------------------------------------------------------------

def _database():
    import database as m
    return m


def _config():
    import config as m
    return m


def _settings_service_module():
    from services import settings_service as m
    return m


def _router():
    import routers.settings as m
    return m


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mkdb() -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    """Just `system_settings` — the only table the recorder touches.

    Mirrors the moment it runs: after `init_schema`, at the tail of
    `init_database`. The recorder deliberately has NO ordering requirement
    against the seeds or `_ensure_admin_user`, so `users` is absent on purpose.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE system_settings ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn, conn.cursor()


def _recorded(cur) -> str | None:
    cur.execute("SELECT value FROM system_settings WHERE key = 'install_source'")
    row = cur.fetchone()
    return row[0] if row else None


@pytest.fixture
def marker(monkeypatch):
    """Set the boot marker the recorder reads.

    `TRINITY_INSTALL_SOURCE` is a module-level constant resolved at config
    import, but every consumer imports it FUNCTION-LOCALLY, so rebinding the
    attribute is what a fresh boot with a different `.env` looks like. The env
    var is set alongside it so a test can never accidentally pass because some
    other layer went to `os.environ` instead.
    """
    def _set(value: str | None):
        cfg = _config()
        monkeypatch.setattr(cfg, "TRINITY_INSTALL_SOURCE", value or "")
        if value is None:
            monkeypatch.delenv(cfg.INSTALL_SOURCE_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(cfg.INSTALL_SOURCE_ENV_VAR, value)
    return _set


# ===========================================================================
# The closed value set
# ===========================================================================

class TestConfigContract:

    def test_the_allowlist_is_closed_and_contains_unknown(self):
        cfg = _config()
        assert cfg.INSTALL_SOURCE_UNKNOWN == UNKNOWN
        assert cfg.INSTALL_SOURCE_VALUES == frozenset(
            {MARKETPLACE, OTHER_MARKETPLACE, SCRIPT, UNKNOWN}
        )

    def test_marketplace_sources_are_a_strict_subset(self):
        """The gate's set must never drift outside the recordable set — a
        marketplace value that cannot be recorded could never fire the guide."""
        cfg = _config()
        assert cfg.MARKETPLACE_INSTALL_SOURCES == frozenset(
            {MARKETPLACE, OTHER_MARKETPLACE}
        )
        assert cfg.MARKETPLACE_INSTALL_SOURCES < cfg.INSTALL_SOURCE_VALUES
        assert cfg.INSTALL_SOURCE_UNKNOWN not in cfg.MARKETPLACE_INSTALL_SOURCES


# ===========================================================================
# RECORDER — SQLite arm
# ===========================================================================

class TestRecorderSqlite:

    def test_a_valid_marker_is_recorded_on_a_fresh_install(self, marker):
        marker(MARKETPLACE)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == MARKETPLACE

    @pytest.mark.parametrize("value", [MARKETPLACE, OTHER_MARKETPLACE, SCRIPT, UNKNOWN])
    def test_every_allowlisted_value_is_recordable(self, marker, value):
        marker(value)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == value

    def test_the_write_commits(self, marker):
        """The recorder is the last thing `init_database` does; nothing after
        it commits, so an uncommitted row would be lost on the next open."""
        marker(MARKETPLACE)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)
        conn.rollback()

        assert _recorded(cur) == MARKETPLACE

    # -- THE security property ------------------------------------------------

    def test_an_existing_row_is_never_overwritten(self, marker):
        """WRITE-ONCE. This is the security property the whole feature rests on.

        Provenance is a historical fact about an installation event, not a
        setting. If a later `.env` edit could rewrite it, the row would answer
        "what does this box currently claim" rather than "how was this box
        installed", and the marketplace gate would be self-assertable by anyone
        who can edit a file on the host.
        """
        marker(MARKETPLACE)
        conn, cur = _mkdb()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES ('install_source', ?, '2026-01-01T00:00:00Z')",
            (SCRIPT,),
        )
        conn.commit()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == SCRIPT, (
            "a valid env marker must not be able to rewrite recorded provenance"
        )

    def test_write_once_holds_in_the_suppressing_direction_too(self, marker):
        """The inverse abuse: a droplet that IS a marketplace install trying to
        suppress its own hardening guide by declaring `script` later."""
        marker(SCRIPT)
        conn, cur = _mkdb()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES ('install_source', ?, '2026-01-01T00:00:00Z')",
            (MARKETPLACE,),
        )
        conn.commit()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == MARKETPLACE

    def test_a_conflict_is_logged_not_silent(self, marker, capsys):
        """A disagreement is a real operator signal — a box whose `.env` no
        longer matches how it was installed. Keeping the row silently would
        make a genuine re-image indistinguishable from a tamper attempt."""
        marker(MARKETPLACE)
        conn, cur = _mkdb()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES ('install_source', ?, '2026-01-01T00:00:00Z')",
            (SCRIPT,),
        )
        conn.commit()

        _database()._record_install_source(cur, conn)

        out = capsys.readouterr().out
        assert MARKETPLACE in out and SCRIPT in out

    def test_an_agreeing_reboot_is_a_silent_no_op(self, marker, capsys):
        """The common case — every restart after the first. Same value, already
        recorded: no conflict warning, and nothing re-written."""
        marker(MARKETPLACE)
        conn, cur = _mkdb()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES ('install_source', ?, '2026-01-01T00:00:00Z')",
            (MARKETPLACE,),
        )
        conn.commit()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == MARKETPLACE
        assert "WARNING" not in capsys.readouterr().out
        cur.execute(
            "SELECT updated_at FROM system_settings WHERE key = 'install_source'"
        )
        assert cur.fetchone()[0] == "2026-01-01T00:00:00Z", (
            "an agreeing boot must not even touch the row"
        )

    # -- unrecognised markers -------------------------------------------------

    @pytest.mark.parametrize(
        "bogus",
        [
            "do-marketplac",        # the realistic typo
            "DO-MARKETPLACE",       # case: the marker is matched exactly
            "aws-marketplace",      # a channel that does not exist yet
            "; DROP TABLE users",
            "true",
        ],
    )
    def test_an_unrecognised_marker_records_nothing_at_all(self, marker, bogus):
        """Not the bogus value, and NOT `unknown`.

        Recording `unknown` would look harmless and would combine with
        write-once to freeze a typo permanently — the row would exist, so the
        corrected marker on the next boot could never land. An absent row reads
        as `unknown` all the same (see `TestResolver`), at no cost.
        """
        marker(bogus)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) is None, (
            "an unrecognised marker must leave the row ABSENT — recording "
            "'unknown' here would freeze the typo forever"
        )

    def test_an_unrecognised_marker_is_reported(self, marker, capsys):
        """A silently-ignored marker is the #1039 inert-by-obscurity class: the
        operator set the variable and nothing anywhere says it did nothing."""
        marker("do-marketplac")
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        out = capsys.readouterr().out
        assert "do-marketplac" in out
        assert "WARNING" in out

    def test_a_corrected_marker_lands_on_a_later_boot(self, marker):
        """The reason the test above insists on an ABSENT row.

        Boot 1 carries a typo and records nothing. The operator fixes `.env`
        and restarts; boot 2 must be able to record. Had boot 1 written
        `unknown`, write-once would have made this impossible and the install's
        provenance would be permanently wrong.
        """
        db = _database()
        conn, cur = _mkdb()

        marker("do-marketplac")
        db._record_install_source(cur, conn)
        assert _recorded(cur) is None

        marker(MARKETPLACE)
        db._record_install_source(cur, conn)

        assert _recorded(cur) == MARKETPLACE

    # -- absent marker --------------------------------------------------------

    @pytest.mark.parametrize("absent", [None, "", "   "])
    def test_an_absent_or_blank_marker_records_nothing(self, marker, absent):
        """An install that predates the marker, or a hand-rolled deploy. It has
        no provenance to record and must not be given a fabricated one."""
        marker(absent)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) is None

    def test_an_absent_marker_is_quiet(self, marker, capsys):
        """No marker is the NORMAL state for a self-hosted install. Warning on
        it would train operators to ignore this feature's output entirely."""
        marker(None)
        conn, cur = _mkdb()

        _database()._record_install_source(cur, conn)

        assert capsys.readouterr().out == ""

    # -- fail-safe ------------------------------------------------------------

    def test_a_missing_table_does_not_raise(self, marker):
        """FAIL-SAFE. `init_database` runs at IMPORT time, so a raise here does
        not fail one boot — it crash-loops the backend permanently, with no
        route in to fix it. A hidden hardening guide is survivable; an
        instance that will not start is not."""
        marker(MARKETPLACE)
        conn = sqlite3.connect(":memory:")  # no tables at all

        _database()._record_install_source(conn.cursor(), conn)  # must not raise

    def test_a_raising_cursor_does_not_propagate(self, marker):
        marker(MARKETPLACE)

        class _Boom:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("database is locked")

        class _Conn:
            def commit(self):
                raise AssertionError("commit must not be reached")

        _database()._record_install_source(_Boom(), _Conn())  # must not raise

    def test_a_raising_commit_does_not_propagate(self, marker, capsys):
        """The write can fail at commit rather than at execute — a disk-full or
        locked DB. Same contract, and it must still say so."""
        marker(MARKETPLACE)
        conn, cur = _mkdb()

        class _Conn:
            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

        _database()._record_install_source(cur, _Conn())  # must not raise

        assert "WARNING" in capsys.readouterr().out


# ===========================================================================
# RECORDER — engine (PostgreSQL) twin
# ===========================================================================

class _FakeSettingsOps:
    """Stand-in for `SettingsOperations`, recording intent rather than SQL."""

    def __init__(self, rows=None, raise_on_read=False):
        self.rows = dict(rows or {})
        self.writes: list[tuple[str, str]] = []
        self.raise_on_read = raise_on_read

    def get_setting_value(self, key, default=None):
        if self.raise_on_read:
            raise RuntimeError("connection reset")
        return self.rows.get(key, default)

    def set_setting(self, key, value):
        # The recorder must NOT reach this: `set_setting` is an upsert and the
        # real one refuses `install_source` outright (db/settings.py). Recorded
        # rather than raised so a regression shows up as a readable diff on
        # `writes` instead of an opaque swallowed exception — the recorder's
        # fail-safe contract would hide a raise entirely.
        self.writes.append(("set_setting", key, value))
        self.rows[key] = value

    def insert_setting_if_absent(self, key, value):
        """Write-once insert — the real one is `INSERT … ON CONFLICT DO NOTHING`.

        Returns whether a row was written, and refuses to overwrite, so the
        stub enforces the same contract the PRIMARY KEY does in production.
        """
        if self.rows.get(key):
            return False
        self.writes.append((key, value))
        self.rows[key] = value
        return True


class TestRecorderEngine:
    """The PostgreSQL arm. It carries the SAME write-once contract — a fix
    applied to only one backend is how the two silently diverge."""

    def _install(self, monkeypatch, ops):
        monkeypatch.setattr(_database(), "SettingsOperations", lambda: ops)
        return ops

    def test_records_on_a_fresh_install(self, marker, monkeypatch):
        marker(MARKETPLACE)
        ops = self._install(monkeypatch, _FakeSettingsOps())

        _database()._record_install_source_engine()

        assert ops.writes == [("install_source", MARKETPLACE)]

    def test_an_existing_row_is_never_overwritten(self, marker, monkeypatch):
        marker(MARKETPLACE)
        ops = self._install(
            monkeypatch, _FakeSettingsOps({"install_source": SCRIPT})
        )

        _database()._record_install_source_engine()

        assert ops.writes == []
        assert ops.rows["install_source"] == SCRIPT

    def test_an_unrecognised_marker_records_nothing(self, marker, monkeypatch):
        marker("do-marketplac")
        ops = self._install(monkeypatch, _FakeSettingsOps())

        _database()._record_install_source_engine()

        assert ops.writes == []

    def test_an_absent_marker_records_nothing(self, marker, monkeypatch):
        marker(None)
        ops = self._install(monkeypatch, _FakeSettingsOps())

        _database()._record_install_source_engine()

        assert ops.writes == []

    def test_a_raising_read_does_not_propagate(self, marker, monkeypatch):
        """Same import-time crash-loop stakes as the SQLite arm."""
        marker(MARKETPLACE)
        ops = self._install(monkeypatch, _FakeSettingsOps(raise_on_read=True))

        _database()._record_install_source_engine()  # must not raise

        assert ops.writes == []


# ===========================================================================
# RESOLVER
# ===========================================================================

class _Reader:
    """A `SettingsService` whose `get_setting` is under the test's control."""

    def __init__(self, rows=None, raises=False):
        self.rows = dict(rows or {})
        self.raises = raises

    def __call__(self, key, default=None):
        if self.raises:
            raise sqlite3.OperationalError("database is locked")
        return self.rows.get(key, default)


def _service(monkeypatch, reader):
    """A fresh SettingsService instance with its DB read stubbed."""
    svc = _settings_service_module().SettingsService()
    monkeypatch.setattr(svc, "get_setting", reader)
    return svc


class TestResolver:

    def test_an_absent_row_reads_as_unknown(self, monkeypatch):
        """The state of every install that predates this feature, and of every
        install whose marker was a typo. `unknown` hides the guide, which is
        the safe direction: prompting a correctly-configured managed instance
        to harden itself is the failure this feature is shaped to avoid."""
        svc = _service(monkeypatch, _Reader())

        assert svc.get_install_source() == UNKNOWN
        assert svc.is_marketplace_install() is False

    @pytest.mark.parametrize("value", [MARKETPLACE, OTHER_MARKETPLACE, SCRIPT, UNKNOWN])
    def test_an_allowlisted_row_reads_back_verbatim(self, monkeypatch, value):
        svc = _service(monkeypatch, _Reader({"install_source": value}))

        assert svc.get_install_source() == value

    @pytest.mark.parametrize(
        "rogue", ["aws-marketplace", "do-marketplac", "true", "", "   "]
    )
    def test_a_row_outside_the_allowlist_reads_as_unknown(self, monkeypatch, rogue):
        """Defence in depth against a value written by any path other than the
        recorder — a direct DB write, a restored backup from a future version,
        a migration. The recorder validates on the way in; the resolver refuses
        to trust that it was the only writer."""
        svc = _service(monkeypatch, _Reader({"install_source": rogue}))

        assert svc.get_install_source() == UNKNOWN
        assert svc.is_marketplace_install() is False

    def test_the_row_is_normalised_before_the_allowlist_check(self, monkeypatch):
        """Whitespace/case from a hand-edited row still resolves rather than
        being thrown away — the value is recognisable, just untidy."""
        svc = _service(monkeypatch, _Reader({"install_source": "  DO-Marketplace \n"}))

        assert svc.get_install_source() == MARKETPLACE

    # -- the gate -------------------------------------------------------------

    @pytest.mark.parametrize(
        "value,expected",
        [
            (MARKETPLACE, True),
            (OTHER_MARKETPLACE, True),
            (SCRIPT, False),
            (UNKNOWN, False),
        ],
    )
    def test_is_marketplace_install_matches_the_channel(
        self, monkeypatch, value, expected
    ):
        svc = _service(monkeypatch, _Reader({"install_source": value}))

        assert svc.is_marketplace_install() is expected

    # -- fail-open ------------------------------------------------------------

    def test_a_raising_settings_read_never_propagates(self, monkeypatch):
        """This feeds `GET /api/settings/feature-flags`. A raise there does not
        degrade one field — it 500s the endpoint and zeroes EVERY flag in the
        frontend store, taking Workspace, voice and the Brain Orb down with it
        (`_resolve_bool_flag`'s rationale)."""
        svc = _service(monkeypatch, _Reader(raises=True))

        assert svc.get_install_source() == UNKNOWN
        assert svc.is_marketplace_install() is False

    def test_the_failure_direction_is_never_toward_a_marketplace(self, monkeypatch):
        """Stated as its own assertion because `unknown` is not merely 'some
        default': it is the one verdict that HIDES the guide. Failing toward a
        marketplace value would show a hardening prompt on every managed
        instance the moment the settings table hiccupped."""
        svc = _service(monkeypatch, _Reader(raises=True))

        assert svc.get_install_source() not in _config().MARKETPLACE_INSTALL_SOURCES

    # -- the read side of the security property --------------------------------

    def test_the_env_var_is_not_a_resolver_fallback(self, monkeypatch, marker):
        """The mirror of write-once, one layer along.

        With no row recorded and `TRINITY_INSTALL_SOURCE=do-marketplace` live in
        the environment, provenance must STILL read `unknown`. If the resolver
        had an env leg, the recorder's write-once guarantee would be worth
        nothing: anyone able to edit `.env` could assert the marketplace gate at
        any time without ever touching the database.
        """
        marker(MARKETPLACE)
        svc = _service(monkeypatch, _Reader())  # no row

        assert svc.get_install_source() == UNKNOWN
        assert svc.is_marketplace_install() is False

    def test_the_env_var_cannot_override_a_recorded_row(self, monkeypatch, marker):
        marker(MARKETPLACE)
        svc = _service(monkeypatch, _Reader({"install_source": SCRIPT}))

        assert svc.get_install_source() == SCRIPT

    def test_module_level_wrappers_delegate_to_the_singleton(self, monkeypatch):
        """`routers/settings.py` reaches these through the module-level
        functions; a wrapper that drifted from the method would make the flag
        surface disagree with everything else."""
        mod = _settings_service_module()
        monkeypatch.setattr(
            mod.settings_service, "get_setting",
            _Reader({"install_source": MARKETPLACE}),
        )

        assert mod.get_install_source() == MARKETPLACE
        assert mod.is_marketplace_install() is True


# ===========================================================================
# URL CLASSIFIER
# ===========================================================================

def _classify(url):
    return _settings_service_module().classify_advertised_url(url)


class TestClassifyAdvertisedUrl:
    """The honesty-critical piece.

    Nothing here observes TLS. The verdict is a claim about the URL the
    instance ADVERTISES (`public_chat_url`, else the baked `FRONTEND_URL`) —
    which is all any in-process check can know, since TLS terminates outside
    the backend (HOST-010).
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://1.2.3.4",
            "https://1.2.3.4:8443",
            "https://127.0.0.1",
        ],
    )
    def test_an_https_ipv4_literal_is_https_ip(self, url):
        """A working posture to be upgraded, not a broken one: an IP cert is
        real and browser-trusted, it just renews on a ~6-day short-lived
        profile and the address is unmemorable."""
        assert _classify(url) == "https-ip"

    @pytest.mark.parametrize(
        "url",
        [
            "https://[2001:db8::1]",
            "https://[2001:db8::1]:8443",
            "https://[::1]",
        ],
    )
    def test_a_bracketed_ipv6_literal_is_https_ip(self, url):
        """`urlsplit().hostname` already strips the brackets, so the address
        reaches `ip_address()` in the form it can parse. Getting this wrong
        classifies every IPv6 instance as a domain and tells the operator they
        have a memorable hostname they do not have."""
        assert _classify(url) == "https-ip"

    @pytest.mark.parametrize(
        "url",
        [
            "https://trinity.example.com",
            "https://trinity.example.com.",   # fully-qualified trailing dot
            "https://trinity.example.com:8443",
            "HTTPS://TRINITY.EXAMPLE.COM",
            "https://user:pw@trinity.example.com",
        ],
    )
    def test_an_https_name_is_https_domain(self, url):
        assert _classify(url) == "https-domain"

    def test_localhost_is_a_domain_not_an_ip(self):
        """Pinning what the implementation ACTUALLY does: `localhost` is a
        NAME, so `ip_address()` rejects it and it lands in `https-domain` —
        even though it resolves to a loopback address. Harmless for the guide
        (a localhost URL is not an advertised public endpoint either way) and
        recorded here so the behaviour is a decision, not an accident."""
        assert _classify("https://localhost") == "https-domain"
        assert _classify("https://localhost:8443") == "https-domain"

    def test_a_dotted_quad_out_of_range_is_a_domain(self):
        """`999.999.999.999` is not a parseable IP, so it is treated as a name.
        Also pinned as a decision rather than an accident."""
        assert _classify("https://999.999.999.999") == "https-domain"

    @pytest.mark.parametrize(
        "url",
        [
            "http://trinity.example.com",
            "http://1.2.3.4",
            "http://[2001:db8::1]",
            "http://localhost",
            "HTTP://TRINITY.EXAMPLE.COM",
        ],
    )
    def test_any_http_url_is_http_regardless_of_host_shape(self, url):
        """The scheme decides first. An IP-vs-domain split under `http` would
        imply a distinction that does not exist — neither is encrypted."""
        assert _classify(url) == "http"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "\t\n",
            "not a url",
            "https://",           # scheme but no host
            "https://[::1",       # unparseable IPv6 — urlsplit raises
        ],
    )
    def test_unusable_input_is_unconfigured(self, url):
        """Fails toward `unconfigured` rather than guessing: every other
        verdict is a claim about how the instance is reached, and an
        unparseable URL supports none of them."""
        assert _classify(url) == "unconfigured"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://files.example.com",
            "file:///etc/passwd",
            "ssh://box",
            "wss://trinity.example.com",
            "javascript:alert(1)",
        ],
    )
    def test_a_non_http_scheme_is_unconfigured(self, url):
        """Only `http`/`https` are postures this vocabulary can describe. Note
        `wss://` in particular: it IS encrypted, but it is not what a browser
        would be handed, so claiming a posture from it would be a guess."""
        assert _classify(url) == "unconfigured"

    @pytest.mark.parametrize("url", ["example.com", "//example.com", "1.2.3.4"])
    def test_a_schemeless_url_is_unconfigured(self, url):
        """The implementation prepends `//` to a schemeless string, which gives
        it a host but never a scheme — so it always lands on `unconfigured`.
        Correct direction (no scheme, no claim), pinned so a later 'fix' that
        defaults the scheme to https has to argue for itself."""
        assert _classify(url) == "unconfigured"

    def test_the_verdict_vocabulary_is_closed(self):
        """The frontend switches on these four strings; a fifth would fall
        through every branch silently."""
        vocabulary = {"unconfigured", "http", "https-ip", "https-domain"}
        samples = [
            "", "   ", "not a url", "ftp://x", "http://x", "https://x",
            "https://1.2.3.4", "https://[::1]", "https://x.y.z:1",
        ]
        assert {_classify(s) for s in samples} <= vocabulary


class TestTlsPostureResolution:
    """`get_install_tls_posture` = which URL, then the pure classifier."""

    def test_the_stored_public_chat_url_wins(self, monkeypatch):
        svc = _service(
            monkeypatch, _Reader({"public_chat_url": "https://trinity.example.com"})
        )
        monkeypatch.setattr(_config(), "FRONTEND_URL", "http://10.0.0.5")

        assert svc.get_install_tls_posture() == "https-domain"

    def test_it_falls_back_to_the_baked_frontend_url(self, monkeypatch):
        """A marketplace image has no `public_chat_url` row on first boot, so
        the fallback is the branch that actually runs there."""
        svc = _service(monkeypatch, _Reader())
        monkeypatch.setattr(_config(), "FRONTEND_URL", "http://10.0.0.5")

        assert svc.get_install_tls_posture() == "http"

    def test_neither_configured_is_unconfigured(self, monkeypatch):
        svc = _service(monkeypatch, _Reader())
        monkeypatch.setattr(_config(), "FRONTEND_URL", "")

        assert svc.get_install_tls_posture() == "unconfigured"

    def test_a_raising_settings_read_falls_back_rather_than_500ing(self, monkeypatch):
        """Same flag-surface stakes as `get_install_source`."""
        svc = _service(monkeypatch, _Reader(raises=True))
        monkeypatch.setattr(_config(), "FRONTEND_URL", "https://trinity.example.com")

        assert svc.get_install_tls_posture() == "https-domain"

    def test_a_blank_row_does_not_shadow_the_fallback(self, monkeypatch):
        """An empty `public_chat_url` row must read as unset, not as
        'configured to nothing' — the `os.getenv(...) or None` lesson (#993)."""
        svc = _service(monkeypatch, _Reader({"public_chat_url": "   "}))
        monkeypatch.setattr(_config(), "FRONTEND_URL", "https://trinity.example.com")

        assert svc.get_install_tls_posture() == "https-domain"


# ===========================================================================
# ROUTER — the catch-all guards
# ===========================================================================

def _principal(role="admin"):
    return types.SimpleNamespace(
        id=1, username="admin", email="admin@example.com", role=role,
        agent_name=None, connector_agent=None, mcp_scope=None,
    )


@pytest.fixture
def client(monkeypatch):
    """A TestClient over the real settings router with the principal overridden.

    The override MUST be keyed on ``rs.get_current_user``, never on a fresh
    ``from dependencies import get_current_user``. The unit conftest pops
    ``dependencies`` from ``sys.modules`` after every test (`_POP_PREFIXES`),
    so a re-import mints a NEW function object while the already-cached router
    still holds the OLD one in its ``Depends(...)`` — the override then keys on
    something no route asks for and every request 401s from the second test in
    the file onward. Reading the symbol off the router module is what the
    decorator actually saw (the `test_2217` `require_admin` note).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    rs = _router()
    app = FastAPI()
    app.include_router(rs.router)
    app.dependency_overrides[rs.get_current_user] = lambda: _principal()

    # Nothing below the guards may reach a real DB or audit sink. The PUT
    # route declares a `SystemSetting` response model, so the write stub has to
    # return one — a bare truthy value 500s in response validation and would
    # look like a guard failure.
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from database import SystemSetting

    monkeypatch.setattr(rs.platform_audit_service, "log", AsyncMock(return_value=None))
    monkeypatch.setattr(rs.db, "delete_setting", lambda key: True)
    monkeypatch.setattr(
        rs.db, "set_setting",
        lambda key, value: SystemSetting(
            key=key, value=value, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        ),
    )

    return TestClient(app, raise_server_exceptions=False)


class TestRouterGuards:

    def test_the_generic_PUT_refuses_install_source(self, client):
        """Provenance is a RECORDED FACT, not a setting. On the catch-all it
        would be self-assertable: `PUT {"value": "do-marketplace"}` summons the
        hardening guide on a managed instance, and `"script"` suppresses it on
        the droplet that needs it. There is deliberately no dedicated write
        route to redirect to — the only supported way to set provenance is to
        provision the box with the marker."""
        resp = client.put("/api/settings/install_source", json={"value": MARKETPLACE})

        assert resp.status_code == 422
        assert "TRINITY_INSTALL_SOURCE" in resp.json()["detail"]

    def test_the_generic_DELETE_refuses_install_source(self, client):
        """Independently load-bearing, and SHARPER than the PUT guard.

        The recorder is write-once: it refuses to overwrite an existing row. So
        a DELETE here is not "revert to a default" — it is the ONE move that
        unlocks a rewrite. Delete the row, edit `.env`, restart, and the boot
        recorder cheerfully records whatever the attacker chose. Blocking PUT
        while leaving DELETE open would be no gate at all: it would take two
        requests instead of one and reach exactly the same place.
        """
        resp = client.delete("/api/settings/install_source")

        assert resp.status_code == 422
        assert "install_source" in resp.json()["detail"]

    @pytest.mark.parametrize("value", [MARKETPLACE, SCRIPT, UNKNOWN, "anything"])
    def test_the_PUT_guard_is_key_scoped_not_value_scoped(self, client, value):
        """Refused for EVERY value, including the ones the recorder would have
        accepted. A value-aware guard would be a validation rule; this is an
        authority rule — the API is not a writer of provenance at all."""
        resp = client.put("/api/settings/install_source", json={"value": value})

        assert resp.status_code == 422

    def test_the_guards_are_exact_match_not_a_prefix(self, client):
        """A blocklist, not a wholesale closure of the route (the ent#14
        rule) — and not an accidental namespace grab either."""
        assert client.delete("/api/settings/install_source_note").status_code == 200
        assert client.put(
            "/api/settings/install_source_note", json={"value": "x"}
        ).status_code == 200

    def test_the_refusal_precedes_any_write(self, client, monkeypatch):
        """The guard must sit above the sink, not beside it — a 422 returned
        after the row was already written would be the worst of both."""
        rs = _router()
        writes = []
        monkeypatch.setattr(rs.db, "set_setting", lambda k, v: writes.append((k, v)))
        monkeypatch.setattr(rs.db, "delete_setting", lambda k: writes.append(("del", k)))

        assert client.put(
            "/api/settings/install_source", json={"value": MARKETPLACE}
        ).status_code == 422
        assert client.delete("/api/settings/install_source").status_code == 422
        assert writes == []

    def test_a_non_admin_is_still_refused_first(self, client, monkeypatch):
        """The provenance guards must not have opened a hole above the admin
        gate — `assert_admin` runs before either of them."""
        rs = _router()
        client.app.dependency_overrides[rs.get_current_user] = lambda: _principal(
            role="user"
        )

        assert client.put(
            "/api/settings/install_source", json={"value": MARKETPLACE}
        ).status_code == 403
        assert client.delete("/api/settings/install_source").status_code == 403


# ===========================================================================
# ROUTER — /feature-flags
# ===========================================================================

def _flags(monkeypatch, *, source, marketplace, posture):
    """Drive `get_public_feature_flags` with every DB-backed service stubbed.

    Mirrors `test_2217_canary_status.py`: a pure handler test that still
    catches a dropped or misnamed key.
    """
    rs = _router()

    stub_settings = types.SimpleNamespace(
        is_brain_orb_enabled=lambda: False,
        is_session_tab_enabled=lambda: False,
        is_workspace_enabled=lambda: False,
        is_brain_orb_voice_enabled=lambda: False,
        is_brain_orb_write_enabled=lambda: False,
        get_elevenlabs_api_key=lambda: None,
        get_platform_default_model=lambda: "model",
        get_anthropic_api_key=lambda: None,
        get_install_source=lambda: source,
        is_marketplace_install=lambda: marketplace,
        get_install_tls_posture=lambda: posture,
    )
    monkeypatch.setattr(rs, "settings_service", stub_settings)
    monkeypatch.setattr(
        rs, "telemetry_sharing_service",
        types.SimpleNamespace(is_consent_enabled=lambda: False),
    )
    monkeypatch.setattr(rs.db, "has_any_subscription", lambda: False)

    import services.a2a_outbound_service as a2a_module
    from services.entitlement_service import entitlement_service

    monkeypatch.setattr(a2a_module, "is_outbound_enabled", lambda: False)
    monkeypatch.setattr(entitlement_service, "list_entitled_features", lambda: [])

    return asyncio.run(rs.get_public_feature_flags(current_user=None))


class TestFeatureFlagSurface:
    """The flag surface is the portal's ONLY capability channel for this — the
    frontend cannot read `system_settings` and has no other route to ask."""

    def test_all_three_fields_are_present_and_carry_the_resolved_values(
        self, monkeypatch
    ):
        flags = _flags(
            monkeypatch,
            source=MARKETPLACE, marketplace=True, posture="http",
        )

        assert flags["install_source"] == MARKETPLACE
        assert flags["marketplace_install"] is True
        assert flags["install_tls_posture"] == "http"

    def test_a_non_marketplace_install_reports_the_gate_closed(self, monkeypatch):
        """The managed fleet's shape: `script` provenance over plain HTTP. The
        posture is identical to an unhardened droplet's — provenance is the
        ONLY thing that separates them, which is why the boolean must not be
        derivable from `install_tls_posture`."""
        flags = _flags(
            monkeypatch, source=SCRIPT, marketplace=False, posture="http"
        )

        assert flags["install_source"] == SCRIPT
        assert flags["marketplace_install"] is False
        assert flags["install_tls_posture"] == "http"

    def test_the_gate_is_shipped_resolved_not_derivable_from_the_raw_value(
        self, monkeypatch
    ):
        """`marketplace_install` is passed through from the service, not
        recomputed in the handler — the browser must hold no second copy of
        WHICH sources count as a marketplace (the ent#386 rule). A handler that
        re-derived it would flip this deliberately-mismatched pair."""
        flags = _flags(
            monkeypatch, source=MARKETPLACE, marketplace=False, posture="http"
        )

        assert flags["install_source"] == MARKETPLACE
        assert flags["marketplace_install"] is False

    def test_the_unknown_default_hides_the_guide(self, monkeypatch):
        flags = _flags(
            monkeypatch, source=UNKNOWN, marketplace=False, posture="unconfigured"
        )

        assert flags["install_source"] == UNKNOWN
        assert flags["marketplace_install"] is False

    def test_the_surface_carries_no_url(self, monkeypatch):
        """`public_chat_url` sits behind an admin-only read and this endpoint
        reaches every authenticated principal — so the POSTURE is derived and
        the URL itself never ships."""
        flags = _flags(
            monkeypatch,
            source=MARKETPLACE, marketplace=True, posture="https-domain",
        )

        blob = repr(flags)
        assert "public_chat_url" not in flags
        assert "http://" not in blob and "https://" not in blob


# ===========================================================================
# /api/version
# ===========================================================================

def _load_builder():
    """Exec-slice `_build_version_payload` out of main.py.

    Verbatim from `test_926_version_endpoint.py`: the builder must stay
    stdlib-only so this slice works without pulling main.py's whole router
    graph. #2380 threads `install_source` in as a PARAMETER for exactly that
    reason — a `system_settings` read inside the function would break it.
    """
    src_path = _BACKEND / "main.py"
    if not src_path.exists():
        pytest.skip("backend source not present")
    text = src_path.read_text()
    marker = "def _build_version_payload"
    start = text.find(marker)
    assert start != -1, f"_build_version_payload not found in {src_path}"
    rest = text[start:]
    end = rest.find("\n\n\n")
    snippet = rest[: end if end != -1 else len(rest)]
    ns: dict = {"__file__": str(src_path)}
    exec(snippet, ns)
    return ns["_build_version_payload"]


class TestVersionPayload:

    def test_install_source_is_threaded_through(self):
        build = _load_builder()

        payload = build(
            voice_enabled=False,
            edition="oss",
            enterprise_features=[],
            install_source=MARKETPLACE,
        )

        assert payload["install_source"] == MARKETPLACE

    def test_the_builder_passes_it_through_without_re_deriving(self):
        """Pure passthrough, the `edition` precedent one field up. The handler
        owns the DB read; the builder must not second-guess the value."""
        build = _load_builder()

        for value in (MARKETPLACE, OTHER_MARKETPLACE, SCRIPT, UNKNOWN, "nonsense"):
            payload = build(
                voice_enabled=False, edition="oss", enterprise_features=[],
                install_source=value,
            )
            assert payload["install_source"] == value

    def test_the_parameter_is_optional_and_defaults_to_unknown(self):
        """The default keeps the existing #926/#1443 exec-slice call sites
        working unchanged — they pass three positional/keyword args and must
        still get a well-typed payload."""
        build = _load_builder()

        payload = build(voice_enabled=False, edition="oss", enterprise_features=[])

        assert payload["install_source"] == UNKNOWN

    def test_the_builder_stays_stdlib_only(self):
        """The exec-slice is the contract. If a later change reaches for
        `settings_service` (or anything else module-level) inside this
        function, every #926 test breaks at `exec` — this says so in one line
        instead of five confusing NameErrors."""
        build = _load_builder()

        payload = build(
            voice_enabled=True, edition="oss", enterprise_features=[],
            install_source=SCRIPT,
        )
        # Pre-existing keys must survive alongside the new one.
        for key in ("version", "platform", "components", "runtimes", "voice_enabled"):
            assert key in payload
        assert payload["install_source"] == SCRIPT

    def test_the_handler_resolves_install_source_from_the_service(self):
        """Static guard: the value must come from `settings_service`, resolved
        in the HANDLER and threaded in — the same shape #1443 used for
        `edition` and for the same reason (see `_load_builder`)."""
        src = (_BACKEND / "main.py").read_text(encoding="utf-8")
        idx = src.find("async def get_version")
        assert idx != -1, "get_version handler not found in main.py"
        handler = src[idx : idx + 2500]

        assert "get_install_source()" in handler, (
            "get_version must resolve install_source from settings_service"
        )
        assert "install_source" in handler

    def test_the_builder_body_never_reads_settings(self):
        """The other half of the same contract: the DB read must NOT migrate
        back into the builder. It would look tidier and it would break every
        #926/#1443 test at `exec`, since the slice is stdlib-only by design."""
        src = (_BACKEND / "main.py").read_text(encoding="utf-8")
        start = src.find("def _build_version_payload")
        assert start != -1
        rest = src[start:]
        end = rest.find("\n\n\n")
        snippet = rest[: end if end != -1 else len(rest)]

        assert "settings_service" not in snippet, (
            "the builder must stay stdlib-only — the exec-slice depends on it"
        )
        assert "get_install_source" not in snippet


# ===========================================================================
# Post-review hardening (#2380 review round)
# ===========================================================================

class TestMarkerNormalisation:
    """The env value is normalised at config import, and the tests must exercise
    THAT path.

    The `marker` fixture rebinds `cfg.TRINITY_INSTALL_SOURCE` directly, which is
    the right shape for testing the recorder but bypasses the `.strip().lower()`
    the real boot applies — so a case/whitespace claim asserted through the
    fixture alone proves nothing about production. These go through `os.environ`
    and a genuine execution of `config.py` instead — under a throwaway module
    name, never `importlib.reload`; see `_load_config_with` for why the
    distinction is load-bearing rather than stylistic.
    """

    def _load_config_with(self, monkeypatch, raw):
        """Execute the real `config.py` under a THROWAWAY module name.

        Deliberately not `importlib.reload(config)`. Reload mutates the shared
        module object in place, and `config.py` mints a random `SECRET_KEY` at
        import whenever the env var is unset — which it is under the unit suite.
        Modules that bound `from config import SECRET_KEY` at their own import
        keep the old value while anything reading `config.SECRET_KEY` per request
        gets the new one, so every JWT signed on one side fails verification on
        the other. That is the #1895 divergence, and the conftest hook written
        for it cannot catch this shape: it restores `sys.modules["config"]` to
        the same object a reload has already mutated.

        Loading under a fresh name runs the identical module-level statement —
        `os.getenv(...).strip().lower()` — with none of that reach. There is
        nothing to restore afterwards, so no `finally` can be forgotten, and it
        stays correct if config grows another import-time-derived value.

        Safe because `config.py` imports only `os` and `urlparse` and its only
        import-time side effect is a warning print.
        """
        import importlib.util

        cfg = _config()
        if raw is None:
            monkeypatch.delenv(cfg.INSTALL_SOURCE_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(cfg.INSTALL_SOURCE_ENV_VAR, raw)

        spec = importlib.util.spec_from_file_location(
            "_trinity_config_probe_2380", cfg.__file__
        )
        probe = importlib.util.module_from_spec(spec)
        # Not registered in sys.modules: the point is that nothing else can
        # reach it, and it is garbage-collected with the test.
        spec.loader.exec_module(probe)
        return probe

    def test_case_and_whitespace_are_normalised_by_the_real_boot_path(
        self, monkeypatch
    ):
        """`.env` is a script-written trusted channel, so a forgiving spelling
        costs nothing — normalisation can only ever land on a value already in
        the closed set, so it widens what is SPELLED acceptably, never what is
        accepted."""
        loaded = self._load_config_with(monkeypatch, "  DO-Marketplace  ")
        assert loaded.TRINITY_INSTALL_SOURCE == MARKETPLACE
        assert loaded.TRINITY_INSTALL_SOURCE in loaded.INSTALL_SOURCE_VALUES

    def test_a_whitespace_only_marker_is_indistinguishable_from_absent(
        self, monkeypatch
    ):
        """`"   "` strips to `""`, so it takes the ABSENT branch (silent), not
        the invalid branch (which warns). Pinned because the two are easy to
        conflate and only one of them is supposed to log."""
        loaded = self._load_config_with(monkeypatch, "   ")
        assert loaded.TRINITY_INSTALL_SOURCE == ""

    def test_normalisation_cannot_invent_a_value_outside_the_set(
        self, monkeypatch
    ):
        """The point of the previous test, stated as the property that matters:
        no spelling of a non-member normalises into a member."""
        loaded = self._load_config_with(monkeypatch, "  DO_MARKETPLACE  ")
        assert loaded.TRINITY_INSTALL_SOURCE not in loaded.INSTALL_SOURCE_VALUES


class TestWriteOnceIsStructural:
    """Write-once must not rest solely on the SELECT that precedes the write.

    That SELECT is a separate statement, so on its own it is a check-then-act.
    Both arms now enforce the property at the PRIMARY KEY instead — SQLite via
    `INSERT OR IGNORE`, PostgreSQL via `ON CONFLICT DO NOTHING`.
    """

    def test_sqlite_insert_does_not_overwrite_even_if_the_read_is_defeated(
        self, marker
    ):
        """Simulates the check-then-act LOSING: the row lands between the SELECT
        and the INSERT, so the guard read sees nothing and the write proceeds
        anyway. `INSERT OR REPLACE` would clobber the winner; `INSERT OR IGNORE`
        leaves it alone, which is what makes write-once a property of the key
        rather than of the read.

        A blind cursor is the whole point — patching `sqlite3.Cursor.execute`
        is not possible (C-level attribute), and a proxy states the scenario
        more plainly anyway.
        """
        marker(MARKETPLACE)
        conn, cur = _mkdb()

        class _RaceCursor:
            """Answers the guard SELECT as if the table were empty, then lets
            the concurrent writer's row exist for the INSERT."""

            def __init__(self, inner):
                self._inner = inner
                self._armed = True

            def execute(self, sql, params=()):
                if self._armed and sql.strip().upper().startswith("SELECT"):
                    self._armed = False
                    self._inner.execute(
                        "INSERT INTO system_settings (key, value, updated_at) "
                        "VALUES ('install_source', ?, 'x')",
                        (SCRIPT,),
                    )
                    self._empty = True
                    return self
                self._empty = False
                return self._inner.execute(sql, params)

            def fetchone(self):
                return None if getattr(self, "_empty", False) else self._inner.fetchone()

            @property
            def rowcount(self):
                return self._inner.rowcount

        _database()._record_install_source(_RaceCursor(cur), conn)

        assert _recorded(cur) == SCRIPT

    def test_an_empty_row_is_not_silently_upgraded_into_provenance(
        self, marker
    ):
        """An empty value can only come from a direct DB write. It reads as
        "no provenance recorded", but the INSERT is still refused by the key —
        silently promoting somebody's manual edit into a recorded marketplace
        value is exactly the self-assertion this design refuses."""
        marker(MARKETPLACE)
        conn, cur = _mkdb()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES ('install_source', '', 'x')"
        )
        conn.commit()

        _database()._record_install_source(cur, conn)

        assert _recorded(cur) == ""

    def test_the_engine_arm_uses_the_write_once_insert_not_the_upsert(
        self, marker, monkeypatch
    ):
        """`set_setting` is an upsert AND now refuses this key outright, so the
        recorder reaching it at all is the regression."""
        marker(MARKETPLACE)
        ops = _FakeSettingsOps()
        monkeypatch.setattr(_database(), "SettingsOperations", lambda: ops)

        _database()._record_install_source_engine()

        assert ops.writes == [("install_source", MARKETPLACE)]
        assert not any(w[0] == "set_setting" for w in ops.writes)


class TestSinkGuard:
    """Defence in depth, per `db/settings.py`'s own stated rule: validate at the
    boundary AND at the sink, because the generic catch-all can write ANY key
    and that same door has been found open six times."""

    def test_set_setting_refuses_install_source(self):
        from db.settings import SettingsOperations

        with pytest.raises(ValueError) as exc:
            SettingsOperations().set_setting("install_source", MARKETPLACE)
        assert "install_source" in str(exc.value)

    def test_the_refusal_is_key_scoped_not_value_scoped(self):
        """Every value is refused, including the harmless-looking ones — the key
        is not writable through an upsert at all."""
        from db.settings import SettingsOperations

        for value in (UNKNOWN, SCRIPT, "", "anything"):
            with pytest.raises(ValueError):
                SettingsOperations().set_setting("install_source", value)

    def test_a_neighbouring_key_is_unaffected(self):
        """Guard placement regression: an over-broad match here would break
        every ordinary setting write on the platform."""
        from services.secret_settings import assert_plaintext_write_allowed

        # The credential guard still governs its own keys, and a plain key
        # passes both guards. Asserting via the policy function keeps this test
        # free of a live engine.
        assert assert_plaintext_write_allowed("public_chat_url") is None

