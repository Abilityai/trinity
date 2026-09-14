"""#2610 — every table that stores a `--resume` handle must be in the reaper's keep set.

The instances were ent#358 (Workspace threads) and #2610 (rooms). Two shipped
occurrences of one shape means the MECHANISM is the defect, not the two authors:
the writer of a resume handle and the reader of the keep set live in different
modules with no link between them, so adding a new resume-capable surface costs
nothing at the time and silently breaks under the next sweep. Nothing errors at
write time, resuming keeps working for up to a full 6h cycle, and the eventual
symptom is an agent that has forgotten the conversation — which reads as a model
problem, not a deletion.

So this guard is deliberately anchored on the SCHEMA rather than on the reaper:
it fails when a table gains a resume-handle column that no keep-set accessor
covers. A test that merely asserted "the reaper calls three accessors" would
pass on the very next occurrence of the bug, because the new surface simply
would not be in it — the same reason a keep set assembled from a hardcoded list
of sources is a denylist wearing an allowlist's clothes.
"""
from __future__ import annotations

import ast
import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"

# A resume handle: a column holding a Claude session id that some surface later
# passes as `resume_session_id`. Both spellings in the tree today.
_HANDLE_COLUMN = re.compile(r"\bcached_(?:claude_)?session_id\b")

# Each table that stores one, mapped to the module whose
# `list_active_claude_session_ids` puts it in the keep set. Adding a row here is
# the second half of shipping a resume surface — and it is only honest if the
# module is actually consulted by the sweep, which the last test checks.
COVERED = {
    "agent_sessions": "db/sessions.py",
    "enterprise_portal_sessions": "client_portal/db.py",
    "enterprise_room_participants": "shared_sessions/db.py",
}


def _tables_with_resume_handles() -> set[str]:
    """Table names in `db/schema.py` whose DDL declares a resume-handle column.

    `schema.py` is a module-level dict of ``{table_name: ddl_string}``; parsing
    the literal is stabler than regexing for CREATE TABLE, which would also
    match the ones spelled inside migrations and comments.
    """
    tree = ast.parse((BACKEND / "db" / "schema.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if _HANDLE_COLUMN.search(value.value):
                    found.add(key.value)
    return found


def test_every_resume_handle_table_has_a_keep_set_accessor():
    """The guard that would have caught #2610 before it shipped."""
    found = _tables_with_resume_handles()

    assert found, (
        "no resume-handle columns found at all — the column spelling probably "
        "changed, which makes this guard silently vacuous. Update _HANDLE_COLUMN."
    )

    uncovered = found - set(COVERED)
    assert not uncovered, (
        f"{sorted(uncovered)} store a --resume handle but no keep-set accessor "
        "covers them. The JSONL reaper deletes any file whose id is not in the "
        "keep set once it is an hour old, so this surface will lose every "
        "conversation on the next 6h sweep — with no error anywhere. Add a "
        "`list_active_claude_session_ids` to the owning db module, union it in "
        "`services/session_cleanup_service.py::_sweep_agent`, and record it in "
        "COVERED above."
    )


def test_the_covered_map_has_no_stale_rows():
    """A row for a table that no longer stores a handle is a guard that has
    quietly stopped guarding — it would keep passing while its real subject
    went missing."""
    stale = set(COVERED) - _tables_with_resume_handles()
    assert not stale, (
        f"{sorted(stale)} are listed as covered but declare no resume-handle "
        "column any more — drop them from COVERED (and consider whether the "
        "accessor and its union in the reaper are now dead code)."
    )


def test_each_accessor_module_declares_the_accessor_and_is_swept():
    """COVERED is only true if the accessor exists AND the sweep unions it.

    An accessor nothing calls fixes nothing — that is the original bug one step
    later, and it is invisible to every test of the accessor itself.

    The second half counts CALL SITES rather than looking for import lines: the
    three surfaces are reached three different ways (the `database.db` facade,
    and two function-local imports under their own aliases), so matching on
    module paths would assert the plumbing rather than the property. One call
    per covered table is the property.
    """
    sweep_src = (BACKEND / "services" / "session_cleanup_service.py").read_text()

    for table, module_path in sorted(COVERED.items()):
        module_src = (BACKEND / module_path).read_text()
        assert "def list_active_claude_session_ids" in module_src, (
            f"{module_path} is listed as covering {table} but declares no "
            "list_active_claude_session_ids"
        )

    body = sweep_src[sweep_src.index("def _sweep_agent"):]
    calls = body.count("list_active_claude_session_ids(")
    assert calls == len(COVERED), (
        f"_sweep_agent makes {calls} keep-set call(s) but {len(COVERED)} tables "
        "store a resume handle. Every covered surface must be unioned into the "
        "keep set exactly once — an accessor that exists but is never called "
        "leaves its surface as exposed as it was before the accessor existed."
    )
