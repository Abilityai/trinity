"""trinity-enterprise#719 — no grant route is guarded by an agent-only check alone.

`reject_agent_principal`, `if current_user.agent_name: raise` and an early
`return` for every non-agent refuse a principal because it carries an agent
name. The `system` key, and any scope that is neither an agent key nor a
person's, carries none, so such a check admits it. A route that grants
something therefore needs an allowlist (a person or interactive rule) or the
admin gate beside it. The scan lives in `tests/unit/_grant_guard_scan.py`: it
takes the route set from the route census and follows each route into the code
it runs, the shared doors included (the Workspace principal, the room
moderator check).

`test_no_grant_route_is_guarded_by_an_agent_only_check_alone` is the issue's
acceptance check: it fails on any tree where a route still rests on an
agent-only check alone, and the failure lists each such route with the check
that guards it. What it does not see is listed in the scan's "Stated limits":
among them a route with no machine refusal at all (the census baseline holds
those), a check behind a call the scan cannot follow when another route reaches
it a way it can, and the private tree (the census excludes `enterprise/`).

One grant passes without a person rule on purpose: the entry in `DEFERRED`
(`_grant_guard_scan.py`), naming the follow-up that rules it. It is printed in
the report, and it fails as stale once its route is closed.

The self-tests plant sources in a temp tree and run them through the same scan.
Each asserts the scan's verdict on the shape its rule is about; most plant the
passing shape beside the failing one in the same tree.

Related: docs/memory/requirements/auth.md §2.8, architecture/security.md §6.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grant_guard_scan as g  # noqa: E402
import _route_census as rc  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def report():
    return g.scan()


# ===========================================================================
# The live tree
# ===========================================================================


def test_no_grant_route_is_guarded_by_an_agent_only_check_alone(report):
    assert not report.findings, (
        "grant routes still guarded by an agent-only check alone — gate each with "
        "Depends(require_person|require_interactive) (or a person check inside its "
        "shared door), or list a reviewed non-grant in _grant_guard_scan.py:\n"
        + g.format_report(report)
    )


def test_the_scan_tables_are_current(report):
    assert not report.problems, "\n".join(report.problems)


def test_the_scan_is_not_vacuous(report):
    """It walks the census route table and reaches every shape it checks."""
    assert report.routes > 600
    assert (
        report.sites > 0
    ), "no agent-only check found anywhere: the patterns stopped matching"
    assert (
        report.workspace_writes >= 20
    ), "the Workspace write list came back near-empty"
    assert {
        "shared_sessions/router.py::close_room",
        "shared_sessions/router.py::add_participant",
        "shared_sessions/router.py::remove_participant",
    } <= set(
        report.door_routes
    ), "the room moderator acts no longer reach the moderator door"


def test_only_the_reviewed_grants_are_deferred(report):
    """The grants that pass without a person rule are exactly `DEFERRED`."""
    assert {key for _route, key in report.deferred} == set(g.DEFERRED)


def test_the_census_literal_is_the_baseline_length(report):
    """The re-count rule: whatever merged, the literal equals the baseline.

    The same fact as census rule 4 (`test_2996_human_only_routes.py`), restated
    here so this file reports both closure facts together."""
    assert report.literal == report.baseline


# ===========================================================================
# Self-tests on planted trees
# ===========================================================================

_DEPENDENCIES = """
def get_current_user(): ...
def reject_agent_principal(current_user): ...
def human_only(current_user=None): ...
def require_person(): ...
def require_interactive(): ...
def assert_person(current_user): ...
def assert_admin(current_user): ...
def is_person_principal(current_user): ...
def require_person_or_capability(capability): ...
def require_admin_allowing(*scopes): ...
"""

_IMPORTS = """\
from fastapi import APIRouter, Depends, HTTPException
from dependencies import (
    assert_admin, assert_person, get_current_user, human_only, is_person_principal,
    reject_agent_principal, require_admin_allowing, require_person,
    require_person_or_capability,
)
router = APIRouter()
"""


def _plant(root: Path, rel: str, body: str, *, imports: bool = True) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((_IMPORTS if imports else "") + textwrap.dedent(body))


def _scan(root: Path, **kw) -> g.Report:
    _plant(
        root,
        "dependencies.py",
        _DEPENDENCIES + kw.get("dependencies_extra", ""),
        imports=False,
    )
    census = rc.walk(root=root)
    baseline = {
        k: "planted"
        for k, r in census.routes.items()
        if r.code_class is None and r.self_gate is None
    }
    tables = {
        t: {}
        for t in (
            "admin_widened",
            "agent_callable_self",
            "agent_callable_any",
            "own_auth",
            "delegated",
        )
    }
    return g.scan(
        root,
        baseline,
        tables=tables,
        not_grant_guards=kw.get("not_grant_guards", {}),
        reviewed_credits=kw.get("reviewed_credits", {}),
        workspace_uses=kw.get("workspace_uses", {}),
        deferred=kw.get("deferred", {}),
        person_branches=kw.get("person_branches", {}),
        door_fallbacks=kw.get("door_fallbacks", {}),
        moderator_door=kw.get("moderator_door"),
    )


def _entry(reason, *routes):
    """A pinned table entry: (reason, the routes it covers)."""
    return (reason, frozenset(routes))


def _routes(rep: g.Report, check: str = g.AGENT_ONLY_SITE):
    return sorted({f.route for f in rep.findings if f.check == check})


class TestAgentOnlySites:
    def test_a_denylist_alone_is_a_finding_and_an_allowlist_beside_it_is_not(
        self, tmp_path
    ):
        """The mutation pair: the same route, with and without the person rule."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/grant")
            async def grant(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1

            @router.post("/gated", dependencies=[Depends(require_person)])
            async def gated(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == ["routers/a.py::grant"]

    def test_a_denylist_beside_the_admin_gate_is_not_alone(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/setting")
            async def setting(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                assert_admin(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert not rep.findings
        assert [(r, why) for r, _s, why in rep.not_alone] == [
            ("routers/a.py::setting", "admin-gated")
        ]

    def test_an_if_on_the_agent_name_in_a_helper_in_another_module_is_followed(
        self, tmp_path
    ):
        _plant(
            tmp_path,
            "services/gate.py",
            """
            def gate_human(current_user):
                if current_user.agent_name:
                    raise HTTPException(status_code=403)
        """,
        )
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from services import gate

            @router.post("/share")
            async def share(current_user=Depends(get_current_user)):
                gate.gate_human(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == ["routers/a.py::share"]
        assert {f.site for f in rep.findings} == {"services/gate.py::gate_human"}

    def test_an_early_return_for_non_agents_then_a_refusal_is_a_finding(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def _guard(current_user, values):
                if not current_user.agent_name:
                    return
                if any(v.startswith("dept-") for v in values):
                    raise HTTPException(status_code=403)

            @router.put("/tags")
            async def tags(current_user=Depends(get_current_user)):
                _guard(current_user, [])
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::tags"]

    def test_a_denylist_dependency_alone_is_a_finding(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/export", dependencies=[Depends(human_only)])
            async def export(current_user=Depends(get_current_user)):
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == ["routers/a.py::export"]
        assert {f.site for f in rep.findings} == {"dependencies.py::human_only"}

    def test_a_dependency_reached_through_an_annotated_alias_is_followed(
        self, tmp_path
    ):
        _plant(
            tmp_path,
            "routers/deps.py",
            """
            from typing import Annotated

            def no_agents(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return current_user

            HumanUser = Annotated[object, Depends(no_agents)]
        """,
        )
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from routers.deps import HumanUser

            @router.post("/x")
            async def x(user: HumanUser):
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::x"]

    def test_a_nested_import_cannot_hide_an_annotated_dependency(self, tmp_path):
        _plant(tmp_path, "helpers.py", "def noop():\n    pass\n", imports=False)
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from typing import Annotated

            def guard(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return current_user

            HumanUser = Annotated[object, Depends(guard)]

            @router.post("/x")
            def x(current_user: HumanUser):
                def unused():
                    from helpers import noop as HumanUser
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::x"]

    def test_a_conjunction_with_the_agent_name_is_a_site(self, tmp_path):
        """`agent_name and <condition>` refuses some agents and admits every
        principal that carries no agent name: still a denylist."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.put("/tags")
            async def tags(values: list, current_user=Depends(get_current_user)):
                reserved = [v for v in values if v.startswith("dept-")]
                if current_user.agent_name and reserved:
                    raise HTTPException(status_code=403)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::tags"]

    def test_a_check_on_which_agent_is_not_a_site(self, tmp_path):
        """A branch that returns for a matching agent checks WHICH agent, not
        whether the caller is one."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/attribution")
            async def attribution(header: str, current_user=Depends(get_current_user)):
                identity = current_user.agent_name
                if identity:
                    if header == identity:
                        return header
                    raise HTTPException(status_code=403)
                return None
        """,
        )
        rep = _scan(tmp_path)
        assert not rep.findings and rep.sites == 0

    def test_building_a_gate_factory_runs_no_check(self, tmp_path):
        """A factory called in a body, or declared as the default of a helper
        that is called directly, builds a dependency nobody resolves. Only a
        route declaration counts (through the census class)."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def gate(current_user, dep=Depends(require_person_or_capability("x.manage"))):
                reject_agent_principal(current_user)
                return current_user

            def built(current_user):
                reject_agent_principal(current_user)
                require_person_or_capability("x.manage")
                return current_user

            @router.post("/default")
            async def default(current_user=Depends(get_current_user)):
                gate(current_user)
                return 1

            @router.post("/built")
            async def built_route(current_user=Depends(get_current_user)):
                built(current_user)
                return 1

            @router.post("/admin-built")
            async def admin_built(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                require_admin_allowing("ops")
                return 1

            @router.post(
                "/declared",
                dependencies=[Depends(require_person_or_capability("x.manage"))],
            )
            async def declared(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == [
            "routers/a.py::admin_built",
            "routers/a.py::built_route",
            "routers/a.py::default",
        ]

    def test_a_site_whose_own_function_refuses_a_non_person_is_not_alone(
        self, tmp_path
    ):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def tagger(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not is_person_principal(principal):
                    raise HTTPException(status_code=403)
                return principal

            @router.post("/tag")
            async def tag(current_user=Depends(get_current_user)):
                tagger(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert not rep.findings
        assert [why for _r, _s, why in rep.not_alone] == [
            "the same function refuses a non-person"
        ]

    def test_a_person_helper_counts_only_when_it_returns_the_predicate(
        self, tmp_path
    ):
        """`return x or is_person_principal(principal)` and `return not ...` are not
        person predicates; `return x and is_person_principal(principal)` is."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def _and(principal):
                return principal.kind == "user" and is_person_principal(principal)

            def _or(principal):
                return principal.kind == "user" or is_person_principal(principal)

            def _not(principal):
                return not is_person_principal(principal)

            def _mixed(principal):
                if principal.kind == "user":
                    return is_person_principal(principal)
                return principal.enabled

            def _nested(principal):
                person = principal.enabled
                def unused():
                    person = is_person_principal(principal)
                return person

            def _nested_only(principal):
                def unused():
                    person = is_person_principal(principal)
                return person

            def g_and(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _and(principal):
                    raise HTTPException(status_code=403)

            def g_or(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _or(principal):
                    raise HTTPException(status_code=403)

            def g_not(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _not(principal):
                    raise HTTPException(status_code=403)

            @router.post("/and")
            async def r_and(current_user=Depends(get_current_user)):
                g_and(current_user)

            @router.post("/or")
            async def r_or(current_user=Depends(get_current_user)):
                g_or(current_user)

            @router.post("/not")
            async def r_not(current_user=Depends(get_current_user)):
                g_not(current_user)

            def g_mixed(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _mixed(principal):
                    raise HTTPException(status_code=403)

            def g_nested(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _nested(principal):
                    raise HTTPException(status_code=403)

            @router.post("/mixed")
            async def r_mixed(current_user=Depends(get_current_user)):
                g_mixed(current_user)

            @router.post("/nested")
            async def r_nested(current_user=Depends(get_current_user)):
                g_nested(current_user)

            def g_nested_only(principal):
                if principal.agent_name:
                    raise HTTPException(status_code=403)
                if not _nested_only(principal):
                    raise HTTPException(status_code=403)

            @router.post("/nested-only")
            async def r_nested_only(current_user=Depends(get_current_user)):
                g_nested_only(current_user)
        """,
        )
        assert _routes(_scan(tmp_path)) == [
            "routers/a.py::r_mixed",
            "routers/a.py::r_nested",
            "routers/a.py::r_nested_only",
            "routers/a.py::r_not",
            "routers/a.py::r_or",
        ]

    def test_an_import_in_a_nested_function_does_not_rename_the_outer_call(
        self, tmp_path
    ):
        """A nested function's import never binds the outer name, so it cannot
        turn the outer call into a person check."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def enabled(current_user):
                return True

            def guard(current_user):
                def unused():
                    from dependencies import is_person_principal as enabled
                reject_agent_principal(current_user)
                if not enabled(current_user):
                    raise HTTPException(status_code=403)

            @router.post("/x")
            async def x(current_user=Depends(get_current_user)):
                guard(current_user)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::x"]

    def test_the_later_of_two_imports_is_the_one_called(self, tmp_path):
        _plant(tmp_path, "helpers.py", "def enabled(user):\n    return True\n", imports=False)
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def guard(current_user):
                from dependencies import is_person_principal as enabled
                from helpers import enabled
                reject_agent_principal(current_user)
                if not enabled(current_user):
                    raise HTTPException(status_code=403)

            @router.post("/x")
            async def x(current_user=Depends(get_current_user)):
                guard(current_user)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::x"]

    def test_a_nested_import_cannot_hide_the_outer_denylist(self, tmp_path):
        _plant(tmp_path, "helpers.py", "def noop(user):\n    return None\n", imports=False)
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/x")
            async def x(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                def unused():
                    from helpers import noop as reject_agent_principal
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::x"]

    def test_a_deferred_site_passes_is_reported_and_goes_stale(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/create")
            async def create(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1

            @router.post("/closed", dependencies=[Depends(require_person)])
            async def closed(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1
        """,
        )
        rep = _scan(
            tmp_path,
            deferred={
                "routers/a.py::create": _entry("ruled elsewhere", "routers/a.py::create"),
                "routers/a.py::closed": _entry(
                    "its route took a person rule", "routers/a.py::closed"
                ),
            },
        )
        assert not rep.findings
        assert rep.deferred == [("routers/a.py::create", "routers/a.py::create")]
        assert rep.problems == [
            "stale DEFERRED route routers/a.py::closed for routers/a.py::closed: it no "
            "longer rests on that check alone (gone, person-gated or credited another "
            "way) — remove it"
        ]

    def test_a_reviewed_non_grant_site_passes_and_a_stale_entry_fails(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/restart")
            async def restart(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return 1
        """,
        )
        rep = _scan(
            tmp_path,
            not_grant_guards={
                "routers/a.py::restart": _entry("a lifecycle use", "routers/a.py::restart"),
                "routers/a.py::gone": _entry("no longer exists"),
            },
        )
        assert not rep.findings
        assert rep.problems == [
            "stale NOT_GRANT_GUARDS entry routers/a.py::gone: no agent-only check there now — remove it"
        ]

    def test_a_denylist_in_a_dependency_of_a_dependency_is_followed(self, tmp_path):
        """FastAPI resolves a dependency's own dependencies: a denylist in the
        `Depends(...)` default of the route's dependency guards the route."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def no_agents(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return current_user

            def owned(current_user=Depends(no_agents)):
                return current_user

            @router.post("/grant")
            async def grant(user=Depends(owned)):
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == ["routers/a.py::grant"]
        assert {f.site for f in rep.findings} == {"routers/a.py::no_agents"}

    def test_a_denylist_behind_an_inline_annotated_parameter_is_followed(
        self, tmp_path
    ):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from typing import Annotated

            def no_agents(current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                return current_user

            @router.post("/grant")
            async def grant(user: Annotated[object, Depends(no_agents)]):
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == ["routers/a.py::grant"]
        assert {f.site for f in rep.findings} == {"routers/a.py::no_agents"}

    def test_a_test_that_the_agent_name_is_set_or_the_scope_is_agent_is_a_site(
        self, tmp_path
    ):
        """`is not None`, `!= None` and a scope test naming the agent scope are
        denylists too; a scope allowlist is not a site."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/is-not-none")
            async def is_not_none(current_user=Depends(get_current_user)):
                if current_user.agent_name is not None:
                    raise HTTPException(status_code=403)
                return 1

            @router.post("/not-eq-none")
            async def not_eq_none(current_user=Depends(get_current_user)):
                if None != getattr(current_user, "agent_name", None):
                    raise HTTPException(status_code=403)
                return 1

            @router.post("/scope-eq")
            async def scope_eq(current_user=Depends(get_current_user)):
                if current_user.mcp_scope == "agent":
                    raise HTTPException(status_code=403)
                return 1

            @router.post("/scope-in")
            async def scope_in(current_user=Depends(get_current_user)):
                if getattr(current_user, "mcp_scope", None) in ("agent", "connector"):
                    raise HTTPException(status_code=403)
                return 1

            @router.post("/scope-allowlist")
            async def scope_allowlist(current_user=Depends(get_current_user)):
                if current_user.mcp_scope not in (None, "user"):
                    raise HTTPException(status_code=403)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == [
            "routers/a.py::is_not_none",
            "routers/a.py::not_eq_none",
            "routers/a.py::scope_eq",
            "routers/a.py::scope_in",
        ]
        assert rep.sites == 4

    def test_a_credit_counts_only_as_a_top_level_statement_before_any_return(
        self, tmp_path
    ):
        """An allowlist or admin call under an `if`, or after an early
        `return`, and a person refusal that does not refuse on every path, do
        not run on every path past the denylist: none is credited."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def person_under_if(current_user, flag):
                reject_agent_principal(current_user)
                if flag:
                    assert_person(current_user)

            def person_never(current_user):
                reject_agent_principal(current_user)
                if False:
                    assert_person(current_user)

            def person_after_return(current_user, flag):
                reject_agent_principal(current_user)
                if flag:
                    return current_user
                assert_person(current_user)
                return current_user

            def person_sometimes(current_user, debug):
                reject_agent_principal(current_user)
                if not is_person_principal(current_user):
                    if debug:
                        raise HTTPException(status_code=403)
                return current_user

            def person_first(current_user):
                reject_agent_principal(current_user)
                if not is_person_principal(current_user):
                    raise HTTPException(status_code=403)
                return current_user

            @router.post("/p1")
            async def p1(current_user=Depends(get_current_user)):
                person_under_if(current_user, True)

            @router.post("/p2")
            async def p2(current_user=Depends(get_current_user)):
                person_never(current_user)

            @router.post("/p3")
            async def p3(current_user=Depends(get_current_user)):
                person_after_return(current_user, True)

            @router.post("/p4")
            async def p4(current_user=Depends(get_current_user)):
                person_sometimes(current_user, True)

            @router.post("/p5")
            async def p5(current_user=Depends(get_current_user)):
                person_first(current_user)

            @router.post("/a1")
            async def a1(flag: bool, current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                if flag:
                    assert_admin(current_user)
                return 1

            @router.post("/a2")
            async def a2(flag: bool, current_user=Depends(get_current_user)):
                reject_agent_principal(current_user)
                if flag:
                    return 0
                assert_admin(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert _routes(rep) == [
            "routers/a.py::a1",
            "routers/a.py::a2",
            "routers/a.py::p1",
            "routers/a.py::p2",
            "routers/a.py::p3",
            "routers/a.py::p4",
        ]
        assert [(r, why) for r, _s, why in rep.not_alone] == [
            ("routers/a.py::p5", "the same function refuses a non-person")
        ]

    def test_a_check_no_route_reaches_is_a_problem(self, tmp_path):
        """A check behind a call the scan cannot follow (a method, through an
        object or a class) fails loudly as unreached, never as a pass."""
        _plant(
            tmp_path,
            "services/svc.py",
            """
            class Svc:
                def grant(self, current_user):
                    if current_user.agent_name:
                        raise HTTPException(status_code=403)

                @staticmethod
                def grant_static(current_user):
                    reject_agent_principal(current_user)

            svc = Svc()
        """,
        )
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from services.svc import Svc, svc

            @router.post("/m")
            async def m(current_user=Depends(get_current_user)):
                svc.grant(current_user)
                Svc.grant_static(current_user)
                return 1
        """,
        )
        rep = _scan(tmp_path)
        assert not rep.findings
        unreached = sorted(
            p.split(":")[2] for p in rep.problems if "no census route reaches" in p
        )
        assert unreached == ["Svc.grant", "Svc.grant_static"], rep.problems

    def test_a_listed_check_exempts_only_the_routes_its_entry_names(self, tmp_path):
        """Each pinned table covers the routes it names: a new route that
        reaches the check is a finding, and a named route that no longer rests
        on it alone (here, person-gated) is stale."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            def fence(current_user):
                reject_agent_principal(current_user)

            @router.post("/start")
            async def start(current_user=Depends(get_current_user)):
                fence(current_user)
                return 1

            @router.post("/grant")
            async def grant(current_user=Depends(get_current_user)):
                fence(current_user)
                return 1

            @router.post("/closed", dependencies=[Depends(require_person)])
            async def closed(current_user=Depends(get_current_user)):
                fence(current_user)
                return 1
        """,
        )
        entry = _entry("reviewed", "routers/a.py::start", "routers/a.py::closed")
        for table in ("not_grant_guards", "reviewed_credits", "deferred"):
            rep = _scan(tmp_path, **{table: {"routers/a.py::fence": entry}})
            assert _routes(rep) == ["routers/a.py::grant"], table
            assert "not this one" in rep.findings[0].detail, table
            assert rep.problems == [
                "stale "
                + table.upper()
                + " route routers/a.py::closed for routers/a.py::fence: it no longer "
                "rests on that check alone (gone, person-gated or credited another "
                "way) — remove it"
            ], table

    def test_a_local_bound_from_the_agent_name_is_read(self, tmp_path):
        _plant(
            tmp_path,
            "routers/a.py",
            """
            @router.post("/alias")
            async def alias(current_user=Depends(get_current_user)):
                agent = current_user.agent_name
                if agent:
                    raise HTTPException(status_code=403)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path)) == ["routers/a.py::alias"]

    def test_a_check_in_a_gate_module_helper_counts_where_it_is_called(
        self, tmp_path
    ):
        """`dependencies.py` is entered but not followed: a function there
        that holds an agent-only check, and is not a recognised gate, is a site
        for every route that calls it."""
        _plant(
            tmp_path,
            "routers/a.py",
            """
            from dependencies import fence_spawned

            @router.post("/start")
            async def start(current_user=Depends(get_current_user)):
                fence_spawned(current_user)
                return 1
        """,
        )
        extra = (
            "\ndef fence_spawned(current_user):\n"
            "    reject_agent_principal(current_user)\n"
        )
        rep = _scan(tmp_path, dependencies_extra=extra)
        assert _routes(rep) == ["routers/a.py::start"]
        assert {f.site for f in rep.findings} == {"dependencies.py::fence_spawned"}


_PORTAL_AUTH = """
from dependencies import reject_agent_principal, is_person_principal

def get_portal_principal(user=None):
    reject_agent_principal(user)
    return user
"""

_PORTAL_IMPORTS = """\
from fastapi import APIRouter, Depends, HTTPException
from client_portal.portal_auth import get_portal_principal
router = APIRouter()
"""


class TestWorkspaceWrites:
    def _tree(self, root: Path, body: str) -> None:
        _plant(root, "client_portal/portal_auth.py", _PORTAL_AUTH, imports=False)
        path = root / "client_portal" / "router.py"
        path.write_text(_PORTAL_IMPORTS + textwrap.dedent(body))

    def test_a_write_with_no_person_check_and_no_use_ruling_is_a_finding(
        self, tmp_path
    ):
        self._tree(
            tmp_path,
            """
            def _require_person(principal):
                if not principal.is_person:
                    raise HTTPException(status_code=403)

            @router.post("/readiness")
            def readiness(principal=Depends(get_portal_principal)):
                return 1

            @router.post("/answer")
            def answer(principal=Depends(get_portal_principal)):
                from services import rate_limiter
                if not principal.is_person:
                    raise HTTPException(status_code=403)
                return 1

            @router.put("/pin")
            def pin(principal=Depends(get_portal_principal)):
                _require_person(principal)
                return 1

            @router.put("/star")
            def star(principal=Depends(get_portal_principal)):
                return 1

            @router.get("/read")
            def read(principal=Depends(get_portal_principal)):
                return 1
        """,
        )
        rep = _scan(tmp_path, workspace_uses={"client_portal/router.py::star": "a use"})
        assert _routes(rep, g.WORKSPACE_WRITE) == ["client_portal/router.py::readiness"]
        assert (
            _routes(rep) == []
        ), "the portal door's own denylist is check B's, never check A's"
        assert rep.workspace_writes == 4

    def test_a_person_check_after_another_statement_does_not_count(self, tmp_path):
        self._tree(
            tmp_path,
            """
            @router.post("/decide")
            def decide(principal=Depends(get_portal_principal)):
                record = 1
                if not principal.is_person:
                    raise HTTPException(status_code=403)
                return record
        """,
        )
        assert _routes(_scan(tmp_path), g.WORKSPACE_WRITE) == [
            "client_portal/router.py::decide"
        ]

    def test_a_deferred_write_passes_and_a_redundant_one_fails(self, tmp_path):
        self._tree(
            tmp_path,
            """
            @router.delete("/document")
            def document(principal=Depends(get_portal_principal)):
                return 1

            @router.post("/answer")
            def answer(principal=Depends(get_portal_principal)):
                if not principal.is_person:
                    raise HTTPException(status_code=403)
                return 1
        """,
        )
        rep = _scan(
            tmp_path,
            deferred={
                "client_portal/router.py::document": _entry(
                    "one arm ruled elsewhere", "client_portal/router.py::document"
                ),
                "client_portal/router.py::answer": _entry(
                    "listed although it checks", "client_portal/router.py::answer"
                ),
            },
        )
        assert _routes(rep, g.WORKSPACE_WRITE) == []
        assert rep.deferred == [
            ("client_portal/router.py::document", "client_portal/router.py::document")
        ]
        assert [x.split(":")[0] for x in rep.problems] == [
            "redundant DEFERRED entry client_portal/router.py"
        ]

    def test_a_nested_import_does_not_make_the_first_call_a_person_check(
        self, tmp_path
    ):
        self._tree(
            tmp_path,
            """
            def noop(principal):
                return None

            def require_person_helper(principal):
                if not principal.is_person:
                    raise HTTPException(status_code=403)

            @router.post("/grant")
            def grant(principal=Depends(get_portal_principal)):
                noop(principal)
                def unused():
                    from client_portal.router import require_person_helper as noop
                return 1
        """,
        )
        assert _routes(_scan(tmp_path), g.WORKSPACE_WRITE) == [
            "client_portal/router.py::grant"
        ]

    def test_redundant_and_stale_use_entries_fail(self, tmp_path):
        self._tree(
            tmp_path,
            """
            @router.post("/answer")
            def answer(principal=Depends(get_portal_principal)):
                if not principal.is_person:
                    raise HTTPException(status_code=403)
                return 1
        """,
        )
        rep = _scan(
            tmp_path,
            workspace_uses={
                "client_portal/router.py::answer": "listed although it checks",
                "client_portal/router.py::gone": "no such write",
            },
        )
        assert sorted(p.split(":")[0] for p in rep.problems) == [
            "redundant WORKSPACE_USES entry client_portal/router.py",
            "stale WORKSPACE_USES entry client_portal/router.py",
        ]

    def test_a_person_check_that_does_not_always_refuse_does_not_count(
        self, tmp_path
    ):
        self._tree(
            tmp_path,
            """
            @router.post("/decide")
            def decide(debug: bool, principal=Depends(get_portal_principal)):
                if not principal.is_person:
                    if debug:
                        raise HTTPException(status_code=403)
                return 1
        """,
        )
        assert _routes(_scan(tmp_path), g.WORKSPACE_WRITE) == [
            "client_portal/router.py::decide"
        ]

    def test_a_deferred_write_whose_arm_refuses_a_non_person_goes_stale(
        self, tmp_path
    ):
        """Once the deferred arm takes its person rule, the route's other arm is
        a use, so the entry must move to WORKSPACE_USES: inline, nested or via a
        helper, the arm check makes the entry stale."""
        self._tree(
            tmp_path,
            """
            def _require_person(principal):
                if not principal.is_person:
                    raise HTTPException(status_code=403)

            @router.delete("/inline")
            def inline(scope: str, principal=Depends(get_portal_principal)):
                if scope == "everyone" and not principal.is_person:
                    raise HTTPException(status_code=403)
                return 1

            @router.delete("/nested")
            def nested(scope: str, principal=Depends(get_portal_principal)):
                if scope == "everyone":
                    if not principal.is_person:
                        raise HTTPException(status_code=403)
                return 1

            @router.delete("/helper")
            def helper(scope: str, principal=Depends(get_portal_principal)):
                if scope == "everyone":
                    _require_person(principal)
                return 1

            @router.delete("/open")
            def still_open(scope: str, principal=Depends(get_portal_principal)):
                return 1
        """,
        )
        keys = [
            f"client_portal/router.py::{n}" for n in ("helper", "inline", "nested", "still_open")
        ]
        rep = _scan(tmp_path, deferred={k: _entry("one arm ruled elsewhere", k) for k in keys})
        stale = sorted(
            p.split(" ")[3].rstrip(":") for p in rep.problems if "an arm of it" in p
        )
        assert stale == keys[:3], rep.problems
        assert rep.deferred == [(keys[3], keys[3])]
        assert not rep.findings

    def test_the_door_outside_portal_routes_is_exempt_only_through_a_fallback(
        self, tmp_path
    ):
        """A non-portal route that reaches the Workspace door does so through
        a listed fallback (exempt) or not (a finding); a fallback no route uses
        is stale."""
        self._tree(tmp_path, "")
        _plant(
            tmp_path,
            "rooms/router.py",
            """
            from client_portal.portal_auth import get_portal_principal

            def room_principal(current_user=None):
                if current_user is not None:
                    return current_user
                return get_portal_principal(current_user)

            def other_principal(current_user=None):
                return get_portal_principal(current_user)

            @router.post("/rooms")
            async def create(principal=Depends(room_principal)):
                return 1

            @router.post("/other")
            async def other(principal=Depends(other_principal)):
                return 1
        """,
        )
        fallbacks = {
            "rooms/router.py::room_principal": "a platform credential resolves first",
            "rooms/router.py::unused": "no route uses it",
        }
        rep = _scan(tmp_path, door_fallbacks=fallbacks)
        assert _routes(rep) == ["rooms/router.py::other"]
        assert {f.site for f in rep.findings} == {g.WORKSPACE_DOOR}
        assert [p.split(":")[0] for p in rep.problems] == [
            "stale DOOR_FALLBACKS entry rooms/router.py"
        ]


_ROOMS_BEFORE = """
def _caller(current_user):
    return "user", "x"

def _require_moderator(room_id, current_user):
    kind, identity = _caller(current_user)
    if getattr(current_user, "role", None) == "admin" and kind == "user":
        return room_id
    if kind == "user" and identity:
        return room_id
    raise ValueError("not_moderator")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""

_ROOMS_AFTER = """
def _caller(current_user):
    return "user", "x"

def _is_room_person(current_user):
    from dependencies import is_person_principal
    kind, _identity = _caller(current_user)
    return kind == "user" and is_person_principal(current_user)

def _require_moderator(room_id, current_user):
    kind, identity = _caller(current_user)
    person = _is_room_person(current_user)
    if person and getattr(current_user, "role", None) == "admin":
        return room_id
    if person and identity:
        return room_id
    raise ValueError("not_moderator")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""

_ROOMS_ROUTER = """
from rooms import service

@router.post("/rooms/{room_id}/close")
async def close(room_id: str, current_user=Depends(get_current_user)):
    service.close_room(current_user, room_id)
    return 1
"""


class TestRoomModeratorDoor:
    DOOR = "rooms/service.py::_require_moderator"

    def test_each_branch_without_a_person_term_is_a_finding(self, tmp_path):
        _plant(tmp_path, "rooms/service.py", _ROOMS_BEFORE, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
        assert {f.route for f in rows} == {"rooms/router.py::close"}
        assert len(rows) == 2, "both admission branches are reported"
        assert rep.door_routes == ["rooms/router.py::close"] and not rep.problems

    def test_a_person_term_on_both_branches_passes(self, tmp_path):
        _plant(tmp_path, "rooms/service.py", _ROOMS_AFTER, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        assert not rep.findings and not rep.problems

    def test_an_unconditional_admission_is_a_finding(self, tmp_path):
        """Every way out that admits counts, not only an `if` that returns."""
        tail = 'raise ValueError("not_moderator")\n'
        assert _ROOMS_AFTER.count(tail) == 1
        for body in (
            _ROOMS_AFTER.replace(tail, "return room_id\n"),
            _ROOMS_AFTER.replace(tail, "pass\n"),
        ):
            root = tmp_path / str(abs(hash(body)))
            _plant(root, "rooms/service.py", body, imports=False)
            _plant(root, "rooms/router.py", _ROOMS_ROUTER)
            rep = _scan(root, moderator_door=self.DOOR)
            rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
            assert len(rows) == 1 and "no condition" in rows[0].detail, body

    def test_a_check_ending_in_a_try_that_always_exits_is_clean(self, tmp_path):
        """No fall-through is read after a `try` whose every path returns
        under a person term or raises."""
        door = """
def _caller(current_user):
    return "user", "x"

def _require_moderator(room_id, current_user):
    from dependencies import is_person_principal
    try:
        if is_person_principal(current_user):
            return room_id
        raise ValueError("denied")
    except LookupError:
        raise ValueError("denied")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""
        _plant(tmp_path, "rooms/service.py", door, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        assert not rep.findings and not rep.problems

    def test_a_refusal_inside_a_with_may_be_suppressed(self, tmp_path):
        """A context manager can swallow the refusal a `with` ends on, so the
        check can fall through to its end and admit."""
        door = """
from contextlib import suppress

def _caller(current_user):
    return "user", "x"

def _require_moderator(room_id, current_user):
    from dependencies import is_person_principal
    with suppress(ValueError):
        if is_person_principal(current_user):
            return room_id
        raise ValueError("denied")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""
        _plant(tmp_path, "rooms/service.py", door, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
        assert len(rows) == 1 and "no condition" in rows[0].detail

    def test_an_admission_inside_a_match_case_is_read(self, tmp_path):
        tail = 'raise ValueError("not_moderator")\n'
        body = _ROOMS_AFTER.replace(
            tail,
            'match kind:\n        case "user":\n            return room_id\n    ' + tail,
        )
        assert body != _ROOMS_AFTER
        _plant(tmp_path, "rooms/service.py", body, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
        assert len(rows) == 1 and "no condition" in rows[0].detail

    def test_a_person_local_bound_again_another_way_does_not_count(self, tmp_path):
        """`person = True` after `person = _is_room_person(...)` admits a
        non-person on both branches; so does an augmented assignment."""
        bound = "    person = _is_room_person(current_user)\n"
        assert _ROOMS_AFTER.count(bound) == 1
        for again in ("    person = True\n", "    person |= True\n"):
            body = _ROOMS_AFTER.replace(bound, bound + again)
            root = tmp_path / str(abs(hash(again)))
            _plant(root, "rooms/service.py", body, imports=False)
            _plant(root, "rooms/router.py", _ROOMS_ROUTER)
            rep = _scan(root, moderator_door=self.DOOR)
            rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
            assert len(rows) == 2, again

    def test_a_person_helper_with_or_does_not_count(self, tmp_path):
        body = _ROOMS_AFTER.replace(
            'return kind == "user" and is_person_principal(current_user)',
            'return kind == "user" or is_person_principal(current_user)',
        )
        assert body != _ROOMS_AFTER
        _plant(tmp_path, "rooms/service.py", body, imports=False)
        _plant(tmp_path, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        assert len([f for f in rep.findings if f.check == g.ROOM_MODERATOR]) == 2

    def test_a_missing_door_fails_loudly(self, tmp_path):
        _plant(tmp_path, "rooms/router.py", "\n")
        rep = _scan(tmp_path, moderator_door=self.DOOR)
        assert any("is gone" in p for p in rep.problems)

    def test_a_door_no_route_reaches_or_that_never_admits_fails_loudly(
        self, tmp_path
    ):
        never = """
def _require_moderator(room_id, current_user):
    raise ValueError("not_moderator")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""
        _plant(tmp_path / "never", "rooms/service.py", never, imports=False)
        _plant(tmp_path / "never", "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(tmp_path / "never", moderator_door=self.DOOR)
        assert [p.split(": ")[1] for p in rep.problems] == ["no admission branch found"]
        _plant(tmp_path / "orphan", "rooms/service.py", _ROOMS_AFTER, imports=False)
        _plant(tmp_path / "orphan", "rooms/router.py", "\n")
        rep = _scan(tmp_path / "orphan", moderator_door=self.DOOR)
        assert any("no route reaches it" in p for p in rep.problems), rep.problems

    def test_an_early_person_refusal_guards_every_later_admission(self, tmp_path):
        """`if not is_person_principal(u): raise` as a top-level statement
        guards every exit after it; one that does not always refuse does not."""
        door = """
def _caller(current_user):
    return "user", "x"

def _require_moderator(room_id, current_user, debug=False):
    from dependencies import is_person_principal
    if not is_person_principal(current_user):
        raise ValueError("not_a_person")
    if getattr(current_user, "role", None) == "admin":
        return room_id
    return room_id

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""
        refusal = '        raise ValueError("not_a_person")\n'
        weak = door.replace(refusal, "        if debug:\n    " + refusal)
        assert weak != door
        for body, expected in ((door, 0), (weak, 2)):
            root = tmp_path / str(expected)
            _plant(root, "rooms/service.py", body, imports=False)
            _plant(root, "rooms/router.py", _ROOMS_ROUTER)
            rep = _scan(root, moderator_door=self.DOOR)
            rows = [f for f in rep.findings if f.check == g.ROOM_MODERATOR]
            assert len(rows) == expected and not rep.problems, body


_ROOMS_WORKSPACE = """
WORKSPACE_KIND = "workspace_user"

def _caller(current_user):
    return "user", "x"

def _is_room_person(current_user):
    from dependencies import is_person_principal
    kind, _identity = _caller(current_user)
    if kind == WORKSPACE_KIND:
        return True
    return kind == "user" and is_person_principal(current_user)

def _require_moderator(room_id, current_user):
    kind, identity = _caller(current_user)
    person = _is_room_person(current_user)
    if person and getattr(current_user, "role", None) == "admin":
        return room_id
    if person and identity:
        return room_id
    raise ValueError("not_moderator")

def close_room(current_user, room_id):
    _require_moderator(room_id, current_user)
"""


class TestPersonBranches:
    DOOR = "rooms/service.py::_require_moderator"
    HELPER = "rooms/service.py::_is_room_person"
    LISTED = {HELPER: ("kind == WORKSPACE_KIND", "a person by construction")}

    def _run(self, root, body, branches):
        _plant(root, "rooms/service.py", body, imports=False)
        _plant(root, "rooms/router.py", _ROOMS_ROUTER)
        rep = _scan(root, moderator_door=self.DOOR, person_branches=branches)
        return [f for f in rep.findings if f.check == g.ROOM_MODERATOR], rep

    def test_a_constant_true_counts_only_on_the_listed_branch(self, tmp_path):
        rows, rep = self._run(tmp_path / "listed", _ROOMS_WORKSPACE, self.LISTED)
        assert rows == [] and rep.problems == []
        rows, rep = self._run(tmp_path / "unlisted", _ROOMS_WORKSPACE, {})
        assert len(rows) == 2, "a constant True on an unlisted branch is no person term"

    def test_a_widened_branch_is_not_the_listed_one_and_the_entry_goes_stale(
        self, tmp_path
    ):
        """The mutation that admits the system key (kind `user`) again: the
        branch no longer matches, so the helper is no person term and the entry
        is stale."""
        body = _ROOMS_WORKSPACE.replace(
            "if kind == WORKSPACE_KIND:", 'if kind != "agent":'
        )
        assert body != _ROOMS_WORKSPACE
        rows, rep = self._run(tmp_path, body, self.LISTED)
        assert len(rows) == 2
        assert [p.split(":")[0] for p in rep.problems] == [
            "stale PERSON_BRANCHES entry rooms/service.py"
        ]

    def test_an_entry_for_a_helper_the_tree_lacks_is_printed_not_failed(
        self, tmp_path
    ):
        """So the table can name a helper before the code that defines it lands;
        a helper the tree defines without the branch is stale (above)."""
        rep = _scan(
            tmp_path,
            moderator_door=None,
            person_branches={"rooms/service.py::_gone": ("x", "y")},
        )
        assert rep.problems == []
        assert rep.absent_person_branches == ["rooms/service.py::_gone"]
