"""trinity-enterprise#719 — a refused human-only attempt is audited.

The human-only primitives in `dependencies.py` (`require_person`,
`assert_person`, `require_interactive`, `reject_non_interactive_principal`,
`reject_non_person_principal`) answer a machine principal with a 403. Before
this, nothing recorded it: a `Depends` gate refuses before the handler body
(where an audit call would sit) runs, and the imperative call sites hold no
`Request`. They now raise `HumanOnlyRefused`, an `HTTPException` subclass, and
one app handler (`error_handlers.human_only_refused`) records the attempt and
then answers with the handler the app registers for `HTTPException` (FastAPI's
own `http_exception_handler` on this app), so the response does not change.

Every case runs through the REAL app — `main.app`, with the handler table
`main.py` builds — in a subprocess (`_human_only_audit_app.py`), the way
`test_2996_route_census_runtime.py` imports it, so nothing leaks into the unit
island. The subprocess starts on a new database file (a fresh install: no audit
rows), overrides `get_current_user` with stated `User` principals, and drives
real routes: one `Depends` gate and one imperative gate, plus one route per
remaining raise site, the already-auditing owner-tier file read, and routes that
answer an unrelated 403.

What is pinned:

* exactly one `authorization` / `human_only_refused` row per refusal, naming the
  agent for an agent key, `trinity-system` for the system key and the person for
  the person's `user` key; every other machine principal is filed as itself;
* the response is byte-identical with the handler registered and removed, and
  comes from whatever handler the app answers every other `HTTPException` with;
* an audit failure (the sink raising, the insert failing, the handler's own code
  raising) still answers the same 403, and does not use the window: the same
  refusal a second later writes its row;
* a site that already writes its own row for the refusal writes no second one;
* a repeat inside the window writes none, the next window writes one, the key
  is the route template and the method, and a full window records nothing new
  rather than forgetting an unexpired key;
* an unrelated 403 writes none;
* a sentinel sent in the body, the query string, `User-Agent`, a custom header
  and the forwarding headers (`X-Forwarded-For`, `X-Real-IP`,
  `CF-Connecting-IP`) reaches no row; the handler copies only the request id
  from the headers (the app's `X-Request-ID`, carried on every audit row), and
  `actor_ip` is the client address the app resolved for the request (here the
  test client's own: the server's launch flags and its proxy resolver are not
  in this path, and are pinned by their own tests);
* the values whose length the caller chooses (the concrete path, the agent name
  in it, the request id) are cut to the length the row keeps.

Mutation: remove the `add_exception_handler` line for `HumanOnlyRefused` from
`main.py` and every row-asserting test here goes red while the byte-identity
test stays green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_BACKEND = _REPO / "src" / "backend"
_DRIVER = _HERE / "_human_only_audit_app.py"
_MARK = "__AUDIT__"

sys.path.insert(0, str(_HERE))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# Pinned at import time, as test_ent109 pins it: `error_handlers` imports only
# FastAPI and `utils.credential_sanitizer`, never the app.
from _human_only_audit_app import AGENT_A, AGENT_B, SENTINEL  # noqa: E402
from starlette.requests import Request  # noqa: E402

import error_handlers  # noqa: E402

ACTION = "human_only_refused"


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """One run of the driver against a new database file. Fails, never skips,
    when the app cannot be imported or prints nothing parseable."""
    db_path = tmp_path_factory.mktemp("refusal-audit") / "trinity.db"
    env = {
        k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DATABASE_URL")
    }
    env["TRINITY_DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, str(_DRIVER), str(_BACKEND)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(_REPO),
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"the real-app driver failed (exit {proc.returncode}):\n{proc.stderr[-6000:]}"
        )
    marker = proc.stdout.rfind(_MARK)
    if marker < 0:
        raise RuntimeError(f"the driver printed no result:\n{proc.stdout[-2000:]}")
    return json.loads(proc.stdout[marker + len(_MARK) :])


def _refusal_rows(call: dict) -> list:
    return [r for r in call["rows"] if r["event_action"] == ACTION]


def _details(row: dict) -> dict:
    return (
        json.loads(row["details"])
        if isinstance(row["details"], str)
        else row["details"]
    )


def _one_refusal_row(call: dict) -> dict:
    assert call["status"] == 403, call["body"]
    rows = call["rows"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["event_action"] == ACTION
    assert row["event_type"] == "authorization"
    assert row["source"] == "api"
    assert row["request_id"] == call["request_id"]
    return row


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_the_real_app_routes_the_refusal_to_the_handler(run):
    assert run["handler_registered"] is True
    assert run["status_403_handler"] is False, (
        "a handler registered for status 403 answers before the class handler, "
        "so no human-only refusal would be recorded"
    )


@pytest.mark.parametrize(
    "template, params, expected",
    [
        ("/api/agents/{agent_name}/autonomy", {"agent_name": "a"}, "a"),
        ("/api/nevermined/agents/{agent_name}/delegation", {"agent_name": "a"}, "a"),
        ("/api/agents/{name}/public-links", {"name": "a"}, "a"),
        ("/api/agents/{name}", {"name": "a"}, "a"),
        ("/api/nevermined/agents/{name}/config", {"name": "a"}, "a"),
        ("/api/skills/{name}", {"name": "a-skill"}, None),
        ("/api/users/me/preferences/{key}", {"key": "grid"}, None),
        ("/api/mcp/keys", {}, None),
    ],
)
def test_the_row_names_an_agent_target_only_where_the_route_names_one(
    template, params, expected
):
    """A unit test of the helper on a built scope: it reads only the route
    template and the path parameters. The `{agent_name}` case also runs through
    the real app (`test_every_raise_site_is_recorded`). No route on this tree
    that raises the refusal takes `{name}`; that case covers routes such as
    `/api/agents/{name}/canvas/shares` and
    `/api/nevermined/agents/{name}/config`, which other changes for
    trinity-enterprise#719 put behind the same gates."""
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/unused",
            "headers": [],
            "route": SimpleNamespace(path=template),
            "path_params": params,
        }
    )
    assert error_handlers._path_agent(request) == expected


# ---------------------------------------------------------------------------
# the other shape: a fresh install
# ---------------------------------------------------------------------------


def test_a_fresh_install_has_no_rows_and_the_first_refusal_writes_one(run):
    assert run["rows_at_start"] == 0
    row = _one_refusal_row(run["first_refusal"])
    assert (row["actor_type"], row["actor_id"]) == ("agent", AGENT_A)


# ---------------------------------------------------------------------------
# one Depends route and one imperative route, each principal named correctly
# ---------------------------------------------------------------------------

_EXPECTED_ACTOR = {
    "agent": ("agent", AGENT_A, "agent", "key-agent-a"),
    "system": ("agent", "trinity-system", "system", "key-system"),
    "user": ("user", None, "user", "key-user"),  # actor_id is the person's user id
}
_ROUTES = {
    "depends": ("GET", "/api/mcp/keys"),
    "imperative": ("GET", "/api/users/me/preferences"),
}


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("kind", sorted(_EXPECTED_ACTOR))
def test_each_refusal_writes_exactly_one_row_naming_who_was_refused(run, route, kind):
    call = run["matrix"][route][kind]
    row = _one_refusal_row(call)
    actor_type, actor_id, scope, key_id = _EXPECTED_ACTOR[kind]
    assert row["actor_type"] == actor_type
    if actor_id is not None:
        assert row["actor_id"] == actor_id
    else:  # the person: their own user id, never an agent or the platform
        assert row["actor_id"] == str(run["owner_id"])
    assert row["actor_email"] == "owner@example.com"
    assert row["mcp_scope"] == scope
    assert row["mcp_key_id"] == key_id
    method, path = _ROUTES[route]
    assert row["endpoint"] == path
    assert _details(row) == {"method": method, "route": path, "rule": "interactive"}
    assert row["target_type"] is None and row["target_id"] is None


def test_the_person_row_names_the_person_not_the_owner_of_an_agent(run):
    """The same owner's agent key and user key land on different actors."""
    agent_row = _one_refusal_row(run["matrix"]["depends"]["agent"])
    user_row = _one_refusal_row(run["matrix"]["depends"]["user"])
    assert (agent_row["actor_type"], agent_row["actor_id"]) == ("agent", AGENT_A)
    assert (user_row["actor_type"], user_row["actor_id"]) == (
        "user",
        str(run["owner_id"]),
    )


@pytest.mark.parametrize(
    "kind, actor_type, actor_id, scope",
    [
        ("sibling", "agent", AGENT_B, "agent"),
        ("connector", "mcp_client", "key-connector", "connector"),
        ("ops", "mcp_client", "key-ops", "ops"),
        ("portal_delegate", "mcp_client", "key-delegate", "portal_delegate"),
        ("unknown", "mcp_client", "key-unknown", "a-scope-no-one-has-shipped"),
        ("loopback", "agent", AGENT_A, None),
    ],
)
def test_every_other_machine_principal_is_filed_as_itself(
    run, kind, actor_type, actor_id, scope
):
    row = _one_refusal_row(run["other_kinds"][kind])
    assert (row["actor_type"], row["actor_id"], row["mcp_scope"]) == (
        actor_type,
        actor_id,
        scope,
    )
    details = _details(row)
    assert details.get("loopback") is (True if kind == "loopback" else None)


@pytest.mark.parametrize(
    "kind, loopback", [("unvouched_loopback", True), ("no_scope", None)]
)
def test_a_principal_naming_no_agent_or_key_is_filed_as_the_platform(
    run, kind, loopback
):
    """Neither names an agent or a key, so the row says so rather than naming a
    person: the audit resolver's platform actor, with the owner beside it."""
    row = _one_refusal_row(run["unnamed_kinds"][kind])
    assert (row["actor_type"], row["actor_id"]) == ("system", "trinity-system")
    assert row["mcp_scope"] is None and row["mcp_key_id"] is None
    assert row["actor_email"] == "owner@example.com"
    assert (row["target_type"], row["target_id"]) == ("agent", AGENT_A)
    details = _details(row)
    assert details["rule"] == "person" and details["code"] == "person_required"
    assert details.get("loopback") is loopback


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_person_is_not_refused_and_writes_nothing(run, route):
    call = run["person_passes"][route]
    assert call["status"] == 200, call["body"]
    assert _refusal_rows(call) == []


# ---------------------------------------------------------------------------
# every raise site of the family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "site, kind, detail_key, rule, target",
    [
        ("person", "agent", "HUMAN_ONLY_DETAIL", "person", AGENT_A),
        ("person", "system", "HUMAN_ONLY_DETAIL", "person", AGENT_A),
        ("ask", "agent", "PERSON_REQUIRED_DETAIL", "person", None),
        ("ask", "system", "PERSON_REQUIRED_DETAIL", "person", None),
        ("self_grant", "agent", "HUMAN_ONLY_DETAIL", "person", AGENT_A),
    ],
)
def test_every_raise_site_is_recorded(run, site, kind, detail_key, rule, target):
    call = run["sites"][site][kind]
    assert json.loads(call["body"]) == {"detail": run["details"][detail_key]}
    row = _one_refusal_row(call)
    details = _details(row)
    assert details["rule"] == rule
    assert details["code"] == "person_required"
    if target:
        assert (row["target_type"], row["target_id"]) == ("agent", target)
    else:
        assert row["target_type"] is None and row["target_id"] is None


# ---------------------------------------------------------------------------
# the response does not change
# ---------------------------------------------------------------------------


def test_the_response_is_byte_identical_with_and_without_the_handler(run):
    with_handler, without = run["identity"]["with"], run["identity"]["without"]
    assert [c["case"] for c in with_handler] == [c["case"] for c in without]
    for a, b in zip(with_handler, without):
        assert a["status"] == b["status"] == 403, a["case"]
        assert a["body"] == b["body"], a["case"]
        assert a["headers"] == b["headers"], a["case"]
    # the handler, and only the handler, is what writes the row
    assert all(c["human_only_rows"] == 0 for c in without)


def test_the_body_is_fastapis_own_rendering_of_the_detail(run):
    """The bytes are what `JSONResponse({"detail": ...})` renders, the shape every
    client of these routes already parses."""
    call = run["matrix"]["depends"]["agent"]
    detail = json.loads(call["body"])["detail"]
    rendered = json.dumps({"detail": detail}, ensure_ascii=False, separators=(",", ":"))
    assert call["body"] == rendered
    headers = dict(call["headers"])
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(rendered.encode()))


def test_the_answer_is_whatever_the_app_answers_an_http_exception_with(run):
    """If the app replaces its `HTTPException` handler, a human-only refusal is
    answered by the replacement, exactly as an unrelated 403 is, and is still
    recorded."""
    refused = run["replaced_http_handler"]["refused"]
    unrelated = run["replaced_http_handler"]["unrelated"]
    assert dict(unrelated["headers"]).get("x-test-http-handler") == "replaced"
    assert dict(refused["headers"]).get("x-test-http-handler") == "replaced"
    assert refused["status"] == 403
    assert refused["body"] == run["matrix"]["depends"]["agent"]["body"]
    assert len(_refusal_rows(refused)) == 1
    assert unrelated["rows"] == []


def test_the_row_writing_path_is_exercised_by_the_byte_identity_run(run):
    """Guard against a vacuous identity check: with the handler, every refused
    request in that list did write its row."""
    assert all(
        c["human_only_rows"] == 1
        for c in run["identity"]["with"]
        if "files/download" not in c["case"]
    )


# ---------------------------------------------------------------------------
# an audit failure still answers 403
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["log_raises", "insert_raises", "handler_raises"])
def test_an_audit_failure_still_answers_the_same_403(run, failure):
    call = run["failures"][failure]
    reference = run["matrix"]["depends"]["agent"]
    assert call["status"] == 403
    assert call["body"] == reference["body"]
    assert _refusal_rows(call) == []


@pytest.mark.parametrize("failure", ["log_raises", "insert_raises", "handler_raises"])
def test_a_failed_write_does_not_use_the_window(run, failure):
    """The same refusal one second after a failed write, inside what would have
    been its window, writes the row the failure lost."""
    retry = run["failure_retries"][failure]
    assert retry["status"] == 403
    assert len(_refusal_rows(retry)) == 1


# ---------------------------------------------------------------------------
# a site that already audits writes no second row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["agent", "system"])
def test_a_site_that_already_audits_writes_no_second_row(run, kind):
    call = run["already_audits"][kind]
    assert call["status"] == 403
    assert json.loads(call["body"]) == {"detail": run["details"]["HUMAN_ONLY_DETAIL"]}
    actions = [r["event_action"] for r in call["rows"]]
    assert actions == ["file_read_refused"], actions


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------


def test_a_repeat_inside_the_window_writes_none_and_the_next_window_writes_one(run):
    first, at_30s, at_59_9s, at_60s, other_route, other_principal = run["window"]
    assert all(c["status"] == 403 for c in run["window"])
    assert len(_refusal_rows(first)) == 1
    assert _refusal_rows(at_30s) == []
    assert _refusal_rows(at_59_9s) == []
    assert len(_refusal_rows(at_60s)) == 1
    # the key is the principal, the method and the route
    assert len(_refusal_rows(other_route)) == 1
    assert len(_refusal_rows(other_principal)) == 1
    # the suppressed answers are the same 403
    assert at_30s["body"] == first["body"]


def test_the_window_is_keyed_on_the_route_template_and_the_method(run):
    put_a, put_b, delete_a = (
        run["template"][k] for k in ("put_a", "put_b", "delete_a")
    )
    assert {c["status"] for c in (put_a, put_b, delete_a)} == {403}
    assert len(_refusal_rows(put_a)) == 1
    # another concrete path on the same template is the same key
    assert _refusal_rows(put_b) == []
    # another method on the same template is another key
    (row,) = _refusal_rows(delete_a)
    assert _details(row)["method"] == "DELETE"
    assert row["endpoint"] == "/api/users/me/preferences/a"


def test_a_refusal_loop_writes_one_row(run):
    assert run["loop"] == {"statuses": [403], "rows": 1}


def test_a_full_window_records_nothing_new_and_never_forgets_a_key(run):
    """With room for two keys: a third key is not recorded while the window is
    full, the first key is not forgotten (its repeat writes nothing), the full
    window is logged once, and once the entries expire each key writes again."""
    assert run["full_window"] == {
        "rows": [1, 1, 0, 0, 0, 1, 1],
        "sizes": [1, 2, 2, 2, 2, 1, 2],
        "full_warnings": 1,
    }


# ---------------------------------------------------------------------------
# an unrelated 403 writes none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["admin_gate_operator", "admin_gate_agent"])
def test_an_unrelated_403_writes_no_row(run, case):
    call = run["unrelated"][case]
    assert call["status"] == 403, call["body"]
    assert call["rows"] == []


def test_a_capability_refusal_keeps_its_own_single_row(run):
    call = run["unrelated"]["capability_refusal"]
    assert call["status"] == 403, call["body"]
    assert [r["event_action"] for r in call["rows"]] == ["capability_refused"]


# ---------------------------------------------------------------------------
# no row carries a body
# ---------------------------------------------------------------------------


def test_no_row_carries_the_body_the_query_user_agent_a_custom_or_a_forwarding_header(
    run,
):
    call = run["body"]
    row = _one_refusal_row(call)
    # it was sent in the body, the query string and five headers, and the body
    # was parsed: the refusal is the handler's own first statement
    assert SENTINEL in json.dumps(call["sent"]["json"])
    assert SENTINEL in call["sent"]["url"]
    sent = call["sent"]["headers"]
    for header in ("X-Probe", "User-Agent", "X-Real-IP", "CF-Connecting-IP"):
        assert sent[header] == SENTINEL
    assert sent["X-Forwarded-For"].startswith(SENTINEL)
    assert SENTINEL not in json.dumps(call["rows"])
    # actor_ip is the address the app resolved for this caller (the test
    # client's own), not a forwarding header the caller sent
    assert row["actor_ip"] == "testclient"
    # the one header the handler copies: the request id, as every audit row does
    assert row["request_id"] == sent["X-Request-ID"]
    assert row["endpoint"] == "/api/users/me/preferences/grid"
    assert _details(row) == {
        "method": "PUT",
        "route": "/api/users/me/preferences/{key}",
        "rule": "interactive",
    }


def test_the_values_the_caller_sizes_are_cut_to_the_length_the_row_keeps(run):
    """A 4,000-character agent name and request id: the row keeps the first 256
    characters (the length `requirements/auth.md` states) of the path, the agent
    name and the id."""
    call = run["caller_sized"]
    cap = 256
    assert call["status"] == 403, call["body"]
    (row,) = _refusal_rows(call)
    url = call["sent"]["url"]
    assert len(url) > cap and len(call["sent"]["headers"]["X-Request-ID"]) > cap
    assert row["endpoint"] == url[:cap]
    assert row["target_type"] == "agent"
    assert row["target_id"] == url.split("/")[3][:cap]
    assert row["request_id"] == call["sent"]["headers"]["X-Request-ID"][:cap]
    assert _details(row)["route"] == "/api/agents/{agent_name}/autonomy"


def test_the_driver_leaves_no_temp_dir_behind(run):
    assert run["temp_dir"]
    assert not Path(run["temp_dir"]).exists()


def test_no_row_anywhere_in_the_run_carries_the_sentinel(run):
    assert run["all_rows"], "the run wrote rows"
    assert SENTINEL not in json.dumps(run["all_rows"])
    allowed = {"method", "route", "rule", "code", "loopback"}
    for row in run["all_rows"]:
        if row["event_action"] == ACTION:
            assert set(_details(row)) <= allowed, row
