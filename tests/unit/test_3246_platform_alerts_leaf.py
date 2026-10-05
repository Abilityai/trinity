"""#3246 C1 — the platform-alert leaf: registry, key belt, legacy subjects, sweep planner.

A platform alert describes a CONDITION ("this subscription is near its limit"),
but the operator queue stored each reading as a separate MESSAGE, so the pending
set filled with repeats of one condition. The fix keys every platform alert on a
SUBJECT (`kind:key`) the database can see. This file pins the pure half of that:

  * `services/platform_alerts.py` is a stdlib-only leaf — both migration tracks
    and the service graph import it, so it must not import them back;
  * the registry declares, per kind, its id prefix and lifetime class
    (14 d default + env knob, a 30 d net, or none — a person must act);
  * the key belt turns any key into an id-shaped subject, deterministically;
  * every legacy id shape the in-repo emitters wrote maps to the subject the
    seam will use for the same condition — or to "known kind, no subject" when
    the id does not identify the condition, never to a guess;
  * `plan_sweep` keeps the newest pending row per (agent, subject), ends the
    rest, stamps survivors, leaves unknown prefixes alone, and plans nothing on
    its own output (idempotent).
"""
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

import services.platform_alerts as pa  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(id, request_id, *, agent="_sub-headroom", created_at=None, status="pending",
         context=None, subject=None, expires_at=None, disposed_by=None,
         disposed_at=None, priority="high"):
    return {
        "id": id,
        "agent_name": agent,
        "request_id": request_id,
        "status": status,
        "priority": priority,
        "created_at": created_at,
        "expires_at": expires_at,
        "context": context,
        "subject": subject,
        "disposed_by": disposed_by,
        "disposed_at": disposed_at,
    }


# ---------------------------------------------------------------------------
# The leaf is stdlib-only
# ---------------------------------------------------------------------------


class TestImportIsolation:
    def test_loads_by_path_in_a_bare_interpreter(self):
        """Loaded by file path with NOTHING of the backend on sys.path (the way
        `test_schema_parity.py` loads `db/migrations.py`): any non-stdlib import
        at module level would raise here."""
        path = os.path.join(_BACKEND, "services", "platform_alerts.py")
        code = textwrap.dedent(f"""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location("pa_leaf", {path!r})
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            leaked = [m for m in ("database", "db", "services", "services.ask_service",
                                  "services.operator_queue_service", "utils",
                                  "sqlalchemy", "config") if m in sys.modules]
            assert not leaked, leaked
            assert mod.KINDS, "registry is empty"
            print("ok")
        """)
        out = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True,
            cwd="/", timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_issue_named_kinds_are_registered(self):
        for name in ("subscription_headroom", "skills_legacy_adoption",
                     "base_image_stale", "system_agent_start_failed", "circuit_dormant"):
            assert name in pa.KINDS

    def test_lifetime_classes(self, monkeypatch):
        monkeypatch.delenv(pa.LIFETIME_ENV, raising=False)
        assert pa.lifetime_for("subscription_headroom") == timedelta(days=14)
        assert pa.lifetime_for("circuit_dormant") == timedelta(days=pa.NET_LIFETIME_DAYS) == timedelta(days=30)
        # The four a person must act on never expire on their own.
        for name in ("poison", "gitignore_untracked", "workspace_problem_report",
                     "portal_inbox_collision"):
            assert pa.lifetime_for(name) is None, name

    def test_lifetime_env_knob(self, monkeypatch):
        monkeypatch.setenv(pa.LIFETIME_ENV, "3")
        assert pa.lifetime_for("subscription_headroom") == timedelta(days=3)
        # the 30-day net is fixed, not the knob
        assert pa.lifetime_for("circuit_dormant") == timedelta(days=30)

    @pytest.mark.parametrize("bad", ["0", "-2", "abc", "", "1.5"])
    def test_bad_lifetime_env_falls_back_with_warning(self, monkeypatch, caplog, bad):
        monkeypatch.setenv(pa.LIFETIME_ENV, bad)
        with caplog.at_level("WARNING"):
            assert pa.default_lifetime_days() == pa.DEFAULT_LIFETIME_DAYS == 14
        if bad:
            assert pa.LIFETIME_ENV in caplog.text

    def test_snooze_window_is_seven_days_with_env_knob(self, monkeypatch, caplog):
        monkeypatch.delenv(pa.SNOOZE_ENV, raising=False)
        assert pa.snooze_window() == timedelta(days=7)
        monkeypatch.setenv(pa.SNOOZE_ENV, "2")
        assert pa.snooze_window() == timedelta(days=2)
        monkeypatch.setenv(pa.SNOOZE_ENV, "nope")
        with caplog.at_level("WARNING"):
            assert pa.snooze_window() == timedelta(days=7)
        assert pa.SNOOZE_ENV in caplog.text

    def test_kinds_sharing_a_prefix_share_a_lifetime_class(self):
        by_prefix = {}
        for kind in pa.KINDS.values():
            by_prefix.setdefault(kind.prefix, set()).add(kind.lifetime)
        assert all(len(v) == 1 for v in by_prefix.values()), by_prefix

    def test_register_refuses_a_duplicate_and_accepts_a_new_kind(self, monkeypatch):
        monkeypatch.setattr(pa, "KINDS", dict(pa.KINDS))
        with pytest.raises(ValueError):
            pa.register(pa.Kind(name="circuit_dormant", prefix="cb-dormant-",
                                lifetime=pa.LIFETIME_NET, clears=pa.CLEARS_HOOK))
        pa.register(pa.Kind(name="role_drift", prefix="role-drift-",
                            lifetime=pa.LIFETIME_DEFAULT, clears=pa.CLEARS_HOOK))
        assert pa.subject_for("role_drift", "agent-a") == "role_drift:agent-a"

    def test_gate_and_external_prefixes_are_not_kinds(self):
        prefixes = {k.prefix for k in pa.KINDS.values()}
        assert "gate-" not in prefixes
        assert pa.EXTERNAL_PREFIXES == frozenset({"role-drift-"})
        assert not prefixes & pa.EXTERNAL_PREFIXES


# ---------------------------------------------------------------------------
# Key belt / subject
# ---------------------------------------------------------------------------


class TestSubject:
    def test_id_shaped_key_is_kept(self):
        assert pa.subject_for("circuit_dormant", "agent-a") == "circuit_dormant:agent-a"
        assert pa.subject_for("poison", 42) == "poison:42"

    def test_odd_or_long_key_is_hashed_deterministically(self):
        odd = pa.subject_for("skill_not_found", "/do thing now")
        assert odd == pa.subject_for("skill_not_found", "/do thing now")
        assert odd.startswith("skill_not_found:") and len(odd.split(":", 1)[1]) == 16
        long_key = "a" * 129
        assert len(pa.subject_for("circuit_dormant", long_key).split(":", 1)[1]) == 16
        assert pa.subject_for("circuit_dormant", "a" * 128) == "circuit_dormant:" + "a" * 128

    @pytest.mark.parametrize("bad", [None, "", "   ", True, 1.5, ["x"]])
    def test_missing_key_on_a_condition_kind_is_refused(self, bad):
        with pytest.raises(ValueError):
            pa.subject_for("circuit_dormant", bad)

    def test_event_kind_has_no_subject(self):
        assert pa.subject_for("gitignore_untracked", None) is None
        assert pa.subject_for("workspace_problem_report", "anything") is None

    def test_unregistered_kind_raises(self):
        with pytest.raises(KeyError):
            pa.subject_for("no_such_kind", "x")


# ---------------------------------------------------------------------------
# Legacy subject derivation — one row per legacy id shape
# ---------------------------------------------------------------------------

TS = "2026-09-01T10:30:00.123456Z"


@pytest.mark.parametrize("request_id,context,expected", [
    # A1/A2 subscription headroom — per subscription (dashed sid), both tiers, fleet
    ("sub-headroom-sub-abc-123-2026-10-09-warn", None, ("subscription_headroom", "sub-abc-123")),
    ("sub-headroom-sub-abc-123-2026-10-09-crit", None, ("subscription_headroom", "sub-abc-123")),
    ("sub-headroom-s1-unknown-2026-10-01-warn", None, ("subscription_headroom", "s1")),
    ("sub-headroom-fleet-2026-10-09", None, ("subscription_headroom", "fleet")),
    # A3/A4 system agent — the start-failure shape must not be read as staleness
    (f"base-image-stale-trinity-system-{TS}", None, ("base_image_stale", "trinity-system")),
    ("base-image-stale-start-trinity-system-987654", None, ("system_agent_start_failed", "trinity-system")),
    # A5 reconcile refusal — the orphan count is a reading, not identity
    ("skills-reconcile-agent-a-7", None, ("skills_reconcile_refused", "agent-a")),
    # A6/A7 legacy adoption — both branches, one subject (T7)
    ("skills-legacy-adoption-refused-0123456789ab", None, ("skills_legacy_adoption", "0123456789ab")),
    (f"skills-legacy-adoption-{TS}", {"url": " https://example.com/lib.git "},
     ("skills_legacy_adoption", "0d2c2b5e6c5d")),
    # A8–A10 per-agent transport / sync
    (f"cb-dormant-agent-a-{TS}", None, ("circuit_dormant", "agent-a")),
    (f"sync-failing-agent-a-{TS}", None, ("sync_failing", "agent-a")),
    (f"sync-diverged-agent-a-{TS}", None, ("sync_diverged", "agent-a")),
    # A12 db backup — the condition lives in context
    (f"db-backup-{TS}", {"alert_type": "db_backup_failure", "backup_dir": "/data/backups"},
     ("db_backup_failure", "/data/backups")),
    (f"db-backup-{TS}", {"alert_type": "db_backup_stale", "backup_dir": "/data/backups"},
     ("db_backup_stale", "/data/backups")),
    # A13 archive — keyed by the path in context
    ("log-archive-data-archives-2026-09-01", {"path": "/data/archives"},
     ("log_archive_unwritable", "/data/archives")),
    # B1–B14
    ("poison-exec-123", None, ("poison", "exec-123")),
    ("system-seed-template_missing", None, ("system_seed", "template_missing")),
    ("val_exec-9_20260901103000", None, ("validation_failed", "exec-9")),
    ("ent615-git-token-scrub-agent-a-2026-09-01", None, ("git_token_scrub_refused", "agent-a")),
    ("ent615-git-token-scrub-unreadable-agent-a-2026-09-01", None, ("git_token_scrub_unreadable", "agent-a")),
    ("retention-guard-execution_retention_days-30", None, ("retention_refused", "execution_retention_days")),
    (f"skills-fleet-reinject-{TS}", None, ("skills_fleet_reinject", "fleet")),
    (f"effect-unguarded-agent-a-{TS}", {"effect_type": "email", "reason": "no_execution_id"},
     ("effect_unguarded", "email:no_execution_id")),
    (f"skill-not-found-agent-a-{TS}", {"command": "/daily"}, ("skill_not_found", "/daily")),
    ("portal-inbox-collision-inbox", None, ("portal_inbox_collision", "inbox")),
    (f"queue-flood-agent-a-{TS}", {"held": 3, "reason": "ingestion_cap"}, ("queue_flood", "ingestion_cap")),
    ("alert-budget-agent-a-effect_unguarded-b123", {"alert_type": "effect_unguarded"},
     ("alert_budget", "effect_unguarded")),
])
def test_legacy_subject_per_shape(request_id, context, expected):
    kind, key = expected
    if kind == "skills_legacy_adoption" and context:
        import hashlib
        key = hashlib.sha256(context["url"].strip().encode()).hexdigest()[:12]
    got = pa.derive_legacy_subject(request_id, context)
    assert got == pa.LegacyMatch(kind, pa.subject_for(kind, key))


@pytest.mark.parametrize("request_id,context,kind", [
    # two conditions under one prefix, told apart only in prose → never guessed
    (f"git-bloat-agent-a-{TS}", None, "git_bloat_size"),
    # a known family whose row lacks the context the subject needs
    (f"db-backup-{TS}", None, "db_backup_failure"),
    (f"skills-legacy-adoption-{TS}", None, "skills_legacy_adoption"),
    (f"skill-not-found-agent-a-{TS}", {}, "skill_not_found"),
    # events: each is its own row by design
    (f"gitignore-untracked-agent-a-{TS}", None, "gitignore_untracked"),
    ("workspace-problem-" + "f" * 32, None, "workspace_problem_report"),
    # a malformed id under a known prefix
    ("sub-headroom-garbage", None, "subscription_headroom"),
])
def test_known_kind_without_a_subject(request_id, context, kind):
    got = pa.derive_legacy_subject(request_id, context)
    assert got is not None and got.subject is None
    assert pa.KINDS[got.kind].lifetime == pa.KINDS[kind].lifetime


@pytest.mark.parametrize("request_id", [
    "role-drift-agent-a-1", "gate-abc", "gate-note-abc", "my-own-ask-1", "", None,
])
def test_unknown_or_external_prefix_is_not_derived(request_id):
    assert pa.derive_legacy_subject(request_id, None) is None


def test_a_longer_prefix_is_always_tried_before_a_shorter_one_it_extends():
    """`base-image-stale-start-` / `ent615-git-token-scrub-unreadable-` must win
    over the family prefix they extend, whatever order the table is written in."""
    order = [prefix for prefix, _kind, _parse in pa._LEGACY_PARSERS]
    for i, short in enumerate(order):
        for longer in order[i + 1:]:
            assert not (longer.startswith(short) and longer != short), (short, longer)


def test_context_may_arrive_as_json_text():
    ctx = json.dumps({"alert_type": "db_backup_stale", "backup_dir": "/b"})
    assert pa.derive_legacy_subject(f"db-backup-{TS}", ctx).subject == pa.subject_for("db_backup_stale", "/b")
    assert pa.derive_legacy_subject(f"db-backup-{TS}", "{not json").subject is None


# ---------------------------------------------------------------------------
# plan_sweep
# ---------------------------------------------------------------------------


def _ids(stamps):
    return sorted(s.id for s in stamps)


class TestPlanSweep:
    def test_keeps_newest_per_subject_and_ends_the_rest(self, monkeypatch):
        monkeypatch.delenv(pa.LIFETIME_ENV, raising=False)
        rows = [
            _row("a", "sub-headroom-s1-2026-10-09-warn", created_at="2026-10-01T00:00:00.000000Z"),
            _row("b", "sub-headroom-s1-2026-10-09-crit", created_at="2026-10-03T00:00:00.000000Z",
                 context={"tier": "crit"}),
            _row("c", "sub-headroom-s2-2026-10-09-warn", created_at="2026-10-02T00:00:00.000000Z"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert plan.ended_ids == ("a",)
        survivors = {s.id: s for s in plan.survivor_stamps}
        assert set(survivors) == {"b", "c"}
        b = survivors["b"]
        assert b.subject == "subscription_headroom:s1"
        assert b.last_seen_at == "2026-10-03T00:00:00.000000Z"
        assert b.expires_at == _iso(NOW + timedelta(days=14))
        assert b.context == {"tier": "crit", "seen_count": 1}

    def test_same_subject_on_different_agents_is_two_groups(self):
        rows = [
            _row("a", f"cb-dormant-x-{TS}", agent="x", created_at="2026-10-01T00:00:00Z"),
            _row("b", f"cb-dormant-x-{TS}", agent="y", created_at="2026-10-02T00:00:00Z"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert plan.ended_ids == ()
        assert _ids(plan.survivor_stamps) == ["a", "b"]

    def test_tie_on_created_at_broken_by_id_and_null_sorts_oldest(self):
        rows = [
            _row("m", f"cb-dormant-x-{TS}", agent="x", created_at=None),
            _row("k", f"cb-dormant-x-{TS}", agent="x", created_at="2026-10-01T00:00:00Z"),
            _row("l", f"cb-dormant-x-{TS}", agent="x", created_at="2026-10-01T00:00:00Z"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert [s.id for s in plan.survivor_stamps] == ["l"]
        assert sorted(plan.ended_ids) == ["k", "m"]

    def test_null_created_at_survivor_is_last_seen_now(self):
        plan = pa.plan_sweep([_row("m", f"cb-dormant-x-{TS}", agent="x")], now=NOW)
        assert plan.survivor_stamps[0].last_seen_at == _iso(NOW)

    def test_existing_expiry_is_kept_and_opt_out_kind_gets_none(self):
        rows = [
            _row("a", f"cb-dormant-x-{TS}", agent="x", expires_at="2026-11-01T00:00:00.000000Z"),
            _row("p", "poison-exec-1", agent="x"),
        ]
        stamps = {s.id: s for s in pa.plan_sweep(rows, now=NOW).survivor_stamps}
        assert stamps["a"].expires_at is None  # None = leave the column as it is
        assert stamps["p"].expires_at is None
        assert stamps["p"].subject == "poison:exec-1"

    def test_net_kind_gets_thirty_days(self):
        stamps = pa.plan_sweep([_row("a", f"cb-dormant-x-{TS}", agent="x")], now=NOW).survivor_stamps
        assert stamps[0].expires_at == _iso(NOW + timedelta(days=30))

    def test_unknown_and_external_prefixes_are_untouched(self):
        rows = [
            _row("r1", "role-drift-x-1", agent="x"),
            _row("r2", "role-drift-x-2", agent="x"),
            _row("g", "gate-abc", agent="x"),
            _row("o", "my-own-ask", agent="x"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert plan.is_empty()

    def test_known_kind_without_subject_gets_a_lifetime_stamp_only(self):
        rows = [
            _row("g1", f"git-bloat-x-{TS}", agent="x"),
            _row("g2", f"git-bloat-x-2026-09-02T00:00:00.000000Z", agent="x"),
            _row("w", f"gitignore-untracked-x-{TS}", agent="x"),  # event, no lifetime
            _row("g3", f"git-bloat-x-{TS}", agent="x", expires_at="2026-12-01T00:00:00Z"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert plan.ended_ids == ()
        assert plan.survivor_stamps == ()
        assert [(s.id, s.expires_at) for s in plan.lifetime_stamps] == [
            ("g1", _iso(NOW + timedelta(days=30))),
            ("g2", _iso(NOW + timedelta(days=30))),
        ]

    def test_already_stamped_pending_row_holds_its_subject(self):
        """A row the seam already owns keeps the index slot; unstamped
        duplicates of its subject end, whatever their age."""
        rows = [
            _row("live", f"cb-dormant-x-{TS}", agent="x", subject="circuit_dormant:x",
                 created_at="2026-09-01T00:00:00Z"),
            _row("old", f"cb-dormant-x-{TS}", agent="x", created_at="2026-10-01T00:00:00Z"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert plan.ended_ids == ("old",)
        assert plan.survivor_stamps == ()

    def test_person_ended_row_in_snooze_window_gets_subject_only(self, monkeypatch):
        monkeypatch.delenv(pa.SNOOZE_ENV, raising=False)
        rows = [
            _row("p1", f"cb-dormant-x-{TS}", agent="x", status="responded",
                 disposed_by="person", disposed_at=_iso(NOW - timedelta(days=6))),
            _row("p2", f"cb-dormant-x-{TS}", agent="x", status="cancelled",
                 disposed_by="person", disposed_at=_iso(NOW - timedelta(days=8))),
            _row("t", f"cb-dormant-x-{TS}", agent="x", status="expired",
                 disposed_by="timeout", disposed_at=_iso(NOW - timedelta(days=1))),
            _row("p3", f"cb-dormant-x-{TS}", agent="x", status="responded",
                 disposed_by="person", disposed_at="garbage"),
        ]
        plan = pa.plan_sweep(rows, now=NOW)
        assert [(s.id, s.subject) for s in plan.snooze_stamps] == [("p1", "circuit_dormant:x")]
        assert plan.ended_ids == () and plan.survivor_stamps == ()

    def test_idempotent_on_its_own_output(self):
        rows = [
            _row("a", "sub-headroom-s1-2026-10-09-warn", created_at="2026-10-01T00:00:00Z"),
            _row("b", "sub-headroom-s1-2026-10-09-crit", created_at="2026-10-03T00:00:00Z"),
            _row("g", f"git-bloat-x-{TS}", agent="x"),
            _row("p", f"cb-dormant-x-{TS}", agent="x", status="responded",
                 disposed_by="person", disposed_at=_iso(NOW - timedelta(days=1))),
        ]
        first = pa.plan_sweep(rows, now=NOW)
        applied = _apply(rows, first, now=NOW)
        second = pa.plan_sweep(applied, now=NOW + timedelta(minutes=1))
        assert second.is_empty(), second

    def test_context_text_is_parsed_and_bad_context_replaced(self):
        rows = [
            _row("a", f"cb-dormant-x-{TS}", agent="x", context=json.dumps({"k": 1})),
            _row("b", f"cb-dormant-y-{TS}", agent="y", context="{bad"),
        ]
        stamps = {s.id: s for s in pa.plan_sweep(rows, now=NOW).survivor_stamps}
        assert stamps["a"].context == {"k": 1, "seen_count": 1}
        assert stamps["b"].context == {"seen_count": 1}

    def test_naive_now_is_refused(self):
        with pytest.raises(ValueError):
            pa.plan_sweep([], now=datetime(2026, 10, 5))


def _apply(rows, plan, *, now):
    """What C2's migration will do with a plan, over plain dicts."""
    by_id = {r["id"]: dict(r) for r in rows}
    for rid in plan.ended_ids:
        by_id[rid].update(status="cancelled", disposed_by="platform", disposed_at=_iso(now))
    for s in plan.survivor_stamps:
        r = by_id[s.id]
        r["subject"], r["last_seen_at"] = s.subject, s.last_seen_at
        if s.expires_at is not None:
            r["expires_at"] = s.expires_at
        r["context"] = s.context
    for s in plan.lifetime_stamps:
        by_id[s.id]["expires_at"] = s.expires_at
    for s in plan.snooze_stamps:
        by_id[s.id]["subject"] = s.subject
    return list(by_id.values())
