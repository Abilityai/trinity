"""Usage sharing: "never asked" is told apart from "asked and refused"
(abilityai/trinity-enterprise#1397).

A caller that turns sharing on for the operator read only the consent flag,
which is off both for an install that was never asked and for one whose admin
switched sharing off. `has_opted_out()` answers the second from the rows the
service already writes. The states below are reached through the service's own
writers, never by seeding keys, so a writer that stops leaving its mark turns
these red.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _settings_db(store: dict) -> MagicMock:
    """A settings store with the real write-once semantics."""
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
    mdb.first_autonomous_success_at.return_value = None
    return mdb


@pytest.fixture
def sharing():
    import services.telemetry_sharing_service as mod

    store: dict = {}
    with patch.object(mod, "db", _settings_db(store)):
        yield mod, store


def test_a_fresh_install_has_not_opted_out(sharing):
    mod, store = sharing
    assert store == {}
    assert mod.is_consent_enabled() is False
    assert mod.has_opted_out() is False


def test_an_install_that_is_sharing_has_not_opted_out(sharing):
    mod, _ = sharing
    mod.set_consent(True)
    assert mod.has_opted_out() is False


def test_switching_sharing_off_in_settings_is_an_opt_out(sharing):
    mod, _ = sharing
    mod.set_consent(True)
    mod.set_consent(False)
    assert mod.is_consent_enabled() is False
    assert mod.has_opted_out() is True


def test_switching_off_without_ever_being_on_is_an_opt_out(sharing):
    mod, _ = sharing
    mod.set_consent(False)
    assert mod.has_opted_out() is True


def test_dont_ask_again_is_an_opt_out(sharing):
    mod, _ = sharing
    mod.mark_ask_dismissed()
    assert mod.is_consent_enabled() is False
    assert mod.has_opted_out() is True


@pytest.mark.parametrize("left_behind", ["telemetry_sharing_consent_at",
                                         "telemetry_sharing_dismissed_at"])
def test_either_mark_alone_is_an_opt_out_once_the_consent_row_is_gone(sharing, left_behind):
    """The consent row can be deleted on its own (the generic settings DELETE);
    the time sharing was on, or the dismissal, still says the operator chose."""
    mod, store = sharing
    mod.set_consent(True)
    mod.set_consent(False)
    for key in list(store):
        if key != left_behind:
            del store[key]
    assert store, "the writer left no such mark"
    assert mod.has_opted_out() is True


def test_deleting_every_row_returns_the_install_to_never_asked(sharing):
    """The reset path: every deletion moves toward off / ask again."""
    mod, store = sharing
    mod.set_consent(True)
    mod.set_consent(False)
    store.clear()
    assert mod.has_opted_out() is False


def test_turning_sharing_back_on_clears_the_opt_out(sharing):
    mod, _ = sharing
    mod.set_consent(True)
    mod.set_consent(False)
    mod.set_consent(True)
    assert mod.has_opted_out() is False


def test_an_unreadable_store_counts_as_an_opt_out(sharing):
    """Nothing is switched on for an operator on a failed read."""
    mod, _ = sharing
    mod.db.get_setting_value.side_effect = RuntimeError("db down")
    assert mod.has_opted_out() is True
