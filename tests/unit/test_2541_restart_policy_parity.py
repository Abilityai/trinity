"""Static guard: every durable container Trinity creates is born restarting (#2541).

Docker's default restart policy is ``no``, so a container created without an
explicit ``restart_policy`` stays Exited after a host reboot or a daemon restart
until somebody starts it by hand. ``trinity-system`` was the only container
Trinity created with a policy; the 2026-09-04 power-off brought back 11 of 19
agents — exactly the cohort a manual ``docker update`` had patched after the
FIRST occurrence — and left 8 dead for ~42 hours.

This is the **second** container property to prove the ``docs/memory/learnings.md``
(2026-07-10) class *"the create path is never one call site"*, after #1871's
``log_config``. That is why the fix ships with a guard rather than three edits.

Rule — **both** directions, and both are load-bearing:

* a **durable** create must pass ``restart_policy=AGENT_RESTART_POLICY``;
* a **``remove=True``** create must pass **no** restart policy at all.

The reverse direction is protective, not decorative. It is tempting to read the
exemption as "Docker rejects the combination", and that is FALSE for the shape
Trinity actually writes. A *detached* ``remove=True`` create is rejected outright
(``400 "can't create 'AutoRemove' container with restart policy"``), but all five
of Trinity's transient helpers are **non-detached**, and docker-py only sets
``auto_remove`` when ``detach and remove``. So the daemon **accepts** the create,
the helper exits, the policy restarts it, and the client-side ``remove()`` then
409s — leaving a forever-restarting orphan that has to be force-removed.
Reproduced against a live daemon while planning this change.

Three deliberate departures from #1871's otherwise-identical scan, each with a
reason (they are why this is a standalone file and not a shared scanner — the
exemption rationales genuinely differ, and factoring them would mean editing a
currently-green guard plus adding an importable helper into the ``tests/unit/``
sys.path island):

1. **Value, not presence.** #1871 asks "was ``log_config`` passed?"; a wrong log
   value is a degradation. ``restart_policy={"Name": "always"}`` at a future site
   would pass a presence check while breaking RESTART-002 outright — Trinity
   stops agents through ``container.stop()``, which sets Docker's manual-stop
   flag, and ``unless-stopped`` honours it where ``always`` resurrects a
   deliberately quarantined agent. So the argument must be the shared constant
   BY NAME, and the constant's own value is pinned separately below.
2. **A third creation form.** #1871's ``_is_container_create`` matches
   ``containers_run(...)`` and ``<x>.containers.create(...)`` but not
   ``<x>.containers.run(...)`` — docker-py's raw high-level API, and the form
   ``services/docker_utils.py`` itself uses. That omission is the same class the
   guard exists to close, and it also makes #1871's ``_HELPER_LAYER`` exemption
   dead code (it exempts a file that produces no offenders anyway). Adding the
   form here makes ``_HELPER_LAYER`` load-bearing for the first time.
   Back-porting the form to #1871 is a separate one-line change, not this diff.
3. **Both directions**, per the ``remove=True`` reasoning above.

Lives in tests/unit/ so it runs as a pure static check with no backend
connection and no docker / fastapi / database imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# The shared constant every durable create site must name. Not a literal dict:
# a by-name pin is what lets the value live in exactly one place (see
# `test_the_constant_is_unless_stopped_and_not_always`).
_CONSTANT = "AGENT_RESTART_POLICY"

# ALL THREE docker-py container-creation entry points. #1871 watches two; the
# third (`<x>.containers.run`) is the one `services/docker_utils.py:536` uses.
_CALL_NAMES = ("containers_run", "containers.run", "containers.create")

# Cheap pre-filter token. Deliberately `containers` and NOT `run`/`create`: every
# accepted call form contains it (asserted below), while the bare words appear in
# dozens of unrelated files and would drag them all through the parser — which is
# how the pre-filter's whole purpose (never reporting an unrelated file that
# merely uses newer syntax than the running interpreter) gets defeated.
_PREFILTER_TOKEN = "containers"

# The util module that *defines* the wrapper and forwards **kwargs to docker-py.
# Load-bearing here, unlike in #1871: `docker_utils.py:536` is a real
# `docker_client.containers.run(...)`, which this scan (unlike #1871's) matches.
_HELPER_LAYER = {"services/docker_utils.py"}

# Transient containers created and then explicitly removed by the same function,
# so a restart policy would both be wrong and leak (see the module docstring).
# These do NOT use `remove=True` (which the scan detects automatically) — they
# call `.remove(force=True)` in a `finally`, which is not statically
# distinguishable from a durable create. Allowlisted BY FILE with the reason.
_TRANSIENT_CREATE_ALLOWLIST = {
    # deploy.py: an alpine:3.20 workspace pre-pop helper that runs one chown and
    # is torn down in `finally: transient.remove(force=True)`.
    "services/agent_service/deploy.py",
}

# The scheduler tree ships its own client and creates no containers.
_EXCLUDE_DIRS = ("scheduler/",)


def _is_container_create(node: ast.Call) -> bool:
    """True for the three docker-py creation forms.

    ``containers_run(...)`` / ``x.containers_run(...)`` (Trinity's async wrapper),
    ``x.containers.run(...)`` and ``x.containers.create(...)`` (docker-py's own).
    The bare names ``run`` / ``create`` are deliberately NOT matched — only a
    receiver called ``containers``, so unrelated ``.create()`` / ``.run()`` calls
    (volumes, images, networks, services) are not swept in.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "containers_run"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "containers_run":
        return True
    if func.attr not in ("run", "create"):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "containers"
    if isinstance(receiver, ast.Name):
        return receiver.id == "containers"
    return False


def _offending_calls(rel_path: str, text: str) -> list[str]:
    """Call sites in this file that get the restart policy wrong, either way.

    Uses the AST rather than a regex so a *mention* of a creation call in a
    comment or docstring can never be mistaken for a call site (lifecycle.py's
    own explanatory comments do exactly that).
    """
    if rel_path in _HELPER_LAYER or rel_path in _TRANSIENT_CREATE_ALLOWLIST:
        return []

    # Cheap pre-filter. A file that never mentions any creation call cannot
    # contain a call site, so it is skipped WITHOUT parsing. This matters
    # because the guard may run on an older interpreter than the backend
    # targets (a local 3.9 venv vs the 3.13 runtime), where an unrelated file
    # using newer syntax — e.g. `except*` — would otherwise be reported as an
    # offender. Fail-closed is preserved exactly where it earns its keep.
    if _PREFILTER_TOKEN not in text:
        return []

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Fail closed: a file that DOES mention a creation call but cannot be
        # parsed might be hiding a policy-less call site — report, never skip.
        return [f"{rel_path}: unparseable ({exc.__class__.__name__})"]

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_container_create(node):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        policy = kwargs.get("restart_policy")
        remove = kwargs.get("remove")
        is_transient = isinstance(remove, ast.Constant) and remove.value is True

        if is_transient:
            # Reverse direction: a policy here leaks a restarting orphan.
            if policy is not None:
                offenders.append(f"{rel_path}:{node.lineno} (transient WITH a policy)")
            continue

        if isinstance(policy, ast.Name) and policy.id == _CONSTANT:
            continue  # durable and correct
        if policy is None:
            offenders.append(f"{rel_path}:{node.lineno} (durable, no policy)")
        else:
            # Present but not the shared constant — a literal dict here is how
            # `{"Name": "always"}` would slip past a presence-only check.
            offenders.append(
                f"{rel_path}:{node.lineno} (durable, policy is not {_CONSTANT})"
            )
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


def test_every_durable_container_is_born_restarting():
    offenders = _scan_backend()
    assert not offenders, (
        "These call sites get the Docker restart policy wrong (#2541).\n"
        "  durable  -> pass restart_policy=AGENT_RESTART_POLICY (from "
        "services.agent_service.capabilities); Docker's default is `no`, so "
        "without it the container does not survive a host reboot.\n"
        "  remove=True -> pass NO restart policy: Trinity's transient helpers "
        "are non-detached, so the daemon accepts the combination and the helper "
        "leaks as a forever-restarting orphan.\n"
        "A create torn down explicitly (finally: .remove(force=True)) goes in "
        "_TRANSIENT_CREATE_ALLOWLIST with its reason:\n  " + "\n  ".join(offenders)
    )


def test_the_three_known_agent_create_sites_use_the_shared_constant():
    """Pin the sites #2541 fixed, so a refactor that drops one is caught by name
    and not only by the generic scan above."""
    expected = {
        "services/agent_service/crud.py",
        "services/agent_service/lifecycle.py",
        "services/system_agent_service.py",
    }
    for rel in expected:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        assert f"restart_policy={_CONSTANT}" in text, (
            f"{rel} no longer passes restart_policy={_CONSTANT} to its container "
            "create — agents born there do not survive a host reboot (#2541)."
        )


def test_the_constant_is_unless_stopped_and_not_always():
    """The by-name pin above is only as good as the value behind the name.

    `always` would resurrect an agent an operator deliberately stopped: Trinity
    stops through `container.stop()` (docker_utils.container_stop, and the
    Operating Room fast path in routers/ops), which sets Docker's manual-stop
    flag — the flag `unless-stopped` honours and `always` ignores. That makes the
    choice a safety property, not an availability preference (RESTART-002).

    Read out of the source rather than imported: this file is a pure static
    check and must not drag capabilities.py's import chain into tests/unit.
    """
    src = (_BACKEND / "services/agent_service/capabilities.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == _CONSTANT
    ]
    assert len(values) == 1, f"expected exactly one {_CONSTANT} definition"
    assert ast.literal_eval(values[0]) == {"Name": "unless-stopped"}


# --- meta-tests: the guard must actually catch things ------------------


def test_guard_detects_planted_violation():
    """A NEW durable create site with no restart policy must fail the guard."""
    bad = (
        "container = await containers_run(\n"
        '    image, detach=True, name=f"agent-{n}", log_config=AGENT_LOG_CONFIG,\n'
        ")\n"
    )
    assert _offending_calls("services/planted_bad.py", bad) != []


def test_guard_passes_a_correct_create():
    good = (
        "container = await containers_run(\n"
        '    image, detach=True, name=f"agent-{n}",\n'
        "    restart_policy=AGENT_RESTART_POLICY,\n"
        ")\n"
    )
    assert _offending_calls("services/planted_good.py", good) == []


def test_guard_rejects_a_literal_policy_even_when_it_is_correct_today():
    """Presence-only is not the rule. A literal is how the value drifts out of
    its single home — and how `{"Name": "always"}` would arrive."""
    literal = (
        "await containers_run(image, detach=True, "
        'restart_policy={"Name": "unless-stopped"})\n'
    )
    assert _offending_calls("services/planted_literal.py", literal) != []


def test_guard_rejects_always():
    """The value this guard exists to keep unreachable (RESTART-002)."""
    wrong = (
        'await containers_run(image, detach=True, restart_policy={"Name": "always"})\n'
    )
    assert _offending_calls("services/planted_always.py", wrong) != []


def test_guard_exempts_ephemeral_helper():
    """A `remove=True` helper needs no policy — Docker deletes it on exit."""
    helper = (
        "await containers_run(\n"
        "    'alpine', command='chown 1000:1000 /shared',\n"
        "    volumes={v: {'bind': '/shared', 'mode': 'rw'}}, remove=True\n"
        ")\n"
    )
    assert _offending_calls("services/planted_helper.py", helper) == []


def test_guard_flags_a_restart_policy_on_an_ephemeral_helper():
    """The reverse direction, and the reason it is not decorative: a non-detached
    `remove=True` create is ACCEPTED by the daemon with a policy attached, then
    leaks as a forever-restarting orphan the client's `remove()` cannot delete."""
    leaky = (
        "await containers_run(\n"
        "    'alpine', command='chown 1000:1000 /shared', remove=True,\n"
        "    restart_policy=AGENT_RESTART_POLICY,\n"
        ")\n"
    )
    assert _offending_calls("services/planted_leaky.py", leaky) != []


def test_guard_flags_file_where_only_one_of_two_calls_is_correct():
    """File-level compliance is not enough — each call site is checked."""
    mixed = (
        "await containers_run(image, detach=True, restart_policy=AGENT_RESTART_POLICY)\n"
        'await containers_run(image, detach=True, name="agent-x")\n'
    )
    assert len(_offending_calls("services/planted_mixed.py", mixed)) == 1


def test_guard_fails_closed_on_unparseable_file_that_mentions_the_call():
    """A file the scanner cannot parse but that DOES mention a creation call
    might be hiding a policy-less call site — report, never skip."""
    broken = "async def f(:\n    await containers_run(image, detach=True)\n"
    assert _offending_calls("services/planted_broken.py", broken) != []


def test_prefilter_token_covers_every_call_form():
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
    newer_syntax = "try:\n    pass\nexcept* ValueError:\n    pass\n"  # 3.11+ only
    assert _offending_calls("services/planted_newer.py", newer_syntax) == []


def test_guard_detects_the_third_creation_form():
    """`<x>.containers.run(...)` — docker-py's raw high-level API, which #1871's
    scan does not match. It is the form docker_utils.py itself uses, which is why
    that file has to be exempted BY NAME here rather than incidentally."""
    bad = 'c = client.containers.run(image, detach=True, name="agent-x")\n'
    assert _offending_calls("services/planted_raw_run.py", bad) != []


def test_guard_detects_policy_less_containers_create():
    bad = 'c = client.containers.create(image, name="agent-x")\n'
    assert _offending_calls("services/planted_create.py", bad) != []


def test_guard_ignores_unrelated_run_and_create_calls():
    """`.run()` / `.create()` on volumes, images, networks or services is not
    container creation — only a receiver named `containers` counts."""
    unrelated = (
        'v = client.volumes.create(name="x")\n'
        'n = client.networks.create("trinity-agent-network")\n'
        "s = client.services.create(image)\n"
        'r = subprocess.run(["ls"])\n'
    )
    assert _offending_calls("services/planted_unrelated.py", unrelated) == []


def test_helper_layer_entries_still_exist():
    """`docker_utils.py` is exempt because it DEFINES the wrapper and forwards
    **kwargs. A moved or renamed file would silently widen the exemption."""
    for rel in _HELPER_LAYER:
        assert (_BACKEND / rel).is_file(), (
            f"{rel} is in _HELPER_LAYER but no longer exists — drop the entry or "
            "point it at the new path."
        )


def test_helper_layer_is_load_bearing():
    """It is not enough that the exemption exists — it must actually be needed.

    In #1871 this same entry is dead code, because that scan never matches
    `docker_client.containers.run(...)`. Here it does, so the exemption carries
    weight; if this ever stops being true the entry should be deleted rather than
    left as reassurance.
    """
    rel = "services/docker_utils.py"
    text = (_BACKEND / rel).read_text(encoding="utf-8")
    # Scan it as if it were NOT exempt: it must produce an offender.
    tree = ast.parse(text)
    matched = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_container_create(node)
    ]
    assert matched, (
        f"{rel} no longer contains a matched creation call, so its _HELPER_LAYER "
        "exemption is now dead code (that is #1871's bug, not a feature) — "
        "delete the entry."
    )


def test_transient_create_allowlist_entries_still_exist():
    """An allowlisted file that has been moved or deleted would silently widen
    the exemption — pin that every entry still resolves."""
    for rel in _TRANSIENT_CREATE_ALLOWLIST:
        assert (_BACKEND / rel).is_file(), (
            f"{rel} is allowlisted in _TRANSIENT_CREATE_ALLOWLIST but no longer "
            "exists — drop the entry or point it at the new path."
        )


def test_guard_ignores_calls_named_in_comments_and_docstrings():
    """The regression that motivated #1871's AST rewrite: lifecycle.py explains
    the recreate flow with `containers_run(detach=True)` inside a comment, which
    a regex guard flagged as a call site."""
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
