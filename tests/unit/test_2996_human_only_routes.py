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
(7, runtime file) a stored `METHOD /path` that the app does not register fails.

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
        tables = [rc.AGENT_CALLABLE, rc.OWN_AUTH, rc.DELEGATED, rc.ADMIN_WIDENED]
        seen = set()
        for t in tables:
            assert not (seen & set(t)), seen & set(t)
            seen |= set(t)

    def test_rule6_no_duplicate_keys(self, classified):
        _, failures = classified
        assert not _of(failures, rc.DUPLICATE), "\n".join(_of(failures, rc.DUPLICATE))

    def test_every_widening_is_listed_and_every_delegated_test_exists(self, classified):
        _, failures = classified
        for tag in (rc.WIDENED_UNLISTED, rc.DELEGATED_TEST):
            assert not _of(failures, tag), "\n".join(_of(failures, tag))

    def test_websocket_routes_are_pinned(self, census):
        """Out of the HTTP census but not silent: a new websocket is a visible edit."""
        assert census.websockets == rc.WEBSOCKET_ROUTES

    def test_every_listed_entry_carries_a_method_path_and_a_reason(self):
        for table in (rc.AGENT_CALLABLE, rc.OWN_AUTH):
            for key, (mp, reason) in table.items():
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
        "routers/agent_config.py::set_agent_read_only_status",
        "routers/agent_config.py::set_agent_resources",
        "routers/agent_config.py::set_agent_capabilities",
        "routers/agent_config.py::set_agent_capacity",
        "routers/agent_config.py::set_agent_timeout",
        "routers/agent_config.py::set_public_channel_model",
        "routers/agent_config.py::set_agent_guardrails",
    ]
    INTERACTIVE = [
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

    @pytest.mark.parametrize("key", INTERACTIVE)
    def test_interactive(self, classified, key):
        assert classified[0][key] == "interactive"

    @pytest.mark.parametrize(
        "key",
        [
            "routers/agents.py::agent_heartbeat",
            "routers/agents.py::agent_execution_result",
            "routers/reports.py::create_report",
            "routers/notifications.py::create_notification",
            "routers/schedules.py::create_schedule",
            "routers/schedules.py::enable_schedule",
            "routers/schedules.py::disable_schedule",
        ],
    )
    def test_the_agent_runtime_surface_stays_agent_callable(self, classified, key):
        assert classified[0][key] == "agent_callable"


# ===========================================================================
# Self-tests: planted sources through the same walker
# ===========================================================================

_IMPORTS = """\
from fastapi import APIRouter, Depends
from dependencies import (
    get_current_user, require_person, require_interactive, require_admin,
    require_admin_allowing, assert_person, assert_admin, reject_non_interactive_principal,
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
        for k in ("admin_widened", "agent_callable", "own_auth", "delegated")
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
        assert msgs and "routers/a.py::h" in msgs[0] and "AGENT_CALLABLE" in msgs[0]

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
            tmp_path, agent_callable={"routers/a.py::h": ("GET /x", "a use")}
        )
        assert classes == {"routers/a.py::h": "agent_callable"} and not failures

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
            agent_callable={"routers/a.py::h": ("GET /x", "r")},
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
