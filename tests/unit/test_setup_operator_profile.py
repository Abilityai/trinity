"""trinity-enterprise#38 / #82 / #49 — first-run admin account + operator profile.

The setup endpoint registers the admin email (now REQUIRED — it is the sign-in
identity, #49) and, only on affirmative consent, schedules the fire-and-forget
operator intake as a background task. None of it may block or break setup, and a
missing/blank/typo'd email is rejected before any write. The setup token was
removed in #49, so there is no token machinery to stub here.

Import isolation (read before "simplifying" the lazy `_get_setup()` below):
`routers.setup` pulls in database/dependencies/services, which import several
backend `utils.*` leaves. Importing it at module level runs that chain during
COLLECTION; in the CI backend-unit gate (`cd tests && pytest unit/`) the
perturbation of sys.modules mid-collection interrupted the *entire* unit suite
(the head side collected ~2 of 2734 tests → the regression-diff gate flagged it).
So we do NOT import `routers.setup` at module level — collection stays
backend-free — and defer it to `_get_setup()`, called inside the tests after
collection completes (when `tests/unit/conftest.py` has made `utils` the backend
package, so the import resolves natively).
"""
import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_setup_module = None


def _get_setup():
    """Import `routers.setup` lazily (at test run time, not collection time).

    Importing it at module level pulls in database/dependencies/services and
    their many backend `utils.*` leaves during COLLECTION; in the CI backend-unit
    gate (`cd tests && pytest unit/`) that perturbs sys.modules mid-collection and
    interrupts the whole unit suite. Deferring to first use keeps collection to
    stdlib/pytest/fastapi/pydantic. At run time the unit conftest
    (`tests/unit/conftest.py`) has already installed src/backend/utils as the
    canonical `utils` package, so the backend `utils.*` leaves resolve natively —
    no manual sys.modules juggling (which the #762 lint forbids anyway). Cached so
    the import happens once per session.
    """
    global _setup_module
    if _setup_module is None:
        import routers.setup as setup
        _setup_module = setup
    return _setup_module


class FakeDB:
    def __init__(self):
        self.settings = {"setup_completed": "false"}
        self.users = {}

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed):
        self.users.setdefault(username, {"username": username, "email": username})["password"] = hashed
        return True

    def update_user(self, username, updates):
        self.users.setdefault(username, {"username": username}).update(updates)
        return self.users[username]


PW = "Sup3rSecret!!"


@pytest.fixture
def patched(monkeypatch):
    setup = _get_setup()
    db = FakeDB()
    monkeypatch.setattr(setup, "db", db)
    monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
    monkeypatch.setattr(setup, "hash_password", lambda p: "hashed:" + p)
    return db


def _req(email="me@acme.com", **kw):
    # email defaults so happy-path callers stay terse; pass email="" / a bad
    # value to exercise the rejection paths.
    return _get_setup().SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email=email, **kw
    )


def _run(data, bg):
    # request arg is unused by the handler body.
    return asyncio.run(_get_setup().set_admin_password(data, None, bg))


def test_email_registered_and_intake_scheduled(patched):
    bg = BackgroundTasks()
    res = _run(_req(email="Me@Acme.com", company="Acme", consent_updates=True), bg)

    assert res["email_registered"] is True
    assert patched.users["admin"]["email"] == "me@acme.com"  # normalized
    assert patched.settings["setup_completed"] == "true"
    assert len(bg.tasks) == 1  # intake scheduled (runs after response)


def test_email_only_completes_setup_cleanly(patched):
    """Email but no company/consent — completes, binds email, no intake."""
    bg = BackgroundTasks()
    res = _run(_req(email="solo@acme.com"), bg)

    assert res["email_registered"] is True
    assert patched.users["admin"]["email"] == "solo@acme.com"
    assert patched.settings["setup_completed"] == "true"
    assert len(bg.tasks) == 0


def test_email_without_consent_registers_but_no_intake(patched):
    bg = BackgroundTasks()
    res = _run(_req(email="me@acme.com", consent_updates=False), bg)

    assert res["email_registered"] is True
    assert patched.users["admin"]["email"] == "me@acme.com"
    assert len(bg.tasks) == 0


def test_invalid_email_rejected_before_any_write(patched):
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as ei:
        _run(_req(email="not-an-email", consent_updates=True), bg)

    assert ei.value.status_code == 400
    # Validated before writes — setup not marked complete, no admin email written.
    assert patched.settings["setup_completed"] == "false"
    assert "admin" not in patched.users
    assert len(bg.tasks) == 0


def test_blank_email_rejected_before_any_write(patched):
    """A present-but-blank email is rejected with 400 (required field, #49)."""
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as ei:
        _run(_req(email="   ", consent_updates=True), bg)

    assert ei.value.status_code == 400
    assert patched.settings["setup_completed"] == "false"
    assert len(bg.tasks) == 0


def test_email_is_required_at_model_layer():
    """A missing email fails request validation (→ 422 at the API layer, #49)."""
    setup = _get_setup()
    with pytest.raises(ValidationError):
        setup.SetAdminPasswordRequest(password=PW, confirm_password=PW)


def test_no_setup_token_field_on_model():
    """The setup-token field is gone (#49) — the model no longer declares it."""
    assert "setup_token" not in _get_setup().SetAdminPasswordRequest.model_fields
