"""#3004 — `ADMIN_PASSWORD_SOURCE=instance-id`: the first admin needs the EC2 instance ID.

AWS Marketplace review requires that creating the first user proves the buyer
controls the instance with a value unique to it (the instance ID), asks for no
PII, and presets no password (PROV-018). Provisioning writes the instance ID to
`/data/setup-claim`; the backend reads it, never IMDS.

Under test:

* `GET /api/setup/status` advertises `claim_required` for this mode only;
* the POST checks the claim after the existing-admin refusal and before any
  password work, rate-limited per client IP, one generic 403 for every failure,
  fail-closed on a missing/empty claim file;
* email is optional on this path only;
* the claim file is deleted after success, and a failed delete never fails setup.

Import isolation: `routers.setup` is imported lazily (see
test_setup_operator_profile.py for why).
"""
import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
HASH = "$2b$12$abcdefghijklmnopqrstuv"
IID = "i-0abc1234def567890"
MISMATCH = "That instance ID does not match this server."


def _setup():
    import routers.setup as m
    return m


class FakeDB:
    def __init__(self, users=None):
        self.settings = {"setup_completed": "false"}
        self.users = users or {}
        self.password_writes = []
        self.user_updates = []

    def get_user_by_username(self, username):
        return self.users.get(username)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed):
        self.password_writes.append((username, hashed))
        self.users.setdefault(username, {"username": username})["password"] = hashed
        return True

    def update_user(self, username, updates):
        self.user_updates.append((username, updates))
        self.users.setdefault(username, {"username": username}).update(updates)
        return self.users[username]


class Env:
    """Patched router + a record of what the rate limiter and hasher saw."""

    def __init__(self, db, claim_file, calls, limited):
        self.db, self.claim_file, self.calls, self.limited = db, claim_file, calls, limited


@pytest.fixture
def env(monkeypatch, tmp_path):
    def _apply(mode="instance-id", claim=IID, users=None, limited=False):
        setup = _setup()
        db = FakeDB(users)
        calls = []
        claim_file = tmp_path / "setup-claim"
        if claim is not None:
            claim_file.write_text(claim)
        if mode is None:
            monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
        else:
            monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", mode)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.setattr(setup, "db", db)
        monkeypatch.setattr(setup, "_SETUP_CLAIM_PATH", claim_file)
        monkeypatch.setattr(setup, "validate_password_strength", lambda p: calls.append("validate") or [])
        monkeypatch.setattr(setup, "hash_password", lambda p: calls.append("hash") or "hashed:" + p)
        monkeypatch.setattr(setup, "_get_client_ip", lambda request: "198.51.100.7")

        def _check(ip, account=None):
            calls.append(("check", ip, account))
            if limited:
                raise HTTPException(status_code=429, detail="Too many attempts")
            return True

        monkeypatch.setattr(setup, "check_login_rate_limit", _check)
        monkeypatch.setattr(
            setup, "record_login_attempt",
            lambda ip, success, account=None: calls.append(("record", ip, success, account)),
        )
        return Env(db, claim_file, calls, limited)
    return _apply


def _post(claim_code=IID, email="me@acme.com", consent=False, bg=None):
    setup = _setup()
    data = setup.SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email=email,
        claim_code=claim_code, consent_updates=consent,
    )
    return asyncio.run(setup.set_admin_password(data, object(), bg or BackgroundTasks()))


def _expect(status, **kw):
    with pytest.raises(HTTPException) as ei:
        _post(**kw)
    assert ei.value.status_code == status
    return ei.value


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mode", "expected"),
    [("instance-id", "instance-id"), ("browser", None), (None, None), ("unset", None), ("INSTANCE-ID", None)],
)
def test_status_advertises_the_claim_only_in_instance_id_mode(env, mode, expected):
    env(mode=mode)
    out = asyncio.run(_setup().get_setup_status())
    assert out["claim_required"] == expected
    assert IID not in repr(out), "status must never carry the claim value"


# --------------------------------------------------------------------------
# claim check
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["i-0000000000000000", "", None, "  ", IID + "0"])
def test_wrong_or_missing_claim_is_a_generic_403_before_password_work(env, bad):
    e = env()
    err = _expect(403, claim_code=bad)
    assert err.detail == MISMATCH
    assert "hash" not in e.calls and "validate" not in e.calls
    assert ("record", "198.51.100.7", False, None) in e.calls
    assert e.db.password_writes == []
    assert e.claim_file.exists(), "a failed claim must not consume the claim"


def test_non_ascii_claim_is_a_403_not_a_500(env):
    env()
    assert _expect(403, claim_code="i-0abcé中").detail == MISMATCH


@pytest.mark.parametrize("stored", [None, "", "   \n"])
def test_missing_or_empty_claim_file_fails_closed(env, stored):
    e = env(claim=stored)
    assert _expect(403, claim_code="").detail == MISMATCH
    assert _expect(403, claim_code=IID).detail == MISMATCH
    assert e.db.password_writes == []


def test_rate_limit_is_checked_per_ip_before_the_compare(env):
    e = env(limited=True)
    _expect(429, claim_code=IID)
    assert e.calls == [("check", "198.51.100.7", None)], (
        "per-IP bucket only (no account: a shared account bucket would let anyone "
        "lock the owner out), and nothing after the 429"
    )
    assert e.db.password_writes == []


def test_existing_admin_refusal_runs_before_the_claim_check(env):
    e = env(users={"admin": {"username": "admin", "password": HASH}})
    err = _expect(403, claim_code="wrong")
    assert err.detail != MISMATCH
    assert e.calls == [], "rate limiter and hasher untouched on a provisioned install"


@pytest.mark.parametrize("given", [IID, f"  {IID.upper()}\n", f"\t{IID} "])
def test_correct_claim_creates_the_admin_and_deletes_the_claim(env, given):
    e = env(claim=IID + "\n")
    out = _post(claim_code=given)
    assert out["success"] is True
    assert e.db.password_writes == [("admin", "hashed:" + PW)]
    assert e.db.settings["setup_completed"] == "true"
    assert not e.claim_file.exists(), "the instance ID must stop being a credential"
    assert list(e.claim_file.parent.glob("setup-claim*")) == [], "no claimed copy left behind"
    assert ("record", "198.51.100.7", True, None) in e.calls, "a correct claim clears the IP's failures"
    assert not any(c[0] == "record" and c[2] is False for c in e.calls if isinstance(c, tuple))


def test_stored_claim_is_normalised_too(env):
    e = env(claim=f"  {IID.upper()}  \n")
    assert _post(claim_code=IID)["success"] is True
    assert e.db.password_writes


def test_a_failed_claim_delete_never_fails_setup(env, monkeypatch):
    import pathlib

    e = env()

    def _no_unlink(self, missing_ok=False):
        raise PermissionError("read-only")

    monkeypatch.setattr(pathlib.Path, "unlink", _no_unlink)
    assert _post()["success"] is True
    assert e.db.settings["setup_completed"] == "true"


def test_a_claim_consumed_by_a_concurrent_request_is_a_403(env, monkeypatch):
    """Two requests with the right ID: both pass the compare, only the one that
    takes the file creates the admin. Simulated by the file vanishing between
    the compare and the claim (another request took it)."""
    e = env()
    setup = _setup()

    def _other_request_wins(p):
        e.calls.append("validate")
        e.claim_file.unlink()
        return []

    monkeypatch.setattr(setup, "validate_password_strength", _other_request_wins)
    assert _expect(403, claim_code=IID).detail == MISMATCH
    assert e.db.password_writes == [] and "hash" not in e.calls


def test_second_request_after_the_claim_is_consumed_is_a_403(env):
    e = env()
    assert _post()["success"] is True
    e.db.users.clear()  # even if the existing-admin refusal were bypassed
    assert _expect(403, claim_code=IID).detail == MISMATCH


def test_failed_validation_keeps_the_claim_for_a_retry(env, monkeypatch):
    e = env()
    setup = _setup()
    monkeypatch.setattr(setup, "validate_password_strength", lambda p: ["too weak"])
    _expect(400, claim_code=IID)
    assert e.claim_file.read_text() == IID

    monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
    _expect(400, claim_code=IID, email="not-an-email")
    assert e.claim_file.read_text() == IID

    assert _post(claim_code=IID)["success"] is True


def test_a_crash_after_the_claim_puts_it_back(env, monkeypatch):
    e = env()
    setup = _setup()

    def _boom(p):
        raise RuntimeError("bcrypt backend missing")

    monkeypatch.setattr(setup, "hash_password", _boom)
    with pytest.raises(RuntimeError):
        _post(claim_code=IID)
    assert e.claim_file.read_text() == IID, "the owner must be able to retry"
    assert [p.name for p in e.claim_file.parent.glob("setup-claim*")] == ["setup-claim"]


@pytest.mark.parametrize("mode", ["browser", None])
def test_claim_code_is_ignored_outside_instance_id_mode(env, mode):
    e = env(mode=mode, claim=None)
    assert _post(claim_code="anything")["success"] is True
    assert not any(isinstance(c, tuple) for c in e.calls), "no rate limiting outside the claim path"


# --------------------------------------------------------------------------
# email optional on this path only
# --------------------------------------------------------------------------

def test_blank_email_succeeds_on_instance_id_and_signs_in_by_username(env):
    e = env()
    bg = BackgroundTasks()
    out = _post(email="  ", consent=True, bg=bg)
    assert out == {"success": True, "email_registered": False, "username": "admin"}
    assert e.db.user_updates == [], "no email to bind"
    assert bg.tasks and all(t.func.__name__ != "submit_operator_intake" for t in bg.tasks), (
        "the operator intake sends the email; with none it must not run"
    )


def test_given_email_is_still_shape_checked_and_bound_on_instance_id(env):
    e = env()
    assert _expect(400, email="not-an-email").detail == "A valid admin email is required"
    assert e.db.password_writes == []
    out = _post(email="Me@Acme.com")
    assert out["email_registered"] is True
    assert e.db.user_updates == [("admin", {"email": "me@acme.com"})]


@pytest.mark.parametrize("mode", ["browser", None])
def test_blank_email_still_rejected_on_every_other_path(env, mode):
    e = env(mode=mode, claim=None)
    assert _expect(400, email="").detail == "A valid admin email is required"
    assert e.db.password_writes == []


# --------------------------------------------------------------------------
# verify-platform.sh accepts the blank password in claim mode
# --------------------------------------------------------------------------

def test_verify_platform_treats_instance_id_like_browser():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "scripts/deploy/verify-platform.sh").read_text()
    m = re.search(r"grep -qE '(\^ADMIN_PASSWORD_SOURCE=[^']*)' \.env", text)
    assert m, "verify-platform.sh no longer greps the claim marker"
    pattern = m.group(1)
    for ok in ("ADMIN_PASSWORD_SOURCE=browser", "ADMIN_PASSWORD_SOURCE=instance-id"):
        assert re.search(pattern, ok), ok
    for bad in ("ADMIN_PASSWORD_SOURCE=unset", "ADMIN_PASSWORD_SOURCE=instance-idx"):
        assert not re.search(pattern, bad), bad
