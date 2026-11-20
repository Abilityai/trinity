"""trinity-enterprise#1312 — an emailed setup link bound to one email address.

`ADMIN_PASSWORD_SOURCE=instance-id` reads `/data/setup-claim` (#3004). That file
now has two forms:

1. one line, compared trimmed and lowercased (the EC2 instance ID) — unchanged;
2. one line `email-bound:<secret>`, `<secret>` = 64 lowercase hex. A request
   must then carry an email, and its claim code must be
   `hex(HMAC-SHA256(bytes.fromhex(secret), email.strip().lower()))`.

The admin email is bound by the claim itself, so a provisioner can build the
claim before anyone has signed up and an emailed setup link carries the code.

Under test: the email-bound form passes only for the email it was derived for
(case/whitespace-insensitive), every failure is the one generic 403 with one
failed-attempt record and no password work, a malformed secret fails closed, the
instance-ID form is untouched (a 64-hex form-1 claim is still a literal compare),
a recycle between the check and the take is refused and never clobbered, and
nothing logs the code, the secret or the email.

Import isolation: `routers.setup` is imported lazily (see
test_setup_operator_profile.py for why).
"""
import asyncio
import hashlib
import hmac
import logging

import pytest
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
EMAIL = "owner@acme.com"
SECRET = "a1" * 32
OTHER_SECRET = "b2" * 32
MISMATCH = "That instance ID does not match this server."
IP = "198.51.100.7"


def link_code(secret: str, email: str) -> str:
    """The contract's reference computation (what the sender and provisioner build)."""
    key = bytes.fromhex(secret)
    msg = email.strip().lower().encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


CODE = link_code(SECRET, EMAIL)


def _setup():
    import routers.setup as m
    return m


class FakeDB:
    def __init__(self):
        self.settings = {"setup_completed": "false"}
        self.users = {}
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
    def __init__(self, db, claim_file, calls):
        self.db, self.claim_file, self.calls = db, claim_file, calls

    @property
    def failures(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "record" and c[2] is False]

    @property
    def successes(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "record" and c[2] is True]

    def claim_files(self):
        return sorted(p.name for p in self.claim_file.parent.glob("setup-claim*"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    def _apply(claim=f"email-bound:{SECRET}\n"):
        setup = _setup()
        db = FakeDB()
        calls = []
        claim_file = tmp_path / "setup-claim"
        if claim is not None:
            claim_file.write_text(claim)
        monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", "instance-id")
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.setattr(setup, "db", db)
        monkeypatch.setattr(setup, "_SETUP_CLAIM_PATH", claim_file)
        monkeypatch.setattr(setup, "validate_password_strength", lambda p: calls.append("validate") or [])
        monkeypatch.setattr(setup, "hash_password", lambda p: calls.append("hash") or "hashed:" + p)
        monkeypatch.setattr(setup, "_get_client_ip", lambda request: IP)
        monkeypatch.setattr(setup, "check_login_rate_limit", lambda ip, account=None: calls.append(("check", ip)))
        monkeypatch.setattr(
            setup, "record_login_attempt",
            lambda ip, success, account=None: calls.append(("record", ip, success, account)),
        )
        return Env(db, claim_file, calls)
    return _apply


def _post(claim_code=CODE, email=EMAIL):
    setup = _setup()
    data = setup.SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email=email, claim_code=claim_code,
    )
    return asyncio.run(setup.set_admin_password(data, object(), BackgroundTasks()))


def _refused(e, **kw):
    """Assert the one generic refusal: 403, one failure record, no password work."""
    with pytest.raises(HTTPException) as ei:
        _post(**kw)
    assert ei.value.status_code == 403
    assert ei.value.detail == MISMATCH
    assert len(e.failures) == 1 and e.successes == []
    assert "validate" not in e.calls and "hash" not in e.calls
    assert e.db.password_writes == [] and e.db.settings["setup_completed"] == "false"


# --------------------------------------------------------------------------
# the email-bound form
# --------------------------------------------------------------------------

def test_right_code_and_right_email_create_the_admin_bound_to_that_email(env):
    e = env()
    out = _post()
    assert out["success"] is True and out["email_registered"] is True
    assert e.db.password_writes == [("admin", "hashed:" + PW)]
    assert e.db.users["admin"]["email"] == EMAIL, "the bound email is the sign-in identity"
    assert e.db.settings["setup_completed"] == "true"
    assert len(e.successes) == 1 and e.failures == []
    assert e.claim_files() == [], "the claim is deleted, no taken copy left"


@pytest.mark.parametrize("given_email", ["  OWNER@Acme.COM ", "Owner@acme.com\t"])
def test_other_case_or_padded_email_passes_when_it_normalises_to_the_same_address(env, given_email):
    e = env()
    assert _post(email=given_email)["success"] is True
    assert e.db.users["admin"]["email"] == EMAIL


def test_the_code_is_normalised_like_an_instance_id(env):
    e = env()
    assert _post(claim_code=CODE.upper())["success"] is True
    assert e.db.password_writes


def test_the_code_fits_the_unchanged_64_char_field(env):
    from pydantic import ValidationError

    env()
    assert len(CODE) == 64
    with pytest.raises(ValidationError):
        _post(claim_code=CODE + "0")


def test_right_code_with_another_email_is_the_generic_403(env):
    e = env()
    _refused(e, email="intruder@acme.com")
    assert e.claim_file.read_text() == f"email-bound:{SECRET}\n", "a refusal never consumes the claim"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_email_is_the_generic_403(env, blank):
    e = env()
    _refused(e, email=blank)


def test_code_derived_from_another_secret_is_the_generic_403(env):
    e = env()
    _refused(e, claim_code=link_code(OTHER_SECRET, EMAIL))


@pytest.mark.parametrize("bad_code", ["", None, SECRET, CODE[:-1], CODE[:-1] + "0" if CODE[-1] != "0" else CODE[:-1] + "1"])
def test_wrong_or_missing_code_is_the_generic_403(env, bad_code):
    e = env()
    _refused(e, claim_code=bad_code)


@pytest.mark.parametrize(
    "line",
    [
        "email-bound:",
        "email-bound:" + "a1" * 31,          # 62 hex: too short
        "email-bound:" + "a1" * 32 + "0",    # 65 hex: too long
        "email-bound:" + "A1" * 32,          # uppercase hex is not the format
        "email-bound:" + "g1" * 32,          # not hex
        "email-bound: " + "a1" * 32,         # inner whitespace
        "email-bound:" + "a1" * 32 + "\nextra",
    ],
)
def test_a_malformed_secret_line_refuses_every_setup(env, line):
    e = env(claim=line)
    # Even a code derived the way a broken provisioner might have derived it.
    candidate = line[len("email-bound:"):].strip()[:64]
    attempt = link_code(candidate, EMAIL) if len(candidate) == 64 and all(
        c in "0123456789abcdefABCDEF" for c in candidate) else CODE
    _refused(e, claim_code=attempt)


def test_the_prefix_itself_is_never_accepted_as_a_literal_claim(env):
    """A form-2 file is never compared literally, whatever its content."""
    e = env(claim="email-bound:not-hex")
    _refused(e, claim_code="email-bound:not-hex")


# --------------------------------------------------------------------------
# the instance-ID form is untouched
# --------------------------------------------------------------------------

def test_a_form_1_file_holding_64_hex_is_still_a_literal_compare(env):
    e = env(claim=SECRET + "\n")
    _refused(e, claim_code=link_code(SECRET, EMAIL))


def test_a_form_1_file_holding_64_hex_accepts_its_literal_value(env):
    e = env(claim=SECRET + "\n")
    assert _post(claim_code=SECRET, email="")["success"] is True
    assert e.claim_files() == []


def test_a_form_1_file_still_allows_a_blank_email(env):
    e = env(claim="i-0abc1234def567890")
    out = _post(claim_code="i-0abc1234def567890", email="")
    assert out["success"] is True and out["email_registered"] is False


# --------------------------------------------------------------------------
# status never reveals the form
# --------------------------------------------------------------------------

def test_status_is_identical_for_both_forms(env):
    env()
    bound = asyncio.run(_setup().get_setup_status())
    env(claim="i-0abc1234def567890")
    plain = asyncio.run(_setup().get_setup_status())
    assert bound == plain == {"setup_completed": False, "setup_available": True, "claim_required": "instance-id"}
    assert SECRET not in repr(bound) and "email" not in repr(bound)


# --------------------------------------------------------------------------
# recycle races: the take re-verifies, the restore never clobbers
# --------------------------------------------------------------------------

def _recycle(path, secret):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"email-bound:{secret}\n")
    tmp.rename(path)


def test_a_recycle_between_check_and_take_is_refused_and_keeps_the_new_claim(env, monkeypatch):
    e = env()
    setup = _setup()

    def _recycled_meanwhile(p):
        e.calls.append("validate")
        _recycle(e.claim_file, OTHER_SECRET)
        return []

    monkeypatch.setattr(setup, "validate_password_strength", _recycled_meanwhile)
    with pytest.raises(HTTPException) as ei:
        _post()
    assert ei.value.status_code == 403 and ei.value.detail == MISMATCH
    assert e.db.password_writes == [] and "hash" not in e.calls
    assert e.claim_file.read_text() == f"email-bound:{OTHER_SECRET}\n", "the new link must survive"
    assert e.claim_files() == ["setup-claim"], "no taken copy left behind"

    # ...and the person the new link was sent to can still complete setup.
    monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
    assert _post(claim_code=link_code(OTHER_SECRET, "next@acme.com"), email="next@acme.com")["success"] is True


def test_a_crash_after_the_take_restores_the_claim(env, monkeypatch):
    e = env()
    setup = _setup()

    def _boom(p):
        raise RuntimeError("bcrypt backend missing")

    monkeypatch.setattr(setup, "hash_password", _boom)
    with pytest.raises(RuntimeError):
        _post()
    assert e.claim_file.read_text() == f"email-bound:{SECRET}\n", "the owner must be able to retry"
    assert e.claim_files() == ["setup-claim"]


def test_a_crash_restore_never_overwrites_a_claim_recycled_meanwhile(env, monkeypatch):
    e = env()
    setup = _setup()

    def _recycle_then_boom(p):
        _recycle(e.claim_file, OTHER_SECRET)
        raise RuntimeError("bcrypt backend missing")

    monkeypatch.setattr(setup, "hash_password", _recycle_then_boom)
    with pytest.raises(RuntimeError):
        _post()
    assert e.claim_file.read_text() == f"email-bound:{OTHER_SECRET}\n", "the newer claim wins"
    assert e.claim_files() == ["setup-claim"]


def test_a_form_1_crash_restore_never_overwrites_a_newer_claim_either(env, monkeypatch):
    e = env(claim="i-0abc1234def567890")
    setup = _setup()

    def _recycle_then_boom(p):
        _recycle(e.claim_file, OTHER_SECRET)
        raise RuntimeError("boom")

    monkeypatch.setattr(setup, "hash_password", _recycle_then_boom)
    with pytest.raises(RuntimeError):
        _post(claim_code="i-0abc1234def567890", email="")
    assert e.claim_file.read_text() == f"email-bound:{OTHER_SECRET}\n"


# --------------------------------------------------------------------------
# nothing is logged
# --------------------------------------------------------------------------

def test_no_code_secret_or_email_reaches_the_logs(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    e = env()
    with pytest.raises(HTTPException):
        _post(email="intruder@acme.com")
    env(claim="email-bound:" + "zz" * 32)
    with pytest.raises(HTTPException):
        _post()
    env()
    assert _post()["success"] is True
    text = "\n".join(r.getMessage() for r in caplog.records)
    for value in (SECRET, CODE, EMAIL, "intruder@acme.com", "zz" * 32):
        assert value not in text
