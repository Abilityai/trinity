"""
422 bodies must not echo the value that failed validation (ent#109, /cso S1).

Pydantic v2 records the rejected value in ``errors()[i]["input"]`` and FastAPI's
default ``RequestValidationError`` handler passes ``exc.errors()`` through
verbatim. For a ``SecretStr`` field that means a failed validation returns the
SECRET in the response body — and, wherever a 422 is logged, in the platform log.

This is load-bearing rather than incidental. ent#109 added a charset guard to
``models._validate_pat_secret`` so a GitHub PAT carrying ``\\r``/``\\n`` is
rejected at the boundary, precisely because such a token makes h11 echo it in a
``LocalProtocolError``. Without the handler under test, that guard would
*relocate* the leak from a 500 into a 422 instead of closing it — the fix would
have been a lateral move.

The handler drops ``input`` for EVERY field rather than for names that look
sensitive: a name-matching allowlist is the "new producer missing from the
consumer's list" bug class, and the value carries no information the caller
lacks — they just sent it.

Modules: src/backend/error_handlers.py (validation_error_without_input)
         src/backend/main.py (registration)
         src/backend/models.py (_validate_pat_secret)
Issue:   abilityai/trinity-enterprise#109 (Epic ent#122)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_validation.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Pinned at IMPORT time, not resolved lazily in a fixture (the sys.modules
# leak that makes a sibling module's collection-time Mock the victim's problem).
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import models  # noqa: E402
from error_handlers import validation_error_without_input  # noqa: E402

SECRET = "ghp_THE_USERS_REAL_TOKEN"


@pytest.fixture
def client():
    """A minimal app carrying ONLY the handler under test.

    Deliberately not the real `main.app`: importing it drags in the lifespan,
    every router, and a live DB/Redis, none of which this behaviour depends on.
    The handler is registered the same way `main.py` registers it, so what is
    asserted is the handler, not a re-implementation of it.
    """
    from fastapi.exceptions import RequestValidationError

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_input)

    @app.post("/bind")
    def _bind(body: models.BindAgentRepoRequest):  # pragma: no cover - never reached
        return {"ok": True}

    @app.post("/fork")
    def _fork(body: models.ForkToOwnRequest):  # pragma: no cover - never reached
        return {"ok": True}

    return TestClient(app)


class TestSecretNeverEchoed:
    @pytest.mark.parametrize("route", ["/bind", "/fork"])
    def test_a_rejected_pat_is_not_in_the_422_body(self, client, route):
        """The regression this handler exists for. Both PAT-bearing models are
        covered — ent#93's create path has the same field and the same guard."""
        r = client.post(
            route,
            json={
                "destination_repo": "alice/brain",
                "github_pat": f"{SECRET}\r\nX-Injected: 1",
                "private": True,
            },
        )
        assert r.status_code == 422
        assert SECRET not in r.text

    def test_input_is_absent_from_every_error_entry(self, client):
        r = client.post(
            "/bind",
            json={
                "destination_repo": "not a repo",
                "github_pat": f"{SECRET} bad",
                "private": True,
            },
        )
        assert r.status_code == 422
        errors = r.json()["detail"]
        assert errors, "expected at least one validation error"
        assert all("input" not in e for e in errors)
        assert all("ctx" not in e for e in errors)
        assert SECRET not in r.text

    def test_a_missing_field_does_not_leak_a_sibling_secret(self, client):
        """A failure on ANOTHER field must not carry the PAT either — Pydantic
        reports per-field, but a future `model_validator` reporting at model
        level would put the whole body in `input`."""
        r = client.post("/bind", json={"github_pat": SECRET})
        assert r.status_code == 422
        assert SECRET not in r.text


class TestStillUsable:
    """Stripping `input` must not turn a 422 into an unactionable one — a client
    that cannot tell WHICH field failed and WHY would just retry blindly."""

    def test_loc_type_and_msg_survive(self, client):
        r = client.post(
            "/bind",
            json={
                "destination_repo": "not a repo",
                "github_pat": "ghp_ok",
                "private": True,
            },
        )
        assert r.status_code == 422
        err = r.json()["detail"][0]
        assert err["loc"] == ["body", "destination_repo"]
        assert err["type"]
        assert "owner/name" in err["msg"]

    def test_a_valid_body_is_unaffected(self, client):
        r = client.post(
            "/bind",
            json={
                "destination_repo": "alice/brain",
                "github_pat": "ghp_valid_token",
                "private": True,
            },
        )
        assert r.status_code == 200


class TestHandlerHasTeeth:
    def test_fastapis_default_would_have_leaked(self, client):
        """Proof the handler is doing the work, not the model.

        Without it, FastAPI's default returns `exc.errors()` verbatim — so this
        asserts the leak is real and that the fix is what closes it, rather than
        the test passing for an unrelated reason.
        """

        unguarded = FastAPI()

        @unguarded.post("/bind")
        def _bind(body: models.BindAgentRepoRequest):  # pragma: no cover
            return {"ok": True}

        leaky = TestClient(unguarded).post(
            "/bind",
            json={
                "destination_repo": "alice/brain",
                "github_pat": f"{SECRET} bad",
                "private": True,
            },
        )
        assert leaky.status_code == 422
        assert SECRET in leaky.text, (
            "FastAPI's default handler no longer echoes `input` — if this fails, "
            "the framework changed and the handler in main.py may be redundant. "
            "Re-verify before removing it."
        )
