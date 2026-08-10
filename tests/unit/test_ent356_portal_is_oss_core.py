"""The workspace / client portal is OSS core and stays that way (ent#356).

It shipped as an entitled enterprise module: `register_module("client_portal")`
plus `requires_entitlement("client_portal")` on the router, so a community build
mounted nothing and every endpoint 404'd. The workspace is the main surface a
non-operator uses to work with agents, so that gate capped adoption at exactly
the population we most want using it (Intelligence Design Weekly, 2026-08-07).

These guards pin the four things that could silently undo the move:

  1. the module imports and mounts with no entitlement machinery at all;
  2. `client_portal` is never advertised as an enterprise feature again;
  3. the tables stay on the OSS two-track runner — and keep their historical
     names, because renaming them is the data migration the move forbids;
  4. `client_portal.router` still means the MODULE, not the APIRouter object.

(4) reads like trivia and is not: the first version of `__init__.py` did
`from .router import router`, which shadowed the submodule, so
`client_portal.router.block_agent_client` raised AttributeError and took 13
tests with it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_MODULE = _BACKEND / "client_portal"

pytestmark = pytest.mark.unit


def test_the_module_lives_in_the_public_repo():
    assert _MODULE.is_dir(), "client_portal is not in the OSS backend"
    for expected in ("router.py", "service.py", "db.py", "models.py", "portal_auth.py"):
        assert (_MODULE / expected).is_file(), f"missing {expected}"


def test_no_entitlement_gate_anywhere_in_the_module():
    """A single re-added `requires_entitlement` would 404 the whole surface in
    community builds again — the exact state this issue removed."""
    offenders = []
    for path in _MODULE.glob("*.py"):
        for num, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue          # prose about the old gate is fine
            if "requires_entitlement" in line or "register_module" in line:
                offenders.append(f"{path.name}:{num}: {stripped[:70]}")
    assert not offenders, (
        "entitlement machinery is back in an OSS-core module: " + "; ".join(offenders)
    )


def test_the_router_is_mounted_unconditionally():
    """Mounted at import in `main.py`, not inside `register_enterprise`."""
    src = (_BACKEND / "main.py").read_text()
    assert "from client_portal.router import router as client_portal_router" in src
    assert "app.include_router(client_portal_router)" in src

    # …and not smuggled back in through the enterprise seam.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "enterprise" in node.name:
            body = ast.dump(node)
            assert "client_portal" not in body, (
                f"{node.name}() references client_portal — the module is OSS core "
                "and must not be registered through the entitlement seam"
            )


def test_router_attribute_still_resolves_to_the_submodule():
    """`client_portal.router` must be the module, not the APIRouter object."""
    import sys

    sys.path.insert(0, str(_BACKEND))
    import client_portal
    import client_portal.router as router_module

    assert client_portal.router is router_module, (
        "`__init__` is shadowing the `router` submodule with the APIRouter "
        "object — handler lookups like client_portal.router.<fn> break"
    )
    from fastapi import APIRouter
    assert isinstance(router_module.router, APIRouter)


def test_the_frontend_no_longer_gates_on_the_entitlement():
    """A leftover `isEntitled('client_portal')` would hide the surface in
    exactly the builds this change opened it for."""
    frontend = _REPO / "src" / "frontend" / "src"
    offenders = []
    for path in list(frontend.rglob("*.vue")) + list(frontend.rglob("*.js")):
        text = path.read_text(errors="replace")
        if "isEntitled('client_portal')" in text or 'isEntitled("client_portal")' in text:
            offenders.append(str(path.relative_to(_REPO)))
        if "requiresEntitlement: 'client_portal'" in text:
            offenders.append(str(path.relative_to(_REPO)) + " (route meta)")
    assert not offenders, f"frontend still gates the workspace: {offenders}"


# ---------------------------------------------------------------------------
# The tables — adopted, not recreated
# ---------------------------------------------------------------------------

PORTAL_TABLES = (
    "enterprise_portal_sessions",
    "enterprise_portal_messages",
    "enterprise_client_blocks",
)


def test_tables_are_declared_on_the_oss_schema():
    from importlib import util

    spec = util.spec_from_file_location("_oss_schema", _BACKEND / "db" / "schema.py")
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for table in PORTAL_TABLES:
        assert table in mod.TABLES, f"{table} is not in db/schema.py TABLES"


def test_both_migration_tracks_carry_the_adoption():
    """Invariant #9: every schema change lands on SQLite AND Alembic."""
    sqlite_track = (_BACKEND / "db" / "migrations.py").read_text()
    assert "client_portal_tables_to_oss" in sqlite_track

    alembic = _BACKEND / "migrations" / "versions" / "0036_client_portal_oss.py"
    assert alembic.is_file(), "no Alembic revision for the adoption"
    text = alembic.read_text()
    for table in PORTAL_TABLES:
        assert table in text, f"{table} missing from the Alembic revision"


@pytest.mark.parametrize("track", ["sqlite", "alembic"])
def test_the_adoption_is_idempotent_on_an_existing_install(track):
    """The tables already exist on every entitled install and hold live client
    conversations. "No data migration" means every statement must be a no-op
    there — a bare CREATE TABLE would abort the migration chain at boot."""
    if track == "sqlite":
        src = (_BACKEND / "db" / "migrations.py").read_text()
        start = src.index("def _migrate_client_portal_tables_to_oss")
        body = src[start:src.index("\nMIGRATIONS = [")]
    else:
        body = (_BACKEND / "migrations" / "versions" / "0036_client_portal_oss.py").read_text()

    creates = [ln for ln in body.splitlines() if "CREATE TABLE" in ln.upper()]
    assert creates, "no CREATE TABLE statements found"
    for line in creates:
        assert "IF NOT EXISTS" in line.upper(), f"non-idempotent create: {line.strip()}"

    for line in body.splitlines():
        if "CREATE INDEX" in line.upper():
            assert "IF NOT EXISTS" in line.upper(), f"non-idempotent index: {line.strip()}"


def test_the_alembic_downgrade_does_not_drop_client_history():
    """This revision ADOPTED pre-existing tables; dropping them on downgrade
    would destroy data it never created."""
    text = (_BACKEND / "migrations" / "versions" / "0036_client_portal_oss.py").read_text()
    downgrade = text[text.index("def downgrade"):]
    assert "drop_table" not in downgrade and "DROP TABLE" not in downgrade.upper(), (
        "downgrade drops the portal tables — that is client conversation history"
    )


def test_table_names_keep_their_historical_prefix():
    """Renaming would force a data migration on every entitled install, which
    the acceptance criteria forbid. The prefix is history, not licensing."""
    schema = (_BACKEND / "db" / "schema.py").read_text()
    for table in PORTAL_TABLES:
        assert table.startswith("enterprise_")
        assert table in schema


# ---------------------------------------------------------------------------
# CodeQL py/polynomial-redos on the email validator (surfaced by the move)
# ---------------------------------------------------------------------------

def _service():
    import sys
    sys.path.insert(0, str(_BACKEND))
    from client_portal import service
    return service


def test_the_email_regex_has_no_ambiguous_repetition():
    """`[^@\\s]` matches a dot, so `[^@\\s]+\\.[^@\\s]+` let the two domain atoms
    compete for the same characters — polynomial backtracking by construction.

    Moving this file into the public repo is what made CodeQL see it (the
    private submodule is not scanned), so the finding is as old as the code.
    Excluding the dot from the domain classes removes the ambiguity.
    """
    pattern = _service()._EMAIL_RE.pattern
    domain = pattern.split("@", 1)[1]
    assert "[^@\\s]+" not in domain, (
        "the domain part accepts dots inside a repeated class again — that is "
        "the ambiguity CodeQL flags (py/polynomial-redos)"
    )


def test_the_length_cap_is_checked_before_the_regex():
    """The cap is what actually bounds the cost, and only while it is evaluated
    FIRST. Reordering the `or` chain would hand the regex unbounded input."""
    import inspect

    src = inspect.getsource(_service().normalize_client_email)
    guard = src[src.index("if not candidate"):src.index("raise ClientPortalError")]
    assert guard.index("len(candidate) > 320") < guard.index("_EMAIL_RE"), (
        "the regex is evaluated before the length cap — an arbitrarily long "
        "input would reach it"
    )


@pytest.mark.parametrize("address", [
    "a@b.co", "first.last@sub.example.com", "x@y.z", "UPPER@Example.COM",
])
def test_real_addresses_still_validate(address):
    assert _service().normalize_client_email(address) == address.strip().lower()


@pytest.mark.parametrize("address", ["a@b", "a@.com", "a@b..com", "no-at.com", "a b@c.d", ""])
def test_malformed_addresses_are_still_rejected(address):
    service = _service()
    with pytest.raises(service.ClientPortalError):
        service.normalize_client_email(address)
