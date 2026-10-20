"""trinity-enterprise#812 — owned objectives come from the seat on record.

Operator ruling 2026-10-06 (R50 + R50a): a seat is held by a person or by an
agent, recorded in Trinity; `x-role.role` in template.yaml stops being truth.
The objective join therefore resolves "owned" through `assignment_provider.
resolve_seat`:

* the seat the agent HOLDS (an autonomous player) — that seat only;
* otherwise the seats its assigned people hold (a companion), primary first —
  a shared companion owns the union;
* neither → it owns nothing, and a `no_seat` finding says why; work that names
  it in `supporting_agents` (read as before, now frozen) still shows.

The canon declaration alone enables the join: `x-role` is neither required nor
read. Driven through the real `read_objective_join` with the ent#666 suite's
fakes (the agent door and the store).
"""
import pytest

from services import objective_join_service as svc
from unit.test_ent666_objective_join import (  # noqa: F401 — fixtures by name
    AGENT, NOW, OBJECTIVE_YAML, _FakeClient, running, store,
)

pytestmark = pytest.mark.unit

CANON_ONLY = "name: sales-companion\nx-canon:\n  clone_path: canon\n"


def _objective(obj_id, owner, supporting=None):
    text = OBJECTIVE_YAML.replace("id: q4-close-rate", f"id: {obj_id}")
    text = text.replace("owner: role:revenue-lead", f"owner: role:{owner}")
    if supporting:
        text += f"supporting_agents: [{supporting}]\n"
    return text


@pytest.fixture
def seat():
    """Register a provider answering `seat_for` with what the test sets."""
    from services import assignment_provider as ap

    answer = {"value": None}

    class _Seats:
        def assignment_for(self, agent_name, triggered_by):
            return None

        def seat_for(self, agent_name):
            return answer["value"]

    ap.register_provider(_Seats())
    yield answer
    ap.clear_provider()


def _files(*objectives, template=CANON_ONLY):
    files = {"template.yaml": template}
    for i, text in enumerate(objectives):
        files[f"canon/objectives/o{i}.yaml"] = text
    return files


def _owned(result):
    return sorted(o["id"] for o in result["objectives"] if o["owned"])


@pytest.mark.asyncio
async def test_an_agent_that_holds_a_seat_owns_through_that_seat(store, running, seat):
    seat["value"] = {"case": "holds", "role_id": "orchestrator", "seats": ["cfo"]}
    client = _FakeClient(files=_files(_objective("run-the-fleet", "orchestrator"),
                                      _objective("cut-costs", "cfo")))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert _owned(result) == ["run-the-fleet"], "holds wins: its people's seats are not its own"
    assert result["role"] == {"id": "orchestrator", "path": "canon/roles/orchestrator.yaml",
                              "case": "holds", "seats": ["orchestrator"]}


@pytest.mark.asyncio
async def test_a_companion_owns_the_union_of_its_peoples_seats(store, running, seat):
    seat["value"] = {"case": "serves", "role_id": "revenue-lead",
                     "seats": ["revenue-lead", "cfo"]}
    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead"),
                                      _objective("cut-costs", "cfo"),
                                      _objective("brand", "marketing-lead")))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert _owned(result) == ["close-rate", "cut-costs"]
    assert result["role"]["seats"] == ["revenue-lead", "cfo"], "primary first"
    assert result["role"]["id"] == "revenue-lead"


@pytest.mark.asyncio
async def test_x_role_is_neither_required_nor_read(store, running, seat):
    seat["value"] = {"case": "serves", "role_id": "revenue-lead", "seats": ["revenue-lead"]}
    lying = "x-role:\n  role: marketing-lead\nx-canon:\n  clone_path: canon\n"
    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead"),
                                      _objective("brand", "marketing-lead"), template=lying))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)
    assert _owned(result) == ["close-rate"], "the template's x-role is not read"

    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead")))
    assert _owned(await svc.read_objective_join(AGENT, now=NOW, client=client)) == ["close-rate"]


@pytest.mark.asyncio
async def test_the_canon_declaration_alone_enables_the_join(store, running, seat):
    seat["value"] = {"case": "serves", "role_id": "revenue-lead", "seats": ["revenue-lead"]}
    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead"),
                                      template="x-role:\n  role: revenue-lead\n"))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["objectives"] == []
    assert "no `x-canon`" in result["message"]
    assert result["source"]["objectives_dir"] == "skipped", "no canon, no file read"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"case": "none", "role_id": None, "seats": []},
    None,
], ids=["provider-says-none", "provider-answers-nothing"])
async def test_no_seat_owns_nothing_and_says_why(store, running, seat, answer):
    seat["value"] = answer
    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead"),
                                      _objective("helping", "cfo", supporting=AGENT)))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert _owned(result) == []
    assert [o["id"] for o in result["objectives"]] == ["helping"], "supporting work still shows"
    assert result["objectives"][0]["supporting"] is True
    codes = [f["code"] for f in result["findings"]]
    assert "no_seat" in codes
    assert result["role"]["case"] == "none" and result["role"]["seats"] == []


@pytest.mark.asyncio
async def test_with_no_provider_at_all_the_answer_is_no_seat(store, running):
    from services import assignment_provider as ap

    ap.clear_provider()
    client = _FakeClient(files=_files(_objective("close-rate", "revenue-lead")))
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["objectives"] == []
    assert "no_seat" in [f["code"] for f in result["findings"]]
    assert "Access tab" in next(f["message"] for f in result["findings"] if f["code"] == "no_seat")


@pytest.mark.parametrize("given,expected", [
    (None, ()),
    ("cfo", ("cfo",)),
    (["cfo", "cfo", "head-of-ops"], ("cfo", "head-of-ops")),
    (["cfo", "not valid", None, ""], ("cfo",)),
])
def test_owned_seat_ids_normalises_one_or_many(given, expected):
    assert svc.owned_seat_ids(given) == expected


def test_supporting_agents_is_still_read_by_the_pure_join():
    obj = {"owner": "role:someone", "supporting_agents": [AGENT], "status": "active"}
    assert svc.objective_concerns(obj, (), AGENT) is True
    assert svc.objective_concerns(obj, ("cfo",), "other-agent") is False
