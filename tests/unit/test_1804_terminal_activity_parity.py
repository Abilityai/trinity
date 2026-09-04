"""#1804 static parity guard — every terminal writer closes its paired activity.

**Anchored on terminal WRITES, not on completion-event emission.** The obvious
guard to write here is "scan for ``spawn_task_terminal_event``" — the #1578 emit
set is *approximately* the #1804 close set. It is not the same set, and the
difference is the bug: ``task_execution_service``'s and ``routers/internal``'s
backend-shutdown ``CancelledError`` handlers both write an execution terminal and
emit nothing, which is exactly how they hid from review while orphaning their
activity permanently (the row goes ``failed``, so startup recovery — which scans
``running`` — never revisits it).

So the anchor is the terminal write itself:

    db.update_execution_status(...) | db.mark_execution_failed_by_watchdog(...)
    | db.fail_stale_slot_execution(...)

If a function performs one, that function must also close an activity. Function
granularity (not statement granularity) is deliberate: several writers share one
enclosing function across branches, and the close often sits in a sibling branch.

The allowlist is explicit and every entry carries a justification. The bar for
adding one is high: "there is no dispatch activity to close at this point in the
lifecycle", proven by where the write sits relative to ``track_activity``.

Companion to ``#429``: that issue deletes the 120-minute
``mark_stale_activities_failed`` backstop AND the functions holding three of the
original gap sites. A per-site patch would be deleted with its host, leaving no
owner. This guard survives, because it is anchored on terminal writes — which
#429 preserves.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# A call to any of these on the db facade writes a terminal to
# `schedule_executions` — the moment the #1804 contract attaches.
TERMINAL_WRITERS = {
    "update_execution_status",
    "mark_execution_failed_by_watchdog",
    "fail_stale_slot_execution",
}

# Any of these discharges the obligation.
ACTIVITY_CLOSERS = {
    "close_execution_activity",              # the shared owner (#1804)
    "spawn_close_execution_activity",        # its sync fire-and-forget wrapper
    "close_open_activities_for_executions",  # the batched bulk-sweep close
    "complete_activity",                     # the underlying primitive
    # Named per-path close helpers (each delegates to the owner above).
    "_close_stale_slot_activity",
    "_close_reaped_activity",
    "_close_requeued_activity",
    "_close_bulk_swept_activities",
    "_close_dispatch_activity_cancelled",
}

# (module path relative to src/backend, enclosing function) -> why it is exempt.
ALLOWLIST = {
    (
        "services/backlog_service.py",
        "drain_next",
    ): (
        "BACKLOG-001 drain of a `queued` row. The dispatch activity is opened by "
        "execute_task step 3, which this row never reached — a corrupt-metadata "
        "or spawn failure here has no activity to close."
    ),
    (
        "routers/chat.py",
        "_raise_ephemeral_exhausted_410",
    ): (
        "Admission-path terminal: the row is pre-created but capacity.acquire "
        "raised before execute_task step 3, so no dispatch activity exists."
    ),
    (
        "routers/chat.py",
        "_raise_circuit_open_503",
    ): (
        "Admission-path terminal (#526 fast-fail), same lifecycle position as "
        "_raise_ephemeral_exhausted_410 — nothing was admitted or tracked."
    ),
    (
        "services/chat_execution_service.py",
        "_circuit_open_dispatch_error",
    ): (
        "Byte-identical mirror of routers.chat._raise_circuit_open_503 for the "
        "/task path — admission-path terminal, no activity yet."
    ),
    (
        "services/chat_execution_service.py",
        "_ephemeral_dispatch_error",
    ): (
        "Byte-identical mirror of routers.chat._raise_ephemeral_exhausted_410 — "
        "admission-path terminal, no activity yet."
    ),
    (
        "services/chat_execution_service.py",
        "_acquire_task_capacity",
    ): (
        "The CapacityFull branch fails the pre-created row when BOTH capacity "
        "and the backlog are full. Admission-path: nothing was admitted, so "
        "execute_task step 3 never opened a dispatch activity."
    ),
    (
        "services/task_execution_service.py",
        "_admission_gate",
    ): (
        "Step 2 of execute_task, extracted in-place by #2314. Its three "
        "fast-fail terminals (CapacityFull / CircuitOpen / "
        "EphemeralBudgetExhausted) fire when capacity.acquire refused the "
        "turn — before execute_task step 3 opens the dispatch activity, so "
        "there is no activity to close. Identical lifecycle position to the "
        "chat_execution_service admission-path entries above; before #2314 "
        "these writes sat inline in execute_task, whose own close calls "
        "satisfied the function-level scan."
    ),
    (
        "client_portal/service.py",
        "_fail_unstarted_execution",
    ): (
        "ent#286 pre-created streaming row. It exists so the client has an id "
        "to subscribe to BEFORE the turn is dispatched, and this helper only "
        "runs when the background turn raised before execute_task saw that id "
        "(a resume lock held by another tab, a thread-resolution failure) — so "
        "step 3 never opened a dispatch activity. Same lifecycle position as "
        "the admission-path entries above. If execute_task DID run, its own "
        "terminal wins the CAS and this write is a no-op."
    ),
}

# Directories walked by the guard. NOT free-form: `test_scan_covers_every_
# package_that_writes_a_terminal` below proves this tuple still covers every
# backend package that actually calls a terminal writer, so adopting a module
# into OSS core cannot silently leave it unguarded (which is exactly what
# happened to `client_portal/` between ent#356 and ent#286 — see #2131).
_SCAN_DIRS = ("services", "routers", "client_portal")

# Top-level backend locations that contain a terminal-writer call but are
# deliberately NOT scanned, each with the reason. The coverage test consults
# this, so an unlisted newcomer fails rather than being skipped.
_UNSCANNED_JUSTIFIED = {
    "database.py": (
        "The db facade DELEGATES update_execution_status to ScheduleOperations "
        "(Invariant #2). It is the persistence layer the contract is defined "
        "against, not a caller with an activity obligation — scanning it would "
        "flag the plumbing that every legitimate closer runs through."
    ),
}

# The ONLY exclusion, and it needs a reason: the submodule is optional
# (`update = none`, #1443) and owns its own migration/test tracks, so a guard
# that reached into it would be stronger on core-team clones than on OSS ones
# — i.e. not a guard. Nothing else is excluded on purpose: `migrations/` was
# briefly in this tuple and taken back out, because Alembic revisions write
# raw `op.execute("UPDATE ...")` rather than calling the db facade, so they
# cannot match TERMINAL_WRITERS anyway — the exclusion bought nothing and
# would have silently swallowed the surprising case if one ever appeared.
_COVERAGE_EXCLUDED_PREFIXES = ("enterprise/",)


def _call_attrs(node: ast.AST) -> set:
    """Names of every call in this subtree — both ``obj.method()`` (Attribute)
    and bare ``helper()`` (Name), since the per-path close helpers are called
    bare. EXCLUDES nested function bodies (checked as their own units)."""
    found = set()

    def walk(n: ast.AST, is_root: bool) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not is_root:
                continue
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Attribute):
                    found.add(child.func.attr)
                elif isinstance(child.func, ast.Name):
                    found.add(child.func.id)
            walk(child, False)

    walk(node, True)
    return found


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _violations():
    out = []
    for directory in _SCAN_DIRS:
        for path in sorted((_BACKEND / directory).rglob("*.py")):
            rel = path.relative_to(_BACKEND).as_posix()
            tree = ast.parse(path.read_text())
            for fn in _iter_functions(tree):
                attrs = _call_attrs(fn)
                if not (attrs & TERMINAL_WRITERS):
                    continue
                if attrs & ACTIVITY_CLOSERS:
                    continue
                if (rel, fn.name) in ALLOWLIST:
                    continue
                out.append((rel, fn.name, fn.lineno))
    return out


@pytest.mark.unit
class TestTerminalWriteActivityParity:
    def test_every_terminal_writer_closes_its_activity(self):
        found = _violations()
        assert not found, (
            "#1804: these functions write an execution terminal but never close "
            "the paired dispatch activity. Call "
            "activity_service.close_execution_activity(...) (or the sync spawn "
            "wrapper) on the CAS-won branch, or add an ALLOWLIST entry in this "
            "file WITH a justification proving no dispatch activity exists at "
            "that point in the lifecycle:\n"
            + "\n".join(f"  {f}::{fn} (line {ln})" for f, fn, ln in found)
        )

    def test_allowlist_entries_all_still_exist(self):
        """A stale allowlist entry exempts nothing — but it does hide that the
        guard's coverage assumption has drifted."""
        stale = []
        for rel, fn_name in ALLOWLIST:
            path = _BACKEND / rel
            if not path.exists():
                stale.append(f"{rel}::{fn_name} (file missing)")
                continue
            names = {fn.name for fn in _iter_functions(ast.parse(path.read_text()))}
            if fn_name not in names:
                stale.append(f"{rel}::{fn_name} (function missing)")
        assert not stale, "Stale #1804 parity allowlist entries: " + ", ".join(stale)

    def test_allowlist_entries_carry_a_justification(self):
        thin = [k for k, v in ALLOWLIST.items() if len(v.strip()) < 60]
        assert not thin, f"#1804 allowlist entries need a real justification: {thin}"

    def test_scan_covers_every_package_that_writes_a_terminal(self):
        """The guard's blind spot is its own directory list.

        #1804 anchored the contract on terminal WRITES rather than on emission,
        which survives a writer moving between functions or files. It does not
        survive a writer appearing in a package nobody thought to walk — and
        that is not hypothetical: ent#356 moved `client_portal/` into OSS core,
        ent#286 added `_fail_unstarted_execution` to it, and the guard reported
        green throughout because `_SCAN_DIRS` was written when that code lived
        in the submodule (#2131).

        So this asserts the coverage assumption directly: every backend
        location holding a terminal-writer call is either scanned or carries a
        written justification for why it is not.
        """
        offenders = {}
        for path in sorted(_BACKEND.rglob("*.py")):
            rel = path.relative_to(_BACKEND).as_posix()
            if rel.startswith(_COVERAGE_EXCLUDED_PREFIXES):
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover - a parse failure is its own bug
                continue
            if not any(
                (c.func.attr if isinstance(c.func, ast.Attribute) else
                 c.func.id if isinstance(c.func, ast.Name) else None) in TERMINAL_WRITERS
                for c in ast.walk(tree) if isinstance(c, ast.Call)
            ):
                continue
            top = rel.split("/")[0]
            if top in _SCAN_DIRS or top in _UNSCANNED_JUSTIFIED:
                continue
            offenders.setdefault(top, []).append(rel)

        assert not offenders, (
            "#1804/#2131: these backend locations call a terminal writer but the "
            "parity guard never walks them, so the close contract is unenforced "
            "there. Add the package to _SCAN_DIRS (preferred), or add it to "
            "_UNSCANNED_JUSTIFIED with a reason:\n"
            + "\n".join(f"  {k}: {sorted(v)}" for k, v in sorted(offenders.items()))
        )

    def test_unscanned_justifications_are_real_and_still_apply(self):
        """A justification for a file that moved is not a justification."""
        problems = []
        for rel, why in _UNSCANNED_JUSTIFIED.items():
            if not (_BACKEND / rel).exists():
                problems.append(f"{rel} (missing)")
            elif len(why.strip()) < 60:
                problems.append(f"{rel} (justification too thin)")
        assert not problems, (
            "#2131 _UNSCANNED_JUSTIFIED entries need to exist and explain "
            f"themselves: {problems}"
        )

    def test_the_guard_actually_fires(self):
        """A guard nobody has seen fail is a guard nobody knows works."""
        src = (
            "class X:\n"
            "    async def write_terminal(self):\n"
            "        db.update_execution_status(execution_id='e', status='failed')\n"
        )
        fn = next(_iter_functions(ast.parse(src)))
        attrs = _call_attrs(fn)
        assert attrs & TERMINAL_WRITERS
        assert not (attrs & ACTIVITY_CLOSERS)
