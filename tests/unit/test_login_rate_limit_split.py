"""Regression tests for #591: split per-account / per-IP login rate limit.

The previous design used a single per-IP bucket at 5 fails / 10 min. Any
user behind a corporate NAT / VPN / CDN locked out everyone else at the
same egress IP after just four bad attempts, and a rotating-proxy
attacker could keep an organisation locked out continuously.

The fix splits the bucket: a tight per-account bucket (5 / 15 min) limits
credential stuffing on one account, and a loose per-IP bucket (30 / 5 min)
catches single-source abuse without locking shared-IP users.

Module under test: src/backend/routers/auth.py — `check_login_rate_limit`
and `record_login_attempt` only. Heavy cascading deps are stubbed so the
test stays a true unit test (no DB, no FastAPI app boot).
"""
from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


# ---------------------------------------------------------------------------
# In-memory Redis double — only the operations the rate-limit code uses.
# ---------------------------------------------------------------------------

class FakeRedisPipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, seconds):
        self._ops.append(("expire", key, seconds))
        return self

    def execute(self):
        for op in self._ops:
            if op[0] == "incr":
                k = op[1]
                cur = self._store.get(k, {"value": 0, "expires_at": None})
                cur["value"] = int(cur["value"]) + 1
                self._store[k] = cur
            elif op[0] == "expire":
                k, sec = op[1], op[2]
                if k in self._store:
                    self._store[k]["expires_at"] = time.monotonic() + sec
        self._ops.clear()


class FakeRedis:
    """Subset of redis.Redis used by check_login_rate_limit / record_login_attempt."""

    def __init__(self):
        self._store: dict = {}

    def _evict(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None
        exp = entry.get("expires_at")
        if exp is not None and time.monotonic() >= exp:
            self._store.pop(key, None)
            return None
        return entry

    def get(self, key):
        entry = self._evict(key)
        return None if entry is None else str(entry["value"])

    def delete(self, key):
        self._store.pop(key, None)

    def ttl(self, key):
        entry = self._evict(key)
        if entry is None or entry.get("expires_at") is None:
            return -1
        return max(0, int(entry["expires_at"] - time.monotonic()))

    def pipeline(self):
        return FakeRedisPipeline(self._store)

    def ping(self):
        return True


# ---------------------------------------------------------------------------
# Surgical loader for auth.py — stubs cascading deps so we don't boot the DB.
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_module(monkeypatch):
    """Load ``routers.auth`` with cascading deps stubbed.

    The rate-limit functions only touch ``logger``, ``redis``,
    ``HTTPException``, and ``status`` — none of the stubbed names are
    exercised, but Python still resolves them at import time.
    """
    # Stub everything auth.py imports apart from the stdlib + fastapi + jose
    # + redis, which are real deps already available in the test venv.
    stubs: dict[str, types.ModuleType] = {}

    config_mod = types.ModuleType("config")
    config_mod.SECRET_KEY = "test"
    config_mod.ALGORITHM = "HS256"
    config_mod.ACCESS_TOKEN_EXPIRE_MINUTES = 60
    config_mod.EMAIL_AUTH_ENABLED = False
    config_mod.REDIS_URL = "redis://stub"
    # auth.py grew a PUBLIC_ACCESS_REQUESTS_ENABLED dependency (trinity-enterprise#10);
    # keep this stub in sync or its import fails under HEAD.
    config_mod.PUBLIC_ACCESS_REQUESTS_ENABLED = False
    stubs["config"] = config_mod

    database_mod = types.ModuleType("database")
    database_mod.db = types.SimpleNamespace()
    database_mod.EmailLoginRequest = type("EmailLoginRequest", (), {})
    database_mod.EmailLoginVerify = type("EmailLoginVerify", (), {})
    database_mod.EmailLoginResponse = type("EmailLoginResponse", (), {})
    stubs["database"] = database_mod

    deps_mod = types.ModuleType("dependencies")
    deps_mod.authenticate_user = lambda *a, **k: None
    deps_mod.create_access_token = lambda *a, **k: ""
    # auth.py grew these imports via the #187 JWT-revocation/logout work; keep the
    # dependencies stub in sync or `from dependencies import (...)` fails at import.
    # None are exercised by the rate-limit paths — bare lambdas resolve the import.
    deps_mod.get_current_user = lambda *a, **k: None
    deps_mod.is_token_revoked = lambda *a, **k: False
    deps_mod.oauth2_scheme = lambda *a, **k: None
    deps_mod.revoke_token_jti = lambda *a, **k: None
    stubs["dependencies"] = deps_mod

    # Token must be a real Pydantic model — FastAPI validates response_model=
    # at decorator time when the module is imported.
    from pydantic import BaseModel

    class _Token(BaseModel):
        access_token: str
        token_type: str

    models_mod = types.ModuleType("models")
    models_mod.Token = _Token
    # auth.py also imports `User` (a `Depends(get_current_user)` param annotation,
    # not a response_model) via the #187 logout endpoint — a bare class resolves
    # the import; it is never instantiated on the rate-limit paths under test.
    models_mod.User = type("User", (), {})
    stubs["models"] = models_mod

    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []
    audit_mod = types.ModuleType("services.platform_audit_service")
    audit_mod.platform_audit_service = types.SimpleNamespace(
        log=lambda *a, **k: None,
    )
    audit_mod.AuditEventType = types.SimpleNamespace(AUTHENTICATION="authentication")
    stubs["services"] = services_pkg
    stubs["services.platform_audit_service"] = audit_mod

    routers_pkg = types.ModuleType("routers")
    routers_pkg.__path__ = []
    stubs["routers"] = routers_pkg

    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location(
        "routers.auth_under_test",
        str(_BACKEND / "routers" / "auth.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_redis(auth_module, monkeypatch):
    fr = FakeRedis()
    monkeypatch.setattr(auth_module, "_redis_client", fr)
    monkeypatch.setattr(auth_module, "get_redis_client", lambda: fr)
    return fr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPerAccountIsolation:
    """The whole point of #591: account-A bad attempts must not lock account-B."""

    def test_per_account_lockout_does_not_affect_other_account(self, auth_module, fake_redis):
        """Five bad logins for `admin` from one IP must not lock `bob@example.com`
        — even from the same IP — because the two accounts have independent
        per-account buckets and the per-IP bucket has not yet been exhausted.
        """
        from fastapi import HTTPException

        ip = "203.0.113.10"
        for _ in range(auth_module.LOGIN_ACCOUNT_LIMIT):
            auth_module.record_login_attempt(ip, success=False, account="admin")

        with pytest.raises(HTTPException) as exc:
            auth_module.check_login_rate_limit(ip, account="admin")
        assert exc.value.status_code == 429
        assert "this account" in exc.value.detail

        # Different account from same IP: still allowed.
        assert auth_module.check_login_rate_limit(ip, account="bob@example.com") is True

    def test_per_account_lockout_applies_across_ips(self, auth_module, fake_redis):
        """The per-account bucket is global by design — the attack surface is
        a targeted account, not the source IP. So `admin` locked from IP-A
        stays locked from IP-B."""
        from fastapi import HTTPException

        ip_a = "203.0.113.10"
        ip_b = "198.51.100.20"

        for _ in range(auth_module.LOGIN_ACCOUNT_LIMIT):
            auth_module.record_login_attempt(ip_a, success=False, account="admin")

        with pytest.raises(HTTPException):
            auth_module.check_login_rate_limit(ip_b, account="admin")

        # Different account from a different IP: free.
        assert auth_module.check_login_rate_limit(ip_b, account="alice@example.com") is True


class TestPerIpBucket:
    """The per-IP bucket is the loose secondary protection."""

    def test_per_ip_threshold_loose_enough_for_shared_nat(self, auth_module, fake_redis):
        """29 fails (under the 30 limit) from a NAT egress IP must not lock
        out a legitimate user behind that NAT."""
        ip = "203.0.113.10"
        for i in range(auth_module.LOGIN_IP_LIMIT - 1):
            auth_module.record_login_attempt(ip, success=False, account=f"user{i}@x.com")
        assert auth_module.check_login_rate_limit(ip, account="legit@example.com") is True

    def test_per_ip_lockout_when_threshold_crossed(self, auth_module, fake_redis):
        """Once 30 fails accumulate from one IP, that IP is locked out."""
        from fastapi import HTTPException

        ip = "203.0.113.10"
        for i in range(auth_module.LOGIN_IP_LIMIT):
            auth_module.record_login_attempt(ip, success=False, account=f"user{i}@x.com")

        with pytest.raises(HTTPException) as exc:
            auth_module.check_login_rate_limit(ip, account="another@example.com")
        assert exc.value.status_code == 429
        assert "this network" in exc.value.detail

    def test_no_account_only_uses_ip_bucket(self, auth_module, fake_redis):
        """Endpoints without an account context (e.g. /api/access/request)
        skip the per-account bucket but still enforce the per-IP one."""
        from fastapi import HTTPException

        ip = "203.0.113.10"
        for _ in range(auth_module.LOGIN_IP_LIMIT):
            auth_module.record_login_attempt(ip, success=False, account=None)
        with pytest.raises(HTTPException):
            auth_module.check_login_rate_limit(ip, account=None)


class TestSuccessClears:
    """A successful login must clear both buckets so the next attempt is free."""

    def test_success_clears_both_buckets(self, auth_module, fake_redis):
        ip = "203.0.113.10"
        account = "admin"

        for _ in range(4):
            auth_module.record_login_attempt(ip, success=False, account=account)
        auth_module.record_login_attempt(ip, success=True, account=account)

        assert fake_redis.get(f"login_attempts_acct:{account}") is None
        assert fake_redis.get(f"login_attempts_ip:{ip}") is None
        assert auth_module.check_login_rate_limit(ip, account=account) is True

    def test_account_normalisation(self, auth_module, fake_redis):
        """Account keys are lowercased and stripped — `Admin ` and `admin`
        share the same bucket."""
        from fastapi import HTTPException

        ip = "203.0.113.10"
        for _ in range(auth_module.LOGIN_ACCOUNT_LIMIT):
            auth_module.record_login_attempt(ip, success=False, account="Admin ")
        with pytest.raises(HTTPException):
            auth_module.check_login_rate_limit(ip, account="admin")


class TestDegraded:
    """When Redis is unreachable the limiter must fail open (allow login)."""

    def test_redis_unavailable_fails_open(self, auth_module, monkeypatch):
        monkeypatch.setattr(auth_module, "_redis_client", None)
        monkeypatch.setattr(auth_module, "get_redis_client", lambda: None)
        assert auth_module.check_login_rate_limit("203.0.113.10", account="admin") is True
        # record_login_attempt is a no-op when Redis is down — must not raise.
        auth_module.record_login_attempt("203.0.113.10", success=False, account="admin")
