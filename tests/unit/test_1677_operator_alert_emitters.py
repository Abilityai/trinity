"""Static guard: every operator-queue direct-create caller is classified (#1677).

#1632 capped the agent-authored FILE seam; platform direct-DB creates bypass it
by construction. That exemption is only safe while every direct caller is
genuinely platform-cadence-bound — `task_execution_service._alert_skill_not_found`
(#1410) was agent-influenceable (one high-priority item per distinct unknown
slash-command) and shipped uncapped for months. This guard makes the NEXT such
emitter a red build instead of a CVE (the #1890 lesson, deliberately inverted:
a sink-level default bound would fail QUIETLY at the load-bearing #1402
poison-park create, while a classification-forcing CI test fails LOUDLY at
review time — that fail-direction asymmetry is why the inversion is deliberate).

Rule: every call site of ``create_operator_queue_item`` (any receiver spelling —
``db.``, ``core_db.``, bare name) or of ``create_item`` on an
``_operator_queue_ops`` / ``OperatorQueueOperations`` receiver (the
facade-bypass spelling) must be listed in ``_ALLOWED_CALLERS`` keyed by
``(relative path, enclosing qualname)`` with a one-line justification —
either "platform-only" (with the cadence bound that makes it so) or the
budget seam itself. An agent-influenceable emitter must instead route through
``operator_queue_service.create_bounded_alert``.

Scope: the OSS tree only. ``src/backend/enterprise/**`` (a private submodule,
unmounted on public CI and differing per clone) is deliberately excluded —
Invariant #5's "a guard that walks only one tree is not a guard" cuts the
other way here: claiming coverage of a tree this test cannot see would be the
false guard. The private repo owns its own twin (flagged follow-up).

AST-shaped, not a grep (house test_293 / test_ent109 / test_1871 idiom): a
*mention* in a comment or docstring is never a call site, and the
receiver-chain match keeps unrelated ``.create_item()`` calls out.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# Cheap pre-filter: both real spellings (`create_operator_queue_item(` and
# `_operator_queue_ops.create_item(`) contain this token, so a file without it
# cannot hold a call site and is skipped WITHOUT parsing (keeps the guard
# immune to unrelated files using syntax newer than the running interpreter —
# the test_1871 precedent).
_PREFILTER_TOKEN = "operator_queue"

# The private enterprise submodule is out of scope (see module docstring).
_EXCLUDE_DIRS = ("enterprise/",)

# ---------------------------------------------------------------------------
# The classified caller inventory. Adding a create call site REQUIRES an entry
# here (platform-only, with the cadence bound that justifies it) OR routing the
# emitter through create_bounded_alert. This dict is also the living caller
# inventory the operating-room feature flow points at.
# ---------------------------------------------------------------------------
_ALLOWED_CALLERS = {
    ("client_portal/service.py", "_alert_collided_inbox"):
        "platform-only: idempotent id per portal client's legacy dir (ent#308)",
    ("database.py", "DatabaseManager.create_operator_queue_item"):
        "the facade delegation itself (the _operator_queue_ops.create_item spelling)",
    ("services/agent_client/circuit.py", "_emit_dormant_alert"):  # 1028: package split
        "platform-only: edge-triggered after consecutive failed CB probes (cb-dormant)",
    ("services/archive_storage.py", "_alarm_unwritable_archive_dir"):
        "platform-only: raised from probe_archive_writability() at the start of an "
        "archival RUN (daily maintenance cadence), and the id is bucketed per path "
        "per UTC day — with on_conflict_do_nothing that is <=1 row/path/day even in "
        "a restart loop; no agent input reaches it (#2205, mirrors #2216)",
    ("services/db_backup_service.py", "DBBackupService._emit_alarm"):
        "platform-only: edge-triggered on the durable prior-status transition "
        "(ok->failed), plus a staleness re-alarm throttled to weekly; volume is "
        "bound by the daily job cadence and no agent input reaches it (#2216)",
    ("services/lease_reaper_service.py", "_create_park_item"):
        "platform-only + LOAD-BEARING: #1402 poison-park parks ONLY on a successful "
        "create — must never be throttled (the reason a db-sink bound was rejected)",
    ("services/operator_queue_service.py", "OperatorQueueSyncService._sync_agent"):
        "the #1632-capped agent-file ingestion seam itself (depth+rate+clamp)",
    ("services/operator_queue_service.py",
     "OperatorQueueSyncService._maybe_emit_flood_alert"):
        "the #1632 flood alert: cooldown-gated, one per episode",
    ("services/subscription_headroom_alerts.py", "_emit"):
        "platform-only: edge-triggered per weekly window via a deterministic "
        "id (sub-headroom-{sid}-{reset-day}-{tier}) whose ON CONFLICT DO NOTHING "
        "makes a re-emit a no-op; volume is bound by the sweep cadence and a "
        "per-cycle cap. Agent-CHOSEN names do reach the body (an agent may "
        "spawn children and name them) but arrive sanitized and capped at five "
        "per alert — it is the VOLUME an agent cannot drive, which is what this "
        "exemption rests on (ent#434)",
    ("services/operator_queue_service.py", "create_bounded_alert"):
        "the #1677 budget helper's own admit-path create (the seam itself)",
    ("services/operator_queue_service.py", "_maybe_emit_alert_budget_episode"):
        "the #1677 budget episode alert: cooldown-gated, bucketed reserved id",
    ("services/retention_guard.py", "announce_refusal"):
        "platform-only: edge-gated (fresh) + idempotent id (#1644)",
    ("services/skill_service.py", "SkillService._announce_reconcile_refusal"):
        "platform-only: assignment-driven, quasi-idempotent id (ent#236)",
    ("services/skill_service.py", "SkillService._record_adoption_failure"):
        "platform-only: admin-driven sync cadence (ent#346)",
    ("services/skills_sync_service.py",
     "SkillsLibrarySyncService._announce_fleet_failures"):
        "platform-only: leader-locked, one per sync run (ent#236)",
    ("services/sync_health_service.py", "SyncHealthService._emit_sync_failing_alert"):
        "platform-only: edge-triggered once per failure series (2→3 crossing, #389)",
    ("services/sync_health_service.py", "SyncHealthService._emit_git_bloat_alert"):
        "platform-only: edge-triggered per threshold crossing (#1595)",
    ("services/system_agent_service.py",
     "SystemAgentService._emit_base_image_stale_alert"):
        "platform-only: per-process cooldown, hosted on trinity-system (#1816)",
    ("services/system_agent_service.py", "SystemAgentService._emit_start_failed_alert"):
        "platform-only: time-bucketed idempotent id, hosted on trinity-system (#1816)",
    ("services/system_seed_service.py", "SystemSeedService._notify_operator"):
        "platform-only: deterministic id ⇒ ≤1 open row per seed kind (ent#124)",
    ("services/validation_service.py", "ValidationService._notify_operator_on_failure"):
        "platform-only: fires only for schedules the operator enabled validation "
        "on; cadence operator-bound (#1632 classification)",
}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _receiver_names(node: ast.AST) -> list[str]:
    """Identifiers along an attribute/name receiver chain (`a.b.c` → [c, b, a])."""
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return names


def _is_emitter_call(call: ast.Call) -> bool:
    """True for any spelling of the operator-queue create.

    * ``create_operator_queue_item(...)`` — bare name (a hypothetical
      ``from database import`` spelling);
    * ``<anything>.create_operator_queue_item(...)`` — the ``db.`` /
      ``core_db.`` facade spellings, receiver-agnostic on purpose;
    * ``<chain>.create_item(...)`` ONLY when the receiver chain names
      ``_operator_queue_ops`` or ``OperatorQueueOperations`` — the
      facade-bypass spelling. A bare ``create_item`` on any other receiver is
      deliberately NOT matched (other domains own methods of that name).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "create_operator_queue_item"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "create_operator_queue_item":
        return True
    if func.attr == "create_item":
        return any(
            n in ("_operator_queue_ops", "OperatorQueueOperations")
            for n in _receiver_names(func.value)
        )
    return False


def _call_sites(rel_path: str, text: str) -> list[tuple[str, str, int]]:
    """(path, enclosing qualname, lineno) for every emitter call in `text`.

    Fail-closed: a file that mentions the token but cannot be parsed is
    reported (it might be hiding a call site), while a token-less file is
    skipped unparsed (test_1871's interpreter-skew rationale).
    """
    if _PREFILTER_TOKEN not in text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [(rel_path, "<unparseable>", 0)]

    sites: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child.name])
            else:
                if isinstance(child, ast.Call) and _is_emitter_call(child):
                    sites.append((rel_path, ".".join(stack) or "<module>", child.lineno))
                visit(child, stack)

    visit(tree, [])
    return sites


def _scan_backend() -> list[tuple[str, str, int]]:
    sites: list[tuple[str, str, int]] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        sites.extend(_call_sites(rel, path.read_text(encoding="utf-8")))
    return sites


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def test_every_emitter_call_site_is_classified():
    found = {(p, q) for (p, q, _ln) in _scan_backend()}
    unlisted = found - set(_ALLOWED_CALLERS)
    assert not unlisted, (
        "Unclassified operator-queue create call site(s) (#1677). Every direct "
        "create must be classified: EITHER add a (path, qualname) entry to "
        "_ALLOWED_CALLERS with a one-line platform-only justification (the "
        "emitter's volume must be platform-cadence-bound: edge-triggered, "
        "idempotent id, leader-locked, or operator-driven), OR — if an agent "
        "can drive its volume — route it through "
        "operator_queue_service.create_bounded_alert and register its type in "
        "_BUDGETED_ALERT_TYPES:\n  "
        + "\n  ".join(f"{p} :: {q}" for p, q in sorted(unlisted))
    )


def test_no_stale_allowlist_entries():
    """An allowlisted caller that no longer exists silently widens the
    exemption for whatever lands at that (path, qualname) later — pin that
    every entry still resolves to a real call site."""
    found = {(p, q) for (p, q, _ln) in _scan_backend()}
    stale = set(_ALLOWED_CALLERS) - found
    assert not stale, (
        "Stale _ALLOWED_CALLERS entrie(s) — the call site moved or was removed; "
        "update or drop the entry:\n  "
        + "\n  ".join(f"{p} :: {q}" for p, q in sorted(stale))
    )


def test_skill_not_found_emitter_is_routed_not_allowlisted():
    """The #1677 target must never regress to a direct create: the routed
    emitter is absent from the allowlist by design, and its module must call
    the budget helper."""
    assert not any(
        p == "services/task_execution_service.py" for (p, _q) in _ALLOWED_CALLERS
    ), "task_execution_service must route through create_bounded_alert, not be allowlisted"
    text = (_BACKEND / "services/task_execution_service.py").read_text(encoding="utf-8")
    assert "create_bounded_alert" in text


# ---------------------------------------------------------------------------
# Meta-tests: the scanner must actually catch things
# ---------------------------------------------------------------------------

def test_detects_facade_spelling():
    bad = "def emit():\n    db.create_operator_queue_item(agent, item)\n"
    assert _call_sites("services/planted.py", bad) == [
        ("services/planted.py", "emit", 2)
    ]


def test_detects_core_db_and_bare_spellings():
    bad = (
        "async def a():\n    core_db.create_operator_queue_item(n, i)\n"
        "def b():\n    create_operator_queue_item(n, i)\n"
    )
    qualnames = [q for (_p, q, _ln) in _call_sites("x.py", bad)]
    assert qualnames == ["a", "b"]


def test_detects_facade_bypass_create_item_spelling():
    bad = (
        "class C:\n"
        "    def m(self):\n"
        "        self._operator_queue_ops.create_item(agent, item)\n"
    )
    assert _call_sites("y.py", bad) == [("y.py", "C.m", 3)]


def test_ignores_unrelated_create_item_receivers():
    ok = (
        "# mentions operator_queue so the pre-filter does not skip the file\n"
        "def m():\n"
        "    cache.create_item(k, v)\n"
        "    self.store.create_item(k, v)\n"
    )
    assert _call_sites("z.py", ok) == []


def test_ignores_mentions_in_comments_and_docstrings():
    prose = (
        '"""Doc mentioning db.create_operator_queue_item(agent, item) in prose."""\n'
        "# and a comment: db.create_operator_queue_item(agent, item)\n"
        "x = 1\n"
    )
    assert _call_sites("prose.py", prose) == []


def test_fails_closed_on_unparseable_file_with_token():
    broken = "def f(:\n    db.create_operator_queue_item(n, i)\n"
    assert _call_sites("broken.py", broken) == [("broken.py", "<unparseable>", 0)]


def test_skips_tokenless_file_without_parsing():
    newer = "try:\n    pass\nexcept* ValueError:\n    pass\n"  # 3.11+ syntax
    assert _call_sites("newer.py", newer) == []


def test_module_level_call_gets_module_qualname():
    bad = "db.create_operator_queue_item(n, i)\n"
    assert _call_sites("m.py", bad) == [("m.py", "<module>", 1)]
