"""Behavioural half of the #1880 canary alert guard.

`test_1880_canary_alert_parity.py` proves every registry invariant *has* an
entry in all four alert surfaces. It reads source via `ast` and therefore
cannot see two failure modes that produce the same user-visible outcome as
having no entry at all:

1. **A branch that exists but raises.** Eight of the pre-existing
   `_render_message` branches do `sorted({v.observed_state.get("agent_name")
   for v in violations})`, and `sorted({None, "a"})` raises `TypeError`. That
   raise is swallowed by `canary_service`'s transition loop, which records the
   transition regardless — so the alert is lost, the green→red cursor
   advances, and continuing-red gating means nothing ever retries. One
   exception silences the entire red episode.

2. **A payload Slack rejects.** `header.text` (plain_text) caps at 150 chars
   and `section.text` at 3000; over either, Slack 400s and drops the *whole*
   message — same terminal silence. E-03/G-03 are bounded only by the
   5000-row terminal cap and E-06 by schedule count, so these are the first
   invariants that can realistically produce thousands of violations in one
   cycle. That is exactly the fleet-wide 2am event the canary exists for.

Unlike the parity module this one must call the renderers, so it imports.
Imports are lazy (inside each test) so a `services` stub-leak under
pytest-randomly degrades these tests only and never takes the parity gate
down with it.
"""
from __future__ import annotations

import json

import pytest

_SNAP = "2026-07-30T12:00:00Z"

# Worst case on purpose: "critical" is the longest severity string, so the
# header-length assertions below are measured against the widest header the
# composer can emit for a given name.
_SEVERITY = "critical"

# Realistic `observed_state` per invariant, keyed to what each render branch
# actually reads. Sourced from the `ViolationReport(...)` construction in each
# `canary/invariants/*.py`. A new invariant with no entry here fails
# `test_fixtures_cover_the_registry` — a second fail-closed gate behind the
# AST parity guard.
_OBSERVED_STATE = {
    "S-01": {
        "agent_name": "agent-a",
        "redis_slot_count": 2,
        "sql_running_count": 1,
        "in_redis_only": ["exec-1"],
        "in_sql_only": [],
    },
    "S-02": {
        "agent_name": "agent-a",
        "max_parallel_tasks": 3,
        "slot_count": 5,
        "overbooked_by": 2,
    },
    "S-03": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "redis_ttl_seconds": 10,
        "floor_seconds": 3900,
        "kind": "below_floor",
    },
    "E-01": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "age_seconds": 4200,
        "execution_timeout_seconds": 3600,
        "slot_ttl_buffer_seconds": 300,
    },
    "E-02": {
        "execution_id": "exec-1",
        "previous_status": "success",
        "current_status": "running",
    },
    "E-03": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "status": "success",
        "started_at": "2026-07-30T11:00:00Z",
        # NULL by definition on this path — that IS the violation.
        "completed_at": None,
        "snapshot_time": _SNAP,
    },
    "E-04": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "reason": "backlog_metadata_invalid_json",
        "snapshot_time": _SNAP,
    },
    "E-05": {"agent_name": "agent-a", "execution_id": "exec-1", "age_seconds": 120},
    "E-06": {
        "agent_name": "agent-a",
        "schedule_id": "sched-1",
        "next_run_at": "2026-07-25T10:00:00Z",
        "snapshot_time": _SNAP,
        "overdue_seconds": 432000,
        "misfire_grace_seconds": 300,
    },
    "G-03": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "status": "success",
        "started_at": "2026-07-30T10:05:00Z",
        "completed_at": "2026-07-30T10:00:00Z",
        "skew_seconds": 300.0,
        "snapshot_time": _SNAP,
    },
    "G-04": {
        "agent_name": "agent-a",
        "execution_id": "exec-1",
        "matched_pattern": "github_pat",
        "snapshot_time": _SNAP,
    },
    "L-03": {
        "ghost_agent_name": "deleted-agent",
        "tables_hit": ["agent_schedules"],
        "sample_refs": [
            {"table": "agent_schedules", "column": "agent_name", "row_id": "sched-1"}
        ],
    },
    "B-01": {"agent_name": "agent-a", "service_count": 3, "snapshot_count": 5},
    "B-02": {
        "agent_name": "agent-a",
        "queued_count": 4,
        "free_slots": 2,
        "drain_tick_age_seconds": 900,
    },
    "R-01": {"agent_name": "agent-a", "zombie_count": 3},
}


def _violation(invariant_id, observed_state):
    from canary.snapshot import ViolationReport

    return ViolationReport(
        invariant_id=invariant_id,
        tier="A",
        severity=_SEVERITY,
        observed_state=observed_state,
        signal_query=f"{invariant_id} synthetic fixture",
    )


def _payload(invariant_id, violations):
    from services.canary_alerts import CanaryAlerts

    return CanaryAlerts._build_slack_payload(
        invariant_id=invariant_id,
        violations=violations,
        snapshot_time=_SNAP,
        previous_violation_at=None,
        severity=_SEVERITY,
        persisted_ids=[1],
    )


def _registry_ids():
    from canary.invariants import INVARIANTS

    return set(INVARIANTS)


def test_fixtures_cover_the_registry():
    """A new invariant must arrive with a fixture, or these tests go blind."""
    missing = sorted(_registry_ids() - set(_OBSERVED_STATE))
    assert not missing, (
        f"no render fixture for {missing}. Add its `observed_state` shape to "
        f"_OBSERVED_STATE (copy it from the ViolationReport(...) construction "
        f"in canary/invariants/) so the rendering tests below actually "
        f"exercise the new branch instead of skipping it."
    )


@pytest.mark.parametrize("invariant_id", sorted(_OBSERVED_STATE))
def test_every_invariant_renders_without_raising(invariant_id):
    """No branch may raise. A raise here is a silently-dropped alert."""
    payload = _payload(invariant_id, [_violation(invariant_id, _OBSERVED_STATE[invariant_id])])
    assert payload is not None


@pytest.mark.parametrize("invariant_id", sorted(_OBSERVED_STATE))
def test_every_invariant_renders_a_real_summary_not_the_fallback(invariant_id):
    """The #1880 symptom, asserted directly against the composed payload."""
    violations = [_violation(invariant_id, _OBSERVED_STATE[invariant_id])]
    text, blocks = _payload(invariant_id, violations)

    fallback = f"{invariant_id} fired {len(violations)} violation(s)."
    body = next(b for b in blocks if b["type"] == "section")["text"]["text"]
    assert body != fallback, (
        f"{invariant_id} still renders the generic `_render_message` fallback"
    )

    header = next(b for b in blocks if b["type"] == "header")["text"]["text"]
    assert f"{invariant_id} {invariant_id}" not in header, (
        f"{invariant_id}: header stutters the id, which means "
        f"_INVARIANT_NAMES.get(id, id) fell through to its default"
    )

    # A runbook block and a forensic block are both expected — the runbook is
    # the italic trailer, the forensic block is the per-row evidence.
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) >= 3, (
        f"{invariant_id}: expected summary + forensic + runbook sections, got "
        f"{len(sections)}. A missing block means _render_forensic returned "
        f"None or _INVARIANT_RUNBOOKS has no entry."
    )


@pytest.mark.parametrize("invariant_id", sorted(_OBSERVED_STATE))
def test_header_fits_slack_plain_text_limit(invariant_id):
    """Slack caps a `header` plain_text block at 150 chars; over it, 400."""
    _text, blocks = _payload(invariant_id, [_violation(invariant_id, _OBSERVED_STATE[invariant_id])])
    header = next(b for b in blocks if b["type"] == "header")["text"]["text"]
    assert len(header) <= 150, (
        f"{invariant_id}: header is {len(header)} chars (>150). Slack rejects "
        f"the entire message, and the transition is still recorded — so the "
        f"alert is lost with no retry. Shorten the _INVARIANT_NAMES entry."
    )


@pytest.mark.parametrize("invariant_id", ["E-03", "G-03", "E-06", "G-04", "E-04"])
def test_payload_stays_bounded_at_fleet_scale(invariant_id):
    """5000 violations across 500 agents must still produce a sendable payload.

    E-03/G-03 iterate the terminal-row set (capped at 5000) and E-06 every
    enabled schedule, so a systemic producer regression fires on every row at
    once. Without the `[:160]` summary slice and the `violations[:5]` forensic
    slice, the section blows Slack's 3000-char limit and the alert vanishes
    during precisely the fleet-wide event it exists to report.
    """
    violations = []
    for i in range(5000):
        state = dict(_OBSERVED_STATE[invariant_id])
        state["agent_name"] = f"agent-{i % 500:03d}"
        state["execution_id"] = f"exec-{i}"
        state["schedule_id"] = f"sched-{i}"
        violations.append(_violation(invariant_id, state))

    text, blocks = _payload(invariant_id, violations)

    header = next(b for b in blocks if b["type"] == "header")["text"]["text"]
    assert len(header) <= 150, f"{invariant_id}: header {len(header)} chars at scale"
    for block in blocks:
        if block["type"] == "section":
            size = len(block["text"]["text"])
            assert size <= 3000, (
                f"{invariant_id}: a section block is {size} chars at 5000 "
                f"violations (>3000). Slack rejects the whole message. Apply "
                f"the `[:160]` summary slice / `violations[:5]` forensic slice."
            )
    assert len(blocks) <= 50, f"{invariant_id}: {len(blocks)} blocks (>50)"


# Keys that feed a `sorted({...})` set-comprehension in `_render_message`.
# `sorted({None, "a"})` raises TypeError — see the module docstring for why
# that costs the whole alert.
_SORT_KEYS = ("agent_name", "ghost_agent_name", "kind", "reason", "matched_pattern")


@pytest.mark.parametrize("invariant_id", sorted(_OBSERVED_STATE))
def test_null_identity_fields_never_raise(invariant_id):
    """A NULL in a sorted() field must degrade to "?", not kill the alert.

    Every branch reads these off a collector row mapping or an
    `AgentSnapshot`. Production columns are NOT NULL, so this is latent
    rather than live — but the blast radius is total: the `TypeError`
    propagates to `canary_service`'s per-transition `except Exception`, which
    logs and *still* records the transition, so the green→red cursor advances
    and continuing-red gating guarantees no retry. One NULL silences the
    entire red episode for that invariant.

    Locks in the #1880 sweep that coerced all ten pre-existing sites plus the
    five new ones. Without this, a later edit reverts to the bare `.get()`
    shape and nothing notices.
    """
    state = dict(_OBSERVED_STATE[invariant_id])
    for key in _SORT_KEYS:
        if key in state:
            state[key] = None

    # Two violations so the set-comprehension actually has to order a pair —
    # a single-element set never invokes the comparison that raises.
    other = dict(state)
    other["execution_id"] = "exec-2"
    violations = [_violation(invariant_id, state), _violation(invariant_id, other)]

    text, blocks = _payload(invariant_id, violations)
    assert "None" not in text, (
        f"{invariant_id}: a NULL identity field rendered as the literal "
        f'"None". `.get(k, "?")` does NOT fire on an explicit None value — '
        f'coerce with `or "?"`.'
    )


# Slack markup an attacker would want in an operator alert channel. Each is a
# real capability, not a cosmetic: `<url|text>` renders a live clickable link
# (phishing), `<!channel>` / `<!here>` ping everyone in the channel, and a
# newline can forge an extra bullet that looks like a violation nobody saw.
_HOSTILE_NAMES = [
    "<https://evil.example|CLICK HERE TO FIX>",
    "<!channel>",
    "<!here> urgent",
    "<@U01ADMIN>",
    "a\n  • *fabricated* `row-999`: matched `aws_access_key_id`",
    "x&y",
]


@pytest.mark.parametrize("hostile", _HOSTILE_NAMES)
@pytest.mark.parametrize("invariant_id", ["G-04", "E-03", "L-03", "S-01"])
def test_hostile_identity_field_cannot_forge_slack_markup(invariant_id, hostile):
    """Escape at the render boundary, not only at write time (#1880 N1).

    Agent names are sanitized on every creation/rename path today, so nothing
    hostile can actually reach here. That guarantee is *transitive* across ~8
    call sites though, and it has a known counterexample: `retention_guard.py`
    writes an `agent_name` (`_retention-guard`) that `sanitize_agent_name`
    could never produce, precisely because sanitization strips a leading
    underscore. This test makes the property local and structural instead —
    the renderer stops depending on who wrote the row.

    Asserts the *capability* is gone, not that a particular escape was used:
    no live link, no channel-wide mention, no forged bullet line.
    """
    state = dict(_OBSERVED_STATE[invariant_id])
    for key in ("agent_name", "ghost_agent_name"):
        if key in state:
            state[key] = hostile
    if invariant_id == "L-03":
        state["tables_hit"] = [hostile]
        state["sample_refs"] = [{"table": hostile, "column": hostile, "row_id": hostile}]

    text, blocks = _payload(invariant_id, [_violation(invariant_id, state)])
    rendered = text + json.dumps(blocks)

    # The capability, not a proxy for it. Slack parses `<...>` and nothing
    # else into links and mentions — `<url|text>`, `<!channel>`, `<@U…>` all
    # require the angle brackets. Strip those and the markup is inert literal
    # text, so THAT is what to assert. A surviving `|` or `!` is harmless on
    # its own and asserting their absence would be a proxy that fails on
    # correctly-escaped output (it did, on the first run of this test).
    assert "<" not in rendered and ">" not in rendered, (
        f"{invariant_id}: an unescaped angle bracket survived {hostile!r}. "
        f"Slack would parse <url|text> as a live clickable link and "
        f"<!channel> as a channel-wide mention, inside an operator alert."
    )
    if "<" in hostile:
        assert "&lt;" in rendered, (
            f"{invariant_id}: the bracket was dropped rather than escaped — "
            f"the value must survive as readable literal text, or the alert "
            f"loses the very identifier the responder needs."
        )

    # A newline in an identity field must not forge a second bullet line.
    section_texts = [
        b["text"]["text"] for b in blocks if b["type"] == "section"
    ]
    for body in section_texts:
        assert "fabricated" not in body or "\n  • *fabricated*" not in body, (
            f"{invariant_id}: a newline in an identity field forged a bullet "
            f"line that looks like a real violation row."
        )

    # And the escape must not have destroyed the alert.
    assert text and blocks
    header = next(b for b in blocks if b["type"] == "header")["text"]["text"]
    assert len(header) <= 150


def test_mrkdwn_safe_contract():
    """Direct unit coverage of the render-boundary helper."""
    from services.canary_alerts import _mrkdwn_safe

    # absent / empty / whitespace-only → fallback (the `.get(k, "?")` trap:
    # a default does NOT fire on a key present with value None)
    assert _mrkdwn_safe(None) == "?"
    assert _mrkdwn_safe("") == "?"
    assert _mrkdwn_safe("   ") == "?"
    assert _mrkdwn_safe(None, fallback="unknown") == "unknown"

    # Slack's documented escape set — the security-relevant three
    assert _mrkdwn_safe("<https://evil.example|X>") == "&lt;https://evil.example|X&gt;"
    assert _mrkdwn_safe("a&b") == "a&amp;b"
    # `&` escaped first, so an escape sequence is not double-mangled
    assert _mrkdwn_safe("&lt;") == "&amp;lt;"

    # control characters collapse — no forged structure lines
    assert "\n" not in _mrkdwn_safe("a\nb")
    assert "\t" not in _mrkdwn_safe("a\tb")

    # non-str coerced, ordinary values pass through untouched
    assert _mrkdwn_safe(42) == "42"
    assert _mrkdwn_safe("my-agent_1.2") == "my-agent_1.2"


def test_unrendered_invariant_never_dumps_its_observed_state():
    """Pin the state-free fallbacks — they are a security property, not debt.

    The parity gate creates standing pressure toward the one-line way to
    satisfy it forever: a generic `observed_state.items()` renderer. That
    single change converts a safe-by-default fallback into leak-by-default for
    every invariant added afterwards, and relocates the trust boundary away
    from the check — where E-04 and G-04 deliberately put it. It would also be
    near-invisible in review, because it *removes* a special case.
    """
    from services.canary_alerts import CanaryAlerts

    sentinel = "SENTINEL-MUST-NEVER-REACH-SLACK"
    violations = [
        _violation("Z-99", {"agent_name": "agent-a", "leaky_field": sentinel})
    ]

    body = CanaryAlerts._render_message("Z-99", violations, _SNAP)
    assert body == "Z-99 fired 1 violation(s).", (
        "the _render_message fallback must stay count-only and state-free"
    )
    assert CanaryAlerts._render_forensic("Z-99", violations) is None, (
        "the _render_forensic fallback must stay None — no generic dump"
    )

    text, blocks = _payload("Z-99", violations)
    assert sentinel not in text
    assert sentinel not in json.dumps(blocks)


@pytest.mark.parametrize("invariant_id", ["E-04", "G-04"])
def test_scrubbed_invariants_render_only_whitelisted_keys(invariant_id):
    """E-04/G-04 must read named keys, never iterate `observed_state`.

    Both checks scrub at the invariant: E-04 emits a reason code, G-04 a
    pattern name, and neither ever puts `backlog_metadata` in
    `observed_state`. This asserts the *render* side holds up its end — an
    extra key must not reach the payload, so a future invariant that stores
    richer state cannot leak through a branch written for these two.
    """
    sentinel = "SENTINEL-MUST-NEVER-REACH-SLACK"
    state = dict(_OBSERVED_STATE[invariant_id])
    state["backlog_metadata"] = sentinel
    state["raw_value"] = sentinel

    text, blocks = _payload(invariant_id, [_violation(invariant_id, state)])
    assert sentinel not in text, f"{invariant_id}: sentinel reached the text fallback"
    assert sentinel not in json.dumps(blocks), (
        f"{invariant_id}: sentinel reached a Block Kit block. The branch must "
        f"read named keys only — never iterate observed_state."
    )
