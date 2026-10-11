"""trinity-enterprise#811 — the seam's trigger-free seat read.

`assignment_provider.resolve_seat(agent)` answers which seat an agent HOLDS
itself or SERVES through its primary, asked with no triggering user (the
readiness gate, the role card and the objective join ask on their own). It is
always a dict and never raises: every degraded case — no provider, a provider
without `seat_for`, no answer, a raise, a malformed shape — reads as "no seat",
which is the failure direction the readiness gate needs (ent#813).
"""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def seam():
    from services import assignment_provider as ap
    ap.clear_provider()
    yield ap
    ap.clear_provider()


class _Provider:
    def __init__(self, answer=None, raises=False):
        self.answer, self.raises, self.asked = answer, raises, []

    def assignment_for(self, agent_name, triggered_by):
        return None

    def seat_for(self, agent_name):
        self.asked.append(agent_name)
        if self.raises:
            raise RuntimeError("boom")
        return self.answer


def _none(reason):
    return {"case": "none", "role_id": None, "seats": [], "reason": reason}


def test_no_provider_is_no_seat(seam):
    assert seam.resolve_seat("scout") == _none("no_provider")


def test_no_agent_is_no_seat(seam):
    seam.register_provider(_Provider({"case": "holds", "role_id": "x", "seats": []}))
    assert seam.resolve_seat("") == _none("no_agent")
    assert seam.resolve_seat(None) == _none("no_agent")


def test_a_provider_without_seat_for_is_no_seat(seam):
    class Old:
        def assignment_for(self, agent_name, triggered_by):
            return None
    seam.register_provider(Old())
    assert seam.resolve_seat("scout") == _none("unsupported")


@pytest.mark.parametrize("answer,expected", [
    ({"case": "holds", "role_id": "orchestrator", "seats": []},
     {"case": "holds", "role_id": "orchestrator", "seats": [], "reason": "provider"}),
    ({"case": "serves", "role_id": "cfo", "seats": ["cfo", "head-of-ops"]},
     {"case": "serves", "role_id": "cfo", "seats": ["cfo", "head-of-ops"], "reason": "provider"}),
    ({"case": "none", "role_id": None, "seats": ["viewer-seat"]},
     {"case": "none", "role_id": None, "seats": ["viewer-seat"], "reason": "provider"}),
], ids=["holds", "serves", "none-with-people"])
def test_a_provider_answer_passes_through(seam, answer, expected):
    p = _Provider(answer)
    seam.register_provider(p)
    assert seam.resolve_seat("scout") == expected
    assert p.asked == ["scout"], "asked with the agent alone — no trigger"


def test_a_raise_is_no_seat(seam):
    seam.register_provider(_Provider(raises=True))
    assert seam.resolve_seat("scout") == _none("error")


def test_no_answer_is_no_seat(seam):
    seam.register_provider(_Provider(None))
    assert seam.resolve_seat("scout") == _none("no_answer")


@pytest.mark.parametrize("answer", [
    "holds",
    {"case": "owns", "role_id": "x", "seats": []},
    {"case": "holds", "role_id": None, "seats": []},
    {"case": "serves", "role_id": "", "seats": []},
    {"case": "holds", "role_id": 7, "seats": []},
    {"case": "holds", "role_id": "x", "seats": "x"},
    {"case": "holds", "role_id": "x", "seats": ["x", 3]},
    {"case": "holds", "role_id": "x"},
], ids=["str", "unknown-case", "holds-without-seat", "serves-empty-seat", "int-role",
        "seats-str", "seats-mixed", "seats-missing"])
def test_a_malformed_answer_is_no_seat(seam, answer):
    seam.register_provider(_Provider(answer))
    assert seam.resolve_seat("scout") == _none("malformed")


def test_case_none_never_carries_a_seat(seam):
    seam.register_provider(_Provider({"case": "none", "role_id": "stray", "seats": []}))
    assert seam.resolve_seat("scout")["role_id"] is None
