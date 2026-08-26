"""#2350 — the Journey Impact gate decides correctly (Gate G1).

The declaration is cheap; the GATE is the load-bearing part. An intake field on
its own would be `none:` for everything inside a month and would read as
compliance — so the burden sits on the `new:` path, where declaring a new
promise obliges the same PR to carry a skeleton for it. That is what stops
`none:` being the free option.

The decision therefore lives in a pure function and is tested here, not
discovered in a CI log. A rule that only runs in CI is a rule nobody can check.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/ci"))

from journey_impact import (  # noqa: E402
    decide, looks_like_skeleton, parse_declaration,
)

pytestmark = pytest.mark.unit

SKELETON = ("tests/journeys/test_j11_journey.py", """
import pytest

@pytest.mark.xfail(strict=True, reason="J11 not built yet")
def test_j11_promise():
    assert False
""")


# --------------------------------------------------------------- parsing

@pytest.mark.parametrize("body,kind,journey", [
    ("Journey Impact: new: J11", "new", "J11"),
    ("Journey Impact: extends: J03", "extends", "J03"),
    ("**Journey Impact**: new: J07", "new", "J07"),
    ("- Journey Impact - extends: J10", "extends", "J10"),
    ("journey impact: NEW: J01", "new", "J01"),
])
def test_the_declaration_is_read_in_the_shapes_people_actually_write(body, kind, journey):
    d = parse_declaration(body)
    assert (d.kind, d.journey, d.error) == (kind, journey, None)


def test_a_reason_is_required_for_none():
    """AC 4, and the whole anti-gaming argument. A bare `none` is
    indistinguishable from not having thought about it."""
    bad = parse_declaration("Journey Impact: none")
    assert bad.error and "needs a reason" in bad.error
    good = parse_declaration("Journey Impact: none: pure refactor, no behaviour change")
    assert good.kind == "none" and good.reason.startswith("pure refactor")


def test_new_and_extends_need_a_real_journey_id():
    assert parse_declaration("Journey Impact: new: the login one").error
    assert parse_declaration("Journey Impact: extends:").error
    assert parse_declaration("Journey Impact: new: J3").error       # J03, not J3


def test_an_absent_declaration_is_absent_not_malformed():
    """The two must not be conflated: absence is tolerated today, malformed
    never is."""
    d = parse_declaration("## Description\n\nSomething unrelated.")
    assert d.kind is None and d.error is None


def test_an_unfilled_template_placeholder_is_malformed_not_absent():
    """The template ships `Journey Impact: ` with nothing after it. Reading
    that as "absent" would make the field decorative on the exact PRs that
    used the template."""
    assert parse_declaration("Journey Impact: ").error
    assert parse_declaration("Journey Impact: <!-- pick one -->").error


def test_an_edited_body_keeps_the_correction_not_the_draft():
    body = "Journey Impact: none\n\n...later...\n\nJourney Impact: new: J05"
    d = parse_declaration(body)
    assert (d.kind, d.journey) == ("new", "J05")


# --------------------------------------------------------------- skeleton

def test_the_skeleton_must_name_the_journey_it_stands_in_for():
    """Without the id binding, `new: J11` is satisfied by ANY edit in the tier
    — including an unrelated tweak to an existing journey — which hands the
    obligation straight back to the compliance theatre the gate exists to
    prevent."""
    assert looks_like_skeleton(*SKELETON, "J11")
    assert not looks_like_skeleton(*SKELETON, "J07")
    # Naming it in the PATH is enough; the body need not repeat it.
    assert looks_like_skeleton(
        "tests/journeys/test_j07_journey.py",
        "import pytest\n@pytest.mark.xfail(strict=True, reason='later')\ndef test_x(): ...\n",
        "J07",
    )


def test_the_id_match_is_a_whole_token_not_a_substring():
    """J1 must not satisfy J11, and J11 must not satisfy J1 — ids that differ
    only by a digit are exactly the ones a typo produces."""
    for content in ("def test_x(): pass  # J110", "def test_x(): pass  # XJ11"):
        assert not looks_like_skeleton("tests/journeys/test_x_journey.py",
                                       content, "J11"), content
    # but the underscore-delimited filename convention DOES match
    assert looks_like_skeleton("tests/journeys/test_j11_journey.py",
                               "def test_x(): pass", "J11")


def test_a_journey_test_that_names_the_wrong_promise_gets_its_own_message():
    """The confusing case deserves better than "none found": they DID add a
    journey test, it just does not name what they declared."""
    other = ("tests/journeys/test_j07_journey.py",
             "import pytest\n\n\ndef test_j07_promise():\n    assert True\n")
    v = decide(pr_declaration=parse_declaration("Journey Impact: new: J11"),
               changed_files=[other])
    assert not v.ok
    joined = "\n".join(v.lines)
    assert "does touch the journey tier" in joined and "test_j07_journey.py" in joined


def test_a_strict_xfail_in_the_tier_is_a_sufficient_skeleton():
    """AC 3. `strict=True` is what makes it a claim rather than a shrug: it
    fails the build the day the behaviour starts working, which is exactly
    when the skeleton should stop being one."""
    assert looks_like_skeleton(*SKELETON)


def test_a_non_strict_xfail_is_not_a_skeleton():
    assert not looks_like_skeleton(
        "tests/journeys/test_j11_journey.py",
        "import pytest\n@pytest.mark.xfail(reason='later')\ndef nope(): ...\n",
    )


def test_a_test_outside_the_tier_is_not_a_skeleton():
    """A unit test that mentions a journey is not a journey harness — the tier
    is where a live-stack promise can actually be asserted (#2335)."""
    assert not looks_like_skeleton("tests/unit/test_j11.py", SKELETON[1])


def test_the_catalog_and_scaffolding_do_not_count_as_a_skeleton():
    """Declaring the promise in the catalog is #2338's job and is not the same
    as asserting it — otherwise every `new:` would be satisfied by one YAML
    line, which is the gaming this gate exists to prevent."""
    assert not looks_like_skeleton("tests/journeys/catalog.yaml", "journeys: []")
    assert not looks_like_skeleton("tests/journeys/conftest.py", "def test_x(): ...")


# --------------------------------------------------------------- decision

def test_new_without_a_skeleton_is_rejected():
    """AC 2 — the load-bearing case."""
    v = decide(pr_declaration=parse_declaration("Journey Impact: new: J11"),
               changed_files=[("src/backend/main.py", "x = 1")])
    assert not v.ok
    joined = "\n".join(v.lines)
    assert "J11" in joined and "skeleton" in joined


def test_new_with_a_skeleton_passes():
    v = decide(pr_declaration=parse_declaration("Journey Impact: new: J11"),
               changed_files=[SKELETON, ("src/backend/main.py", "x = 1")])
    assert v.ok, v.lines


def test_extends_owes_no_skeleton():
    """Extending an existing promise is covered by that journey's harness; the
    obligation is specific to MINTING one."""
    v = decide(pr_declaration=parse_declaration("Journey Impact: extends: J03"),
               changed_files=[("src/backend/main.py", "x = 1")])
    assert v.ok


def test_an_epic_declaring_new_binds_the_prs_under_it():
    """AC 2's actual wording: the epic declares, the PR must carry."""
    v = decide(
        pr_declaration=parse_declaration("Journey Impact: none: implementation detail"),
        epic_declarations=[("owner/repo#1850", parse_declaration("Journey Impact: new: J11"))],
        changed_files=[("src/backend/main.py", "x = 1")],
    )
    assert not v.ok
    assert "epic owner/repo#1850" in "\n".join(v.lines)


def test_a_malformed_epic_declaration_is_rejected_too():
    v = decide(pr_declaration=parse_declaration("Journey Impact: none: fine"),
               epic_declarations=[("o/r#1", parse_declaration("Journey Impact: none"))])
    assert not v.ok


def test_an_absent_declaration_passes_but_says_so():
    """Deliberate: adopting the field must not red-X every in-flight PR and
    dependabot bump on day one. The gap is reported rather than enforced, and
    tightening it is a follow-up once the templates have been in use."""
    v = decide(pr_declaration=parse_declaration("no field here"))
    assert v.ok
    assert "no Journey Impact" in "\n".join(v.lines)


def test_every_verdict_says_what_it_read_and_why():
    """AC 5. A gate that fails without naming the declaration it acted on is a
    gate people learn to re-run rather than read."""
    for body, files in (
        ("Journey Impact: new: J11", []),
        ("Journey Impact: new: J11", [SKELETON]),
        ("Journey Impact: none", []),
        ("Journey Impact: none: refactor", []),
        ("nothing", []),
    ):
        v = decide(pr_declaration=parse_declaration(body), changed_files=files)
        joined = "\n".join(v.lines)
        assert joined.strip(), body
        assert ("PASSED" in joined) or ("REJECTED" in joined), body


# --------------------------------------------------------------- wiring

def test_the_templates_carry_the_field():
    """AC 1."""
    for rel in (".github/pull_request_template.md", ".github/ISSUE_TEMPLATE/epic.md"):
        body = (REPO / rel).read_text()
        assert "Journey Impact" in body, rel
        assert "none: <why this touches no promise>" in body, (
            f"{rel} must show that `none` needs a REASON — that is the field's "
            f"whole anti-gaming property"
        )


def test_the_workflow_never_interpolates_the_pr_body_into_a_shell():
    """A PR body is attacker-authored. `${{ }}` inside a `run:` is a command
    injection sink; the body must arrive through `env:`."""
    wf = (REPO / ".github/workflows/journey-impact.yml").read_text()
    assert "PR_BODY: ${{ github.event.pull_request.body }}" in wf
    run_bodies = wf.split("run: ")[1:]
    for chunk in run_bodies:
        assert "github.event.pull_request.body" not in chunk
