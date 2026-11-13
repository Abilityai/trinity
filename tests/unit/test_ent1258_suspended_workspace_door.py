"""abilityai/trinity-enterprise#1258 — a deactivated account is refused at the
Workspace door, as it is at `/login`.

The Workspace's email-code door (`POST /api/enterprise/client-portal/auth/*`)
decided on two questions: is an agent shared with the address
(`email_has_access`), and would `/login` admit it (`platform_login_admits`).
Neither read `users.suspended_at` (the #995 primitive). A suspended platform
account whose email also held a share was therefore handled as an outside
client: it got a code, a portal session, and ran turns on the shared agent,
while `/login` refused the same person. A portal session carries an email and
no account, so a session minted before the suspension also kept working.

Ruling on the issue: suspension wins on every door. A share made before or
after it does not re-open the account; an admin reactivates it.

What is pinned here:

* the three mint paths (code request, code verify, delegated exchange) give a
  suspended account nothing, and consume no code;
* the per-request gate every portal-session consumer calls refuses it, so an
  older session stops on the next request, and works again after reactivation;
* an address with no account, and an active account, behave as before;
* a lookup that cannot answer denies;
* every consumer of a portal session goes through that gate (source guard: the
  consumer is the route layer itself, so the set is the thing to pin).
"""
from __future__ import annotations

import ast
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

SUSPENDED = "gone@example.com"     # a platform account, deactivated, with a share
ACTIVE = "member@example.com"      # a platform account in good standing
CLIENT = "client@example.com"      # an outside client: a share, no account

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))


def _core_db(*, suspended=(SUSPENDED,), users=(SUSPENDED, ACTIVE), whitelisted=(), code_ok=True):
    """The platform database: who has an account, who is deactivated."""
    db = MagicMock()
    db.get_setting_value.return_value = "true"
    db.is_email_whitelisted.side_effect = lambda e: e in whitelisted
    db.get_user_by_email.side_effect = lambda e: (
        {"username": e.split("@")[0], "password": None,
         "suspended_at": "2026-11-12T10:00:00Z" if e in suspended else None}
        if e in users else None
    )
    db.create_login_code.return_value = {"code": "111111"}
    db.verify_login_code.return_value = {"email": "x"} if code_ok else None
    return db


def _portal_db(*, shared=(SUSPENDED, ACTIVE, CLIENT), blocked=()):
    """The portal's own reads: the share roster and the block list."""
    db = MagicMock()
    db.get_shared_roster.side_effect = lambda e: [{"agent_name": "a"}] if e in shared else []
    db.is_client_blocked.side_effect = lambda e: e in blocked
    return db


@pytest.fixture(autouse=True)
def _no_policy():
    from services import login_policy_gate
    login_policy_gate.clear_provider()
    yield
    login_policy_gate.clear_provider()


# ------------------------------------------------------------ the predicate

class TestEmailAccountSuspended:
    def test_a_deactivated_account_is_suspended(self):
        from client_portal import service
        with patch("database.db", _core_db()):
            assert service.email_account_suspended(SUSPENDED) is True

    def test_the_address_is_matched_as_it_is_stored(self):
        from client_portal import service
        with patch("database.db", _core_db()):
            assert service.email_account_suspended("  Gone@Example.com ") is True

    def test_an_active_account_is_not(self):
        from client_portal import service
        with patch("database.db", _core_db()):
            assert service.email_account_suspended(ACTIVE) is False

    def test_an_address_with_no_account_is_not(self):
        """An outside client has no `users` row; the share and the block
        govern it, as before."""
        from client_portal import service
        with patch("database.db", _core_db()):
            assert service.email_account_suspended(CLIENT) is False

    def test_no_address_is_not(self):
        from client_portal import service
        with patch("database.db", _core_db()):
            assert service.email_account_suspended(None) is False
            assert service.email_account_suspended("") is False

    def test_a_lookup_that_fails_denies(self):
        from client_portal import service
        db = _core_db()
        db.get_user_by_email.side_effect = RuntimeError("db down")
        with patch("database.db", db):
            assert service.email_account_suspended(ACTIVE) is True


# ------------------------------------------------------------ the mint paths

class TestAccess:
    def test_a_share_does_not_reopen_a_deactivated_account(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()):
            assert service.email_has_access(SUSPENDED) is False

    def test_control_an_outside_client_with_a_share_has_access(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()):
            assert service.email_has_access(CLIENT) is True

    def test_control_an_active_account_with_a_share_has_access(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()):
            assert service.email_has_access(ACTIVE) is True

    def test_reactivating_restores_access(self):
        from client_portal import service
        with patch("database.db", _core_db(suspended=())), patch.object(service, "db", _portal_db()):
            assert service.email_has_access(SUSPENDED) is True

    def test_a_block_still_refuses_and_is_read_first(self):
        from client_portal import service
        core = _core_db()
        with patch("database.db", core), patch.object(service, "db", _portal_db(blocked={CLIENT})):
            assert service.email_has_access(CLIENT) is False
        core.get_user_by_email.assert_not_called()


class TestRequest:
    def test_a_deactivated_account_with_a_share_gets_no_code(self):
        from client_portal import service
        core = _core_db()
        with patch("database.db", core), patch.object(service, "db", _portal_db()):
            assert service.portal_signin_request(SUSPENDED) is None
        core.create_login_code.assert_not_called()

    def test_a_deactivated_account_login_would_admit_gets_no_code(self):
        """The second reason a code is issued (#3922: `/login` would admit the
        address) does not issue one either."""
        from client_portal import service
        core = _core_db(whitelisted={SUSPENDED})
        with patch("database.db", core), patch.object(service, "db", _portal_db(shared=())):
            assert service.platform_login_admits(SUSPENDED) is True   # the branch under test
            assert service.portal_signin_request(SUSPENDED) is None
        core.create_login_code.assert_not_called()

    def test_control_an_outside_client_gets_a_code(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()):
            assert service.portal_signin_request(CLIENT) == "111111"

    def test_control_an_active_account_gets_a_code(self):
        from client_portal import service
        with patch("database.db", _core_db(whitelisted={ACTIVE})), \
             patch.object(service, "db", _portal_db(shared=())):
            assert service.portal_signin_request(ACTIVE) == "111111"


class TestVerify:
    def test_a_deactivated_account_gets_no_session_and_the_code_is_not_redeemed(self):
        """A code issued just before the suspension is still live; it must not
        become a session, and the answer is the wrong-code answer (#186)."""
        from client_portal import service
        core = _core_db()
        mint = MagicMock(return_value="tok")
        with patch("database.db", core), patch.object(service, "db", _portal_db()), \
             patch("dependencies.create_portal_session_token", mint):
            assert service.portal_signin_verify(SUSPENDED, "111111") is None
        mint.assert_not_called()
        core.verify_login_code.assert_not_called()

    def test_a_deactivated_account_is_not_handed_off_to_login(self):
        from client_portal import service
        core = _core_db(whitelisted={SUSPENDED})
        with patch("database.db", core), patch.object(service, "db", _portal_db()):
            result = service.portal_signin_verify(SUSPENDED, "111111")
        assert result is None
        assert result is not service.PLATFORM_LOGIN_REQUIRED

    def test_control_an_outside_client_gets_a_session(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            assert service.portal_signin_verify(CLIENT, "111111") == "tok"

    def test_control_an_active_account_is_still_sent_to_login(self):
        from client_portal import service
        with patch("database.db", _core_db(whitelisted={ACTIVE})), \
             patch.object(service, "db", _portal_db()):
            assert service.portal_signin_verify(ACTIVE, "111111") is service.PLATFORM_LOGIN_REQUIRED


class TestExchange:
    def test_a_delegate_key_cannot_mint_for_a_deactivated_account(self):
        from client_portal import service
        mint = MagicMock(return_value="tok")
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()), \
             patch("dependencies.create_portal_session_token", mint):
            assert service.portal_exchange(SUSPENDED) is None
        mint.assert_not_called()

    def test_control_a_delegate_key_mints_for_an_outside_client(self):
        from client_portal import service
        with patch("database.db", _core_db()), patch.object(service, "db", _portal_db()), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            assert service.portal_exchange(CLIENT) == "tok"


# ------------------------------------------------------------ a live session

class TestLiveSession:
    """`_reject_if_blocked` is the check every consumer of a portal session
    makes before it serves the request (the guard below pins that set)."""

    def _gate(self, email, core, portal=None):
        from client_portal import portal_auth
        with patch("database.db", core), \
             patch("client_portal.db.is_client_blocked",
                   (portal or _portal_db()).is_client_blocked):
            portal_auth._reject_if_blocked(email)

    def test_a_session_of_an_account_suspended_since_is_refused(self):
        with pytest.raises(HTTPException) as exc:
            self._gate(SUSPENDED, _core_db())
        assert exc.value.status_code == 403
        assert "@" not in str(exc.value.detail)

    def test_the_same_session_works_before_the_suspension_and_after_reactivation(self):
        self._gate(SUSPENDED, _core_db(suspended=()))

    def test_control_an_outside_clients_session_is_served(self):
        self._gate(CLIENT, _core_db())

    def test_control_a_blocked_client_is_still_refused(self):
        with pytest.raises(HTTPException) as exc:
            self._gate(CLIENT, _core_db(), _portal_db(blocked={CLIENT}))
        assert exc.value.status_code == 403

    def test_a_lookup_that_fails_refuses_the_session(self):
        core = _core_db()
        core.get_user_by_email.side_effect = RuntimeError("db down")
        with pytest.raises(HTTPException) as exc:
            self._gate(CLIENT, core)
        assert exc.value.status_code == 403


# ------------------------------------------------------------ the consumer set

def _functions_calling(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            called = {
                (c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", None))
                for c in ast.walk(node) if isinstance(c, ast.Call)
            }
            if name in called:
                yield node.name, called


def test_every_consumer_of_a_portal_session_goes_through_the_gate():
    """@source-text-pin: the consumer of this rule is the route layer itself.
    A portal session carries an email and no account, so the only thing that
    stops a deactivated account's session is the gate each decoder calls. A new
    `decode_portal_session` caller that skips it re-opens ent#1258."""
    found = []
    for root, dirs, files in os.walk(_BACKEND):
        dirs[:] = [d for d in dirs if d not in {"enterprise", "__pycache__", "migrations"}]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, _BACKEND)
            if rel == "dependencies.py":       # the decoder and its token helpers
                continue
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if "decode_portal_session" not in src:
                continue
            for fn, called in _functions_calling(ast.parse(src), "decode_portal_session"):
                found.append((rel, fn, "_reject_if_blocked" in called))
    assert found, "no consumer of decode_portal_session was found: the scan is blind"
    assert {(rel, fn) for rel, fn, _ in found} >= {
        (os.path.join("client_portal", "portal_auth.py"), "get_portal_principal"),
        (os.path.join("client_portal", "portal_auth.py"), "workspace_email_from_request"),
        (os.path.join("routers", "terms.py"), "terms_caller"),
    }
    ungated = [(rel, fn) for rel, fn, gated in found if not gated]
    assert not ungated, f"portal-session consumers that skip the gate: {ungated}"
