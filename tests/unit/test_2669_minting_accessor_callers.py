"""Static guard: every use of an identity-MINTING accessor is a classified writer (#2669).

``get_or_create_installation_id`` is a write API wearing a read API's name
(learnings 2026-08-05). Calling it — or its siblings — from a read-shaped path
(a GET, a status readback, an alert label) mints durable identity as a side
effect of *looking*. That has happened three times, each closed by a point fix
at the call site: #1987 (the canary alert label), the ent#190 benchmark read,
and the ent#545 activation-funnel GET (#2663). The only static guard that
existed afterwards lives in the private repo and pins ONE module; the accessor
lives here. This guard pins the caller set itself, so the fourth occurrence is
a red build instead of a fourth point fix.

Rule: every USE of a guarded name — a bare call, a module-attribute call, a
reference passed by name, a ``getattr``/``import_string``-style string — must
sit at a ``(path, enclosing qualname, name)`` listed in ``_ALLOWED_USES`` with
the reason that site legitimately needs the identity to EXIST. A read-shaped
path uses the non-minting twin instead (``get_installation_id()``; the
sharing-id reads are inline ``_read(KEY_SHARING_ID)``). An ``import`` line is a
*binding*, not a use: it feeds the per-file alias map, so ``from … import x as
mint`` followed by ``mint()`` is a hit at the CALL, attributed to the caller —
the learnings 2026-09-09 point (a top-level import re-binds around any
attribute-level sentinel) is honoured by the detector, not by an entry.

Second rule: the identity KEYS themselves (``installation_id``,
``telemetry_sharing_id``) are written only by their home modules. The accessor's
own comment names the door this closes from the code side — the generic
``PUT /api/settings/{key}`` route can address any key (#2668 owns the sink).

Allowlist keyed by (path, qualname, NAME), never a deny-check (Invariant #13's
#848 precedent: a deny-check admits every shape it has not heard of). The site,
not the file, is the unit (learnings 2026-09-07, the #2434 review; the
``test_1832_duration_clamp`` shape): ``routers/product_events.py`` is a router,
and a per-file entry would let a new GET handler in the same file ride.

Scope: the OSS tree only. ``src/backend/enterprise/**`` is a private submodule,
unmounted on public CI and differing per clone — a guard whose verdict depends
on which clone runs it is the wrong shape (#2655, the #1920 guard). The private
repo carries its own twin: abilityai/trinity-enterprise#575.

Fail-closed where it matters (the #1677 / test_1871 shape): a file that cannot
contain a guarded name (no token) is skipped WITHOUT parsing, so unrelated files
using syntax newer than the running interpreter never trip it; a token-bearing
file that cannot be read or parsed is reported as a sentinel site and fails the
build naming the interpreter. CI runs the #1891-pinned 3.13, which parses every
file here; the repo venv does too.

What green does NOT prove: a name assembled at runtime (``importlib``, string
concatenation) is invisible to any static scan; a write reaching the sink over
HTTP is the route's business (#2668); the enterprise tree is ent#575's. The
per-file alias map over-approximates in the fail-closed direction — an alias
bound in one function is watched file-wide.

Every family of spelling is asserted by the self-tests below through the SAME
``_scan``/``_judge`` the live tree goes through (learnings 2026-08-10, lesson 3;
2026-08-11: "enumerate every syntax the thing under guard can be written in,
and assert the scan finds each family").
"""
from __future__ import annotations

import ast
import functools
import platform
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# The private enterprise submodule is out of scope (see module docstring).
_EXCLUDE_DIRS = ("enterprise/",)

# Cheap pre-filter on BYTES: every guarded name, twin and identity key contains
# one of these, so a file without any of them cannot hold a use-site or a key
# write and is skipped without parsing.
_PREFILTER_TOKENS = (b"installation_id", b"sharing_id", b"instance_label")

# The minting accessors → the module that defines each (relative to src/backend).
_GUARDED = {
    "get_or_create_installation_id": "services/operator_intake_service.py",
    "get_or_mint_sharing_id": "services/telemetry_sharing_service.py",
    # One hop above the tier-3 mint: its module docstring invites reuse "verbatim"
    # by other webhook sinks, and a Settings/About card calling it would mint on
    # first open of an un-configured install with the two-name guard green.
    "get_instance_label": "services/instance_identity.py",
}

# The non-minting read twin — pinned to EXIST (it has no OSS caller today; the
# enterprise funnel read in trinity-enterprise#570 is its first consumer), so a
# dead-code sweep cannot delete the only sanctioned read or "fix" the gap by
# re-adopting the mint.
_READ_TWINS = {
    "get_installation_id": "services/operator_intake_service.py",
}

# The identity keys, their module constants, the settings-facade writers, and
# the only two modules allowed to write them.
_IDENTITY_KEY_LITERALS = frozenset({"installation_id", "telemetry_sharing_id"})
_IDENTITY_KEY_CONSTANTS = frozenset({"_INSTALLATION_ID_KEY", "KEY_SHARING_ID"})
_SETTING_WRITERS = frozenset({"set_setting", "insert_setting_if_absent", "delete_setting"})
_IDENTITY_KEY_HOMES = frozenset({
    "services/operator_intake_service.py",
    "services/telemetry_sharing_service.py",
})

# ---------------------------------------------------------------------------
# The classified writer inventory. Adding a use of a guarded name REQUIRES an
# entry here — with the reason that site needs the identity to exist — OR
# switching the site to the non-minting read. This dict is also the living
# inventory the requirements (lifecycle-observability FR-3) point at.
# ---------------------------------------------------------------------------
_ALLOWED_USES = {
    ("services/operator_intake_service.py", "submit_operator_intake",
     "get_or_create_installation_id"):
        "the consent POST — the intake payload is keyed on the id, so the "
        "submit path is where it must come to exist (ent#38, ent#545 AC #4)",
    ("routers/product_events.py", "record_product_event",
     "get_or_create_installation_id"):
        "POST /api/product-events — a write path; every event row is keyed on "
        "the id (§45), so the emit is a writer by nature (learnings 2026-08-05)",
    ("services/instance_identity.py", "_label_from_installation_id",
     "get_or_create_installation_id"):
        "the canary alert label — a DOCUMENTED deliberate write (#1987): the "
        "docstring says 'This tier can WRITE' and why a read-only variant would "
        "return None on exactly the un-configured install the tier exists to label",
    ("services/telemetry_sharing_service.py", "set_consent",
     "get_or_mint_sharing_id"):
        "opting IN mints the egress id (opting out deletes it in the same "
        "function — a read-path mint would resurrect an id across an opt-out, "
        "which is the privacy property the delete exists for)",
    ("services/telemetry_sharing_service.py", "share_now",
     "get_or_mint_sharing_id"):
        "the send path — a share cannot leave without a sharing id, and the "
        "send is the write the id exists for (ent#437)",
    ("services/canary_alerts.py", "CanaryAlerts.emit_transition",
     "get_instance_label"):
        "the #1987 alert path — the one sink that needs the instance label, one "
        "hop above the tier-3 mint that _label_from_installation_id documents",
}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

@dataclass
class _Scan:
    rel: str
    hits: list = field(default_factory=list)       # (rel, qualname, name, lineno, shape)
    defs: list = field(default_factory=list)       # (name, rel, qualname, lineno)
    key_writes: list = field(default_factory=list)  # (rel, qualname, key, lineno)
    sentinel: tuple | None = None                  # (rel, "<unparseable>", 0, reason)


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Every identifier that stands for a guarded name in this file → that name.

    The guarded names map to themselves; ``from m import name as alias`` binds
    ``alias``; ``x = name`` / ``x = m.name`` / ``x: T = name`` bind ``x``.
    Iterated to a fixpoint so an alias of an alias is caught too. File-wide on
    purpose: a fail-closed over-approximation (a local alias in one function is
    tracked in every function of that file).
    """
    aliases = {name: name for name in _GUARDED}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name in _GUARDED and bound not in aliases:
                        aliases[bound] = alias.name
                        changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                real = None
                if isinstance(value, ast.Name):
                    real = aliases.get(value.id)
                elif isinstance(value, ast.Attribute) and value.attr in _GUARDED:
                    real = value.attr
                if real is None:
                    continue
                for t in targets:
                    if isinstance(t, ast.Name) and t.id not in aliases:
                        aliases[t.id] = real
                        changed = True
    return aliases


def _string_names(value: str) -> str | None:
    """A whitespace-free string equal to a guarded name, or dotted-ending in one
    (``"pkg.mod.get_or_create_installation_id"`` — import_string / patch targets)."""
    if not value or any(ch.isspace() for ch in value):
        return None
    last = value.rsplit(".", 1)[-1]
    return last if last in _GUARDED else None


def _first_key_arg(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "key":
            return kw.value
    return None


def _identity_key_of(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and node.value in _IDENTITY_KEY_LITERALS:
        return node.value
    if isinstance(node, ast.Name) and node.id in _IDENTITY_KEY_CONSTANTS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _IDENTITY_KEY_CONSTANTS:
        return node.attr
    return None


def _walk(node: ast.AST, stack: list[str], res: _Scan, aliases: dict[str, str]) -> None:
    """Scope-correct walk: decorators, bases, default values and annotations
    evaluate in the ENCLOSING scope; bodies under the def/class/lambda name."""
    qual = ".".join(stack) or "<module>"

    # -- examine this node -------------------------------------------------
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in _GUARDED or node.name in _READ_TWINS:
            res.defs.append((node.name, res.rel, qual, node.lineno))
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases:
        res.hits.append((res.rel, qual, aliases[node.id], node.lineno, "name"))
    elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in _GUARDED:
        res.hits.append((res.rel, qual, node.attr, node.lineno, "attr"))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = _string_names(node.value)
        if name:
            res.hits.append((res.rel, qual, name, node.lineno, "str"))
    elif isinstance(node, ast.Call):
        func = node.func
        writer = (
            (isinstance(func, ast.Name) and func.id in _SETTING_WRITERS)
            or (isinstance(func, ast.Attribute) and func.attr in _SETTING_WRITERS)
        )
        if writer:
            key = _identity_key_of(_first_key_arg(node))
            if key:
                res.key_writes.append((res.rel, qual, key, node.lineno))

    # -- descend -------------------------------------------------------------
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        outer: list[ast.AST] = list(node.decorator_list)
        a = node.args
        outer += [d for d in (*a.defaults, *a.kw_defaults) if d is not None]
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
            if arg is not None and arg.annotation is not None:
                outer.append(arg.annotation)
        if node.returns is not None:
            outer.append(node.returns)
        for n in outer:
            _walk(n, stack, res, aliases)
        for n in node.body:
            _walk(n, stack + [node.name], res, aliases)
        return
    if isinstance(node, ast.ClassDef):
        for n in (*node.decorator_list, *node.bases, *(k.value for k in node.keywords)):
            _walk(n, stack, res, aliases)
        for n in node.body:
            _walk(n, stack + [node.name], res, aliases)
        return
    if isinstance(node, ast.Lambda):
        a = node.args
        for d in (*a.defaults, *a.kw_defaults):
            if d is not None:
                _walk(d, stack, res, aliases)
        _walk(node.body, stack + ["<lambda>"], res, aliases)
        return
    for child in ast.iter_child_nodes(node):
        _walk(child, stack, res, aliases)


def _scan(rel: str, data: bytes) -> _Scan:
    """Scan one module's source BYTES (a PEP 263 coding cookie is honoured).

    Token-less files are skipped unparsed; a token-bearing file that does not
    parse becomes a sentinel site (fail closed where a use could hide).
    """
    res = _Scan(rel)
    # Case-folded: the key CONSTANTS are upper-case (`_INSTALLATION_ID_KEY`,
    # `KEY_SHARING_ID`), and a file that names only those must still be parsed.
    lowered = data.lower()
    if not any(tok in lowered for tok in _PREFILTER_TOKENS):
        return res
    try:
        tree = ast.parse(data)
    except Exception as exc:  # noqa: BLE001 — SyntaxError, ValueError (NUL bytes), decode errors
        res.sentinel = (rel, "<unparseable>", 0, type(exc).__name__)
        return res
    _walk(tree, [], res, _aliases(tree))
    return res


@functools.lru_cache(maxsize=1)
def _scan_backend() -> tuple[_Scan, ...]:
    out: list[_Scan] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:  # a broken symlink is a file we could not read
            out.append(_Scan(rel, sentinel=(rel, "<unparseable>", 0, type(exc).__name__)))
            continue
        out.append(_scan(rel, data))
    return tuple(out)


def _judge(hits, allowed) -> tuple[set, set]:
    """(unlisted use-sites, stale allowlist keys) — pure, so both directions can
    be proven on synthetic data."""
    found = {(rel, qual, name) for (rel, qual, name, _ln, _shape) in hits}
    return found - set(allowed), set(allowed) - found


def _hits(scans: list[_Scan]):
    return [h for s in scans for h in s.hits]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def test_every_minting_accessor_use_is_a_classified_writer():
    scans = _scan_backend()
    hits = _hits(scans)
    unlisted, _stale = _judge(hits, _ALLOWED_USES)
    detail = sorted(
        f"{rel} :: {qual} → {name}  (line {ln}, {shape})"
        for (rel, qual, name, ln, shape) in hits
        if (rel, qual, name) in unlisted
    )
    assert not unlisted, (
        "Unclassified use(s) of an identity-MINTING accessor (#2669). A "
        "get_or_create_* / get_or_mint_* is a write API wearing a read API's "
        "name: calling it from a read-shaped path mints durable identity as a "
        "side effect of looking (the #1987 / ent#190 / ent#545 class). EITHER "
        "use the non-minting read (get_installation_id(); the inline "
        "_read(KEY_SHARING_ID) for the sharing id), OR — if this site genuinely "
        "needs the identity to EXIST — add a (path, qualname, name) entry to "
        "_ALLOWED_USES with that reason:\n  " + "\n  ".join(detail)
    )


def test_no_stale_allowlist_entries():
    """An allowlisted site that no longer uses the accessor silently widens the
    exemption for whatever lands at that (path, qualname) later — every entry
    must still resolve to a live use."""
    _unlisted, stale = _judge(_hits(_scan_backend()), _ALLOWED_USES)
    assert not stale, (
        "Stale _ALLOWED_USES entrie(s) — the use moved, was renamed, or was "
        "removed; update or drop the entry:\n  "
        + "\n  ".join(f"{p} :: {q} → {n}" for p, q, n in sorted(stale))
    )


def test_no_token_bearing_file_was_skipped():
    """A file that mentions an identity token but could not be read or parsed
    might be hiding a use — that is a failure, not a skip."""
    sentinels = [s.sentinel for s in _scan_backend() if s.sentinel]
    assert not sentinels, (
        "Token-bearing file(s) could not be scanned under Python "
        f"{platform.python_version()} — the guard cannot vouch for them. CI runs "
        "the #1891-pinned 3.13, which parses every file here; run the unit suite "
        "under the repo venv (.venv), not the system interpreter:\n  "
        + "\n  ".join(f"{rel} ({reason})" for (rel, _q, _ln, reason) in sentinels)
    )


def test_the_private_submodule_is_never_walked():
    """The verdict must not depend on which clone runs it (#2655): the mounted
    enterprise tree is excluded, and its twin lives in trinity-enterprise#575."""
    scans = _scan_backend()
    assert scans and not any(s.rel.startswith("enterprise/") for s in scans)


def test_each_guarded_accessor_and_its_read_twin_is_defined_once_in_its_home():
    """A guard over a renamed or deleted name would otherwise pass vacuously;
    the read twin is pinned too — it has no OSS caller yet, and deleting it
    would leave the mint as the only accessor."""
    defs = [d for s in _scan_backend() for d in s.defs]
    for name, home in {**_GUARDED, **_READ_TWINS}.items():
        where = [(rel, qual, ln) for (n, rel, qual, ln) in defs if n == name]
        assert len(where) == 1 and where[0][:2] == (home, "<module>"), (
            f"{name} must be defined exactly once, at module level in {home} "
            f"(#2669) — found {where or 'nothing'}. Renaming or moving it is a "
            "deliberate edit of _GUARDED / _READ_TWINS, and (for the twin) of "
            "trinity-enterprise#570's consumer."
        )


def test_identity_keys_are_written_only_by_their_home_modules():
    """The name guard cannot see a read path writing the KEY directly
    (`db.insert_setting_if_absent("installation_id", …)`); this can."""
    writes = [w for s in _scan_backend() for w in s.key_writes]
    outside = sorted(w for w in writes if w[0] not in _IDENTITY_KEY_HOMES)
    assert not outside, (
        "Identity key written outside its home module (#2669) — route the write "
        "through the accessor in operator_intake_service / telemetry_sharing_service "
        "instead:\n  "
        + "\n  ".join(f"{rel} :: {qual} writes {key} (line {ln})" for rel, qual, key, ln in outside)
    )
    # The homes DO write them (otherwise the detector is watching nothing).
    assert {w[0] for w in writes} >= _IDENTITY_KEY_HOMES


# ---------------------------------------------------------------------------
# Meta-tests: the scanner must actually catch each family, and the verdict
# must go both ways. All through the same _scan / _judge the live tree uses.
# ---------------------------------------------------------------------------

_MINT = "get_or_create_installation_id"


def _sites(src: str, rel: str = "routers/planted.py"):
    res = _scan(rel, src.encode())
    assert res.sentinel is None, res.sentinel
    return [(qual, name, shape) for (_r, qual, name, _ln, shape) in res.hits]


@pytest.mark.parametrize("label,src,expected", [
    ("top-level import + bare call: the import is a binding, the call is the hit",
     f"from services.operator_intake_service import {_MINT}\n"
     f"def get_status():\n    return {_MINT}()\n",
     [("get_status", _MINT, "name")]),
    ("lazy in-function import (the instance_identity shape)",
     f"def f():\n    from services.operator_intake_service import {_MINT}\n    return {_MINT}()\n",
     [("f", _MINT, "name")]),
    ("module-attribute call",
     f"import services.operator_intake_service as ois\ndef g():\n    return ois.{_MINT}()\n",
     [("g", _MINT, "attr")]),
    ("aliased import: the CALL is attributed to the caller, under the real name",
     f"from services.operator_intake_service import {_MINT} as mint\n"
     f"def get_status():\n    return mint()\n",
     [("get_status", _MINT, "name")]),
    ("rebinding via assignment: the binding is a module-level hit, the call a second",
     f"from services.operator_intake_service import {_MINT}\n"
     f"x = {_MINT}\ndef h():\n    return x()\n",
     [("<module>", _MINT, "name"), ("h", _MINT, "name")]),
    ("rebinding via attribute assignment",
     f"import services.operator_intake_service as ois\nmint = ois.{_MINT}\ndef h():\n    return mint()\n",
     [("<module>", _MINT, "attr"), ("h", _MINT, "name")]),
    ("getattr by string",
     f"import services.operator_intake_service as ois\ndef k():\n    return getattr(ois, '{_MINT}')()\n",
     [("k", _MINT, "str")]),
    ("dotted string (import_string / patch target)",
     f"TARGET = 'services.operator_intake_service.{_MINT}'\n",
     [("<module>", _MINT, "str")]),
    ("__all__ re-export is a true positive at module level",
     f"from services.operator_intake_service import {_MINT}\n__all__ = ['{_MINT}']\n",
     [("<module>", _MINT, "str")]),
    ("nested def gets a dotted qualname",
     f"from services.operator_intake_service import {_MINT}\n"
     f"def outer():\n    def inner():\n        return {_MINT}()\n    return inner\n",
     [("outer.inner", _MINT, "name")]),
    ("async def",
     f"from services.operator_intake_service import {_MINT}\nasync def a():\n    return {_MINT}()\n",
     [("a", _MINT, "name")]),
    ("same method name in two classes does not collide",
     f"from services.operator_intake_service import {_MINT}\n"
     f"class A:\n    def submit(self):\n        return {_MINT}()\n"
     f"class B:\n    def submit(self):\n        return {_MINT}()\n",
     [("A.submit", _MINT, "name"), ("B.submit", _MINT, "name")]),
    ("class-body attribute is attributed to the class",
     f"from services.operator_intake_service import {_MINT}\nclass Svc:\n    resolver = {_MINT}\n",
     [("Svc", _MINT, "name")]),
    ("decorator and default arg evaluate in the ENCLOSING scope, not the function",
     f"from services.operator_intake_service import {_MINT}\n"
     f"@needs({_MINT})\ndef f(x={_MINT}):\n    return x\n",
     [("<module>", _MINT, "name"), ("<module>", _MINT, "name")]),
    ("module-level lambda body",
     f"from services.operator_intake_service import {_MINT}\ncb = lambda: {_MINT}()\n",
     [("<lambda>", _MINT, "name")]),
    ("the sibling accessors are guarded by the same scan",
     "from services.telemetry_sharing_service import get_or_mint_sharing_id\n"
     "from services.instance_identity import get_instance_label\n"
     "def status():\n    return get_or_mint_sharing_id(), get_instance_label()\n",
     [("status", "get_or_mint_sharing_id", "name"), ("status", "get_instance_label", "name")]),
])
def test_detector_finds_each_family(label, src, expected):
    assert _sites(src) == expected, label


@pytest.mark.parametrize("label,src", [
    ("the read twin only",
     "from services.operator_intake_service import get_installation_id\n"
     "def get_status():\n    return get_installation_id()\n"),
    ("a docstring and a comment that mention the name",
     f'"""Never call {_MINT} from a GET; use get_installation_id."""\n'
     f"# {_MINT}() would mint here\nx = 1\n"),
    ("an unrelated local variable is not an alias",
     "from services.operator_intake_service import get_installation_id\n"
     "def f():\n    mint = 1\n    return mint + len(get_installation_id() or '')\n"),
])
def test_detector_stays_clean_on_innocents(label, src):
    assert _sites(src) == [], label


def test_a_file_naming_only_the_upper_case_key_constant_is_still_scanned():
    # Caught by the first run of this suite: the lower-case token filter skipped
    # a file whose only identity reference was `_INSTALLATION_ID_KEY`.
    res = _scan("routers/foo.py", b"def h():\n    db.set_setting(_INSTALLATION_ID_KEY, 'x')\n")
    assert [(q, k) for (_r, q, k, _l) in res.key_writes] == [("h", "_INSTALLATION_ID_KEY")]


def test_tokenless_file_is_skipped_without_parsing():
    # Genuinely unparseable on EVERY interpreter, and token-less: no sentinel, no hits.
    res = _scan("services/unrelated.py", b"def f(:\n    pass\n")
    assert res.sentinel is None and res.hits == [] and res.defs == []


def test_token_bearing_unparseable_file_is_a_sentinel():
    res = _scan("routers/broken.py", f"def f(:\n    {_MINT}()\n".encode())
    assert res.sentinel == ("routers/broken.py", "<unparseable>", 0, "SyntaxError")


def test_latin1_source_with_coding_cookie_parses_from_bytes():
    src = ("# -*- coding: latin-1 -*-\n"
           f"from services.operator_intake_service import {_MINT}\n"
           "LABEL = 'caf\xe9'\n"
           f"def f():\n    return {_MINT}()\n").encode("latin-1")
    res = _scan("routers/legacy.py", src)
    assert res.sentinel is None
    assert [(q, n) for (_r, q, n, _l, _s) in res.hits] == [("f", _MINT)]


def test_definitions_are_recorded_not_reported_and_their_bodies_are_scanned():
    src = (
        "def get_installation_id():\n    return None\n"
        f"async def {_MINT}():\n    return get_installation_id() or get_or_mint_sharing_id()\n"
    )
    res = _scan("services/operator_intake_service.py", src.encode())
    assert [(n, q) for (n, _r, q, _l) in res.defs] == [
        ("get_installation_id", "<module>"), (_MINT, "<module>"),
    ]
    # the def's own name is not a use; a cross-accessor call inside its body is
    assert [(q, n) for (_r, q, n, _l, _s) in res.hits] == [(_MINT, "get_or_mint_sharing_id")]


@pytest.mark.parametrize("label,src,rel,expected", [
    ("literal key through the facade, outside a home module — a violation",
     "def h():\n    db.set_setting('installation_id', 'x')\n", "routers/foo.py",
     [("h", "installation_id")]),
    ("module constant, insert-if-absent, keyword form",
     "def h():\n    db.insert_setting_if_absent(key=_INSTALLATION_ID_KEY, value=v)\n"
     "def d():\n    core_db.delete_setting(tss.KEY_SHARING_ID)\n", "routers/foo.py",
     [("h", "_INSTALLATION_ID_KEY"), ("d", "KEY_SHARING_ID")]),
    ("a variable key or another key is not matched",
     "def h(key):\n    db.set_setting(key, 'x')\n    db.set_setting('telemetry_sharing_enabled', '1')\n",
     "routers/foo.py", []),
])
def test_key_write_detector(label, src, rel, expected):
    res = _scan(rel, src.encode())
    assert [(q, k) for (_r, q, k, _l) in res.key_writes] == expected, label


def test_judge_reports_both_directions():
    hits = [("routers/planted.py", "get_status", _MINT, 3, "name"),
            ("routers/product_events.py", "record_product_event", _MINT, 76, "name")]
    allowed = {("routers/product_events.py", "record_product_event", _MINT): "ok",
               ("services/gone.py", "removed_writer", _MINT): "stale"}
    unlisted, stale = _judge(hits, allowed)
    assert unlisted == {("routers/planted.py", "get_status", _MINT)}
    assert stale == {("services/gone.py", "removed_writer", _MINT)}
