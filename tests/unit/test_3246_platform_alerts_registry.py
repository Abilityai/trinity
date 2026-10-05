"""
#3246 C4 — the platform-alert seam's static guards.

* Two-way prefix parity: every registered kind's prefix is platform-reserved
  (no agent can mint a row into it), and every reserved prefix is either a
  registered kind's, an external (enterprise) emitter's, or the gate-ask
  prefix (an ask, not an alert — #3247's territory).
* The ratchet: every platform emitter that still writes operator-queue rows
  directly (or through the #1677 budget helper) is listed, with the follow-up
  that moves it. It may only shrink: an emitter moved onto the seam leaves it
  in the same commit, and a new direct emitter cannot land unlisted.
* The clear-site check: a `clears="hook"` kind that is on the seam has a
  `clear(...)` / `reconcile(...)` call naming it — or a named reason why the
  code has no place that knows the condition cleared.
* The caller guard: only the seam writes and ends platform rows through the
  #3246 accessors.

AST-shaped (the test_1677 idiom): a mention in a comment is never a call site.
Related flow: docs/memory/feature-flows/operating-room.md
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

_EXCLUDE_DIRS = ("enterprise/",)

_PR2 = "follow-up PR 2 of #3246 (the remaining emitters)"

# ---------------------------------------------------------------------------
# The ratchet — every platform emitter NOT yet on the seam. Shrinks per PR;
# empty when PR 2 lands. Key: (path under src/backend, enclosing qualname);
# value: (the id prefix it writes, the follow-up that moves it).
# ---------------------------------------------------------------------------
EMITTERS_NOT_YET_ON_SEAM = {
    ("services/subscription_headroom_alerts.py", "_emit"): ("sub-headroom-", "#3246 C5′"),
    ("services/skill_service.py", "SkillService._record_adoption_failure"):
        ("skills-legacy-adoption-", "#3246 C5′"),
    # -- PR 2 ---------------------------------------------------------------
    ("services/skill_service.py", "SkillService._announce_reconcile_refusal"):
        ("skills-reconcile-", _PR2),
    ("services/skills_sync_service.py", "SkillsLibrarySyncService._announce_fleet_failures"):
        ("skills-fleet-reinject-", _PR2),
    ("services/sync_health_service.py", "SyncHealthService._emit_sync_failing_alert"):
        ("sync-failing-", _PR2),
    ("services/sync_health_service.py", "SyncHealthService._emit_sync_diverged_alert"):
        ("sync-diverged-", _PR2),
    ("services/sync_health_service.py", "SyncHealthService._emit_git_bloat_alert"):
        ("git-bloat-", _PR2),
    ("services/db_backup_service.py", "DBBackupService._emit_alarm"): ("db-backup-", _PR2),
    ("services/archive_storage.py", "_alarm_unwritable_archive_dir"): ("log-archive-", _PR2),
    ("services/retention_guard.py", "announce_refusal"): ("retention-guard-", _PR2),
    ("services/system_seed_service.py", "SystemSeedService._notify_operator"): ("system-seed-", _PR2),
    ("services/git_service/token_scrub.py", "_alarm_git_token_scrub_refused"):
        ("ent615-git-token-scrub-", _PR2),
    ("services/git_service/token_scrub.py", "_alarm_git_token_scrub_unreadable"):
        ("ent615-git-token-scrub-unreadable-", _PR2),
    ("services/lease_reaper_service.py", "_create_park_item"): ("poison-", _PR2),
    ("services/validation_service.py", "ValidationService._notify_operator_on_failure"):
        ("val_", _PR2),
    ("client_portal/service.py", "_alert_collided_inbox"): ("portal-inbox-collision-", _PR2),
    ("services/operator_queue_service.py", "_maybe_emit_alert_budget_episode"):
        ("alert-budget-", _PR2),
    # The #1677 budget helper stays as it is in PR 1; the budgeted emitters
    # below call it and move onto the seam (which composes the budget) in PR 2.
    ("services/operator_queue_service.py", "create_bounded_alert_outcome"): (None, _PR2),
    ("services/operator_queue_service.py", "OperatorQueueSyncService._maybe_emit_flood_alert"):
        ("queue-flood-", _PR2),
    ("services/idempotency_service.py", "_on_unguarded_effect"): ("effect-unguarded-", _PR2),
    ("services/task_execution_service.py", "_alert_skill_not_found"): ("skill-not-found-", _PR2),
    ("services/git_service/gitignore_sweep.py", "_emit_gitignore_untracked_alert"):
        ("gitignore-untracked-", _PR2),
    ("client_portal/service.py", "raise_problem_report"): ("workspace-problem-", _PR2),
}

# Writers that are not platform emitters: the facades, the agent-authored
# seams, and the bool view of the budget helper.
_NOT_EMITTERS = {
    ("database.py", "DatabaseManager.create_operator_queue_item"),
    ("database.py", "DatabaseManager.create_operator_queue_item_with_outcome"),
    ("database.py", "DatabaseManager.create_native_operator_queue_item"),
    ("services/ask_service.py", "raise_ask"),
    ("services/operator_queue_service.py", "OperatorQueueSyncService._sync_agent"),
    ("services/operator_queue_service.py", "create_bounded_alert"),
}

# A hook kind on the seam whose emitter has no place that knows the condition
# cleared: kind -> the reason, so "reviewed" is told apart from "forgotten".
NO_CLEAR_SITE: dict = {}

_DIRECT = ("create_operator_queue_item", "create_operator_queue_item_with_outcome",
           "create_native_operator_queue_item")
_OPS = ("create_item", "create_item_with_outcome", "create_native_item")
_BOUNDED = ("create_bounded_alert", "create_bounded_alert_outcome")
# The #3246 accessors: only the seam (and their own facade / sink) may call them.
_SEAM_ONLY = {
    "create_platform_operator_queue_item": {"services/platform_alerts.py"},
    "end_operator_queue_items_by_platform": {"services/ask_service.py"},
    "clear_platform": {"services/platform_alerts.py"},
    "create_platform_item": {"database.py"},
    "end_items_by_platform": {"database.py"},
}


def _callee(call: ast.Call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id, []
    if isinstance(f, ast.Attribute):
        names, node = [], f.value
        while isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            names.append(node.id)
        return f.attr, names
    return None, []


def _walk(rel, text):
    """(qualname, call) for every call in a module, qualname = enclosing defs."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [("<unparseable>", None)]
    out = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child.name])
            else:
                if isinstance(child, ast.Call):
                    out.append((".".join(stack) or "<module>", child))
                visit(child, stack)

    visit(tree, [])
    return out


def _backend_files(token):
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        text = path.read_text(encoding="utf-8")
        if any(t in text for t in token):
            yield rel, text


def _emitter_sites():
    sites = set()
    for rel, text in _backend_files(("operator_queue", "create_bounded_alert")):
        for qual, call in _walk(rel, text):
            if call is None:
                sites.add((rel, qual))
                continue
            name, receivers = _callee(call)
            if name in _DIRECT or name in _BOUNDED or (
                    name in _OPS and {"_operator_queue_ops", "OperatorQueueOperations"} & set(receivers)):
                sites.add((rel, qual))
    return sites - _NOT_EMITTERS


def _seam_kind_calls():
    """{kind literal} for every `clear(` / `reconcile(` call outside the seam."""
    kinds = set()
    for rel, text in _backend_files(("platform_alerts",)):
        if rel == "services/platform_alerts.py":
            continue
        for _qual, call in _walk(rel, text):
            if call is None:
                continue
            name, receivers = _callee(call)
            if name not in ("clear", "reconcile") or "platform_alerts" not in receivers:
                continue
            arg = call.args[1] if len(call.args) > 1 else next(
                (k.value for k in call.keywords if k.arg == "kind"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                kinds.add(arg.value)
    return kinds


# ---------------------------------------------------------------------------
# Prefix parity
# ---------------------------------------------------------------------------

def test_every_kind_prefix_is_reserved():
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa
    reserved = oqs._RESERVED_ID_PREFIXES
    loose = {k.name: k.prefix for k in pa.KINDS.values()
             if not any(k.prefix.startswith(p) for p in reserved)}
    assert not loose, f"kind prefixes an agent could author: {loose}"


def test_every_reserved_prefix_is_a_known_platform_family():
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa
    known = {k.prefix for k in pa.KINDS.values()} | set(pa.EXTERNAL_PREFIXES) | {oqs.GATE_ASK_ID_PREFIX}
    stray = [p for p in oqs._RESERVED_ID_PREFIXES if p not in known]
    assert not stray, f"reserved prefixes with no registered kind: {stray}"


@pytest.mark.parametrize("prefix", ["skills-reconcile-", "skills-fleet-reinject-",
                                    "retention-guard-", "ent615-git-token-scrub-"])
def test_the_four_unreserved_families_are_now_platform_minted(prefix):
    from services import operator_queue_service as oqs
    assert oqs.is_platform_minted({"request_id": f"{prefix}x-1"})


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------

def test_every_direct_platform_emitter_is_on_the_ratchet():
    unlisted = _emitter_sites() - set(EMITTERS_NOT_YET_ON_SEAM)
    assert not unlisted, (
        f"platform emitters writing rows outside the #3246 seam: {sorted(unlisted)} — "
        "report through services.platform_alerts.observe/clear")


def test_no_ratchet_entry_is_stale():
    stale = set(EMITTERS_NOT_YET_ON_SEAM) - _emitter_sites()
    assert not stale, f"moved onto the seam — remove from the ratchet: {sorted(stale)}"


def test_ratchet_prefixes_are_registered_kinds():
    from services import platform_alerts as pa
    prefixes = {k.prefix for k in pa.KINDS.values()}
    bad = {e: p for e, (p, _f) in EMITTERS_NOT_YET_ON_SEAM.items() if p is not None and p not in prefixes}
    assert not bad, bad


# ---------------------------------------------------------------------------
# Clear sites
# ---------------------------------------------------------------------------

def _hook_kinds_on_seam():
    from services import platform_alerts as pa
    waiting = {p for (p, _f) in EMITTERS_NOT_YET_ON_SEAM.values()}
    return {k.name for k in pa.KINDS.values()
            if k.clears == pa.CLEARS_HOOK and k.prefix not in waiting}


def test_every_hook_kind_on_the_seam_has_a_clear_site():
    missing = _hook_kinds_on_seam() - _seam_kind_calls() - set(NO_CLEAR_SITE)
    assert not missing, (
        f"hook kinds on the seam with no platform_alerts.clear/reconcile call: {sorted(missing)} "
        "— call it where the code knows the condition cleared, or name why in NO_CLEAR_SITE")


def test_no_clear_site_exemptions_are_live():
    stale = set(NO_CLEAR_SITE) & _seam_kind_calls()
    assert not stale, f"exempted kinds that now have a clear site: {sorted(stale)}"


def test_clear_site_scanner_reads_positional_and_keyword_kind():
    calls = [c for _q, c in _walk("x.py", (
        "from services import platform_alerts\n"
        "def f():\n"
        "    platform_alerts.clear('a', 'circuit_dormant', 'a')\n"
        "    platform_alerts.reconcile('a', kind='subscription_headroom', live_keys=[])\n"
        "    other.clear('a', 'base_image_stale')\n"))]
    names = [(_callee(c)[0], _callee(c)[1]) for c in calls]
    assert ("clear", ["platform_alerts"]) in names and ("clear", ["other"]) in names


# ---------------------------------------------------------------------------
# The caller guard
# ---------------------------------------------------------------------------

def test_only_the_seam_calls_the_platform_accessors():
    offenders = []
    for rel, text in _backend_files(tuple(_SEAM_ONLY)):
        for qual, call in _walk(rel, text):
            if call is None:
                continue
            name, _r = _callee(call)
            if name in _SEAM_ONLY and rel not in _SEAM_ONLY[name]:
                offenders.append((rel, qual, name, call.lineno))
    assert not offenders, f"platform-alert accessors called outside the seam: {offenders}"
