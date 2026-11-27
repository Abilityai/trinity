"""The live tier reaches the stack's database through the backend's engine (#4620).

Every compose file boots on the bundled PostgreSQL since #4175. Four live-tier
modules still seeded their rows by opening ``trinity.db`` with ``sqlite3``
inside the backend container. On a PostgreSQL stack that opens an empty file
and fails with ``no such table``: 52 of the 73 rows the nightly live tier
failed on 2026-11-24 (51 errors and 1 failure). Each module carried its own
copy of the helper, so the fix is one shared seam (``testkit.backend_db``) and
this test, which stops the next copy.

Two things are pinned here:

* the static part: outside the unit tier, a test module that names both
  ``sqlite3`` and the stack's database file is on a reviewed list or fails;
* the behaviour: the script the seam runs in the backend container is run
  here against a real database, on SQLite always and on PostgreSQL when
  ``TEST_POSTGRES_URL`` is set (``schema-parity.yml``'s PostgreSQL tier).
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from db_harness import db_backend, scalar as harness_scalar  # noqa: F401
from testkit import backend_db

TESTS = Path(__file__).resolve().parents[1]
BACKEND_SRC = TESTS.parent / "src" / "backend"
#: The seam itself explains the old pattern in its docstring. Resolved, because
#: ``tests/testkit`` is a symlink and a walk reports the directory behind it.
SEAM = Path(backend_db.__file__).resolve()

_SQLITE = re.compile(r"\bsqlite3\b")
_STACK_DB = re.compile(r"trinity\.db|TRINITY_DB_PATH")

_SKIPS_ON_POSTGRES = (
    "reads ~/trinity-data/trinity.db on the host and skips when the file is absent, "
    "which is every run on a PostgreSQL stack; move it onto testkit.backend_db"
)

#: Reviewed: these name both words and do not open the stack's database.
#: Paths are relative to ``tests/``.
NOT_THE_STACK_DATABASE = {
    "db_harness.py": "the unit-tier harness; it builds its own throwaway SQLite file",
    "integration/test_fail_queued_for_agent.py":
        "opens a SQLite file it creates in a temp directory, never the stack's",
    "scheduler_tests/test_pg_adapter.py":
        "a SQLite URL as a parametrize case for the URL classifier; opens nothing",
}

#: The defect of #4620 itself, still in place: to be moved onto the seam.
#: Until then these tests do not run on a PostgreSQL stack.
STILL_TO_MOVE = {
    "test_public_links.py": _SKIPS_ON_POSTGRES,
    "test_public_user_memory.py": _SKIPS_ON_POSTGRES,
    "test_slack_integration.py": _SKIPS_ON_POSTGRES,
    "test_whatsapp_integration.py":
        "slow tier, outside the nightly selection; opens /data/trinity.db inside the "
        "backend container and fails on a PostgreSQL stack; move it onto testkit.backend_db",
}

#: Both lists are shrink-only: an entry whose file no longer matches fails the
#: test below, so neither can go stale.
ALLOWED = {**NOT_THE_STACK_DATABASE, **STILL_TO_MOVE}

#: SQLite-only spellings. A statement handed to the seam has to run on both
#: backends, so a module that uses the seam must not carry these.
_SQLITE_ONLY = re.compile(r"INSERT\s+OR\s+(IGNORE|REPLACE)|datetime\(\s*'now'", re.I)


#: Not test code: caches, a local virtualenv (``tests/run-full.sh`` creates
#: ``tests/.venv``), generated reports. The set test_2080_harness_contract uses.
_NOT_TEST_CODE = {"__pycache__", "node_modules", "reports"}


@functools.lru_cache(maxsize=1)
def _live_tier_modules() -> tuple:
    """Every test-tree module outside the unit tier, as ``(path, text)`` pairs."""
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        parts = path.relative_to(TESTS).parts
        if parts[0] == "unit" or path.resolve() == SEAM:
            continue
        if any(part in _NOT_TEST_CODE or part.startswith(".") for part in parts[:-1]):
            continue
        found.append(("/".join(parts), path.read_text(encoding="utf-8", errors="replace")))
    return tuple(found)


def _reads_stack_db_with_sqlite(text: str) -> bool:
    return bool(_SQLITE.search(text) and _STACK_DB.search(text))


def test_no_live_tier_module_opens_the_stack_database_with_sqlite3():
    offenders = [rel for rel, text in _live_tier_modules()
                 if _reads_stack_db_with_sqlite(text) and rel not in ALLOWED]
    assert not offenders, (
        "these modules name both sqlite3 and the stack's database file. The stack "
        "boots on PostgreSQL (#4175), so a raw sqlite3 read fails or skips there. "
        f"Seed and read through testkit.backend_db instead: {offenders}"
    )


def test_the_reviewed_list_only_names_modules_that_still_match():
    live = dict(_live_tier_modules())
    stale = [rel for rel in ALLOWED
             if rel not in live or not _reads_stack_db_with_sqlite(live[rel])]
    assert not stale, f"remove these from ALLOWED, they no longer match: {stale}"


def test_modules_using_the_seam_write_statements_both_backends_accept():
    offenders = {}
    for rel, text in _live_tier_modules():
        if "backend_db" not in text:
            continue
        found = sorted({m.group(0) for m in _SQLITE_ONLY.finditer(text)})
        if found:
            offenders[rel] = found
    assert not offenders, (
        "SQLite-only SQL in a module that uses testkit.backend_db; write "
        f"ON CONFLICT DO NOTHING and pass the time as a bind: {offenders}"
    )


def test_the_nightly_live_job_limit_covers_a_measured_night_with_headroom():
    """A job cancelled at its limit writes no JUnit: the night is red with no
    failure list. At 45 minutes that happened on 5 of 10 nights.

    The nights that did finish took 39.5 to 44.8 minutes, and the 52 tests
    that now run instead of failing at setup add about 1.5. The floor is that
    46 times the 1.94 runner variance measured on this repo (#2462). The
    ceiling says a live job that needs two hours is to be split, not given
    more time (#1896 holds the PR sweep to the same idea).
    """
    import yaml

    workflow = yaml.safe_load(
        (TESTS.parent / ".github" / "workflows" / "dev-nightly.yml").read_text())
    assert 90 <= workflow["jobs"]["integration"]["timeout-minutes"] <= 120


# --------------------------------------------------------------------------
# Where the statement runs: the transport and the target check.
# --------------------------------------------------------------------------

class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class _DockerCalls(list):
    """The ``(argv, stdin)`` pairs handed to ``docker``, plus the scripted answers."""

    def __init__(self):
        super().__init__()
        self.answers = {
            "port": _Done("8000/tcp -> 0.0.0.0:8000\n8000/tcp -> [::]:8000\n"),
            "exec": _Done(backend_db._MARKER + "[]\n"),
        }


@pytest.fixture
def docker_calls(monkeypatch):
    """Record what would be handed to ``docker`` and answer it from a script."""
    calls = _DockerCalls()

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return calls.answers[argv[1]]

    monkeypatch.setattr(backend_db.subprocess, "run", fake_run)
    monkeypatch.setattr(backend_db, "_checked_container", None)
    monkeypatch.delenv("TRINITY_BACKEND_CONTAINER", raising=False)
    monkeypatch.setenv("TRINITY_API_URL", "http://localhost:8000")
    return calls


def _docker_verbs(calls):
    return [argv[1] for argv, _ in calls]


def test_the_request_goes_to_the_backend_container_on_stdin(docker_calls):
    backend_db.execute("SELECT 1 WHERE :v = :v", v="value-4620")

    argv, sent = docker_calls[-1]
    assert argv == ["docker", "exec", "-i", "trinity-backend", "python3", "-c", backend_db.RUNNER]
    assert "value-4620" in sent and "SELECT 1" in sent
    assert not any("value-4620" in part for part in argv), "a bound value on the command line"


def test_the_default_container_is_used_when_it_publishes_the_api_port(docker_calls):
    assert backend_db._backend_container() == "trinity-backend"
    assert backend_db._backend_container() == "trinity-backend"
    assert _docker_verbs(docker_calls) == ["port"], "the check runs once per process"


def test_a_second_stack_on_another_port_is_refused(docker_calls, monkeypatch):
    """A verify stack on :18000 beside a developer's own stack on :8000: the
    container called trinity-backend belongs to the other one."""
    monkeypatch.setenv("TRINITY_API_URL", "http://localhost:18000")

    with pytest.raises(RuntimeError) as refused:
        backend_db.execute("DELETE FROM users WHERE username = :u", u="x")

    assert "TRINITY_BACKEND_CONTAINER" in str(refused.value)
    assert "exec" not in _docker_verbs(docker_calls), "the statement must not be sent"


def test_a_missing_default_container_is_refused(docker_calls):
    docker_calls.answers["port"] = _Done(stderr="Error: No such container", returncode=1)

    with pytest.raises(RuntimeError):
        backend_db._backend_container()


def test_a_remote_target_is_refused_before_docker_is_asked(docker_calls, monkeypatch):
    monkeypatch.setenv("TRINITY_API_URL", "https://trinity.example.com")

    with pytest.raises(RuntimeError) as refused:
        backend_db.execute("DELETE FROM users WHERE username = :u", u="x")

    assert "not a local instance" in str(refused.value)
    assert docker_calls == []


def test_a_named_container_is_taken_as_given(docker_calls, monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_CONTAINER", "verify-backend")
    monkeypatch.setenv("TRINITY_API_URL", "http://localhost:18000")

    backend_db.execute("SELECT 1")

    assert _docker_verbs(docker_calls) == ["exec"]
    assert docker_calls[-1][0][3] == "verify-backend"


def test_the_result_is_found_among_other_output_lines(monkeypatch):
    """Importing the backend's ``db`` package prints notices of its own."""
    noisy = "ADMIN_PASSWORD not set\n" + backend_db._MARKER + "[[1, \"a\"]]\ntrailing notice\n"
    monkeypatch.setattr(backend_db, "_exec", lambda request: _Done(noisy))
    assert backend_db.rows("SELECT 1") == [[1, "a"]]

    monkeypatch.setattr(backend_db, "_exec", lambda request: _Done("only notices\n"))
    with pytest.raises(RuntimeError):
        backend_db.rows("SELECT 1")


# --------------------------------------------------------------------------
# Behaviour: the same RUNNER text, the backend's own engine, a real database.
# --------------------------------------------------------------------------

@pytest.fixture
def seam(db_backend, monkeypatch):
    """``backend_db`` with the container hop replaced by a local process.

    The script, the request and the parsing are the real ones; only
    ``docker exec`` is swapped for this interpreter, started in ``src/backend``
    with the environment ``db_backend`` prepared.
    """
    def _local(request_json: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", backend_db.RUNNER],
            input=request_json, capture_output=True, text=True, timeout=60,
            cwd=str(BACKEND_SRC), env=dict(os.environ),
        )

    monkeypatch.setattr(backend_db, "_exec", _local)
    return backend_db


@pytest.mark.requires_postgres
def test_create_user_writes_a_login_the_backend_can_verify(seam):
    seam.create_user("seam-user", "seam-password-4620", "seam-user@test.example.com")

    stored = harness_scalar(
        "SELECT password_hash FROM users WHERE username = :u", u="seam-user")
    assert stored and stored != "seam-password-4620"
    from passlib.context import CryptContext
    assert CryptContext(schemes=["bcrypt"], deprecated="auto").verify(
        "seam-password-4620", stored)
    assert harness_scalar(
        "SELECT role FROM users WHERE username = :u", u="seam-user") == "operator"


@pytest.mark.requires_postgres
def test_creating_the_same_user_twice_keeps_the_first_row(seam):
    seam.create_user("seam-twice", "first-password", "seam-twice@test.example.com")
    first = harness_scalar(
        "SELECT password_hash FROM users WHERE username = :u", u="seam-twice")

    seam.create_user("seam-twice", "second-password", "seam-twice@test.example.com")

    assert harness_scalar(
        "SELECT COUNT(*) FROM users WHERE username = :u", u="seam-twice") == 1
    assert harness_scalar(
        "SELECT password_hash FROM users WHERE username = :u", u="seam-twice") == first


@pytest.mark.requires_postgres
def test_execute_rows_and_scalar_round_trip(seam):
    now = seam.utc_now()
    assert now.endswith("Z")
    for item in ("seam-q-1", "seam-q-2"):
        touched = seam.execute(
            "INSERT INTO operator_queue "
            "(id, agent_name, type, status, priority, title, question, created_at) "
            "VALUES (:id, :agent, 'question', 'pending', 'medium', 'Seam', 'Proceed?', :now) "
            "ON CONFLICT DO NOTHING",
            id=item, agent="seam-agent", now=now)
        assert touched == 1

    assert seam.rows(
        "SELECT id, status FROM operator_queue WHERE agent_name = :agent ORDER BY id",
        agent="seam-agent") == [["seam-q-1", "pending"], ["seam-q-2", "pending"]]
    assert seam.scalar(
        "SELECT status FROM operator_queue WHERE id = :id", id="seam-q-1") == "pending"
    assert seam.scalar(
        "SELECT status FROM operator_queue WHERE id = :id", id="absent") is None
    assert seam.execute(
        "DELETE FROM operator_queue WHERE agent_name = :agent", agent="seam-agent") == 2


@pytest.mark.requires_postgres
def test_delete_user_removes_the_row(seam):
    seam.create_user("seam-gone", "seam-password-4620", "seam-gone@test.example.com")
    seam.delete_user("seam-gone")
    assert harness_scalar(
        "SELECT COUNT(*) FROM users WHERE username = :u", u="seam-gone") == 0


@pytest.mark.requires_postgres
def test_the_admin_owner_is_found_and_its_absence_is_named(seam):
    with pytest.raises(RuntimeError) as missing:
        seam.admin_user_id()
    assert "admin" in str(missing.value)

    seam.create_user("admin", "seam-password-4620", "seam-admin@test.example.com", role="admin")

    assert seam.admin_user_id() == harness_scalar(
        "SELECT id FROM users WHERE username = :u", u="admin")


@pytest.mark.requires_postgres
def test_a_failing_statement_names_the_cause(seam):
    """The cause, and nothing promised beyond it: on PostgreSQL the server's
    own text repeats the statement and the conflicting key, bound values
    included, which is why the seam is for fixture data only."""
    with pytest.raises(RuntimeError) as failure:
        seam.execute("INSERT INTO no_such_table_4620 (v) VALUES (:v)", v="fixture-value")
    assert "no_such_table_4620" in str(failure.value)
