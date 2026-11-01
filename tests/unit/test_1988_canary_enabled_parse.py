"""#1988 — CANARY_ENABLED accepts the truthy spellings an operator reaches for
(1/true/yes/on, any case), stays silently off when unset, empty or a falsy
spelling, and warns at start when the value is not recognised. Before, only the
exact string "1" enabled it, so CANARY_ENABLED=true ran no canary and said
nothing.
"""
import logging

import pytest

from services import canary_service as module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " true "])
def test_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("CANARY_ENABLED", value)
    assert module.CanaryService.is_enabled() is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "FALSE", "no", "off", "2", "enabled"])
def test_everything_else_disables(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CANARY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CANARY_ENABLED", value)
    assert module.CanaryService.is_enabled() is False


@pytest.mark.parametrize("value", ["2", "enabled", "y"])
def test_unrecognised_value_warns_at_start(monkeypatch, caplog, value):
    monkeypatch.setenv("CANARY_ENABLED", value)
    with caplog.at_level(logging.WARNING, logger=module.logger.name):
        module.CanaryService().start()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("CANARY_ENABLED" in m and repr(value) in m for m in warnings)


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_recognised_off_stays_silent(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("CANARY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CANARY_ENABLED", value)
    with caplog.at_level(logging.WARNING, logger=module.logger.name):
        module.CanaryService().start()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
