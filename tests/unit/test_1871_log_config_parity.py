"""Static guard: every durable container Trinity creates has a bounded log (#1871).

Docker's json-file driver has no default cap, so a container created without an
explicit ``log_config`` grows its log under /var/lib/docker/containers/ forever —
silently, until the Docker data root fills and dockerd wedges (2026-07-27).
Compose's ``logging:`` anchor bounds the platform services; agent containers are
created through the Docker SDK and are bounded by ``AGENT_LOG_CONFIG`` instead.

This guard exists because of a named bug class in ``docs/memory/learnings.md``
(2026-07-10): *"the create path is never one call site."* #1871's fix touches the
three agent-container create sites that exist today; this test is what makes a
FOURTH one fail CI instead of silently shipping unbounded.

Rule: every container-creating call — ``containers_run(...)`` **and**
``containers.create(...)``, docker-py's two creation entry points — must either
  * pass ``log_config``  (a durable container — must be capped), or
  * pass ``remove=True`` (an ephemeral helper — Docker deletes the log with the
    container the moment it exits, so it cannot leak),
  * or be allowlisted in ``_TRANSIENT_CREATE_ALLOWLIST`` with a stated reason
    (a create/teardown pair that a static scan cannot recognise).

Covering only ``containers_run`` was the guard's own first blind spot: it would
have watched one of two creation APIs while claiming to close the class.

Lives in tests/unit/ so it runs as a pure static check with no backend
connection and no docker / fastapi / database imports.
"""
from __future__ import annotations

import ast
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# BOTH docker-py container-creation entry points. Scanning only `containers_run`
# would leave `containers.create()` — a second, real creation API already used in
# `deploy.py` — completely unwatched, i.e. the guard would cover one of two paths
# while claiming to close the "create path is never one call site" class.
_CALL_NAMES = ("containers_run", "containers.create")

# Cheap pre-filter token. Deliberately `containers` and NOT `create`: both real
# call forms (`containers_run(` and `<client>.containers.create(`) contain it,
# while the bare word "create" appears in dozens of unrelated files and would
# drag them all through the parser — which is how the pre-filter's whole purpose
# (never reporting an unrelated file that merely uses newer syntax than the
# running interpreter) got defeated the first time this scan was widened.
_PREFILTER_TOKEN = "containers"

# The util module that *defines* the wrapper (and forwards **kwargs to docker-py).
_HELPER_LAYER = {"services/docker_utils.py"}

# Transient containers that are created and then explicitly removed by the same
# function, so their log file is deleted with them and cannot leak. These do NOT
# use `remove=True` (which the scan detects automatically) — they call
# `.remove(force=True)` in a `finally`, which is not statically distinguishable
# from a durable create. Allowlisted BY FILE with the reason, so a durable
# `containers.create()` anywhere else still fails CI.
_TRANSIENT_CREATE_ALLOWLIST = {
    # deploy.py: an alpine:3.20 workspace pre-pop helper that runs one chown and
    # is torn down in `finally: transient.remove(force=True)`.
    "services/agent_service/deploy.py",
}

# The scheduler tree ships its own client and creates no containers.
_EXCLUDE_DIRS = ("scheduler/",)


def _is_container_create(node: ast.Call) -> bool:
    """True for `containers_run(...)` / `x.containers_run(...)` / `x.containers.create(...)`.

    The bare name `create` is deliberately NOT matched — only `<something>.create`
    whose receiver is an attribute/name called `containers`, so unrelated
    `.create()` calls (volumes, images, networks) are not swept in.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "containers_run"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "containers_run":
        return True
    if func.attr != "create":
        return False
    receiver = func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "containers"
    if isinstance(receiver, ast.Name):
        return receiver.id == "containers"
    return False


def _offending_calls(rel_path: str, text: str) -> list[str]:
    """Call sites in this file that create a durable container with no log cap.

    Uses the AST rather than a regex so a *mention* of ``containers_run(`` in a
    comment or docstring can never be mistaken for a call site (lifecycle.py's
    own explanatory comments do exactly that).
    """
    if rel_path in _HELPER_LAYER or rel_path in _TRANSIENT_CREATE_ALLOWLIST:
        return []

    # Cheap pre-filter. A file that never mentions any creation call cannot
    # contain a call site, so it is skipped WITHOUT parsing. This matters
    # because the guard may run on an older interpreter than the backend
    # targets (a local 3.9 venv vs the 3.13 runtime), where an unrelated file
    # using newer syntax — e.g. `except*` in services/gemini_voice.py — would
    # otherwise be reported as an offender. Fail-closed is preserved exactly
    # where it earns its keep.
    if _PREFILTER_TOKEN not in text:
        return []

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Fail closed: a file that DOES mention containers_run but cannot be
        # parsed might be hiding an uncapped call site — report, never skip.
        return [f"{rel_path}: unparseable ({exc.__class__.__name__})"]

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_container_create(node):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        remove = kwargs.get("remove")
        if isinstance(remove, ast.Constant) and remove.value is True:
            continue  # ephemeral helper — log dies with the container
        if "log_config" in kwargs:
            continue  # durable and capped
        offenders.append(f"{rel_path}:{node.lineno}")
    return offenders


def _scan_backend() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        offenders.extend(_offending_calls(rel, path.read_text(encoding="utf-8")))
    return offenders


# --- the guard ---------------------------------------------------------

def test_every_durable_container_is_log_capped():
    offenders = _scan_backend()
    assert not offenders, (
        "These call sites create a DURABLE container without log_config, so its "
        "Docker json-file log is unbounded (#1871). Pass "
        "log_config=AGENT_LOG_CONFIG (from services.agent_service.capabilities), "
        "or remove=True if the container is genuinely ephemeral. A create that is "
        "torn down explicitly (finally: .remove(force=True)) goes in "
        "_TRANSIENT_CREATE_ALLOWLIST with its reason:\n  "
        + "\n  ".join(offenders)
    )


def test_the_three_known_agent_create_sites_are_capped():
    """Pin the sites #1871 fixed, so a refactor that drops one is caught by name
    and not only by the generic scan above."""
    expected = {
        "services/agent_service/crud.py",
        "services/agent_service/lifecycle.py",
        "services/system_agent_service.py",
    }
    for rel in expected:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "log_config=AGENT_LOG_CONFIG" in text, (
            f"{rel} no longer passes log_config=AGENT_LOG_CONFIG to its "
            "container create — agent logs there are unbounded again (#1871)."
        )
        assert "AGENT_LOG_CONFIG" in text.split("\n\n")[0] or "AGENT_LOG_CONFIG" in text, rel


# --- meta-tests: the guard must actually catch things ------------------

def test_guard_detects_planted_violation():
    """A NEW durable create site with no log cap must fail the guard."""
    bad = (
        'container = await containers_run(\n'
        '    image, detach=True, name=f"agent-{n}", tmpfs=AGENT_TMPFS_MOUNT,\n'
        ')\n'
    )
    assert _offending_calls("services/planted_bad.py", bad) != []


def test_guard_passes_capped_create():
    good = (
        'container = await containers_run(\n'
        '    image, detach=True, name=f"agent-{n}", log_config=AGENT_LOG_CONFIG,\n'
        ')\n'
    )
    assert _offending_calls("services/planted_good.py", good) == []


def test_guard_exempts_ephemeral_helper():
    """The alpine chown / template-read helpers are auto-removed, so their log
    file is deleted with the container and cannot leak."""
    helper = (
        "await containers_run(\n"
        "    'alpine', command='chown 1000:1000 /shared',\n"
        "    volumes={v: {'bind': '/shared', 'mode': 'rw'}}, remove=True\n"
        ")\n"
    )
    assert _offending_calls("services/planted_helper.py", helper) == []


def test_guard_flags_file_where_only_one_of_two_calls_is_capped():
    """File-level compliance is not enough — each call site is checked."""
    mixed = (
        'await containers_run(image, detach=True, log_config=AGENT_LOG_CONFIG)\n'
        'await containers_run(image, detach=True, name="agent-x")\n'
    )
    assert len(_offending_calls("services/planted_mixed.py", mixed)) == 1


def test_guard_fails_closed_on_unparseable_file_that_mentions_the_call():
    """A file the scanner cannot parse but that DOES mention containers_run
    might be hiding an uncapped call site — it must be reported, never skipped."""
    broken = "async def f(:\n    await containers_run(image, detach=True)\n"
    assert _offending_calls("services/planted_broken.py", broken) != []


def test_prefilter_token_covers_both_call_forms():
    """The pre-filter must not be able to hide a real call site: every accepted
    call form has to contain the token, or the AST check never runs on it."""
    for form in _CALL_NAMES:
        assert _PREFILTER_TOKEN in form, (
            f"call form {form!r} does not contain the pre-filter token "
            f"{_PREFILTER_TOKEN!r} — files using it would be skipped unparsed."
        )


def test_guard_skips_unparseable_file_that_cannot_contain_a_call():
    """...but an unrelated file using syntax newer than the running interpreter
    (e.g. `except*` under a 3.9 venv) is NOT an offender. Without this the guard
    reports false positives that depend on which Python runs the test."""
    newer_syntax = (
        "try:\n    pass\nexcept* ValueError:\n    pass\n"  # 3.11+ only
    )
    assert _offending_calls("services/planted_newer.py", newer_syntax) == []


def test_guard_detects_uncapped_containers_create():
    """The second creation API — the guard's own first blind spot. A durable
    `containers.create()` must be caught exactly like `containers_run()`."""
    bad = 'c = client.containers.create(image, name="agent-x")\n'
    assert _offending_calls("services/planted_create.py", bad) != []


def test_guard_passes_capped_containers_create():
    good = 'c = client.containers.create(image, log_config=AGENT_LOG_CONFIG)\n'
    assert _offending_calls("services/planted_create_ok.py", good) == []


def test_guard_ignores_unrelated_create_calls():
    """`.create()` on volumes / images / networks is not container creation and
    must not be swept in — only a receiver named `containers` counts."""
    unrelated = (
        'v = client.volumes.create(name="x")\n'
        'n = client.networks.create("trinity-agent-network")\n'
        's = client.services.create(image)\n'
    )
    assert _offending_calls("services/planted_unrelated.py", unrelated) == []


def test_transient_create_allowlist_entries_still_exist():
    """An allowlisted file that has been moved or deleted would silently widen
    the exemption — pin that every entry still resolves."""
    for rel in _TRANSIENT_CREATE_ALLOWLIST:
        assert (_BACKEND / rel).is_file(), (
            f"{rel} is allowlisted in _TRANSIENT_CREATE_ALLOWLIST but no longer "
            "exists — drop the entry or point it at the new path."
        )


def test_guard_ignores_calls_named_in_comments_and_docstrings():
    """The regression that motivated the AST rewrite: lifecycle.py explains the
    recreate flow with `containers_run(detach=True)` inside a comment, which a
    regex guard flagged as an uncapped call site."""
    prose = (
        '"""Docstring mentioning containers_run(detach=True) in prose."""\n'
        "# updated_config` starts the replacement via `containers_run(detach=True)`\n"
        "x = 1\n"
    )
    assert _offending_calls("services/planted_prose.py", prose) == []


if __name__ == "__main__":  # pragma: no cover - manual scan helper
    found = _scan_backend()
    print("OFFENDERS:" if found else "CLEAN", found)
    raise SystemExit(1 if found else 0)
