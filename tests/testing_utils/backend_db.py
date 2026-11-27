"""Rows a live-tier test seeds or reads in the stack's own database (#4620).

Every compose file boots the stack on the bundled PostgreSQL since #4175. A
fixture that opens ``trinity.db`` with ``sqlite3`` inside the backend container
no longer reaches the database the backend serves from: it opens an empty file
and fails with ``no such table``. That was 52 of the 73 rows the nightly live
tier failed on 2026-11-24 (51 errors and 1 failure).

This module runs one statement with the backend's own Python, through the
backend's own engine (``db.engine.get_engine()``), so it lands in whichever
database the stack booted on. Write statements both backends accept:

* named binds (``:name``), never ``?``;
* ``ON CONFLICT DO NOTHING``, never ``INSERT OR IGNORE``;
* a timestamp passed as a bind (``utc_now()``), never ``datetime('now')``.

Values travel as JSON on stdin and reach the database as bound parameters.
Nothing a test passes is pasted into the script text.

Two limits:

* **Fixture data only.** An error raised here can repeat a bound value: the
  PostgreSQL server echoes the statement it received (``LINE 1: ...``) and the
  conflicting key (``DETAIL: Key (username)=(...)``), and that text reaches the
  job log and the JUnit file. Never bind a real secret.
* **The rows are real, so the target is checked.** The API client talks to
  ``TRINITY_API_URL``; this module talks to a container on the local Docker
  daemon. ``_backend_container`` refuses unless the two are the same stack.

``tests/unit/test_4620_live_tier_db_seam.py`` runs ``RUNNER`` against a real
database and keeps new raw ``sqlite3`` reads of the stack's database out of the
live tier.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from .cleanup import is_local_target

DEFAULT_CONTAINER = "trinity-backend"

_MARKER = "RESULT:"

#: The container this process already checked, so the check runs once.
_checked_container: Optional[str] = None

#: Runs inside the backend container (working directory ``/app``). The request
#: is one JSON object on stdin: ``sql``, ``binds``, and ``bcrypt`` — the names
#: of binds to replace with a bcrypt hash of their value before the statement
#: runs. The answer is the last stdout line that starts with the marker.
RUNNER = '''
import json, sys
from sqlalchemy import text
from db.engine import get_engine

req = json.load(sys.stdin)
binds = dict(req.get("binds") or {})
hashed = req.get("bcrypt") or []
if hashed:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    for name in hashed:
        binds[name] = ctx.hash(binds[name])
with get_engine().begin() as conn:
    result = conn.execute(text(req["sql"]), binds)
    if result.returns_rows:
        payload = [list(row) for row in result.fetchall()]
    else:
        payload = result.rowcount
print("RESULT:" + json.dumps(payload, default=str))
'''


def _backend_container() -> str:
    """The backend container of the stack the API client talks to.

    Nothing else ties ``TRINITY_API_URL`` to a container name, and a mismatch
    writes a login, an ownership row and queue items into another stack's
    database: a second stack on the same machine (a verify stack beside a
    developer's own), or a remote target with a local stack running.

    * The API has to be local. The Docker daemon reached here is.
    * A container named in ``TRINITY_BACKEND_CONTAINER`` is taken as given.
    * Otherwise the default container has to publish the port the API URL
      names, which only the stack serving that URL can do.
    """
    global _checked_container
    if _checked_container:
        return _checked_container

    api_url = os.getenv("TRINITY_API_URL", "http://localhost:8000")
    if not is_local_target(api_url):
        raise RuntimeError(
            f"TRINITY_API_URL ({api_url}) is not a local instance. testkit.backend_db "
            "writes through the local Docker daemon and would reach another stack's database."
        )

    named = os.getenv("TRINITY_BACKEND_CONTAINER")
    if named:
        _checked_container = named
        return named

    parsed = urlparse(api_url)
    api_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    published = subprocess.run(
        ["docker", "port", DEFAULT_CONTAINER], capture_output=True, text=True, timeout=15,
    )
    host_ports = {int(port) for port in re.findall(r":(\d+)\s*$", published.stdout, re.M)}
    if published.returncode != 0 or api_port not in host_ports:
        raise RuntimeError(
            f"the container {DEFAULT_CONTAINER!r} does not publish port {api_port}, the port of "
            f"TRINITY_API_URL ({api_url}), so it is not known to be the stack under test. "
            "Name the backend container of that stack in TRINITY_BACKEND_CONTAINER."
        )
    _checked_container = DEFAULT_CONTAINER
    return DEFAULT_CONTAINER


def _exec(request_json: str) -> subprocess.CompletedProcess:
    """Hand the request to ``RUNNER`` in the backend container."""
    return subprocess.run(
        ["docker", "exec", "-i", _backend_container(), "python3", "-c", RUNNER],
        input=request_json, capture_output=True, text=True, timeout=30,
    )


def _call(sql: str, binds: dict, bcrypt: tuple = ()) -> Any:
    proc = _exec(json.dumps({"sql": sql, "binds": binds, "bcrypt": list(bcrypt)}))
    if proc.returncode != 0:
        # The tail of stderr carries the statement and the cause. It can also
        # carry a bound value (see the module docstring): fixture data only.
        raise RuntimeError(f"backend database call failed: {proc.stderr.strip()[-800:]}")
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_MARKER):
            return json.loads(line[len(_MARKER):])
    raise RuntimeError("backend database call printed no result")


def utc_now() -> str:
    """Now as the ISO-Z text the platform writes (``utils.helpers.utc_now_iso``)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def execute(sql: str, **binds: Any) -> int:
    """Run a write statement; returns the number of rows it touched."""
    return _call(sql, binds)


def rows(sql: str, **binds: Any) -> list:
    """Run a query; returns its rows, each as a list."""
    return _call(sql, binds)


def scalar(sql: str, **binds: Any) -> Any:
    """The first column of the first row, or None when the query has no row."""
    found = rows(sql, **binds)
    return found[0][0] if found else None


def create_user(username: str, password: str, email: str, role: str = "operator") -> None:
    """A password-login user written straight into ``users``.

    ``operator`` by default: a ``user`` is Workspace-only since
    trinity-enterprise#837 and is refused on the routes these tests check.
    The password is hashed inside the backend container with the same bcrypt
    scheme the login verifies against.
    """
    _call(
        "INSERT INTO users (username, password_hash, role, email, created_at, updated_at) "
        "VALUES (:username, :password_hash, :role, :email, :now, :now) "
        "ON CONFLICT DO NOTHING",
        {"username": username, "password_hash": password, "role": role,
         "email": email, "now": utc_now()},
        bcrypt=("password_hash",),
    )


def delete_user(username: str) -> None:
    execute("DELETE FROM users WHERE username = :username", username=username)


def admin_user_id() -> int:
    """The id of the ``admin`` user, who owns the sentinel agents these fixtures seed."""
    found = scalar("SELECT id FROM users WHERE username = 'admin'")
    if found is None:
        raise RuntimeError("no 'admin' user on this stack; the fixture needs one as the owner")
    return found
