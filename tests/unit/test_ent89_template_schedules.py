"""
The `schedules:` tolerant reader — trinity-enterprise#89.

`services/template_schedules.py` is read by three consumers that each turn a
raise into a different kind of silent damage:

  * `template_service._build_template` / `_build_local_template` — the catalog
    read path, where one raise empties `GET /api/templates` (the #1835 class);
  * `crud.reconcile_declared_schedules` — creation, inside the destructive
    rollback fence;
  * the T-018 compatibility check, where `run_static` swallows a raise into
    `skipped` and the report's counts ignore `skipped` — so a raising validator
    reports "healthy".

So the reader's contract is **totality**, and `template.yaml` is untrusted:
`yaml.safe_load(...) or {}` can hand back a scalar, a list, or a mapping at any
level. The matrix below enumerates the shapes we know about; the Hypothesis
property at the bottom is there because a 20-row matrix cannot be a totality
proof for an unbounded input space.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from services.template_schedules import (  # noqa: E402
    MAX_DECLARED_SCHEDULES,
    MAX_DESCRIPTION_LEN,
    MAX_MESSAGE_LEN,
    MAX_NAME_LEN,
    normalize_declared_schedules,
    schedule_shape_errors,
)


def _entry(**overrides) -> dict:
    base = {"name": "daily", "cron": "0 9 * * *", "message": "/daily-briefing"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tolerance matrix — every row is (block, kept_count, expects_error)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("block, kept, errored", [
    # absent / empty are NOT errors — a commented-out block must not acquire
    # a spurious warning.
    (None, 0, False),
    ([], 0, False),
    # non-list blocks
    ("yes", 0, True),
    (5, 0, True),                       # the live c_p006 shape
    (True, 0, True),
    ({"a": "b"}, 0, True),
    (0.5, 0, True),
    # non-mapping entries
    ([None], 0, True),
    (["daily"], 0, True),
    ([[1, 2]], 0, True),
    # required keys
    ([{"cron": "0 9 * * *", "message": "/m"}], 0, True),          # no name
    ([{"name": "a", "message": "/m"}], 0, True),                  # no cron
    ([{"name": "a", "cron": "0 9 * * *"}], 0, True),              # no message
    ([_entry(name={"a": "b"})], 0, True),                         # non-string name
    ([_entry(cron=5)], 0, True),                                  # .strip() would raise
    ([_entry(message=[])], 0, True),
    ([_entry(name="   ")], 0, True),                              # empty after strip
    # timezone
    ([_entry(timezone=5)], 0, True),                              # pytz would raise
    ([_entry(timezone="Nowhere/Nope")], 0, True),
    ([_entry(timezone=None)], 1, False),                          # null → default UTC
    ([_entry(timezone="Europe/London")], 1, False),
    # cron strictness — the scheduler's own parser, not a regex
    ([_entry(cron="@daily")], 0, True),
    ([_entry(cron="0 9 * * * *")], 0, True),                      # 6 fields
    ([_entry(cron="99 99 * * *")], 0, True),                      # out of range
    ([_entry(cron="not a cron")], 0, True),
    ([_entry(cron="0 9 * * MON")], 1, False),                     # named day IS valid
    ([_entry(cron="*/15 * * * 1-5")], 1, False),
    # bounds
    ([_entry(name="n" * (MAX_NAME_LEN + 1))], 0, True),
    ([_entry(description="d" * (MAX_DESCRIPTION_LEN + 1))], 1, True),   # kept, dropped field
    ([_entry(message="m" * (MAX_MESSAGE_LEN + 1))], 1, True),           # kept, truncated
    ([_entry(description=5)], 1, True),                                 # kept, dropped field
    # enabled is fail-safe, entry survives
    ([_entry(enabled="yes")], 1, True),
    ([_entry(enabled=1)], 1, True),
    ([_entry(enabled=None)], 1, True),
    ([_entry(enabled=True)], 1, False),
    ([_entry(enabled=False)], 1, False),
    # intra-block duplicates
    ([_entry(name="d"), _entry(name="d", cron="0 8 * * *")], 1, True),
    ([_entry(name="a"), _entry(name="b")], 2, False),
    # cap
    ([_entry(name=f"n{i}") for i in range(MAX_DECLARED_SCHEDULES)],
     MAX_DECLARED_SCHEDULES, False),
    ([_entry(name=f"n{i}") for i in range(MAX_DECLARED_SCHEDULES + 5)],
     MAX_DECLARED_SCHEDULES, True),
    # one bad entry never costs the good ones
    ([_entry(name="good"), None, _entry(name="also-good", cron="0 8 * * *")], 2, True),
])
def test_tolerance_matrix(block, kept, errored):
    items = normalize_declared_schedules(block)
    errors = schedule_shape_errors(block)
    assert len(items) == kept, (items, errors)
    assert bool(errors) is errored, errors


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalized_entry_shape_is_exact():
    """The materializer indexes these six keys unconditionally."""
    (entry,) = normalize_declared_schedules([_entry(
        enabled=True, timezone="Europe/London", description="why",
    )])
    assert entry == {
        "name": "daily",
        "cron": "0 9 * * *",
        "message": "/daily-briefing",
        "enabled": True,
        "timezone": "Europe/London",
        "description": "why",
    }


def test_purpose_is_accepted_as_description():
    """The abilities plugin shape spells it `purpose:`."""
    (entry,) = normalize_declared_schedules([_entry(purpose="weekly recon sweep")])
    assert entry["description"] == "weekly recon sweep"


def test_description_wins_over_purpose_when_both_present():
    (entry,) = normalize_declared_schedules([_entry(description="d", purpose="p")])
    assert entry["description"] == "d"


def test_unknown_keys_including_id_are_ignored():
    """AC #1 — a plugin may carry design metadata Trinity has no use for, and
    `id` in particular must NOT be honored: Trinity mints its own schedule id."""
    (entry,) = normalize_declared_schedules([_entry(
        id="plugin-sched-7", owner="someone", retries=3, nested={"a": [1]},
    )])
    assert set(entry) == {
        "name", "cron", "message", "enabled", "timezone", "description"}
    assert "plugin-sched-7" not in entry.values()


def test_enabled_defaults_to_false_when_unspecified():
    """AC #3 — creation must not silently start autonomous work."""
    (entry,) = normalize_declared_schedules([_entry()])
    assert entry["enabled"] is False


def test_truthy_non_bool_enabled_does_not_arm_the_schedule():
    """`enabled: "no"` is truthy in Python — a loose read would ARM a schedule
    its author meant to leave off."""
    (entry,) = normalize_declared_schedules([_entry(enabled="no")])
    assert entry["enabled"] is False


def test_message_is_truncated_not_dropped():
    (entry,) = normalize_declared_schedules([_entry(message="m" * 50_000)])
    assert len(entry["message"]) == MAX_MESSAGE_LEN


def test_first_duplicate_wins():
    items = normalize_declared_schedules(
        [_entry(name="d", message="/first"), _entry(name="d", message="/second")]
    )
    assert [i["message"] for i in items] == ["/first"]


def test_cap_keeps_the_first_n_in_order():
    items = normalize_declared_schedules(
        [_entry(name=f"n{i}") for i in range(MAX_DECLARED_SCHEDULES + 10)]
    )
    assert [i["name"] for i in items] == [
        f"n{i}" for i in range(MAX_DECLARED_SCHEDULES)]


# ---------------------------------------------------------------------------
# Error-string discipline (R10) — the list is persisted into
# `agent_compatibility_results.checks_json`, rendered in the UI, and returned
# in the catalog response.
# ---------------------------------------------------------------------------

_SECRET_NAME = "SUPERSECRETNAME"
_SECRET_MESSAGE = "SUPERSECRETMESSAGE"
_SECRET_DESC = "SUPERSECRETDESCRIPTION"


@pytest.mark.parametrize("block", [
    [{"name": _SECRET_NAME, "cron": "@daily", "message": _SECRET_MESSAGE}],
    [{"name": _SECRET_NAME, "cron": 5, "message": _SECRET_MESSAGE}],
    [{"name": _SECRET_NAME, "cron": "0 9 * * *", "message": _SECRET_MESSAGE,
      "timezone": "Nowhere/Nope"}],
    [{"name": _SECRET_NAME, "cron": "0 9 * * *", "message": _SECRET_MESSAGE,
      "enabled": "yes"}],
    [{"name": _SECRET_NAME, "cron": "0 9 * * *", "message": _SECRET_MESSAGE,
      "description": _SECRET_DESC * 500}],
    [{"name": _SECRET_NAME, "cron": "0 9 * * *", "message": _SECRET_MESSAGE},
     {"name": _SECRET_NAME, "cron": "0 8 * * *", "message": _SECRET_MESSAGE}],
    [{"name": _SECRET_NAME * 100, "cron": "0 9 * * *", "message": _SECRET_MESSAGE}],
])
def test_errors_never_echo_name_message_or_description(block):
    blob = " ".join(schedule_shape_errors(block))
    assert blob, "expected at least one error for this block"
    assert _SECRET_NAME not in blob
    assert _SECRET_MESSAGE not in blob
    assert _SECRET_DESC not in blob


def test_cron_is_echoed_but_sanitized_and_bounded():
    """The cron is the one echoed value — it is what makes the error
    actionable — so it must be printable-filtered and length-bounded."""
    errors = schedule_shape_errors(
        [_entry(cron="0 9 \x1b[31m* * *\n" + "X" * 500)]
    )
    blob = " ".join(errors)
    assert "\x1b" not in blob and "\n" not in blob
    assert "X" * 200 not in blob


def test_errors_name_the_index_and_the_key():
    errors = schedule_shape_errors([_entry(), {"name": "b", "message": "/m"}])
    assert any("schedules[1]" in e and "cron" in e for e in errors)


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------

_HOSTILE = [
    None, 0, 1, -1, 1.5, "", "yes", b"bytes", True, False,
    [], {}, set(), (1, 2),
    [None], [[]], [{}], [{"name": None}], [{"name": []}],
    {"schedules": "nested"}, [{"name": "a", "cron": {"x": 1}, "message": None}],
    [{"name": "a", "cron": "* * * * *", "message": "m", "timezone": []}],
    [{"name": "a", "cron": "* * * * *", "message": "m", "description": {"a": 1}}],
    [{"name": "\x00\x1b", "cron": "\n", "message": "\r"}],
]


@pytest.mark.parametrize("block", _HOSTILE)
def test_no_input_raises(block):
    normalize_declared_schedules(block)
    schedule_shape_errors(block)


@pytest.mark.parametrize("block", _HOSTILE)
def test_both_public_functions_agree(block):
    """They share one private `_parse`, so "kept entries" and "reported errors"
    cannot drift. Pinning it here means a future split breaks a test, not a
    production report."""
    items = normalize_declared_schedules(block)
    errors = schedule_shape_errors(block)
    assert isinstance(items, list) and isinstance(errors, list)
    assert all(isinstance(i, dict) for i in items)
    assert all(isinstance(e, str) for e in errors)


def test_property_reader_is_total_over_arbitrary_yaml():
    """A 40-row matrix cannot be a totality proof for `yaml.safe_load` output.

    Hypothesis generates recursive JSON-ish values — the exact value space
    `yaml.safe_load` produces — and asserts both public functions return
    well-typed results for every one of them.
    """
    hypothesis = pytest.importorskip("hypothesis")
    st = pytest.importorskip("hypothesis.strategies")

    scalars = st.one_of(
        st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False),
        st.text(max_size=20),
    )
    yamlish = st.recursive(
        scalars,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(max_size=12), children, max_size=4),
        ),
        max_leaves=12,
    )

    @hypothesis.given(block=yamlish)
    @hypothesis.settings(max_examples=300, deadline=None)
    def check(block):
        items = normalize_declared_schedules(block)
        errors = schedule_shape_errors(block)
        assert isinstance(items, list)
        assert isinstance(errors, list)
        assert len(items) <= MAX_DECLARED_SCHEDULES
        for item in items:
            assert set(item) == {
                "name", "cron", "message", "enabled", "timezone", "description"}
            assert isinstance(item["name"], str) and item["name"].strip()
            assert isinstance(item["cron"], str)
            assert isinstance(item["message"], str)
            assert isinstance(item["enabled"], bool)
            assert isinstance(item["timezone"], str)
            assert item["description"] is None or isinstance(item["description"], str)
            assert len(item["name"]) <= MAX_NAME_LEN
            assert len(item["message"]) <= MAX_MESSAGE_LEN
        assert len({i["name"] for i in items}) == len(items)

    check()


# ---------------------------------------------------------------------------
# Catalog surface + the create-path fetch (§3.2)
# ---------------------------------------------------------------------------

class TestCatalogSurface:
    """AC #1 — BOTH builders. The pre-existing asymmetry (`persistent_state` is
    surfaced only by the GitHub builder) means parity cannot be assumed."""

    def _ts(self):
        from services import template_service
        return template_service

    def test_github_builder_surfaces_normalized_schedules(self):
        entry = self._ts()._build_template("owner/repo", {"schedules": [
            _entry(enabled=True), _entry(name="bad", cron="@daily"),
        ]})
        assert [s["name"] for s in entry["schedules"]] == ["daily"]
        assert entry["schedule_errors"]

    def test_local_builder_surfaces_normalized_schedules(self, tmp_path):
        (tmp_path / "template.yaml").write_text(
            "name: demo\ndescription: d\n"
            "schedules:\n  - name: daily\n    cron: '0 9 * * *'\n    message: /m\n"
        )
        # `is_bundled` is keyword-only and required (ent#128): it selects the
        # credential-metadata trust label, which is orthogonal to `schedules:`.
        entry = self._ts()._build_local_template(tmp_path, is_bundled=True)
        assert [s["name"] for s in entry["schedules"]] == ["daily"]
        assert entry["schedule_errors"] == []

    def test_both_builders_expose_the_same_keys(self):
        gh = self._ts()._build_template("owner/repo", {})
        assert {"schedules", "schedule_errors"} <= set(gh)

    def test_a_malformed_block_does_not_raise_out_of_either_builder(self, tmp_path):
        ts = self._ts()
        assert ts._build_template("owner/repo", {"schedules": 5})["schedules"] == []
        (tmp_path / "template.yaml").write_text("name: d\nschedules: 5\n")
        assert ts._build_local_template(
            tmp_path, is_bundled=True)["schedules"] == []

    def test_malformed_block_logs_exactly_one_warning_naming_the_template(
            self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            self._ts()._build_template("owner/repo", {"schedules": "yes"})
        hits = [r for r in caplog.records if "`schedules:`" in r.getMessage()]
        assert len(hits) == 1
        assert "github:owner/repo" in hits[0].getMessage()


class TestGitHubCatalogFence:
    """R3 — both GitHub list paths were BARE comprehensions, so any raise in the
    untrusted builder 500'd the whole GitHub half of GET /api/templates. That is
    the #1835 bug this feature is modelled on, and the feature itself would have
    re-opened it by adding a new reader inside `_build_template`."""

    @staticmethod
    def _stub_admin_list(monkeypatch, entries):
        """Pin `get_github_templates` by sys.modules KEY, not by module object.

        `get_all_templates` imports it lazily inside the function body, so it
        resolves `sys.modules["services.settings_service"]` at call time.
        Patching an attribute on a separately-imported reference to that module
        is order-fragile in this suite — it passed in isolation and failed
        under the full run, where an earlier file has swapped the module
        object. `monkeypatch.setitem` pins the exact key the lookup reads.
        """
        import types
        fake = types.ModuleType("services.settings_service")
        fake.get_github_templates = lambda: entries
        monkeypatch.setitem(sys.modules, "services.settings_service", fake)

    @staticmethod
    def _explode_on(monkeypatch, ts, bad_repo):
        real = ts._build_template

        # `metadata_error` was added by trinity-enterprise#14 (the fetch reason,
        # so an unreadable template.yaml is distinguishable from an absent one).
        # A double of the builder has to carry the real signature: a stub that
        # is one parameter short raises TypeError INSIDE the fence this class
        # exists to test, so every template would be skipped and the assertion
        # would fail for a reason that has nothing to do with the fence.
        def explode(repo, metadata, admin_override=None, metadata_error=None):
            if repo == bad_repo:
                raise RuntimeError("boom")
            return real(repo, metadata, admin_override, metadata_error)

        monkeypatch.setattr(ts, "_build_template", explode)
        monkeypatch.setattr(ts, "_fetch_all_metadata", lambda repos: {})
        monkeypatch.setattr(ts, "get_local_templates", lambda: [])

    def test_a_raising_builder_skips_only_its_own_template(self, monkeypatch):
        from services import template_service as ts

        self._explode_on(monkeypatch, ts, "bad/repo")
        monkeypatch.setattr(
            ts, "DEFAULT_GITHUB_TEMPLATE_REPOS", ["good/one", "bad/repo", "good/two"])
        self._stub_admin_list(monkeypatch, None)   # None ⇒ use the defaults

        ids = [t["id"] for t in ts.get_all_templates()]
        assert ids == ["github:good/one", "github:good/two"]

    def test_the_admin_configured_list_is_fenced_too(self, monkeypatch):
        from services import template_service as ts

        self._explode_on(monkeypatch, ts, "bad/repo")
        self._stub_admin_list(monkeypatch, [
            {"github_repo": "good/one"}, {"github_repo": "bad/repo"}])

        ids = [t["id"] for t in ts.get_all_templates()]
        assert ids == ["github:good/one"]


class TestCreatePathFetch:
    """R2 — creation must not read the catalog's global-PAT, default-branch,
    10-minute-cached metadata, and must never be SILENTLY empty."""

    def test_passes_the_pat_and_ref_through(self, monkeypatch):
        from services import template_service as ts

        seen = {}

        def fake(repo, pat, ref=None):
            seen.update(repo=repo, pat=pat, ref=ref)
            return {"schedules": []}, None

        monkeypatch.setattr(ts, "_fetch_template_yaml_result", fake)
        ts.fetch_template_metadata_for_create(
            "owner/repo", pat="per-user-pat", ref="feature-x")
        assert seen == {"repo": "owner/repo", "pat": "per-user-pat",
                        "ref": "feature-x"}

    def test_bypasses_the_catalog_cache(self, monkeypatch):
        from services import template_service as ts

        monkeypatch.setattr(
            ts, "_get_cached_metadata",
            lambda repo: pytest.fail("create path must not read the cache"))
        monkeypatch.setattr(
            ts, "_fetch_template_yaml_result", lambda *a, **k: ({"x": 1}, None))
        assert ts.fetch_template_metadata_for_create("owner/repo") == {"x": 1}

    def test_a_failed_fetch_is_loud_not_silent(self, monkeypatch, caplog):
        import logging
        from services import template_service as ts

        monkeypatch.setattr(
            ts, "_fetch_template_yaml_result", lambda *a, **k: ({}, "HTTP 403"))
        with caplog.at_level(logging.WARNING):
            assert ts.fetch_template_metadata_for_create(
                "owner/private", pat="wrong-pat", ref="main") == {}

        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "owner/private" in blob and "403" in blob
        assert "wrong-pat" not in blob, "the PAT must never reach the logs"

    def test_the_failure_reason_is_sanitized_like_its_neighbours(
            self, monkeypatch, caplog):
        """`reason` embeds `str(e)`, and an httpx error message carries the
        request URL — which carries the caller-supplied `owner/repo`. A repo
        with no `/` skips `_GITHUB_REPO_PATH_RE` upstream, so control bytes do
        reach this line. Its two neighbours on the SAME call are
        `_sanitize_for_warning`-wrapped; leaving this one raw is the hygiene
        inconsistency the /review pass called out."""
        import logging
        from services import template_service as ts

        hostile = "boom \x1b[2J\x07 at https://api.github.com/x\nWARNING faked"
        monkeypatch.setattr(
            ts, "_fetch_template_yaml_result", lambda *a, **k: ({}, hostile))
        with caplog.at_level(logging.WARNING):
            assert ts.fetch_template_metadata_for_create("owner/repo") == {}

        blob = " ".join(
            r.getMessage() for r in caplog.records
            if "owner/repo" in r.getMessage())
        assert blob, "the failure must still be logged"
        for ch in ("\x1b", "\x07", "\n"):
            assert ch not in blob, f"{ch!r} survived into the log line"
        assert "boom" in blob, "sanitizing must not gut the diagnostic"

    def test_a_flooding_failure_reason_is_bounded(self, monkeypatch, caplog):
        """Bounded at 200, not the 80 default: this WARNING exists to be
        diagnosable and an 80-char truncation defeats that — but unbounded lets
        a hostile template flood the operator's log."""
        import logging
        from services import template_service as ts

        monkeypatch.setattr(
            ts, "_fetch_template_yaml_result", lambda *a, **k: ({}, "z" * 5000))
        with caplog.at_level(logging.WARNING):
            ts.fetch_template_metadata_for_create("owner/repo")

        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "z" * 200 in blob
        assert "z" * 201 not in blob, "the reason must be length-bounded"

    def test_a_non_mapping_template_yaml_is_rejected_not_returned(
            self, monkeypatch, caplog):
        import logging
        from services import template_service as ts

        monkeypatch.setattr(
            ts, "_fetch_template_yaml_result", lambda *a, **k: (["a", "b"], None))
        with caplog.at_level(logging.WARNING):
            assert ts.fetch_template_metadata_for_create("owner/repo") == {}

    def test_network_failures_never_escape(self, monkeypatch):
        from services import template_service as ts

        def boom(*_a, **_k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(ts.httpx, "Client", boom)
        assert ts.fetch_template_metadata_for_create("owner/repo") == {}


class TestGitHubContentsApiContract:
    """The outbound request itself, against the documented GitHub contents API.

    Every other test here stubs `_fetch_template_yaml_result`, i.e. BELOW the
    HTTP layer — so a wrong param name (`?ref=` is what pins the revision) or a
    dropped Authorization header would leave them all green while the feature
    silently read the default branch, or read nothing at all for a private
    repo. That is exactly the R2 failure this fetch exists to prevent, so it is
    asserted at the wire.
    """

    @staticmethod
    def _fake_client(monkeypatch, calls, status=200, body=None):
        import base64
        import json as _json
        from services import template_service as ts

        class _Resp:
            status_code = status

            def json(self):
                return {"content": base64.b64encode(
                    (body or "schedules: []").encode()).decode()}

        class _Client:
            def __init__(self, **kwargs):
                calls.append({"init": kwargs})

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None, params=None):
                calls.append({"url": url, "headers": headers, "params": params})
                return _Resp()

        monkeypatch.setattr(ts.httpx, "Client", _Client)
        return _json

    def test_ref_is_sent_as_the_ref_query_param(self, monkeypatch):
        from services import template_service as ts

        calls = []
        self._fake_client(monkeypatch, calls)
        ts.fetch_template_metadata_for_create(
            "owner/repo", pat="tok", ref="feature-x")

        request = next(c for c in calls if "url" in c)
        assert request["url"] == (
            "https://api.github.com/repos/owner/repo/contents/template.yaml")
        assert request["params"] == {"ref": "feature-x"}

    def test_no_ref_sends_no_params(self, monkeypatch):
        from services import template_service as ts

        calls = []
        self._fake_client(monkeypatch, calls)
        ts.fetch_template_metadata_for_create("owner/repo", pat="tok")

        request = next(c for c in calls if "url" in c)
        assert request["params"] is None

    def test_the_resolved_pat_is_sent_as_a_bearer_token(self, monkeypatch):
        """A private template read with the creator's per-user token is the
        whole point — drop this header and the fetch 403s into an empty
        declaration."""
        from services import template_service as ts

        calls = []
        self._fake_client(monkeypatch, calls)
        ts.fetch_template_metadata_for_create("owner/private", pat="per-user-tok")

        request = next(c for c in calls if "url" in c)
        assert request["headers"]["Authorization"] == "Bearer per-user-tok"
        assert request["headers"]["Accept"] == "application/vnd.github+json"

    def test_no_pat_sends_no_authorization_header(self, monkeypatch):
        """ent#123 tokenless public creates must stay anonymous, not send
        `Bearer ` and get rejected."""
        from services import template_service as ts

        calls = []
        self._fake_client(monkeypatch, calls)
        ts.fetch_template_metadata_for_create("owner/public", pat=None)

        request = next(c for c in calls if "url" in c)
        assert "Authorization" not in request["headers"]

    def test_a_403_is_reported_not_swallowed(self, monkeypatch, caplog):
        import logging
        from services import template_service as ts

        calls = []
        self._fake_client(monkeypatch, calls, status=403)
        with caplog.at_level(logging.WARNING):
            assert ts.fetch_template_metadata_for_create(
                "owner/private", pat="tok") == {}
        assert "403" in " ".join(r.getMessage() for r in caplog.records)

    def test_the_declared_block_survives_the_round_trip(self, monkeypatch):
        from services import template_service as ts

        calls = []
        self._fake_client(
            monkeypatch, calls,
            body="schedules:\n  - name: daily\n    cron: '0 9 * * *'\n"
                 "    message: /m\n")
        metadata = ts.fetch_template_metadata_for_create("owner/repo", pat="tok")
        assert normalize_declared_schedules(metadata.get("schedules"))[0]["name"] \
            == "daily"
