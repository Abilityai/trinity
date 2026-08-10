"""The four `owner/repo` gates agree — and where they don't, on purpose (ent#14).

There are now FOUR copies of this pattern:

  1. `routers/settings.py::_REPO_PATTERN`            — admin-entered TMPL-001 list
  2. `agent_service/crud.py::_GITHUB_REPO_PATH_RE`   — the create path (ent#123)
  3. `Settings.vue::REPO_PATTERN`                    — the admin form
  4. `template_registry_service::_is_valid_repo`     — the remote registry (new)

Duplicating rather than importing is this codebase's documented convention for a
shared gate that must not create an import edge — a service importing a router
violates Invariant #1, and `_LOCAL_TEMPLATE_NAME_RE` sets the precedent. The
convention comes with an obligation, and this file is it.

It compares BEHAVIOUR ON A CORPUS, not source strings (the
`test_1759_template_root_parity.py` shape): copies 1 and 3 already differ from
copy 2 in character-class ORDERING (`[a-zA-Z0-9._-]` vs `[A-Za-z0-9_.-]`) while
denoting the same set, so a textual diff would report a difference that does not
exist and miss one that does.

THE DELIBERATE DIVERGENCE, pinned rather than left to drift: `.` is inside the
character class, so `../evil` MATCHES all three older copies. Copy 4 refuses dot
segments explicitly, because it is the only one guarding a document fetched from
the network — and because a matching `../evil` would render "../evil" on a
catalog card while `api.github.com/repos/../evil/...` and
`github.com/../evil.git` both normalize to a DIFFERENT repo, i.e. the card
advertising one path and cloning another.

The containment that must hold in every direction: everything the registry gate
ACCEPTS, the other three accept. It is strictly stricter, never differently
strict.

Sync throughout (`tests/unit/pytest.ini` overrides `asyncio_mode = auto`).
"""
import re
from pathlib import Path

import pytest

from routers.settings import _REPO_PATTERN as ROUTER_RE
from services.agent_service.crud import _GITHUB_REPO_PATH_RE as CRUD_RE
from services.template_registry_service import _is_valid_repo as registry_accepts

_SETTINGS_VUE = (
    Path(__file__).resolve().parents[2]
    / "src" / "frontend" / "src" / "views" / "Settings.vue"
)


def _vue_pattern():
    """The Vue copy, read from source so a silent edit there fails CI here."""
    source = _SETTINGS_VUE.read_text()
    match = re.search(r"const REPO_PATTERN = /\^(.+?)\$/", source)
    assert match, "Settings.vue no longer declares REPO_PATTERN in the expected form"
    # JS -> Python: the only difference in this literal is the escaped slash.
    return re.compile("^" + match.group(1).replace("\\/", "/") + "$")


VUE_RE = _vue_pattern()

#: Every case is a real shape somebody types or a real shape somebody attacks
#: with — not random strings.
CORPUS = [
    # --- plainly valid ---
    "Abilityai/cornelius",
    "abilityai/trinity",
    "acme/my-agent",
    "acme/my_agent",
    "acme/agent.v2",
    "A/b",
    "0/9",
    "my-org.inc/repo-name_2",
    "UPPER/lower",
    # --- plainly invalid ---
    "",
    "noslash",
    "a/b/c",
    "/leading",
    "trailing/",
    "a b/c",
    "a/b c",
    "a/b@main",
    "a/b?x=1",
    "a/b#frag",
    "https://github.com/a/b",
    "a//b",
    "a/b\nc",
    "a/b\tc",
    "a/b\x00c",
    "a/b%2fc",
    "a/b:c",
    "~/b",
    "$a/b",
    "a/b|c",
    "*/b",
]

#: Accepted by the three older copies, refused by the registry gate. This IS the
#: divergence — asserted, so it cannot drift back silently in either direction.
DOT_SEGMENTS = ["../evil", "a/..", "./a", "a/.", "..", "../..", "./."]


@pytest.mark.parametrize("value", CORPUS)
def test_all_four_gates_agree_on_the_corpus(value):
    verdicts = {
        "routers/settings.py": bool(ROUTER_RE.match(value)),
        "agent_service/crud.py": bool(CRUD_RE.match(value)),
        "Settings.vue": bool(VUE_RE.match(value)),
        "template_registry_service": registry_accepts(value),
    }
    assert len(set(verdicts.values())) == 1, f"gates disagree on {value!r}: {verdicts}"


@pytest.mark.parametrize("value", DOT_SEGMENTS)
def test_the_registry_gate_is_stricter_on_dot_segments(value):
    assert registry_accepts(value) is False, f"the registry must refuse {value!r}"


@pytest.mark.parametrize("value", DOT_SEGMENTS)
def test_the_other_three_still_share_the_hole(value):
    """Documented, not fixed here.

    This is a pre-existing weakness on paths reachable only by an authenticated
    `creator` typing an id by hand — a different threat model from a remote
    document. It is asserted so that (a) the divergence is deliberate rather
    than accidental, and (b) if somebody DOES tighten those copies, this test
    fails and points them at deleting this assertion rather than leaving a stale
    comment behind.
    """
    if value in ("..", "./.", "./a", "a/."):
        # These fail the shared pattern for ordinary reasons (no slash, or a
        # segment that is not in the character class) — not the hole.
        return
    assert ROUTER_RE.match(value), f"{value!r} no longer matches the router copy"
    assert CRUD_RE.match(value), f"{value!r} no longer matches the crud copy"
    assert VUE_RE.match(value), f"{value!r} no longer matches the Vue copy"


@pytest.mark.parametrize("value", CORPUS + DOT_SEGMENTS)
def test_registry_acceptance_is_a_SUBSET_of_every_other_gate(value):
    """The containment that actually matters: the registry can never admit a
    repo path the create path would then refuse (or, worse, interpret
    differently). Strictly stricter, never differently strict."""
    if registry_accepts(value):
        assert ROUTER_RE.match(value), value
        assert CRUD_RE.match(value), value
        assert VUE_RE.match(value), value


def test_the_vue_copy_is_still_where_this_test_thinks_it_is():
    """A guard that reads a file by regex fails OPEN if the file moves or the
    declaration is reformatted — so assert the anchor, not just the match."""
    assert _SETTINGS_VUE.exists()
    assert "const REPO_PATTERN = /^" in _SETTINGS_VUE.read_text()
