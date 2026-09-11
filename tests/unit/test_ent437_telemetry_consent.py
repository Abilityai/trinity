"""Opt-in instance telemetry — the ent#437 cut of the Tier-2 channel.

Service-level coverage of the schema-v2 payload, the share identity, the send
log, backfill-until-delivered, the tick marker, and the warm-ask memo; router-
level coverage of the human-only gates and the lazy preview. ``db`` / ``httpx``
are mocked (no real backend, no real egress). Mirrors the ent#12 harness
(`test_ent12_telemetry_sharing.py`) and the ent#463 router harness.

Locked behaviour (from the AC + the R1–R9 rulings on the issue):
  * The aggregate carries `sharing_id`, NEVER `installation_id` (banned by the
    validator; the builder never even reads it).
  * A payload outside `PAYLOAD_SCHEMA_V2` is refused and recorded, never sent.
  * Wire vocabularies are telemetry-owned: an unknown trigger bucket or status
    lands in `other` instead of halting egress.
  * Revoke deletes the share id; re-consent mints a different one.
  * The consent-time backfill is retried until the receiver first acknowledges.
  * Every reader is fenced: a stubbed `db` degrades a field, never the payload.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _fake_db(store: dict) -> MagicMock:
    """A settings store with REAL write-once semantics plus coarse readers."""
    mdb = MagicMock()
    mdb.get_setting_value.side_effect = lambda k, d=None: store.get(k, d)
    mdb.set_setting.side_effect = lambda k, v: store.update({k: v})
    mdb.delete_setting.side_effect = lambda k: store.pop(k, None)

    def _insert_if_absent(k, v):
        if k in store:
            return False
        store[k] = v
        return True
    mdb.insert_setting_if_absent.side_effect = _insert_if_absent

    mdb.get_fleet_execution_stats.return_value = {
        "total": 22, "success_count": 21, "failed_count": 1,
    }
    mdb.count_product_events_by_type.return_value = {"setup_started": 2}
    mdb.count_non_system_agents.return_value = 3
    mdb.get_fleet_execution_timeline.return_value = [{"bucket": "schedule"}]
    mdb.shape_execution_timeline.return_value = [
        {"bucket": "Scheduled", "total": 5, "success": 4, "failed": 1, "cost": 1.23, "context_used": 9},
        {"bucket": "Brand New Thing", "total": 2, "success": 2, "failed": 0, "cost": 0.5, "context_used": 1},
    ]
    mdb.count_terminal_executions_by_status.return_value = {"success": 21, "failed": 1, "weird": 3}
    mdb.get_failure_event_counts_by_subscription.return_value = {
        "sub-a": {"total": 3, "by_kind": {"rate_limit": 2, "auth": 1, "unknown": 4}},
        "sub-b": {"total": 1, "by_kind": {"rate_limit": 1}},
    }
    mdb.first_autonomous_success_at.return_value = None
    return mdb


@pytest.fixture
def tss():
    try:
        import services.telemetry_sharing_service as mod
    except ImportError:
        pytest.skip("backend venv required")
    store: dict = {}
    mdb = _fake_db(store)
    settings = MagicMock()
    settings.get_install_source.return_value = "script"
    with patch.object(mod, "db", mdb), \
         patch.object(mod, "settings_service", settings), \
         patch.object(mod, "resolve_release_version", return_value="0.9.5"):
        yield mod, store, mdb


def _fake_client(status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), client


# ---------------------------------------------------------------------------
# Payload v2
# ---------------------------------------------------------------------------

def test_payload_v2_validates_and_carries_the_new_fields(tss):
    mod, _store, _ = tss
    pl = mod.build_aggregate_payload(30, backfill=True, sharing_id="11111111-2222-4333-8444-555555555555")
    mod.validate_payload(pl)  # must not raise
    assert pl["schema_version"] == 2
    assert pl["instance"]["install_source"] == "script"
    assert pl["instance"]["trinity_version"] == "0.9.5"
    assert set(pl["instance"]) == {"trinity_version", "edition", "platform", "python_version", "install_source"}
    assert set(pl["outcomes"]) == {"by_trigger", "by_status", "provider_failures"}


def test_payload_never_carries_installation_id_and_never_reads_it(tss):
    mod, store, mdb = tss
    blob = json.dumps(mod.build_aggregate_payload(30, backfill=True)).lower()
    assert "installation_id" not in blob
    # The builder must not create identity by looking: a preview mints nothing.
    assert mod.KEY_SHARING_ID not in store


def test_preview_before_consent_shows_the_placeholder_id(tss):
    mod, store, _ = tss
    pl = mod.build_aggregate_payload(30, backfill=True)
    assert pl["sharing_id"] == mod.PREVIEW_SHARING_ID
    mod.validate_payload(pl)  # placeholder is validator-clean
    assert mod.KEY_SHARING_ID not in store


def test_by_trigger_projects_to_wire_keys_and_drops_cost(tss):
    mod, _store, _ = tss
    pl = mod.build_aggregate_payload(30, backfill=True)
    by_trigger = pl["outcomes"]["by_trigger"]
    assert by_trigger["schedule"] == {"total": 5, "success": 4, "failed": 1}
    assert by_trigger["other"] == {"total": 2, "success": 2, "failed": 0}
    blob = json.dumps(pl)
    assert "cost" not in blob and "context_used" not in blob
    assert "Scheduled" not in blob  # UI labels never reach the wire


def test_by_status_folds_unknown_into_other(tss):
    mod, _store, _ = tss
    pl = mod.build_aggregate_payload(30, backfill=True)
    assert pl["outcomes"]["by_status"] == {"success": 21, "failed": 1, "other": 3}


def test_provider_failures_two_keys_and_all_time_never_means_since_now(tss):
    mod, _store, mdb = tss
    pl = mod.build_aggregate_payload(0, backfill=True)  # "no history" ⇒ all-time
    assert pl["outcomes"]["provider_failures"] == {"rate_limit": 3, "auth": 1}
    # The reader's cutoff is unconditional: hours=0 would read "since now".
    (hours,), _ = mdb.get_failure_event_counts_by_subscription.call_args
    assert hours == mod._ALL_TIME_HOURS
    # The execution readers keep the ent#12 convention (0 = unbounded).
    assert mdb.get_fleet_execution_stats.call_args.kwargs["hours"] == 0


def test_stubbed_readers_degrade_fields_never_the_payload(tss):
    """The ent#12 harness leaves every NEW reader a bare MagicMock. The payload
    must still validate — a Mock must never reach the validator."""
    mod, _store, _ = tss
    blank = MagicMock()
    blank.get_setting_value.side_effect = lambda k, d=None: d
    with patch.object(mod, "db", blank), \
         patch.object(mod, "settings_service", MagicMock()), \
         patch.object(mod, "resolve_release_version", MagicMock()):
        pl = mod.build_aggregate_payload(30, backfill=True)
    mod.validate_payload(pl)
    assert pl["instance"]["install_source"] == "unknown"
    assert pl["instance"]["trinity_version"] == "unknown"
    assert pl["outcomes"] == {"by_trigger": {}, "by_status": {}, "provider_failures": {"rate_limit": 0, "auth": 0}}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _valid(mod):
    return mod.build_aggregate_payload(30, backfill=True, sharing_id="11111111-2222-4333-8444-555555555555")


@pytest.mark.parametrize("mutate,fragment", [
    (lambda p: p.__setitem__("surprise", 1), "undocumented key"),
    (lambda p: p["counts"].__setitem__("agents", "3"), "expected int"),
    (lambda p: p.__setitem__("installation_id", "x"), "installation_id"),
    (lambda p: p.__setitem__("sharing_id", "not-a-uuid"), "expected uuid"),
    (lambda p: p["outcomes"]["by_trigger"].__setitem__("Scheduled", {"total": 1, "success": 1, "failed": 0}), "vocabulary"),
    (lambda p: p["instance"].pop("install_source"), "missing"),
    (lambda p: p.__setitem__("schema_version", 1), "not the current version"),
])
def test_validator_rejects(tss, mutate, fragment):
    mod, _store, _ = tss
    pl = _valid(mod)
    mutate(pl)
    with pytest.raises(mod.TelemetryPayloadSchemaError) as ei:
        mod.validate_payload(pl)
    assert fragment in str(ei.value)


def test_wire_vocabulary_covers_every_db_trigger_bucket(tss):
    """Parity: every label the executions timeline can emit maps to a wire key,
    so a renamed/added product bucket cannot halt egress (it lands in `other`
    only when the map is silently stale — this test is what makes that loud)."""
    mod, _store, _ = tss
    from db.schedules.analytics import _BUCKET_ORDER
    missing = [b for b in _BUCKET_ORDER if b not in mod.TRIGGER_WIRE_KEYS]
    assert not missing, f"trigger buckets without a wire key: {missing}"
    assert set(mod.TRIGGER_WIRE_KEYS.values()) == mod.WIRE_TRIGGER_BUCKETS


def test_funnel_schema_derives_from_the_constant(tss):
    mod, _store, _ = tss
    from routers.product_events import ALLOWED_EVENT_TYPES
    assert set(mod._FUNNEL_STEPS) <= set(ALLOWED_EVENT_TYPES)
    assert set(mod.PAYLOAD_SCHEMA_V2["activation_funnel"]) == set(mod._FUNNEL_STEPS)


# ---------------------------------------------------------------------------
# share_now: refuse / record / deliver
# ---------------------------------------------------------------------------

def _consent_on(store):
    store["telemetry_sharing_enabled"] = "true"


def test_share_now_refuses_a_schema_violation_and_records_it(tss):
    mod, store, _ = tss
    _consent_on(store)
    bad = _valid(mod)
    bad["surprise"] = 1
    fake_ac, client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "build_aggregate_payload", return_value=bad), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        result = asyncio.run(mod.share_now(backfill=True))
    assert result is False
    fake_ac.assert_not_called()          # nothing left the box
    sends = mod.get_status()["recent_sends"]
    assert sends and sends[0]["ok"] is False and sends[0]["error"] == "schema"


def test_404_hint_is_judged_by_the_recorded_destination_then_by_the_current_url(tss):
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, _client = _fake_client(404)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", mod.DEFAULT_SHARE_URL), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now(backfill=True)) is False
        st = mod.get_status()
    assert st["recent_sends"][0]["http_status"] == 404
    assert st["recent_sends"][0]["ok"] is False
    assert st["receiver_hint"] == "receiver_not_live"
    assert st["last_shared_at"] is None and st["backfill_delivered_at"] is None
    # That 404 came from the DEFAULT address, and re-pointing the env var
    # afterwards does not change where it came from (#2571). The wording follows
    # the send; the panel is separately told the configured address has moved.
    with patch.object(mod, "TELEMETRY_SHARING_URL", "https://example.test/x"):
        moved = mod.get_status()
        assert moved["receiver_hint"] == "receiver_not_live"
        assert moved["destination_changed"] is True
        # A pre-#2571 entry records no destination, so it keeps the pre-#2571
        # reading: an overridden URL is worded as YOUR receiver, not the hosted
        # service. Old data never gains a claim it cannot support.
        mod._record_send({"sent_at": "t", "ok": False, "http_status": 404, "payload": {}})
        legacy = mod.get_status()
        assert legacy["receiver_hint"] == "receiver_404"
        assert legacy["destination_changed"] is False


def test_share_now_success_stamps_delivery_and_posts_the_share_id(tss):
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now(backfill=True)) is True
    body = client.post.call_args.kwargs["json"]
    assert _UUID.match(body["sharing_id"]) and body["sharing_id"] == store[mod.KEY_SHARING_ID]
    assert "installation_id" not in json.dumps(body)
    assert store.get(mod.KEY_LAST_SHARED_AT) and store.get(mod.KEY_BACKFILL_DELIVERED_AT)
    st = mod.get_status()
    assert st["recent_sends"][0]["ok"] is True and st["receiver_hint"] == "ok"


def test_send_log_records_transport_errors_by_class_not_text(tss):
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, client = _fake_client()
    client.post = AsyncMock(side_effect=RuntimeError("marker-that-must-never-be-logged"))
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now(backfill=True)) is False
    sends = mod.get_status()["recent_sends"]
    assert sends[0]["error"] == "RuntimeError"
    assert "marker-that-must-never" not in json.dumps(sends)


def test_send_log_is_bounded_and_survives_a_corrupt_row(tss):
    mod, store, _ = tss
    for i in range(7):
        mod._record_send({"sent_at": f"t{i}", "ok": False, "http_status": 404, "payload": {}})
    sends = mod.get_status()["recent_sends"]
    assert len(sends) == mod.RECENT_SENDS_LIMIT and sends[0]["sent_at"] == "t6"
    store[mod.KEY_RECENT_SENDS] = "{not json"
    assert mod.get_status()["recent_sends"] == []   # never a 500


# ---------------------------------------------------------------------------
# Where the send went (#2571)
# ---------------------------------------------------------------------------

#: (configured url, the origin recorded in the send log)
_ORIGINS = [
    # userinfo, path, query and fragment all dropped; case folded; :443 is https's default
    ("https://u:p@Intake.Example:443/v1/x?k=v#f", "https://intake.example"),
    ("http://h.test:8787/v1", "http://h.test:8787"),   # an explicit non-default port IS the address
    ("http://h.test:80/", "http://h.test"),            # the scheme's own default folds away
    ("https://host.test:0/x", "https://host.test"),    # ":0" reads as "no port given" (ent#398)
    ("https://[::1]:8443/x", "https://[::1]:8443"),    # an IPv6 literal keeps its brackets, skips IDNA
    ("https://tok@LEGACY@host.test/", "https://host.test"),      # the authority ends at the LAST "@"
    ("https://my_registry.example.com/x", "https://my_registry.example.com"),  # underscores: illegal
                                                                              # IDNA, legal operator URL
    ("", None),
    ("not a url", None),
    ("https://[oops/x", None),           # urlsplit itself raises on an unbalanced bracket
    (None, None),                        # never raises, whatever the caller holds
    ("https://h.test:notaport/", None),  # .port raises on a non-numeric port
    ("https://%D0%B0pple.com/x", None),  # a percent-encoded authority is unverifiable — never guessed
    ("example.com/api", None),           # scheme-less: httpx cannot send there either
]


def test_share_destination_is_an_origin_never_userinfo_path_or_query(tss):
    """The stored value is scheme + host + port and nothing else (AC 1): a path
    can carry a token, an origin cannot. Total over any input — it runs inside
    `share_now`, whose entry dict is built OUTSIDE its own try."""
    mod, _store, _ = tss
    for url, expected in _ORIGINS:
        got = mod.share_destination(url)
        assert got == expected, f"{url!r} -> {got!r}, expected {expected!r}"
        for secret in ("u:p", "tok", "LEGACY", "k=v", "/v1", "#f"):
            assert secret not in (got or ""), f"{secret!r} survived into {got!r}"


def test_the_same_host_written_two_ways_is_one_destination(tss):
    """The two sides of the mismatch signal are different inputs captured at
    different times, so one host retyped in another form must compare equal —
    a false "changed" prints "no send has gone there since" about an address
    sends are in fact reaching, which is the dishonest sentence this issue
    exists to delete."""
    mod, _store, _ = tss
    assert mod.share_destination("https://HOST.TEST./x") == mod.share_destination("https://host.test/x")
    assert mod.share_destination("https://fa\u00df.de/x") == mod.share_destination("https://xn--fa-hia.de/x")
    # An IP literal is one address however it is spelled (ent#399). Comparing
    # those textually is what permanently refused an IPv6 peer against its own
    # card; here it would print "no send has gone there since" about an address
    # every send is in fact reaching.
    assert mod.share_destination("https://[0:0:0:0:0:0:0:1]:8443/x") == mod.share_destination("https://[::1]:8443/x")
    assert mod.share_destination("https://[2606:4700:4700:0:0:0:0:1111]/x") \
        == mod.share_destination("https://[2606:4700:4700::1111]/x")
    # …and the folding never equates two addresses a resolver would separate:
    # `ipaddress` REFUSES the ambiguous IPv4 spellings instead of folding them,
    # and the port stays part of the address — that is the issue's own scenario.
    assert mod.share_destination("http://0177.0.0.1/x") != mod.share_destination("http://127.0.0.1/x")
    assert mod.share_destination("http://localhost:8787/x") != mod.share_destination("http://localhost/x")


def test_send_log_records_the_destination_at_send_time(tss):
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, _client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", "https://tok@host.test:8787/v1/telemetry-share?x=1"), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now(backfill=True)) is True
        st = mod.get_status()
    sends = st["recent_sends"]
    assert sends[0]["destination"] == "https://host.test:8787"
    persisted = store[mod.KEY_RECENT_SENDS]
    assert "tok" not in persisted and "x=1" not in persisted and "telemetry-share" not in persisted
    assert st["receiver_destination"] == st["configured_destination"] == "https://host.test:8787"
    assert st["destination_changed"] is False


def test_send_log_records_the_destination_on_a_failed_post_too(tss):
    """The key sits on the entry dict built before anything can raise (#2618),
    so every path records it — transport error, schema refusal, 2xx alike."""
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, client = _fake_client()
    client.post = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", "https://u:p@h.test/v1/x"), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now(backfill=True)) is False
    sends = mod.get_status()["recent_sends"]
    assert sends[0]["destination"] == "https://h.test" and sends[0]["error"] == "RuntimeError"

    bad = _valid(mod)
    bad["surprise"] = 1
    fake_ac2, _client2 = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", "https://u:p@h.test/v1/x"), \
         patch.object(mod, "build_aggregate_payload", return_value=bad), \
         patch.object(mod.httpx, "AsyncClient", fake_ac2):
        assert asyncio.run(mod.share_now(backfill=True)) is False
    sends = mod.get_status()["recent_sends"]
    fake_ac2.assert_not_called()                     # refused before it left the box
    assert sends[0]["error"] == "schema" and sends[0]["destination"] == "https://h.test"
    assert "u:p" not in store[mod.KEY_RECENT_SENDS]


def test_status_says_when_the_newest_send_went_somewhere_else(tss):
    """The reported bug: an operator points the env var at a local receiver, gets
    a 200, restores the default — and the panel credits the hosted service."""
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, _client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", "http://localhost:8787/v1/telemetry-share"), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now()) is True
    with patch.object(mod, "TELEMETRY_SHARING_URL", mod.DEFAULT_SHARE_URL):
        st = mod.get_status()
    assert st["receiver_hint"] == "ok"
    assert st["receiver_destination"] == "http://localhost:8787"
    assert st["configured_destination"] == "https://intake.abilityai.dev"
    assert st["destination_changed"] is True


def test_destination_comparison_ignores_case_default_port_and_path(tss):
    mod, store, _ = tss
    _consent_on(store)
    fake_ac, _client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod, "TELEMETRY_SHARING_URL", "http://localhost:8787/v1/telemetry-share"), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        assert asyncio.run(mod.share_now()) is True
    # The same address written another way is the same address …
    with patch.object(mod, "TELEMETRY_SHARING_URL", "HTTP://LOCALHOST:8787/other/path?y=2"):
        assert mod.get_status()["destination_changed"] is False
    # … and an explicit non-default port is part of it.
    with patch.object(mod, "TELEMETRY_SHARING_URL", "http://localhost/v1/telemetry-share"):
        assert mod.get_status()["destination_changed"] is True


def test_legacy_entries_read_as_unknown_never_as_a_match(tss):
    """An entry written before the key existed keeps rendering — as unknown, not
    as agreement with whatever is configured today (AC 4: no migration)."""
    mod, store, _ = tss
    mod._record_send({"sent_at": "t", "ok": True, "http_status": 200, "payload": {}})
    st = mod.get_status()
    assert st["receiver_hint"] == "ok"
    assert st["receiver_destination"] is None
    assert st["destination_changed"] is False
    assert "destination" not in st["recent_sends"][0]   # the reader never fabricates it


def test_backfill_is_retried_until_the_receiver_acknowledges(tss):
    mod, store, _ = tss
    _consent_on(store)
    store[mod.KEY_BACKFILL_DAYS] = "30"
    # Heartbeat with the backfill still owed ⇒ send the disclosed window again.
    assert mod._resolve_window(False, None) == (True, 30)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = "2026-09-01T00:00:00Z"
    store[mod.KEY_LAST_SHARED_AT] = "2026-08-30T00:00:00Z"
    backfill, days = mod._resolve_window(False, None)
    assert backfill is False and days >= 1      # cumulative since the last success
    assert mod._resolve_window(True, 7) == (True, 7)   # explicit window wins


# ---------------------------------------------------------------------------
# Identity + markers
# ---------------------------------------------------------------------------

def test_sharing_id_lifecycle(tss):
    mod, store, _ = tss
    st = mod.set_consent(True, backfill_days=7)
    first = st["sharing_id"]
    assert _UUID.match(first) and st["sharing_id_rotated"] is True
    assert mod.set_consent(True)["sharing_id"] == first            # stays while on
    assert mod.set_consent(True)["sharing_id_rotated"] is False
    off = mod.set_consent(False)
    assert off["sharing_id"] is None and mod.KEY_SHARING_ID not in store
    again = mod.set_consent(True)
    assert _UUID.match(again["sharing_id"]) and again["sharing_id"] != first
    assert again["sharing_id_rotated"] is True


def test_sharing_id_self_heals_after_a_manual_reset_delete(tss):
    mod, store, _ = tss
    mod.set_consent(True)
    del store[mod.KEY_SHARING_ID]          # the documented DELETE reset path
    minted = mod.get_or_mint_sharing_id()
    assert _UUID.match(minted) and store[mod.KEY_SHARING_ID] == minted


def test_dismissed_marker_first_wins_and_consent_stamps_it(tss):
    mod, store, _ = tss
    a = mod.mark_ask_dismissed()["dismissed_at"]
    b = mod.mark_ask_dismissed()["dismissed_at"]
    assert a and a == b
    store.clear()
    assert mod.set_consent(True)["dismissed_at"]   # a consented install is never asked


def test_first_value_is_memoised_only_once_it_exists(tss):
    mod, store, mdb = tss
    assert mod.first_value_at() is None and mod.KEY_FIRST_VALUE_AT not in store
    mdb.first_autonomous_success_at.return_value = "2026-09-02T10:00:00Z"
    assert mod.first_value_at() == "2026-09-02T10:00:00Z"
    assert store[mod.KEY_FIRST_VALUE_AT] == "2026-09-02T10:00:00Z"
    calls = mdb.first_autonomous_success_at.call_count
    mod.first_value_at()
    assert mdb.first_autonomous_success_at.call_count == calls   # memo, no re-read


def test_public_flags_fail_in_the_hidden_direction(tss):
    mod, store, mdb = tss
    assert mod.public_flags() == {
        "telemetry_sharing_enabled": False,
        "telemetry_sharing_hard_disabled": mod.is_hard_disabled(),
        "telemetry_sharing_dismissed": False,
        "telemetry_sharing_first_value": False,
    }
    mdb.get_setting_value.side_effect = RuntimeError("db down")
    flags = mod.public_flags()               # never raises
    assert flags["telemetry_sharing_dismissed"] is True
    assert flags["telemetry_sharing_first_value"] is False


def test_tick_marker_dedupes_workers_and_fails_open(tss):
    mod, _store, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    svc = mod.TelemetrySharingService(interval_hours=24)
    with patch.object(mod, "get_breaker_redis", return_value=client):
        assert svc._claim_tick() is True      # first worker wins the interval
        assert mod.TelemetrySharingService(interval_hours=24)._claim_tick() is False
        ttl = client.ttl(mod._TICK_LOCK_KEY)
        assert 0 < ttl <= 12 * 3600           # half the interval, never released
    with patch.object(mod, "get_breaker_redis", return_value=None):
        assert svc._claim_tick() is True      # Redis unavailable ⇒ today's behaviour


# ---------------------------------------------------------------------------
# Router layer: human-only gates, lazy preview, audit
# ---------------------------------------------------------------------------

@pytest.fixture
def router_env():
    try:
        from routers import settings as router_mod
        import services.telemetry_sharing_service as tss_mod
    except ImportError:
        pytest.skip("backend venv required")
    store: dict = {}
    mdb = _fake_db(store)
    audit = MagicMock()
    audit.log = AsyncMock()
    settings = MagicMock()
    settings.get_install_source.return_value = "script"
    with patch.object(tss_mod, "db", mdb), \
         patch.object(tss_mod, "settings_service", settings), \
         patch.object(tss_mod, "resolve_release_version", return_value="0.9.5"), \
         patch.object(router_mod, "platform_audit_service", audit):
        yield router_mod, tss_mod, store, audit


def _admin_user():
    from models import User
    return User(id=1, username="admin", email="admin@example.com", role="admin",
                agent_name=None, connector_agent=None, mcp_scope=None)


def _agent_user():
    """Agent-scoped principal resolving to an admin owner (trinity-ops-agent#232)."""
    from models import User
    return User(id=1, username="admin", email="admin@example.com", role="admin",
                agent_name="some-agent", connector_agent=None, mcp_scope="agent")


def test_dismiss_and_status_reject_agent_principals(router_env):
    router_mod, _tss, _store, _audit = router_env
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router_mod.dismiss_telemetry_ask(_agent_user()))
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router_mod.get_telemetry_sharing(True, _agent_user()))
    assert ei.value.status_code == 403


def test_status_get_skips_the_preview_when_asked(router_env):
    router_mod, _tss, _store, _audit = router_env
    cheap = asyncio.run(router_mod.get_telemetry_sharing(False, _admin_user()))
    assert "payload_preview" not in cheap and "recent_sends" in cheap
    full = asyncio.run(router_mod.get_telemetry_sharing(True, _admin_user()))
    assert full["payload_preview"]["schema_version"] == 2


def test_status_route_carries_the_destination_triple(router_env):
    """The router spreads `get_status()`, so the three additive keys reach the
    panel with no router change (#2571)."""
    router_mod, _tss, _store, _audit = router_env
    cheap = asyncio.run(router_mod.get_telemetry_sharing(False, _admin_user()))
    assert cheap["configured_destination"] is None or isinstance(cheap["configured_destination"], str)
    assert cheap["receiver_destination"] is None      # nothing has been sent on this store
    assert cheap["destination_changed"] is False


def test_dismiss_route_is_idempotent_and_audited(router_env):
    router_mod, _tss, store, audit = router_env
    first = asyncio.run(router_mod.dismiss_telemetry_ask(_admin_user()))
    second = asyncio.run(router_mod.dismiss_telemetry_ask(_admin_user()))
    assert first["dismissed_at"] and first["dismissed_at"] == second["dismissed_at"]
    actions = [c.kwargs.get("event_action") for c in audit.log.await_args_list]
    assert actions.count("telemetry_sharing_ask_dismissed") == 2


def test_consent_audit_carries_rotation_as_a_bool_never_the_id(router_env):
    router_mod, tss_mod, store, audit = router_env
    from models import TelemetrySharingUpdate
    with patch.object(tss_mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(tss_mod, "spawn_share") as spawn:
        st = asyncio.run(router_mod.set_telemetry_sharing(
            TelemetrySharingUpdate(enabled=True, backfill_days=30), _admin_user()))
    spawn.assert_called_once_with(backfill=True)
    details = audit.log.await_args.kwargs["details"]
    assert details["sharing_id_rotated"] is True
    assert st["sharing_id"] not in json.dumps(details)


def test_feature_flags_carry_the_four_consent_booleans(router_env):
    router_mod, _tss, _store, _audit = router_env
    flags = asyncio.run(router_mod.get_public_feature_flags(_admin_user()))
    for key in ("telemetry_sharing_enabled", "telemetry_sharing_hard_disabled",
                "telemetry_sharing_dismissed", "telemetry_sharing_first_value"):
        assert isinstance(flags[key], bool), key


# ---------------------------------------------------------------------------
# utils/app_version — must never raise at import or call time
# ---------------------------------------------------------------------------

def test_release_version_resolver_is_import_safe_and_prefers_env(monkeypatch):
    """In the container the module is `/app/utils/app_version.py`: a fixed
    `parents[3]` raised IndexError at IMPORT and took the backend down. The
    candidate list must be computed defensively, and the env value's build
    suffix (`+g<sha>`) must be stripped so the wire never carries a commit."""
    from utils import app_version
    assert app_version._candidate_paths()[0] == app_version.Path("/app/VERSION")
    monkeypatch.setenv("VERSION", "0.9.5-rc2+g1a2b3c4")
    assert app_version.resolve_release_version() == "0.9.5-rc2"
    monkeypatch.setenv("VERSION", "unknown")
    v = app_version.resolve_release_version()
    assert isinstance(v, str) and v            # file fallback or "unknown", never a raise
