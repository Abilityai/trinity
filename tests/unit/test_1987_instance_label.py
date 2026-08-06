"""Instance attribution on outbound alerts (#1987).

A Slack incoming webhook carries no sender identity, so before this every
canary green→red alert said *what* fired and never *where*. Two instances now
post to the same channel (`dev`, plus `eu2` for the #1766 soak), which makes
that ambiguity actively expensive: the alert is a one-shot — continuing-red
does not re-post — so whatever is not in it is not recoverable from a later
message.

Three properties are pinned here, in the order they can break:

1. **The resolver's ladder**, including every degradation. A tier that raises
   instead of falling through would take the alert down with it, which trades
   a mild problem (unlabelled) for the failure mode the whole sink exists to
   avoid (silent).
2. **The label reaches the visible part of the payload** — header *and* the
   `text` fallback, since that fallback is what a mobile push shows and lock
   screen triage is the motivating case.
3. **The compose injection**, because a documented `.env` lever that reaches
   no container is this repo's most-repeated packaging bug (#1039, #1056,
   #1871, and the canary vars themselves — see
   `test_canary_env_prod_parity.py`, whose shape this mirrors).

Imports are lazy (inside each test) to match the sibling #1880 modules: a
`services` stub-leak under pytest-randomly then degrades these tests only.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_SNAP = "2026-08-04T12:00:00Z"

_ENV_VARS = ("TRINITY_INSTANCE_NAME", "FRONTEND_URL")


@pytest.fixture
def clean_env(monkeypatch):
    """Neither env var set, so each test declares exactly the tier it exercises.

    Without this the result depends on the developer's shell (`FRONTEND_URL`
    is a normal thing to have exported), and the all-absent fallback test in
    particular would pass for the wrong reason.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _resolver():
    from services import instance_identity

    return instance_identity


def _stub_installation_id(monkeypatch, value=None, raises=False):
    """Seed `services.operator_intake_service` so tier 3 is hermetic.

    The real accessor reads (and can write) `system_settings`; the resolver
    imports it lazily, so seeding `sys.modules` intercepts it without a DB.
    """
    fake = types.ModuleType("services.operator_intake_service")

    def _get():
        if raises:
            raise RuntimeError("system_settings unavailable")
        return value

    fake.get_or_create_installation_id = _get
    monkeypatch.setitem(sys.modules, "services.operator_intake_service", fake)


# ---------------------------------------------------------------------------
# Tier 1 — explicit override
# ---------------------------------------------------------------------------


def test_override_wins_over_every_other_tier(clean_env, monkeypatch):
    monkeypatch.setenv("TRINITY_INSTANCE_NAME", "soak-pilot")
    monkeypatch.setenv("FRONTEND_URL", "https://eu2.abilityai.dev")
    _stub_installation_id(monkeypatch, value="deadbeefcafe")

    assert _resolver().get_instance_label() == "soak-pilot"


def test_override_is_trimmed(clean_env, monkeypatch):
    monkeypatch.setenv("TRINITY_INSTANCE_NAME", "  eu2  ")
    assert _resolver().get_instance_label() == "eu2"


def test_unusable_override_falls_through_rather_than_blanking_the_label(
    clean_env, monkeypatch
):
    """A garbage override must not cost the alert its attribution.

    Returning "" for a set-but-unusable value would be the worst of both:
    the operator thinks they labelled the instance, and the alert is anonymous
    anyway. Sanitizing to nothing reads as "this tier produced nothing".
    """
    monkeypatch.setenv("TRINITY_INSTANCE_NAME", "!!!")
    monkeypatch.setenv("FRONTEND_URL", "https://eu2.abilityai.dev")

    assert _resolver().get_instance_label() == "eu2"


# ---------------------------------------------------------------------------
# Tier 2 — derived from FRONTEND_URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frontend_url,expected",
    [
        ("https://eu2.abilityai.dev", "eu2"),
        ("https://dev.abilityai.dev/", "dev"),
        ("https://eu2.abilityai.dev:8443", "eu2"),
        ("http://localhost", "localhost"),
        ("http://localhost:5173", "localhost"),
        # No scheme: urlparse reads this as a *path* and yields no hostname at
        # all. A plain host is a plausible .env value, so it is re-parsed.
        ("eu2.abilityai.dev", "eu2"),
        # Case-folded by urlparse — the label must not vary with .env casing.
        ("https://EU2.Abilityai.Dev", "eu2"),
    ],
)
def test_derives_first_dns_label_from_frontend_url(
    clean_env, monkeypatch, frontend_url, expected
):
    """On managed instances the first DNS label *is* the ops slug."""
    monkeypatch.setenv("FRONTEND_URL", frontend_url)
    assert _resolver().get_instance_label() == expected


@pytest.mark.parametrize(
    "frontend_url,expected",
    [
        ("http://10.0.0.5:8000", "10.0.0.5"),
        ("https://192.168.1.20", "192.168.1.20"),
        ("http://[2001:db8::1]:8000", "2001:db8::1"),
    ],
)
def test_ip_host_keeps_the_whole_address(
    clean_env, monkeypatch, frontend_url, expected
):
    """The first label of `10.0.0.5` is `10` — worse than nothing.

    A truncated octet looks like a name, so an on-call would read it as one.
    An IP literal is kept whole.
    """
    monkeypatch.setenv("FRONTEND_URL", frontend_url)
    assert _resolver().get_instance_label() == expected


@pytest.mark.parametrize("frontend_url", ["", "   ", "https://", "http://[oops"])
def test_unusable_frontend_url_falls_through_to_installation_id(
    clean_env, monkeypatch, frontend_url
):
    """Including the malformed-IPv6 bracket, on which urlparse raises."""
    monkeypatch.setenv("FRONTEND_URL", frontend_url)
    _stub_installation_id(monkeypatch, value="0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")

    assert _resolver().get_instance_label() == "0f1e2d3c"


# ---------------------------------------------------------------------------
# Tier 3 — installation id, and the all-absent floor
# ---------------------------------------------------------------------------


def test_falls_back_to_installation_id_prefix(clean_env, monkeypatch):
    """An OSS install with no URL configured still gets *something* distinct."""
    _stub_installation_id(monkeypatch, value="9c3f00a1-1111-2222-3333-444455556666")

    assert _resolver().get_instance_label() == "9c3f00a1"


def test_all_tiers_absent_returns_none_without_raising(clean_env, monkeypatch):
    """The floor is today's unlabelled alert, never a crash.

    `emit_transition` runs after the violation row is already persisted; a
    raise here would lose the alert while the green→red cursor still advances,
    and continuing-red gating means nothing ever retries it.
    """
    _stub_installation_id(monkeypatch, value=None)

    assert _resolver().get_instance_label() is None


def test_installation_id_failure_degrades_to_none(clean_env, monkeypatch):
    """Tier 3 touches the DB; a DB error must cost the label, not the alert."""
    _stub_installation_id(monkeypatch, raises=True)

    assert _resolver().get_instance_label() is None


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # `<!channel>` mass-pings everyone in the alert channel and `<url|text>`
        # renders as a live link; neither survives, so a label cannot forge
        # Slack markup in a message whose entire job is attribution.
        ("<!channel>", "channel"),
        ("<https://evil.example|dev>", "https: evil.example dev"),
        ("<@U123>", "U123"),
        # Newlines would let a label forge an extra line of alert body.
        ("eu2\nrogue line", "eu2 rogue line"),
        ("eu2\t\tprod", "eu2 prod"),
        # Hostname-shaped punctuation is kept — these are the realistic labels.
        ("eu2.abilityai.dev", "eu2.abilityai.dev"),
        ("prod-eu_2", "prod-eu_2"),
        ("2001:db8::1", "2001:db8::1"),
        # Nothing usable → None, which the resolver reads as "next tier".
        ("", None),
        ("   ", None),
        ("///", None),
        (None, None),
    ],
)
def test_sanitizer_contract(raw, expected):
    assert _resolver().sanitize_instance_label(raw) == expected


def test_sanitizer_bounds_length():
    """An unbounded label is an unsendable payload, not a cosmetic problem."""
    identity = _resolver()
    out = identity.sanitize_instance_label("e" * 500)
    assert out is not None
    assert len(out) == identity._MAX_LABEL_CHARS


def test_sanitizer_rejects_non_ascii_homoglyphs():
    """`str.isalnum()` is true for Cyrillic — `еu2` must not read as `eu2`.

    Impersonating another instance in the header of an attribution alert is
    the one spoof that matters here.
    """
    # U+0435 CYRILLIC SMALL LETTER IE, visually identical to ASCII "e".
    assert _resolver().sanitize_instance_label("еu2") == "u2"


# ---------------------------------------------------------------------------
# Payload — the label has to be somewhere a human actually looks
# ---------------------------------------------------------------------------


def _payload(instance_label):
    from canary.snapshot import ViolationReport
    from services.canary_alerts import CanaryAlerts

    violation = ViolationReport(
        invariant_id="S-01",
        tier="A",
        severity="critical",
        observed_state={
            "agent_name": "agent-a",
            "redis_slot_count": 2,
            "sql_running_count": 1,
            "in_redis_only": ["exec-1"],
            "in_sql_only": [],
        },
        signal_query="S-01 synthetic fixture",
    )
    return CanaryAlerts._build_slack_payload(
        invariant_id="S-01",
        violations=[violation],
        snapshot_time=_SNAP,
        previous_violation_at=None,
        severity="critical",
        persisted_ids=[1],
        instance_label=instance_label,
    )


def _header(blocks):
    return next(b for b in blocks if b["type"] == "header")["text"]["text"]


def test_label_lands_in_header_and_text_fallback():
    """Both, deliberately: the fallback is what a push notification renders."""
    text, blocks = _payload("eu2")

    assert "[eu2]" in _header(blocks), "header must name the instance"
    assert "[eu2]" in text, "text fallback (mobile push) must name the instance"
    # Still an S-01 alert — the label is a prefix, not a replacement.
    assert "S-01" in _header(blocks) and "S-01" in text


def test_absent_label_renders_todays_unlabelled_payload():
    """No label must not leave an empty `[]` or a stray separator."""
    text, blocks = _payload(None)

    assert "[" not in _header(blocks)
    assert text.startswith("🚨 canary S-01")


def test_two_instances_produce_visibly_distinct_alerts():
    """AC: one webhook, two instances, tellable apart at a glance."""
    eu2_text, eu2_blocks = _payload("eu2")
    dev_text, dev_blocks = _payload("dev")

    assert eu2_text != dev_text
    assert _header(eu2_blocks) != _header(dev_blocks)


def test_header_stays_within_slack_limit_with_a_max_length_label():
    """150-char plain_text cap. Over it Slack 400s and drops the whole message,
    while the transition is recorded as alerted — a silently lost alert."""
    identity = _resolver()
    _text, blocks = _payload("e" * identity._MAX_LABEL_CHARS)

    assert len(_header(blocks)) <= 150


def test_payload_re_sanitizes_a_hostile_label_from_any_caller():
    """The composer does not trust its argument.

    The resolver is the only producer today, so this is defence in depth —
    the same argument `_mrkdwn_safe` makes for escaping at the render boundary
    rather than relying on every write path staying well-behaved.
    """
    text, blocks = _payload("<!channel>")

    assert "<!channel>" not in text
    assert "<!channel>" not in _header(blocks)
    assert "[channel]" in text


# ---------------------------------------------------------------------------
# End-to-end through the real sink
# ---------------------------------------------------------------------------


def _emit(monkeypatch, webhook_url="https://hooks.slack.com/services/T0/B0/s3cr3tPath"):
    """Drive `emit_transition` against a recorded sink; return the POSTs.

    `emit_transition` does the env → label resolution, so this is the only
    place the wiring itself is proved. The lazy
    `from services.slack_service import slack_service` inside it resolves
    through `sys.modules`, so seeding the entry captures the call without an
    httpx client — the same trick `tests/test_canary_invariants.py` uses (whose
    own copy of this cannot run in the sibling suite's environment).
    """
    import asyncio

    from services.canary_alerts import CanaryAlerts
    from canary.snapshot import ViolationReport

    calls = []

    class _Recorder:
        async def post_webhook(self, url, text, blocks=None, timeout_seconds=5.0):
            calls.append({"url": url, "text": text, "blocks": blocks})
            return True, None

    fake = types.ModuleType("services.slack_service")
    fake.slack_service = _Recorder()
    monkeypatch.setitem(sys.modules, "services.slack_service", fake)
    monkeypatch.setenv("CANARY_SLACK_WEBHOOK_URL", webhook_url)

    violation = ViolationReport(
        invariant_id="S-01",
        tier="A",
        severity="critical",
        observed_state={
            "agent_name": "agent-a",
            "redis_slot_count": 2,
            "sql_running_count": 1,
            "in_redis_only": ["exec-1"],
            "in_sql_only": [],
        },
        signal_query="S-01 synthetic fixture",
    )
    asyncio.run(
        CanaryAlerts.emit_transition(
            invariant_id="S-01",
            violations=[violation],
            snapshot_time=_SNAP,
            previous_violation_at=None,
            persisted_ids=[1],
        )
    )
    return calls


def test_emit_transition_labels_the_alert_from_the_environment(clean_env, monkeypatch):
    """The wiring, not just the composer: env → resolver → payload → wire."""
    monkeypatch.setenv("FRONTEND_URL", "https://eu2.abilityai.dev")

    calls = _emit(monkeypatch)

    assert len(calls) == 1
    assert "[eu2]" in calls[0]["text"]
    assert "[eu2]" in _header(calls[0]["blocks"])


def test_emit_transition_still_alerts_when_nothing_identifies_the_instance(
    clean_env, monkeypatch
):
    """No label is a degraded alert, never a missing one."""
    _stub_installation_id(monkeypatch, value=None)

    calls = _emit(monkeypatch)

    assert len(calls) == 1, "an unidentifiable instance must still alert"
    assert "S-01" in calls[0]["text"]


def test_emit_transition_never_logs_the_webhook_url(clean_env, monkeypatch, caplog):
    """Pre-existing property of `canary_alerts` (#1987 AC) — the URL IS the
    credential: anyone holding it can post to that channel. Adding a label to
    the payload must not tempt a log line that echoes the target."""
    import logging

    monkeypatch.setenv("FRONTEND_URL", "https://eu2.abilityai.dev")
    caplog.set_level(logging.DEBUG)

    _emit(monkeypatch)

    assert "s3cr3tPath" not in caplog.text
    assert "hooks.slack.com" not in caplog.text


# ---------------------------------------------------------------------------
# Packaging — the lever has to reach the container
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_instance_name_is_injected_in_both_composes(compose_file):
    """`docker-compose.prod.yml` launches standalone — no base-compose merge and
    no `env_file:` — so its explicit `environment:` list is the only path in.
    Omitting the var there makes the override inert on every deployed instance
    while it works fine on a laptop, which is exactly how the canary vars
    themselves shipped broken for ~2.5 months (#1039 packaging-gap class)."""
    text = (_REPO_ROOT / compose_file).read_text(encoding="utf-8")
    lines = [
        ln
        for ln in text.splitlines()
        if "TRINITY_INSTANCE_NAME=${TRINITY_INSTANCE_NAME:-" in ln
        and not ln.strip().startswith("#")
    ]
    assert lines, (
        f"{compose_file}: no `TRINITY_INSTANCE_NAME=${{TRINITY_INSTANCE_NAME:-}}` "
        f"injection under backend.environment — the #1987 override would be inert."
    )


def test_env_example_documents_the_override():
    """Undocumented knobs are unusable knobs; .env.example is the catalog."""
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TRINITY_INSTANCE_NAME=" in text
