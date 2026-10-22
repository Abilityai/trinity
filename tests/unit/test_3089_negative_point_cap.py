"""#3089 — a negative stored `metrics_daily_point_cap` takes the same fallback as
an unparseable one: the built-in default, with a warning. Before, `max(int(raw), 0)`
turned "-1" into 0, which for this key means UNLIMITED.

Every reader that reports the cap must agree with the enforcing one: the
record route (`routers/metric_points._ops_int`), the definitions `policy` block
(`routers/agent_files._metric_policy`) and the retention read surface
(`routers/settings/retention` quotas).
"""
import logging

import pytest

import routers.metric_points as _MP
import routers.agent_files as _AF
import services.settings_service as _SS

pytestmark = pytest.mark.unit

CAP = "metrics_daily_point_cap"


@pytest.mark.parametrize(
    "raw, expected",
    [("-1", 100000), ("-500", 100000), ("0", 0), ("42", 42), ("garbage", 100000), (None, 100000)],
)
def test_enforcing_reader(monkeypatch, raw, expected):
    monkeypatch.setattr(_SS.settings_service, "resolve_ops_setting", lambda key: (raw, "db-row"))
    assert _MP._ops_int(CAP, 100000) == expected


def test_negative_cap_warns(monkeypatch, caplog):
    monkeypatch.setattr(_SS.settings_service, "resolve_ops_setting", lambda key: ("-1", "db-row"))
    with caplog.at_level(logging.WARNING, logger=_MP.logger.name):
        _MP._ops_int(CAP, 100000)
    assert any(CAP in r.getMessage() and "'-1'" in r.getMessage() for r in caplog.records)


def test_negative_retention_window_still_reads_as_disabled(monkeypatch):
    """The window's safe fallback is 0, so a negative window is unchanged."""
    monkeypatch.setattr(_SS.settings_service, "resolve_ops_setting", lambda key: ("-3", "db-row"))
    assert _MP._ops_int("metrics_retention_days", 0) == 0


@pytest.mark.parametrize("raw", ["-1", "0", "42", "garbage"])
def test_definitions_policy_agrees_with_enforcement(monkeypatch, raw):
    monkeypatch.setattr(_SS.settings_service, "resolve_ops_setting", lambda key: (raw, "db-row"))
    assert _AF._metric_policy()["daily_point_cap"] == _MP._ops_int(CAP, 100000)
