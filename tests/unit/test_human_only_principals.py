"""The two human-only principal rules and their shared primitives.

`dependencies.py` carries two allowlists over `User.mcp_scope`:

* PERSON — `is_person_principal` (ent#611): a JWT session or the person's own
  `user`-scoped key, and nothing that carries an agent, connector or delegate
  identity.
* INTERACTIVE — `is_interactive_principal` (#1854): a JWT session only.

This suite pins the three primitives built on them — `assert_person`,
`require_person` (its `Depends` form) and `require_interactive` — across every
principal kind, one per arm, with a stated principal (never a `MagicMock`,
whose auto-truthy attributes pass for the wrong reason). Both new primitives
also refuse a principal carrying `vouched_source_agent`: the event-loopback JWT
has `mcp_scope=None` and is otherwise contained only by its entry-point fence.

ent#611's `reject_non_person_principal` and `PERSON_REQUIRED_DETAIL` are pinned
byte-identical so this change cannot alter the ask-endings contract.

Related: docs/memory/requirements/auth.md §2.8, requirements/security.md §20.10.
"""

from __future__ import annotations

import inspect
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


def _principal(**kw):
    from models import User

    base = {"id": 7, "username": "op", "email": "op@example.com", "role": "admin"}
    base.update(kw)
    return User(**base)


# (id, principal kwargs, passes PERSON, passes INTERACTIVE)
KINDS = [
    ("jwt-session", {}, True, True),
    ("user-key", {"mcp_scope": "user"}, True, False),
    ("agent-key", {"mcp_scope": "agent", "agent_name": "agent-a"}, False, False),
    ("system-key", {"mcp_scope": "system"}, False, False),
    (
        "connector-key",
        {"mcp_scope": "connector", "connector_agent": "agent-a"},
        False,
        False,
    ),
    (
        "portal-delegate-key",
        {"mcp_scope": "portal_delegate", "portal_delegate": True},
        False,
        False,
    ),
    ("ops-key", {"mcp_scope": "ops"}, False, False),
    ("unknown-future-scope", {"mcp_scope": "a-scope-shipped-tomorrow"}, False, False),
    (
        "user-scope-carrying-an-agent",
        {"mcp_scope": "user", "agent_name": "agent-a"},
        False,
        False,
    ),
    ("loopback-jwt", {"vouched_source_agent": "agent-a"}, False, False),
]


def _kind_params(pick):
    return [
        pytest.param(kw, id=kid)
        for kid, kw, person, interactive in KINDS
        if pick(person, interactive)
    ]


class TestAssertPerson:
    @pytest.mark.parametrize("kw", _kind_params(lambda p, i: p))
    def test_a_person_passes(self, kw):
        from dependencies import assert_person, require_person

        user = _principal(**kw)
        assert_person(user)  # no raise
        assert require_person(user) is user

    @pytest.mark.parametrize("kw", _kind_params(lambda p, i: not p))
    def test_every_other_principal_is_refused_with_the_human_only_detail(self, kw):
        from fastapi import HTTPException
        from dependencies import HUMAN_ONLY_DETAIL, assert_person, require_person

        for gate in (assert_person, require_person):
            with pytest.raises(HTTPException) as ei:
                gate(_principal(**kw))
            assert ei.value.status_code == 403
            assert ei.value.detail == HUMAN_ONLY_DETAIL
            assert ei.value.detail["code"] == "person_required"

    def test_a_principal_without_a_scope_attribute_fails_closed(self):
        from fastapi import HTTPException
        from dependencies import assert_person

        with pytest.raises(HTTPException) as ei:
            assert_person(
                SimpleNamespace(id=1, username="x", email="x@example.com", role="admin")
            )
        assert ei.value.status_code == 403

    def test_the_detail_is_a_fresh_copy_each_time(self):
        """A caller mutating one refusal's detail must not change the next."""
        from fastapi import HTTPException
        from dependencies import HUMAN_ONLY_DETAIL, assert_person

        with pytest.raises(HTTPException) as ei:
            assert_person(_principal(mcp_scope="agent", agent_name="a"))
        ei.value.detail["code"] = "tampered"
        assert HUMAN_ONLY_DETAIL["code"] == "person_required"


class TestRequireInteractive:
    @pytest.mark.parametrize("kw", _kind_params(lambda p, i: i))
    def test_only_a_jwt_session_passes(self, kw):
        from dependencies import require_interactive

        user = _principal(**kw)
        assert require_interactive(user) is user

    @pytest.mark.parametrize("kw", _kind_params(lambda p, i: not i))
    def test_every_other_principal_is_refused(self, kw):
        from fastapi import HTTPException
        from dependencies import require_interactive

        with pytest.raises(HTTPException) as ei:
            require_interactive(_principal(**kw))
        assert ei.value.status_code == 403

    def test_a_key_refusal_keeps_the_existing_interactive_detail(self):
        """The key branch delegates to `reject_non_interactive_principal`, so its
        detail is unchanged for every existing caller."""
        from fastapi import HTTPException
        from dependencies import reject_non_interactive_principal, require_interactive

        user = _principal(mcp_scope="user")
        with pytest.raises(HTTPException) as a:
            require_interactive(user)
        with pytest.raises(HTTPException) as b:
            reject_non_interactive_principal(user)
        assert a.value.detail == b.value.detail

    def test_the_loopback_jwt_is_refused_with_the_human_only_detail(self):
        from fastapi import HTTPException
        from dependencies import HUMAN_ONLY_DETAIL, require_interactive

        with pytest.raises(HTTPException) as ei:
            require_interactive(_principal(vouched_source_agent="agent-a"))
        assert ei.value.detail == HUMAN_ONLY_DETAIL

    def test_a_principal_without_a_scope_attribute_fails_closed(self):
        from fastapi import HTTPException
        from dependencies import require_interactive

        with pytest.raises(HTTPException):
            require_interactive(
                SimpleNamespace(id=1, username="x", email="x@example.com", role="admin")
            )


class TestDependsForm:
    @pytest.mark.parametrize("name", ["require_person", "require_interactive"])
    def test_resolves_the_principal_through_get_current_user(self, name):
        import dependencies

        param = inspect.signature(getattr(dependencies, name)).parameters[
            "current_user"
        ]
        assert (
            getattr(param.default, "dependency", None) is dependencies.get_current_user
        )


class TestAskEndingsContractUnchanged:
    def test_person_required_detail_is_byte_identical(self):
        from dependencies import PERSON_REQUIRED_DETAIL

        assert PERSON_REQUIRED_DETAIL == {
            "code": "person_required",
            "message": (
                "Only a person can answer or cancel an ask; agent- and "
                "system-scoped keys cannot end one"
            ),
        }

    def test_the_ask_refusal_still_carries_its_own_detail(self):
        from fastapi import HTTPException
        from dependencies import PERSON_REQUIRED_DETAIL, reject_non_person_principal

        with pytest.raises(HTTPException) as ei:
            reject_non_person_principal(_principal(mcp_scope="system"))
        assert ei.value.detail == PERSON_REQUIRED_DETAIL

    def test_the_loopback_jwt_is_still_a_person_to_the_ask_predicate(self):
        """The loopback refusal lives only in the new primitives; the ent#611
        predicate is left exactly as it shipped."""
        from dependencies import is_person_principal

        assert is_person_principal(_principal(vouched_source_agent="agent-a")) is True
