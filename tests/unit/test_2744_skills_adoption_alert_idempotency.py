"""A refused legacy skills-library adoption is bounded by its class (#2744).

`SkillService._adopt_legacy_clone()` is the first statement of every
`sync_library()`, and `sync_library()` runs unattended under the ent#236
auto-sync loop (a 300s–86400s timer). On an install that is past migration but
still carries a `skills_library_url` setting matching no configured source, the
terminal *"this install already has skills sources"* branch fired
`_record_adoption_failure` with a **timestamped** `request_id`, `priority:
"high"` and `expires_at: None` — one permanent, high-priority, operator-
unclearable row **per sync, forever** (17 of them on the reporting install, ~17%
of everything pending).

Three properties made that a defect rather than the design:

  * the branch is the **designed resting state** of a migrated install whose
    legacy key lingers — nothing stopped working, so `high` is a lie that buries
    the items that do need a human;
  * the timestamped id defeats `create_item`'s `ON CONFLICT DO NOTHING` on
    `(agent_name, request_id)` **by construction**, so the operator cannot
    dismiss it: acknowledging files another one on the next sync;
  * the emitter is shared by **three** call sites, two of which are genuine
    failures where a repeat really does carry information.

So the fix is per-call-site, and this file pins both directions: the terminal
branch collapses to exactly one row per refused URL at `low` (tests 1–3, 5–6),
and the two actionable branches are **untouched** (test 4 — green before the fix
and after it; it goes red only if an implementer over-applies the change).

Deliberately NOT asserted here: that the DB actually dedupes. That is
`create_item`'s shipped contract (#1631 / PR #1684 moved the conflict target to
`(agent_name, request_id)`), covered by its own tests. What the service controls
— and what this file asserts — is that the id is **stable and URL-derived**.

Lives in its own file rather than appended to `test_ent346_skills_source_injection.py`
because #2763 is being implemented in the same function and wants that file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2744.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2744-logs")
)

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

# Passes both ent#346 validators, so adoption reaches the terminal branch.
TERMINAL_URL = "https://github.com/abilityai/trinity-skills"
OTHER_URL = "https://github.com/acme/other-skills"
# Fails `validate_skills_library_url` (host is not github.com) ⇒ branch A.
REJECTED_URL = "https://evil.example.com/x"
# Passes SSRF validation (userinfo is not the hostname) and is then refused by
# `reject_embedded_credentials` ⇒ branch A, holding a live-shaped PAT.
PAT = "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH0000"
PAT_URL = f"https://{PAT}@github.com/acme/private-skills"

STABLE_PREFIX = "skills-legacy-adoption-refused-"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_DB_BELT_ID_MAX = 512


def expected_stable_id(url: str) -> str:
    """The contract, spelled out rather than re-derived from the source.

    Asserting "the two ids differ" alone cannot distinguish an id derived from
    the URL from one derived from the clock — the timestamp satisfies it on the
    unfixed tree. Only the exact form pins AC 3.
    """
    return f"{STABLE_PREFIX}{hashlib.sha256(url.strip().encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Harness — drives the REAL `_adopt_legacy_clone`; only `db`, the settings
# readers and `library_root` are stubbed.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """No unit test may depend on a resolver.

    `validate_skills_library_url` does a live `socket.getaddrinfo` for
    defense-in-depth and raises if the host resolves to a private/loopback
    address — so on a sandboxed CI resolver a test intending the terminal
    branch would silently drive the VALIDATION-REJECT branch and fail as "5
    distinct ids" / "priority is high", reading exactly like the fix
    regressing. A `gaierror` is the one outcome that function already treats as
    "allow it to fail later during git clone", so stubbing it keeps the
    validator's real code path while making the branch deterministic.
    """
    def _no_dns(*_a, **_kw):
        raise socket.gaierror("offline unit test")

    import utils.url_validation as uv

    monkeypatch.setattr(uv.socket, "getaddrinfo", _no_dns)


def _wire(monkeypatch, url, *, count=3, sources=(), list_raises=False):
    """Put the service in a chosen adoption state. Returns (items, created)."""
    import services.skill_service as svc_mod

    items: list = []
    created: list = []

    monkeypatch.setattr(svc_mod, "get_skills_library_url", lambda: url, raising=False)
    monkeypatch.setattr(svc_mod, "get_skills_library_branch", lambda: "main", raising=False)

    if list_raises:
        def _boom():
            raise RuntimeError("skill_sources read failed")
        monkeypatch.setattr(svc_mod.db, "list_skill_sources", _boom)
    else:
        monkeypatch.setattr(svc_mod.db, "list_skill_sources", lambda: list(sources))

    monkeypatch.setattr(svc_mod.db, "count_skill_sources", lambda: count)
    monkeypatch.setattr(
        svc_mod.db, "create_skill_source", lambda **kw: created.append(kw)
    )
    monkeypatch.setattr(
        svc_mod.db, "create_operator_queue_item", lambda agent, item: items.append(item)
    )
    return items, created


def _service():
    import services.skill_service as svc_mod

    service = svc_mod.SkillService.__new__(svc_mod.SkillService)
    service.library_root = Path(tempfile.mkdtemp())
    return service


def _freeze_clock(monkeypatch, value="2026-09-14T00:00:00Z"):
    """`_record_adoption_failure` imports `utc_now_iso` INSIDE its body, so the
    patch target is `utils.helpers`, not the service module. Patching
    `services.skill_service.utc_now_iso` binds nothing and yields a vacuous
    test."""
    import utils.helpers  # noqa: F401

    monkeypatch.setattr("utils.helpers.utc_now_iso", lambda: value)


def _increment_clock(monkeypatch):
    """A new timestamp per call — so "one id" can never be an artifact of two
    calls landing in the same microsecond."""
    import utils.helpers  # noqa: F401

    counter = {"n": 0}

    def _tick():
        counter["n"] += 1
        return f"2026-09-14T00:00:{counter['n']:02d}.000000Z"

    monkeypatch.setattr("utils.helpers.utc_now_iso", _tick)


def _assert_terminal_branch(item):
    """Pin WHICH branch produced this item.

    Without it, a test that accidentally drove the validation-reject branch
    fails with the same symptoms as the fix regressing.
    """
    assert "already has" in item["question"], (
        "this item did not come from the terminal 'already has sources' branch "
        f"— got: {item['question']!r}"
    )


# ---------------------------------------------------------------------------
# 1. AC 1 + AC 5 — N syncs, one item
# ---------------------------------------------------------------------------

def test_n_syncs_in_the_terminal_state_file_exactly_one_item(monkeypatch):
    """The reported defect, verbatim: 5 syncs used to mean 5 permanent rows.

    The emitter still FIRES every time — we are not silencing the branch, and
    the log line stays the non-suppressible residual. What changes is that all
    five creates carry ONE `request_id`, so `create_item`'s
    `(agent_name, request_id)` ON CONFLICT DO NOTHING collapses them to a
    single row.
    """
    _increment_clock(monkeypatch)
    items, created = _wire(monkeypatch, TERMINAL_URL)
    service = _service()

    for _ in range(5):
        assert service._adopt_legacy_clone() is None

    assert created == [], "the terminal branch must never create a source"
    assert len(items) == 5, "the refusal must stay observable on every sync"
    _assert_terminal_branch(items[0])
    assert len({i["id"] for i in items}) == 1, (
        "five syncs minted five distinct request_ids — the ON CONFLICT dedup "
        "cannot collapse them, which is the unbounded growth this fixes"
    )
    assert items[0]["id"] == expected_stable_id(TERMINAL_URL)


# ---------------------------------------------------------------------------
# 2. AC 3 — a different refused URL still gets its own item
# ---------------------------------------------------------------------------

def test_a_different_refused_url_gets_its_own_item(monkeypatch):
    """Idempotent per URL, not idempotent per branch.

    Time is FROZEN, so on the unfixed tree both URLs mint the identical
    timestamped id and the contract is inverted. The exact-id assertion is what
    pins this: "two ids differ" alone is satisfied on base by the clock.
    """
    _freeze_clock(monkeypatch)
    ids = {}
    for url in (TERMINAL_URL, OTHER_URL):
        items, _ = _wire(monkeypatch, url)
        service = _service()
        service._adopt_legacy_clone()
        service._adopt_legacy_clone()          # the SAME url twice
        assert items, "the refusal must be observable"
        _assert_terminal_branch(items[0])
        assert {i["id"] for i in items} == {expected_stable_id(url)}, (
            f"the id for {url} is not the sha256 of that URL"
        )
        ids[url] = items[0]["id"]

    assert ids[TERMINAL_URL] != ids[OTHER_URL], (
        "two different refused URLs collapsed onto one id — a second refused "
        "repo would be silently suppressed by the ON CONFLICT dedup"
    )


# ---------------------------------------------------------------------------
# 3. AC 2 — not high priority, and not an ERROR
# ---------------------------------------------------------------------------

def test_the_terminal_refusal_is_not_high_priority(monkeypatch, caplog):
    """`high` is a claim that a human must act. Nobody must act on the resting
    state of a correctly-migrated install — and `logger.error` makes the same
    false claim to every log-based alerting rule."""
    _freeze_clock(monkeypatch)
    items, _ = _wire(monkeypatch, TERMINAL_URL)
    service = _service()

    with caplog.at_level(logging.INFO, logger="services.skill_service"):
        service._adopt_legacy_clone()

    assert items
    _assert_terminal_branch(items[0])
    assert items[0]["priority"] != "high"
    assert items[0]["priority"] == "low"

    # One `alert_type` and one title now span both `low` (this benign resting
    # state) and `high` (a URL that failed validation — the signature of an
    # attempted injection), so the branch carries a machine-readable
    # discriminator rather than leaving `priority` to be read as the cause.
    assert items[0]["context"]["reason"] == "already_migrated"

    ent346 = [r for r in caplog.records if "[ent#346]" in r.getMessage()]
    assert ent346, "the refusal must still be logged"
    assert [r.levelno for r in ent346] == [logging.INFO], (
        "the steady-state refusal still logs at ERROR — the log surface makes "
        "the same 'a human must act' claim the priority just stopped making"
    )


# ---------------------------------------------------------------------------
# 4. AC 4 — the two actionable branches are UNCHANGED (green before and after)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case,url,list_raises,marker",
    [
        ("validation reject", REJECTED_URL, False, "URL rejected"),
        ("adoption exception", TERMINAL_URL, True, "adoption failed"),
    ],
)
def test_the_actionable_branches_keep_high_and_repeat_visible_ids(
    monkeypatch, case, url, list_raises, marker
):
    """A repeat here carries information: something is still broken.

    This is the anti-regression half of the fix. It is GREEN on the unfixed
    tree by design — it goes red only if the `low` / stable-id treatment is
    applied to all three call sites instead of the one the issue names.
    """
    _increment_clock(monkeypatch)
    items, _ = _wire(monkeypatch, url, count=0, list_raises=list_raises)
    service = _service()

    service._adopt_legacy_clone()
    service._adopt_legacy_clone()

    assert len(items) == 2, f"{case}: expected one alarm per attempt"
    for item in items:
        assert marker in item["question"], f"{case}: drove the wrong branch"
        assert item["priority"] == "high", f"{case}: lost its severity"
        assert not item["id"].startswith(STABLE_PREFIX), (
            f"{case}: took the steady-state id — a repeat of a genuine failure "
            "is no longer visible as a repeat (AC 4)"
        )
    assert items[0]["id"] != items[1]["id"], (
        f"{case}: two attempts share one request_id, so the second is deduped "
        "away by ON CONFLICT DO NOTHING"
    )


# ---------------------------------------------------------------------------
# 5. The id is reserved and id-shaped
# ---------------------------------------------------------------------------

def test_the_stable_id_is_reserved_and_id_shaped(monkeypatch):
    """A stable id derived from an admin-visible URL is GUESSABLE.

    That is the C2 self-suppression class `_RESERVED_ID_PREFIXES` exists for:
    an agent that pre-creates the id in its own `~/.trinity/operator-queue.json`
    silences the platform's alarm through the sink's ON CONFLICT. The same tuple
    also drives `is_platform_minted`, which gates the ent#499 responded
    write-back and the ent#329 respond→resume dispatch — so an unreserved
    platform id is mis-classified as agent-authored on both.
    """
    from services import operator_queue_service as oqs

    _freeze_clock(monkeypatch)
    items, _ = _wire(monkeypatch, TERMINAL_URL)
    service = _service()
    service._adopt_legacy_clone()

    assert items
    _assert_terminal_branch(items[0])
    item_id = items[0]["id"]

    assert any(item_id.startswith(p) for p in oqs._RESERVED_ID_PREFIXES), (
        f"{item_id!r} uses no reserved prefix — an agent can pre-create it and "
        "suppress the platform's own alarm (#1632 C2)"
    )
    digest = item_id[len(STABLE_PREFIX):]
    assert re.fullmatch(r"[0-9a-f]{12}", digest), (
        "the id segment is not a 12-char sha256 prefix"
    )
    assert TERMINAL_URL not in item_id, "the raw URL must never be the conflict key"
    assert _ID_RE.match(item_id) and len(item_id) <= _DB_BELT_ID_MAX


# ---------------------------------------------------------------------------
# 6. The URL echo is credential-scrubbed
# ---------------------------------------------------------------------------

def test_a_pat_bearing_url_is_never_echoed_into_the_log_or_the_queue(
    monkeypatch, caplog
):
    """`EmbeddedCredentialError` is a `ValueError` subclass, so the branch that
    fires on a PAT-bearing URL is exactly the one that passes the RAW url to the
    emitter — which logged it at ERROR (Vector-captured) and stored it in
    `operator_queue.context`, i.e. in SQLite, in every backup, and rendered in
    the Operating Room. Invariant #12 / Rule #5."""
    _freeze_clock(monkeypatch)
    items, created = _wire(monkeypatch, PAT_URL, count=0)
    service = _service()

    with caplog.at_level(logging.INFO, logger="services.skill_service"):
        assert service._adopt_legacy_clone() is None

    assert created == [], "a credential-bearing URL must never become a source"
    assert items, "the refusal must be observable"
    assert "URL rejected" in items[0]["question"], "drove the wrong branch"
    assert PAT not in items[0]["context"]["url"], (
        "the PAT is durable in operator_queue.context — every dump, backup and "
        "replica carries a usable token (CWE-312)"
    )
    assert items[0]["context"]["url"] == "https://github.com/acme/private-skills"
    assert PAT not in caplog.text, "the PAT reached the Vector-captured log"
    # `question` is credential-free only because neither ent#346 validator
    # echoes the URL in its `ValueError` (`validate_skills_library_url` names
    # the hostname or the resolved IP; `reject_embedded_credentials` names
    # neither) — the scrub covers the log line and `context`, not the message.
    # Sweep the WHOLE item so a validator that starts echoing the URL fails
    # here instead of persisting a PAT into `operator_queue.question`.
    assert PAT not in json.dumps(items[0], default=str), (
        "the PAT survived somewhere in the emitted item"
    )
