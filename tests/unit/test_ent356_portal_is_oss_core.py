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
import re
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


def _strip_comments(text: str) -> str:
    """Drop HTML/JS comments so the scan below sees CODE only.

    Added by ent#357: removing the dead gate from `NavBar.vue` meant writing a
    comment that *names* the predicate being removed — and this guard, a
    whole-file substring scan, flagged its own explanation. A guard that forbids
    a string cannot also forbid describing it, or the fix and the note about the
    fix are mutually exclusive. (Same trap the `_is_prose` helper below handles
    for the line-based scans.)
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)     # Vue template
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)      # JS block
    text = re.sub(r"(?m)^\s*//.*$", "", text)              # JS line
    return text


def test_the_frontend_no_longer_gates_on_the_entitlement():
    """A leftover `isEntitled('client_portal')` would hide the surface in
    exactly the builds this change opened it for.

    Not hypothetical: ent#357 added the Workspace nav entry carrying
    `v-if="enterpriseStore.isEntitled('client_portal')"` — correct when it was
    written, dead the moment ent#356 landed, because nothing registers that
    entitlement any more. The one-click entry point would have rendered on no
    OSS build at all. This test is what caught it, on the ent#357 PR.
    """
    frontend = _REPO / "src" / "frontend" / "src"
    offenders = []
    for path in list(frontend.rglob("*.vue")) + list(frontend.rglob("*.js")):
        code = _strip_comments(path.read_text(errors="replace"))
        if "isEntitled('client_portal')" in code or 'isEntitled("client_portal")' in code:
            offenders.append(str(path.relative_to(_REPO)))
        if "requiresEntitlement: 'client_portal'" in code:
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


# ---------------------------------------------------------------------------
# How the portal's own tests patch `services.*`
# ---------------------------------------------------------------------------

def _is_prose(line: str) -> bool:
    """Docstring/comment text, not a call.

    Both guards below search for a pattern that their own explanations have to
    *name* in order to forbid it — the read-the-prose trap this repo has hit
    before (a guard that greps source text will happily flag the sentence
    describing the bug). RST literals (``like this``) and comment/bullet lines
    are documentation; a real call site is neither.
    """
    stripped = line.strip()
    return stripped.startswith(("#", "*", '"""', "'''")) or "``" in line


_PORTAL_TEST_FILES = (
    "test_ent79_portal_exposure.py",
    "test_ent163_portal_delegated_identity.py",
    "test_ent281_client_logout_block.py",
    "test_ent287_portal_rate_limits.py",
    "test_ent308_inbox_dir_collision.py",
    "test_ent311_portal_signin_hardening.py",
    "test_ent357_workspace_owned_roster.py",
)


def test_portal_tests_patch_services_by_string_target_not_module_alias():
    """`import services.x as a` + `patch.object(a, ...)` is identity-sensitive.

    `client_portal.service` reaches its dependencies through function-local
    `from services.x import y`, which resolves `sys.modules["services.x"]` at
    call time. `import services.x as a` resolves the `services` package
    ATTRIBUTE instead. Normally the same object — but conftest's #762
    invariant-restore swaps `sys.modules` entries back to a captured baseline
    between tests, so once an earlier test replaces one, the two diverge and the
    alias-form patch targets an object the code never calls.

    That is the worst possible failure shape: it needs the FULL suite to
    reproduce (five of these tests passed in every small selection and failed in
    CI), and when it fires the test fails on the real dependency's behaviour, so
    it reads as a product bug rather than a patch that missed.

    The string form resolves through `sys.modules`, exactly as the code does.
    """
    here = Path(__file__).parent
    offenders = []
    for name in _PORTAL_TEST_FILES:
        path = here / name
        if not path.is_file():
            continue
        aliases = {}
        for num, line in enumerate(path.read_text().splitlines(), 1):
            if _is_prose(line):
                continue
            m = re.match(r"\s*import\s+(services\.[A-Za-z_.]+)\s+as\s+([A-Za-z_]\w*)\s*$", line)
            if m:
                aliases[m.group(2)] = (m.group(1), num)
            for alias, (mod, decl) in aliases.items():
                if re.search(rf"\b(?:patch\.object|monkeypatch\.setattr)\(\s*{alias}\s*,", line):
                    offenders.append(
                        f"{name}:{num} patches via alias `{alias}` (= {mod}, bound "
                        f"line {decl}) — use the string target \"{mod}.<attr>\""
                    )
    assert not offenders, (
        "portal tests patch a `services.*` module through an import alias:\n  "
        + "\n  ".join(offenders)
    )


def test_monkeypatch_string_targets_are_not_used_for_services_modules():
    """`monkeypatch.setattr("services.x.y", ...)` is ALSO unsafe — pytest's own
    resolver walks package attributes.

    This one is the trap inside the trap. `mock.patch` and `monkeypatch.setattr`
    take what looks like the same string target, so converting alias-form patches
    to "the string form" reads like one uniform fix — and it is not. Measured
    against a staged divergence:

        the product's `from services.x import f`  -> LIVE  (sys.modules)
        unittest.mock  _get_target("services.x.f") -> LIVE  (sys.modules)
        pytest   monkeypatch resolve("services.x") -> STALE (package attribute)

    `_pytest.monkeypatch.resolve()` reduces the dotted path with `getattr`, so it
    lands on whatever the `services` package attribute points at. Six of these
    tests were fixed by moving to string targets and one kept failing for exactly
    this reason. Those sites must go through `_services_module()`, which reads
    `sys.modules` directly.
    """
    here = Path(__file__).parent
    offenders = []
    for name in _PORTAL_TEST_FILES:
        path = here / name
        if not path.is_file():
            continue
        for num, line in enumerate(path.read_text().splitlines(), 1):
            if _is_prose(line):
                continue
            if re.search(r'monkeypatch\.setattr\(\s*["\']services\.', line):
                offenders.append(f"{name}:{num}: {line.strip()[:90]}")
    assert not offenders, (
        "monkeypatch string targets on `services.*` resolve the package "
        "attribute, not sys.modules — use _services_module(<name>):\n  "
        + "\n  ".join(offenders)
    )


def test_the_two_resolvers_really_do_disagree(monkeypatch):
    """Executable proof of the claim the two guards above rest on.

    Guards built on a belief about an import mechanism are worth exactly as much
    as the belief. This stages the divergence — `sys.modules["services.x"]` and
    the `services` package attribute holding different module objects for the
    same file, which is the state conftest's #762 invariant-restore produces —
    and checks each resolver against what the product code actually does.

    If a future pytest changes `resolve()` to read sys.modules, this test fails
    and the guard above can be deleted rather than left as cargo cult.
    """
    import sys
    import types
    from unittest.mock import _get_target

    import _pytest.monkeypatch as mp

    pkg_name = "_ent356_probe_pkg"
    mod_name = f"{pkg_name}.leaf"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    live = types.ModuleType(mod_name)
    stale = types.ModuleType(mod_name)

    # `monkeypatch.setitem`, not a bare write + try/finally: it records the prior
    # value — including *absence*, which it undoes by deleting the key — so this
    # probe cannot leak two synthetic modules into a 9,000-test session if an
    # assertion above the restore ever raises. (Enforced by tests/lint_sys_modules.py;
    # writing sys.modules by hand inside a test is the pollution class this whole
    # test file is about, one level up.)
    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, mod_name, live)
    pkg.leaf = stale                      # the divergence

    assert mp.resolve(mod_name) is stale, (
        "pytest's resolver now agrees with sys.modules — the "
        "monkeypatch-string-target guard above is obsolete, delete it"
    )
    getter, _ = _get_target(f"{mod_name}.anything")
    assert getter() is live, "mock's resolver no longer reads sys.modules"
