"""#2996 — the route census: every OSS HTTP route chooses a side.

`get_current_user` resolves an agent- or system-scoped key to its owner carrying
the owner's role, and the owner tier cannot refuse agent keys wholesale (agents
legitimately drive owner-gated routes). So a human-only rule lives per route
(`require_person` / `require_interactive`), and THIS guard is what makes the next
route inherit it: every route, every method (GETs included), must resolve to
exactly one policy class, or the build fails.

The walker and the reviewed tables live in `tests/unit/_route_census.py`
(pure-stdlib AST, imports nothing from `src/`). The live route table is checked
by the sibling `test_2996_route_census_runtime.py`, in its own file so its
subprocess can never take these tests down with it.

Ratchet rules, one test each: (1) an unclassified route fails, naming the key
and the ways out; (2) a stale entry in any list fails; (3) a baseline entry that
is now gated fails — the baseline only shrinks; (4) the baseline is exactly
`FROZEN_BASELINE_COUNT`; (5) a key in two lists fails; (6) a duplicate key fails;
(7, runtime file) a stored `METHOD /path` that the app does not register fails;
(8) an `AGENT_CALLABLE_SELF` entry whose route carries no recognised self-gate
fails — and a self-gated route with no entry is unclassified (rule 1), so an
agent-key self-gate is never a way to skip the review (trinity-enterprise#840).

The self-tests plant sources in a temp tree and run them through the same walker
— each rule is shown to bite on a deliberately broken input.

Related: docs/memory/requirements/auth.md §2.8, architecture/security.md §6.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def census():
    return rc.walk()


@pytest.fixture(scope="module")
def baseline():
    return rc.load_baseline()


@pytest.fixture(scope="module")
def classified(census, baseline):
    return rc.classify(census, baseline)


def _of(failures, tag):
    return [msg for t, msg in failures if t == tag]


# ===========================================================================
# The live census
# ===========================================================================


class TestRouteCensus:
    def test_the_walker_sees_the_route_table(self, census):
        """Not vacuous: hundreds of routes, from every registration shape."""
        assert len(census.routes) > 600
        keys = set(census.routes)
        assert "main.py::health_check" in keys  # @app.<verb>
        assert (
            "routers/agent_config.py::set_agent_autonomy_status" in keys
        )  # @router.<verb>
        assert (
            "routers/settings/generic.py::get_all_settings" in keys
        )  # add_api_route, definition file
        assert any(k.startswith("shared_sessions/") for k in keys)  # outside routers/
        assert any(k.startswith("client_portal/") for k in keys)
        assert not any(k.startswith("enterprise/") for k in keys)

    def test_rule1_every_route_is_classified(self, classified):
        _, failures = classified
        assert not _of(failures, rc.UNCLASSIFIED), "\n".join(
            _of(failures, rc.UNCLASSIFIED)
        )

    def test_rule2_no_stale_entry(self, classified):
        _, failures = classified
        assert not _of(failures, rc.STALE), "\n".join(_of(failures, rc.STALE))

    def test_rule3_a_gated_route_is_not_in_the_baseline(self, classified):
        _, failures = classified
        assert not _of(failures, rc.GATED_IN_BASELINE), "\n".join(
            _of(failures, rc.GATED_IN_BASELINE)
        )

    def test_rule4_the_baseline_count_is_frozen(self, baseline):
        failures = rc.count_failures(baseline, rc.FROZEN_BASELINE_COUNT)
        assert not failures, failures[0][1]

    def test_rule5_no_key_is_in_two_lists(self, classified):
        _, failures = classified
        assert not _of(failures, rc.TWO_LISTS), "\n".join(_of(failures, rc.TWO_LISTS))
        tables = [
            rc.AGENT_CALLABLE_SELF,
            rc.AGENT_CALLABLE_ANY,
            rc.OWN_AUTH,
            rc.DELEGATED,
            rc.ADMIN_WIDENED,
        ]
        seen = set()
        for t in tables:
            assert not (seen & set(t)), seen & set(t)
            seen |= set(t)

    def test_rule6_no_duplicate_keys(self, classified):
        _, failures = classified
        assert not _of(failures, rc.DUPLICATE), "\n".join(_of(failures, rc.DUPLICATE))

    def test_rule8_every_self_entry_carries_its_gate(self, classified):
        _, failures = classified
        assert not _of(failures, rc.SELF_UNGATED), "\n".join(_of(failures, rc.SELF_UNGATED))

    def test_every_widening_is_listed_and_every_delegated_test_exists(self, classified):
        _, failures = classified
        for tag in (rc.WIDENED_UNLISTED, rc.DELEGATED_TEST):
            assert not _of(failures, tag), "\n".join(_of(failures, tag))

    def test_websocket_routes_are_pinned(self, census):
        """Out of the HTTP census but not silent: a new websocket is a visible edit."""
        assert census.websockets == rc.WEBSOCKET_ROUTES

    def test_every_listed_entry_carries_a_method_path_and_a_reason(self):
        rows = [(k, v[0], v[1]) for t in (rc.AGENT_CALLABLE_SELF, rc.OWN_AUTH) for k, v in t.items()]
        rows += [(k, mp, reason) for k, (mp, reason, _sibling) in rc.AGENT_CALLABLE_ANY.items()]
        for key, mp, reason in rows:
            assert mp.split(" ", 1)[0] in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
            }, key
            assert mp.split(" ", 1)[1].startswith("/"), key
            assert reason.strip(), key
        # trinity-enterprise#840: the sibling question is answered per route,
        # in its own field, not by omission.
        for key, (_mp, _reason, sibling) in rc.AGENT_CALLABLE_ANY.items():
            assert sibling.strip(), key
        for key, (mp, scopes, reason) in rc.ADMIN_WIDENED.items():
            assert (
                mp.startswith(("GET ", "POST ", "PUT ", "PATCH ", "DELETE "))
                and scopes
                and reason
            ), key

    def test_the_policy_classes_are_counted(self, classified):
        """The auto-classified classes are named and counted, not silent."""
        classes, _ = classified
        counts = {
            c: sum(1 for v in classes.values() if v == c)
            for c in rc.CODE_CLASSES + rc.LISTED_CLASSES
        }
        print(
            "\nroute census:",
            ", ".join(f"{c}={n}" for c, n in counts.items()),
            "(admin_tier: system admitted by #2323 design)",
        )
        assert counts["admin_tier"] > 0 and counts["portal"] > 0
        assert sum(counts.values()) == len(classes)


class TestTheRoutesThisChangeGates:
    PERSON = [
        "routers/agent_config.py::set_agent_autonomy_status",
        "routers/agent_config.py::update_agent_api_key_setting",
        "routers/agent_config.py::set_agent_capabilities",
        "routers/agent_config.py::set_agent_capacity",
    ]
    # trinity-enterprise#164: a person, or an agent holding `agents.manage`.
    PERSON_OR_GRANT = [
        "routers/agent_config.py::set_agent_read_only_status",
        "routers/agent_config.py::set_agent_resources",
        "routers/agent_config.py::set_agent_timeout",
        "routers/agent_config.py::set_public_channel_model",
        "routers/agent_config.py::set_agent_guardrails",
    ]
    INTERACTIVE = [
        "routers/agent_config.py::set_agent_capability_grant",
        "routers/users.py::update_my_email",
        "routers/users.py::set_my_github_pat",
        "routers/users.py::clear_my_github_pat",
        "routers/mcp_keys.py::create_mcp_api_key_endpoint",
        "routers/mcp_keys.py::ensure_default_mcp_api_key",
        "routers/mcp_keys.py::list_mcp_api_keys_endpoint",
    ]

    @pytest.mark.parametrize("key", PERSON)
    def test_person(self, classified, key):
        assert classified[0][key] == "person"

    @pytest.mark.parametrize("key", PERSON_OR_GRANT)
    def test_person_or_grant(self, classified, key):
        assert classified[0][key] == "person_or_grant"

    @pytest.mark.parametrize("key", INTERACTIVE)
    def test_interactive(self, classified, key):
        assert classified[0][key] == "interactive"

    @pytest.mark.parametrize(
        "key,cls",
        [
            ("routers/agents.py::agent_heartbeat", "agent_callable_any"),
            ("routers/agents.py::agent_execution_result", "agent_callable_any"),
            ("routers/reports.py::create_report", "agent_callable_self"),
            ("routers/notifications.py::create_notification", "agent_callable_any"),
            ("routers/schedules.py::create_schedule", "agent_callable_any"),
            ("routers/schedules.py::enable_schedule", "agent_callable_any"),
            ("routers/schedules.py::disable_schedule", "agent_callable_any"),
        ],
    )
    def test_the_agent_runtime_surface_stays_agent_callable(self, classified, key, cls):
        assert classified[0][key] == cls


# ===========================================================================
# Self-tests: planted sources through the same walker
# ===========================================================================

_IMPORTS = """\
from fastapi import APIRouter, Depends
from dependencies import (
    get_current_user, require_person, require_interactive, require_admin,
    require_admin_allowing, assert_person, assert_admin, reject_non_interactive_principal,
    assert_own_agent, get_self_acting_agent, get_owned_agent, CurrentUser,
)
router = APIRouter()
"""


def _plant(root: Path, rel: str, body: str, *, imports: bool = True) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((_IMPORTS if imports else "") + textwrap.dedent(body))


def _run(root: Path, baseline=None, **tables):
    census = rc.walk(root=root)
    tables = {
        k: tables.get(k, {})
        for k in ("admin_widened", "agent_callable_self", "agent_callable_any", "own_auth", "delegated")
    }
    classes, failures = rc.classify(census, baseline or {}, **tables)
    return census, classes, failures


def _one(root, rel, body, **kw):
    _plant(root, rel, body, **kw)
    return _run(root)


class TestSelfTests:
    def test_a_depends_gate_classifies(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(current_user=Depends(require_person)):
                return 1
        """,
        )
        assert classes == {"routers/a.py::h": "person"} and not failures

    def test_a_decorator_dependencies_gate_classifies(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/x", dependencies=[Depends(require_interactive)])
            async def h():
                return 1
        """,
        )
        assert classes == {"routers/a.py::h": "interactive"} and not failures

    def test_a_first_statement_gate_classifies(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            '''
            @router.put("/x")
            async def h(current_user=Depends(get_current_user)):
                """Docstring first is fine."""
                assert_person(current_user)
                return 1
        ''',
        )
        assert classes == {"routers/a.py::h": "person"} and not failures

    def test_a_conditional_gate_does_not_count(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(flag: bool, current_user=Depends(get_current_user)):
                if flag:
                    assert_person(current_user)
                return 1
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_a_gate_after_a_write_does_not_count(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(current_user=Depends(get_current_user)):
                db.write(1)
                reject_non_interactive_principal(current_user)
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_a_gate_on_the_wrong_name_does_not_count(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(someone, current_user=Depends(get_current_user)):
                assert_person(someone)
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_a_locally_defined_gate_does_not_count(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            def require_person():
                return None

            @router.put("/x")
            async def h(current_user=Depends(require_person)):
                return 1
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_an_aliased_gate_does_not_count(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            from fastapi import APIRouter, Depends
            from dependencies import get_current_user as require_person
            router = APIRouter()

            @router.put("/x")
            async def h(current_user=Depends(require_person)):
                return 1
        """,
            imports=False,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_an_annotated_alias_is_not_recognised(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            from typing import Annotated
            Person = Annotated[object, Depends(require_person)]

            @router.put("/x")
            async def h(current_user: Person):
                return 1
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_a_router_level_dependency_is_not_recognised(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            gated = APIRouter(dependencies=[Depends(require_person)])

            @gated.put("/x")
            async def h():
                return 1
        """,
        )
        assert _of(failures, rc.UNCLASSIFIED) and not classes

    def test_an_admin_depends_is_admin_tier(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.get("/x")
            async def h(current_user=Depends(require_admin)):
                return 1
        """,
        )
        assert classes == {"routers/a.py::h": "admin_tier"} and not failures

    def test_a_widened_admin_gate_needs_an_entry(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.get("/x")
            async def h(current_user=Depends(require_admin_allowing("ops"))):
                return 1

            @router.get("/y")
            async def i(current_user=Depends(get_current_user)):
                assert_admin(current_user, allow_scopes={"ops"})
        """,
        )
        _, classes, failures = _run(tmp_path)
        assert len(_of(failures, rc.WIDENED_UNLISTED)) == 2
        entries = {
            "routers/a.py::h": ("GET /x", ("ops",), "r"),
            "routers/a.py::i": ("GET /y", ("ops",), "r"),
        }
        _, classes, failures = _run(tmp_path, admin_widened=entries)
        assert set(classes.values()) == {"admin_widened"} and not failures

    def test_get_portal_principal_is_portal(self, tmp_path):
        _plant(
            tmp_path,
            "client_portal/portal_auth.py",
            "def get_portal_principal():\n    pass\n",
            imports=False,
        )
        _, classes, failures = _one(
            tmp_path,
            "client_portal/r.py",
            """
            from fastapi import APIRouter, Depends
            from .portal_auth import get_portal_principal
            router = APIRouter()

            @router.get("/x")
            async def h(p=Depends(get_portal_principal)):
                return 1
        """,
            imports=False,
        )
        assert classes == {"client_portal/r.py::h": "portal"} and not failures

    @pytest.mark.parametrize("verb", ["get", "post", "delete", "patch"])
    def test_an_unlisted_route_of_any_method_fails(self, tmp_path, verb):
        _, _, failures = _one(
            tmp_path,
            "routers/a.py",
            f"""
            @router.{verb}("/x")
            async def h(current_user=Depends(get_current_user)):
                return 1
        """,
        )
        msgs = _of(failures, rc.UNCLASSIFIED)
        assert msgs and "routers/a.py::h" in msgs[0]
        assert "AGENT_CALLABLE_ANY" in msgs[0] and "assert_own_agent" in msgs[0]

    def test_a_listed_route_passes(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.get("/x")
            async def h(current_user=Depends(get_current_user)):
                return 1
        """,
        )
        _, classes, failures = _run(
            tmp_path, agent_callable_any={"routers/a.py::h": ("GET /x", "a use", "a sibling")}
        )
        assert classes == {"routers/a.py::h": "agent_callable_any"} and not failures

    def test_add_api_route_resolves_to_the_definition_file(self, tmp_path):
        _plant(
            tmp_path,
            "routers/pkg/impl.py",
            """
            async def handler(current_user=Depends(require_person)):
                return 1
        """,
        )
        _plant(
            tmp_path,
            "routers/pkg/__init__.py",
            """
            from fastapi import APIRouter
            from . import impl
            router = APIRouter()
            router.add_api_route("", impl.handler, methods=["POST"])
        """,
            imports=False,
        )
        census, classes, failures = _run(tmp_path)
        assert classes == {"routers/pkg/impl.py::handler": "person"} and not failures
        assert census.routes["routers/pkg/impl.py::handler"].methods == ("POST",)

    def test_an_unresolvable_add_api_route_target_fails(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            router.add_api_route("/x", make_handler(), methods=["GET"])
        """,
        )
        _, _, failures = _run(tmp_path)
        assert any("cannot be resolved" in m for m in _of(failures, rc.UNCLASSIFIED))

    def test_a_route_in_a_new_nested_package_is_found(self, tmp_path):
        _, _, failures = _one(
            tmp_path,
            "brand_new/deep/pkg/router.py",
            """
            @router.post("/x")
            async def h(current_user=Depends(get_current_user)):
                return 1
        """,
        )
        assert any(
            "brand_new/deep/pkg/router.py::h" in m
            for m in _of(failures, rc.UNCLASSIFIED)
        )

    def test_the_enterprise_tree_is_excluded(self, tmp_path):
        _, classes, failures = _one(
            tmp_path,
            "enterprise/backend/r.py",
            """
            @router.post("/x")
            async def h():
                return 1
        """,
        )
        assert not classes and not failures

    def test_a_seventh_websocket_is_visible(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.websocket("/ws/new")
            async def sock(ws):
                return 1
        """,
        )
        census = rc.walk(root=tmp_path)
        assert census.websockets == {"routers/a.py::sock"}
        assert census.websockets != rc.WEBSOCKET_ROUTES

    def test_rule2_a_stale_baseline_entry_fails(self, tmp_path):
        _plant(tmp_path, "routers/a.py", "")
        _, _, failures = _run(tmp_path, baseline={"routers/a.py::gone": "GET /gone"})
        assert _of(failures, rc.STALE)

    def test_rule2_a_stale_exemption_fails(self, tmp_path):
        _plant(tmp_path, "routers/a.py", "")
        _, _, failures = _run(
            tmp_path, own_auth={"routers/a.py::gone": ("GET /gone", "r")}
        )
        assert _of(failures, rc.STALE)

    def test_rule3_a_gated_baseline_entry_fails(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(current_user=Depends(require_person)):
                return 1
        """,
        )
        _, _, failures = _run(tmp_path, baseline={"routers/a.py::h": "PUT /x"})
        assert _of(failures, rc.GATED_IN_BASELINE)

    def test_rule4_a_count_that_does_not_match_fails(self):
        assert rc.count_failures({"a": "GET /a"}, 1) == []
        assert rc.count_failures({"a": "GET /a"}, 2)[0][0] == rc.COUNT  # headroom
        assert (
            rc.count_failures({"a": "GET /a", "b": "GET /b"}, 1)[0][0] == rc.COUNT
        )  # growth

    def test_rule5_a_key_in_two_lists_fails(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.get("/x")
            async def h(current_user=Depends(get_current_user)):
                return 1
        """,
        )
        _, _, failures = _run(
            tmp_path,
            baseline={"routers/a.py::h": "GET /x"},
            agent_callable_any={"routers/a.py::h": ("GET /x", "r", "s")},
        )
        assert _of(failures, rc.TWO_LISTS)

    def test_rule6_a_duplicate_key_fails(self, tmp_path):
        _, _, failures = _one(
            tmp_path,
            "routers/a.py",
            """
            @router.get("/x")
            async def h(current_user=Depends(require_person)):
                return 1

            @router.get("/y")
            async def h(current_user=Depends(require_person)):
                return 2
        """,
        )
        assert _of(failures, rc.DUPLICATE)

    def test_a_delegated_entry_needs_its_behavioural_test(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/x")
            async def h(current_user=Depends(get_current_user)):
                return service(current_user)
        """,
        )
        real = "tests/unit/test_2996_human_only_routes.py::TestSelfTests::test_a_delegated_entry_needs_its_behavioural_test"
        _, classes, failures = _run(
            tmp_path, delegated={"routers/a.py::h": ("PUT /x", "svc.fn", real)}
        )
        assert classes == {"routers/a.py::h": "delegated"} and not failures
        missing = "tests/unit/test_2996_human_only_routes.py::test_no_such_test"
        _, _, failures = _run(
            tmp_path, delegated={"routers/a.py::h": ("PUT /x", "svc.fn", missing)}
        )
        assert _of(failures, rc.DELEGATED_TEST)

    def test_rule7_a_moved_path_or_method_fails(self):
        live = {"routers/a.py::h": ["GET /api/x"]}
        assert rc.path_failures(live, live, {"routers/a.py::h": "GET /api/x"}) == []
        moved = rc.path_failures(live, live, {"routers/a.py::h": "GET /api/old"})
        assert moved and moved[0][0] == rc.PATH_MISMATCH
        method = rc.path_failures(live, live, {"routers/a.py::h": "POST /api/x"})
        assert method and method[0][0] == rc.PATH_MISMATCH
        unregistered = rc.path_failures(live, {}, {"routers/a.py::h": "GET /api/x"})
        assert unregistered[0][1].endswith(repr(rc.NOT_REGISTERED))

    def test_a_live_route_the_walker_missed_fails(self):
        failures = rc.path_failures(set(), {"routers/a.py::h": ["GET /x"]}, {})
        assert failures and failures[0][0] == rc.UNCLASSIFIED


# ===========================================================================
# trinity-enterprise#840: the agent-key self-gate, recognised AND listed
# ===========================================================================

_SELF = {"routers/a.py::h": ("POST /api/agents/{name}/x", "a use the agent makes on itself")}


def _self_route(body: str, *, decorator='@router.post("/agents/{name}/x")', **kw):
    """One handler under `decorator`, with `body` as its body."""
    return f"""
            {decorator}
            async def h(name: str, current_user=Depends(get_current_user)):
{textwrap.indent(textwrap.dedent(body), " " * 16)}
        """


class TestSelfGateRecognition:
    def _classes(self, tmp_path, src, *, imports=True, **tables):
        _plant(tmp_path, "routers/a.py", src, imports=imports)
        return _run(tmp_path, **tables)

    def test_a_listed_first_statement_gate_is_agent_callable_self(self, tmp_path):
        census, classes, failures = self._classes(
            tmp_path,
            _self_route('''"""Docstring first is fine."""
assert_own_agent(current_user, name, detail="mine only")
return 1'''),
            agent_callable_self=_SELF,
        )
        assert classes == {"routers/a.py::h": "agent_callable_self"} and not failures
        gate = census.routes["routers/a.py::h"].self_gate
        assert (gate.kind, gate.principal, gate.agent, gate.refusal) == (
            "assert_own_agent", "current_user", "name", False)

    def test_a_self_gated_route_with_no_entry_is_unclassified(self, tmp_path):
        """The review the census exists to force: the self-gate alone never
        classifies a route, so a self-scoped GRANT written with it fails until
        someone lists it with a reason (and the reviewer reading that reason is
        what keeps it out)."""
        _, classes, failures = self._classes(
            tmp_path, _self_route("assert_own_agent(current_user, name)\nreturn 1"))
        msgs = _of(failures, rc.UNCLASSIFIED)
        assert not classes and msgs and "AGENT_CALLABLE_SELF" in msgs[0] and "GRANT" in msgs[0]

    def test_an_entry_without_the_gate_fails(self, tmp_path):
        _, classes, failures = self._classes(
            tmp_path, _self_route("return 1"), agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_the_hand_copied_inline_gate_is_not_recognised(self, tmp_path):
        """The shape this issue retires: correct, and invisible to the census."""
        _, _, failures = self._classes(
            tmp_path,
            _self_route('''if current_user.agent_name and current_user.agent_name != name:
    raise HTTPException(status_code=403, detail="no")
return 1'''),
            agent_callable_self=_SELF,
        )
        assert _of(failures, rc.SELF_UNGATED)

    @pytest.mark.parametrize(
        "body",
        [
            # after another statement
            "db.read(name)\nassert_own_agent(current_user, name)",
            # nested in an if
            "if name:\n    assert_own_agent(current_user, name)",
            # on a name that is not the principal
            "assert_own_agent(name, name)",
            # against a parameter that is not the path agent
            "assert_own_agent(current_user, other)",
            # keyword arguments: the census reads the two positional ones
            "assert_own_agent(current_user=current_user, agent_name=name)",
        ],
        ids=["not-first", "nested", "wrong-principal", "wrong-agent", "keywords"],
    )
    def test_a_misplaced_or_misaimed_gate_is_not_recognised(self, tmp_path, body):
        src = _self_route(body).replace("async def h(name: str,", "async def h(name: str, other: str,")
        _, classes, failures = self._classes(tmp_path, src, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_a_dependency_that_is_not_the_principal_is_not_recognised(self, tmp_path):
        """Only a `get_current_user` principal reads an agent name: a gate
        called on another dependency's value (an agent NAME, here) would read
        none and pass every caller."""
        src = """
            @router.post("/agents/{name}/x")
            async def h(name: str, owned=Depends(get_owned_agent), current_user=Depends(get_current_user)):
                assert_own_agent(owned, name)
        """
        _, classes, failures = self._classes(tmp_path, src, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_the_agent_must_be_the_one_every_path_names(self, tmp_path):
        """`name` compared, but the path names the agent `{agent_name}`."""
        _, classes, failures = self._classes(
            tmp_path,
            _self_route("assert_own_agent(current_user, name)",
                        decorator='@router.post("/agents/{agent_name}/x")'),
            agent_callable_self=_SELF,
        )
        assert not classes and _of(failures, rc.SELF_UNGATED)

    @pytest.mark.parametrize("gate", ["assert_own_agent(current_user, name)",
                                      "assert_own_agent(current_user, agent_name)"])
    def test_a_path_naming_both_agent_parameters_is_never_recognised(self, tmp_path, gate):
        """Ambiguous: the gate could fence one agent while the handler acts on
        the other, and the capability fences read `agent_name` first."""
        src = _self_route(gate, decorator='@router.post("/agents/{agent_name}/peers/{name}")')
        src = src.replace("async def h(name: str,", "async def h(name: str, agent_name: str,")
        _, classes, failures = self._classes(tmp_path, src, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)
        both = """
            @router.post("/agents/{agent_name}/peers/{name}")
            async def h(agent_name: str, name: str = Depends(get_self_acting_agent)):
                return 1
        """
        _, classes, failures = self._classes(tmp_path, both, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_a_second_registration_clears_the_self_gate(self, tmp_path):
        """A decorated, self-gated handler ALSO registered with `add_api_route`
        serves a path the gate was never checked against: fail closed."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/agents/{name}/x")
            async def h(name: str, current_user=Depends(get_current_user)):
                assert_own_agent(current_user, name)

            router.add_api_route("/elsewhere", h, methods=["PUT"])
        """,
        )
        census, classes, failures = _run(tmp_path, agent_callable_self=_SELF)
        assert census.routes["routers/a.py::h"].self_gate is None
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_an_aliased_or_locally_defined_gate_is_not_recognised(self, tmp_path):
        aliased = """
            from fastapi import APIRouter, Depends
            from dependencies import get_current_user, assert_own_agent as gate
            router = APIRouter()

            @router.post("/agents/{name}/x")
            async def h(name: str, current_user=Depends(get_current_user)):
                gate(current_user, name)
        """
        _, classes, failures = self._classes(
            tmp_path, aliased, imports=False, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)
        local = """
            def assert_own_agent(user, agent):
                return None

            @router.post("/agents/{name}/x")
            async def h(name: str, current_user=Depends(get_current_user)):
                assert_own_agent(current_user, name)
        """
        _, classes, failures = self._classes(tmp_path, local, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_the_module_attribute_spelling_and_the_currentuser_alias_count(self, tmp_path):
        src = """
            from fastapi import APIRouter
            import dependencies
            from dependencies import CurrentUser
            router = APIRouter()

            @router.post("/agents/{name}/x")
            async def h(name: str, current_user: CurrentUser):
                dependencies.assert_own_agent(current_user, name, refusal=make_404)
        """
        census, classes, failures = self._classes(
            tmp_path, src, imports=False, agent_callable_self=_SELF)
        assert classes == {"routers/a.py::h": "agent_callable_self"} and not failures
        assert census.routes["routers/a.py::h"].self_gate.refusal is True

    def test_get_self_acting_agent_is_a_self_gate(self, tmp_path):
        src = """
            @router.get("/agents/{name}/asks/{request_id}")
            async def h(request_id: str, name: str = Depends(get_self_acting_agent)):
                return 1
        """
        census, classes, failures = self._classes(
            tmp_path, src, agent_callable_self={
                "routers/a.py::h": ("GET /api/agents/{name}/asks/{request_id}", "its own ask")})
        assert classes == {"routers/a.py::h": "agent_callable_self"} and not failures
        gate = census.routes["routers/a.py::h"].self_gate
        assert (gate.kind, gate.principal, gate.agent) == ("get_self_acting_agent", None, "name")

    def test_an_add_api_route_target_is_never_self_recognised(self, tmp_path):
        """No literal decorator path to check the agent against: fail closed."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            async def h(name: str, current_user=Depends(get_current_user)):
                assert_own_agent(current_user, name)

            router.add_api_route("/agents/{name}/x", h, methods=["POST"])
        """,
        )
        _, classes, failures = _run(tmp_path, agent_callable_self=_SELF)
        assert not classes and _of(failures, rc.SELF_UNGATED)

    def test_a_self_gated_baseline_entry_fails(self, tmp_path):
        _, _, failures = self._classes(
            tmp_path,
            _self_route("assert_own_agent(current_user, name)"),
            baseline={"routers/a.py::h": "POST /api/agents/{name}/x"},
            agent_callable_self=_SELF,
        )
        assert _of(failures, rc.GATED_IN_BASELINE)

    def test_a_self_gated_route_listed_as_any_is_stale(self, tmp_path):
        _, _, failures = self._classes(
            tmp_path,
            _self_route("assert_own_agent(current_user, name)"),
            agent_callable_any={"routers/a.py::h": ("POST /api/agents/{name}/x", "r", "s")},
        )
        assert any("AGENT_CALLABLE_SELF" in m for m in _of(failures, rc.STALE))
        assert _of(failures, rc.UNCLASSIFIED)  # and it still has no SELF entry

    def test_a_person_gate_beside_the_self_gate_wins(self, tmp_path):
        """The strictest class: a person-only route makes the self-gate moot."""
        src = """
            @router.post("/agents/{name}/x")
            async def h(name: str, current_user=Depends(require_person)):
                assert_own_agent(current_user, name)
        """
        _, classes, failures = self._classes(tmp_path, src)
        assert classes == {"routers/a.py::h": "person"} and not failures
