"""
Gated skills, the in-container hook — which names a call loads
(trinity-enterprise#752).

Target: the hook module's resolver (``docker/base-image/hooks/_skill_gate.py``:
``resolve_skill_names``, ``parse_skills_field``, ``find_subagent``), imported
in-process (import is side-effect free; the environment is cleared only inside
``main``).

The hook sends every name the invoked skill answers to, and the platform
matches the gate keys against them. A gate is keyed by whatever name its author
used — the directory, or the front-matter ``name:`` — and #751's own resolver
(``_FINGERPRINT_SCRIPT``) defines which keys reach a skill. So the property
pinned here is SUPERSET parity: for every key #751 resolves to a skill, and for
every name the CLI would invoke that skill by, the hook's names contain the key.
Over-matching only adds refusals; under-matching is a bypass.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent752_hook_harness as H  # noqa: E402
from services.skill_gate_service import _FINGERPRINT_SCRIPT  # noqa: E402

pytestmark = pytest.mark.unit

g = H.load_module()


def _skill(home: Path, dirname: str, text: str) -> None:
    d = home / ".claude" / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def _names(home: Path, invoked: str, cwd: Path = None):
    names, complete = g.resolve_skill_names(invoked, g.skill_roots(str(home), str(cwd or home)))
    assert complete
    return {n.casefold() for n in names}


def _fingerprints(home: Path, names):
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _FINGERPRINT_SCRIPT,
         json.dumps({"names": list(names), "home": str(home / ".claude")})],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


# A tree covering every way #751 and the CLI can disagree about a skill's name.
# Each entry: directory → (SKILL.md text, the names the CLI invokes it by).
_TREE = {
    "alpha": ("# no front matter\n", ["alpha"]),
    "beta-dir": ("---\nname: beta\n---\nbody\n", ["beta-dir", "beta"]),
    "gamma-dir": ('---\nname: "gamma"\n---\nbody\n', ["gamma-dir", "gamma"]),
    "delta-dir": ("---\nName: delta\n---\nbody\n", ["delta-dir"]),               # YAML keys are case-sensitive
    "eps-dir": ("\ufeff---\nname: eps\n---\nbody\n", ["eps-dir", "eps"]),        # a BOM: #751 sees no front matter
    "zeta-dir": ("---\nname: zeta  # the zeta skill\n---\nbody\n", ["zeta-dir", "zeta"]),
    "theta-dir": ("---\ndescription: " + "x" * 5000 + "\nname: theta\n---\nbody\n",
                  ["theta-dir", "theta"]),                                        # past #751's 4 KiB head
    "iota-dir": ("---\nname: 'iota'\n---\nbody\n", ["iota-dir", "iota"]),
}


@pytest.fixture
def tree(tmp_path):
    for dirname, (text, _aliases) in _TREE.items():
        _skill(tmp_path, dirname, text)
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "report.md").write_text("---\ndescription: a command\n---\nReport.\n")
    return tmp_path


def test_every_key_751_resolves_is_sent_whichever_name_the_skill_is_invoked_by(tree):
    keys = sorted({k for d, (_t, aliases) in _TREE.items() for k in [d, *aliases]}
                  | {"delta", "zeta  # the zeta skill", "report", "nope"})
    by_dir = {d: _fingerprints(tree, [d])[d].get("fingerprint") for d in _TREE}
    resolved = _fingerprints(tree, keys)
    checked = 0
    for key, entry in resolved.items():
        fp = entry.get("fingerprint")
        if not fp:
            continue
        if entry.get("kind") == "command":
            target, aliases = "report", ["report"]
        else:
            [target] = [d for d, f in by_dir.items() if f == fp]
            aliases = _TREE[target][1]
        for alias in aliases:
            assert key.casefold() in _names(tree, alias), (key, target, alias)
            checked += 1
    assert checked >= 15, "the parity table must exercise real resolutions"


def test_a_directory_name_and_its_front_matter_name_reach_each_other(tree):
    assert {"beta", "beta-dir"} <= _names(tree, "beta")
    assert {"beta", "beta-dir"} <= _names(tree, "beta-dir")


@pytest.mark.parametrize("dirname, expected", [
    ("gamma-dir", "gamma"),              # double-quoted
    ("iota-dir", "iota"),                # single-quoted
    ("zeta-dir", "zeta"),                # a trailing comment
    ("eps-dir", "eps"),                  # a leading BOM
    ("theta-dir", "theta"),              # beyond 4 KiB of front matter
])
def test_the_front_matter_name_is_read_the_way_yaml_reads_it(tree, dirname, expected):
    assert expected in _names(tree, dirname)


def test_751s_case_insensitive_key_is_honoured_too(tree):
    """`Name:` is not a YAML `name` key, so the CLI never answers to it — but
    #751 reads it, so a gate keyed `delta` reaches this skill and must match."""
    assert "delta" in _names(tree, "delta-dir")


def test_a_namespaced_call_is_also_asked_about_by_its_last_segment(tree):
    assert {"acme:beta", "beta", "beta-dir"} <= _names(tree, "acme:beta")
    assert {"apps/web:deploy", "deploy"} <= _names(tree, "apps/web:deploy")


def test_a_name_with_no_skill_on_disk_is_still_asked_about(tree):
    """A plugin or bundled skill, or one that does not exist: the platform
    still decides whether that NAME is gated."""
    assert _names(tree, "verify") == {"verify"}


def test_matching_is_case_insensitive(tree):
    assert "beta-dir" in _names(tree, "BETA")


def test_a_project_skills_directory_is_searched_too(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".claude" / "skills").mkdir(parents=True)
    d = repo / ".claude" / "skills" / "pay"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pay-invoice\n---\n")
    assert "pay" in _names(home, "pay-invoice", cwd=repo)


def test_a_symlinked_skill_directory_is_resolved_by_its_target_name(tmp_path):
    _skill(tmp_path, "pay-invoice", "# real\n")
    (tmp_path / ".claude" / "skills" / "pay").symlink_to(tmp_path / ".claude" / "skills" / "pay-invoice")
    assert "pay-invoice" in _names(tmp_path, "pay")


def test_the_scan_is_bounded_and_says_so(tmp_path):
    root = tmp_path / ".claude" / "skills"
    root.mkdir(parents=True)
    for i in range(g.MAX_ENTRIES + 1):
        (root / f"s{i:05d}").mkdir()
    names, complete = g.resolve_skill_names("x", g.skill_roots(str(tmp_path), str(tmp_path)))
    assert complete is False
    assert "x" in names


# ---------------------------------------------------------------------------
# A subagent definition's `skills:` — the forms YAML allows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("front, expected", [
    ("name: f\nskills:\n  - a\n  - b", ["a", "b"]),
    ("name: f\nskills:\n- a\n- 'b'", ["a", "b"]),
    ("name: f\nskills: [a, \"b\"]", ["a", "b"]),
    ("name: f\nskills: a, b", ["a", "b"]),
    ("name: f\nskills: a", ["a"]),
    ("name: f\nskills: []", []),
    ("name: f\nskills:", []),
    ("name: f\ndescription: none", []),
    ("name: f\nskills:\n  - a  # first\n  - b", ["a", "b"]),
])
def test_the_skills_field_forms(front, expected):
    assert g.parse_skills_field(front) == expected


@pytest.mark.parametrize("front", [
    "name: f\nskills: {a: 1}",
    "name: f\nskills: [a, b",
    "name: f\nskills:\n  a: 1",
    "name: f\nskills: |\n  a",
])
def test_an_unparseable_skills_field_is_none_never_empty(front):
    assert g.parse_skills_field(front) is None


def test_a_subagent_is_found_by_its_front_matter_name_in_any_subfolder(tmp_path):
    p = tmp_path / ".claude" / "agents" / "team" / "money.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: finance\nskills: [pay-invoice]\n---\nbody\n")
    found = g.find_subagent("finance", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved, found.skills) == (True, True, ["pay-invoice"])


def test_two_definitions_answering_to_one_name_preload_the_union(tmp_path):
    for rel, skills in (("a.md", "[x]"), ("deep/b.md", "[y]")):
        p = tmp_path / ".claude" / "agents" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nname: finance\nskills: {skills}\n---\n")
    found = g.find_subagent("finance", str(tmp_path), str(tmp_path))
    assert sorted(found.skills) == ["x", "y"]


def test_a_built_in_subagent_is_not_found_and_preloads_nothing(tmp_path):
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    found = g.find_subagent("Explore", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved, found.skills) == (False, True, [])


def test_a_missing_plugin_subagent_is_unresolved(tmp_path):
    found = g.find_subagent("acme:finance", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved) == (False, False)


# ---------------------------------------------------------------------------
# What cannot be read is "could not tell", never "not gated" (review findings 1 & 7)
# ---------------------------------------------------------------------------

def _resolve(home: Path, invoked: str, config_dir: str = None):
    return g.resolve_skill_names(invoked, g.skill_roots(str(home), str(home), config_dir))


def test_a_block_scalar_name_is_read(tmp_path):
    _skill(tmp_path, "pay", "---\nname: >-\n  pay-invoice\n---\nbody\n")
    assert "pay" in _names(tmp_path, "pay-invoice")


@pytest.mark.parametrize("text", [
    pytest.param("---\nname: [pay-invoice]\n---\nbody\n", id="a-flow-name"),
    pytest.param("---\nname: pay-invoice\ndescription: x\n", id="front-matter-never-closed"),
    pytest.param("---\ndescription: " + "x" * (300 * 1024) + "\nname: pay-invoice\n---\n",
                 id="front-matter-past-the-bound"),
])
def test_a_skill_whose_name_cannot_be_read_leaves_an_unmatched_call_unresolved(tmp_path, text):
    """The CLI may resolve `pay-invoice` to that directory; this cannot tell,
    so it says so (refused on a gated agent only)."""
    _skill(tmp_path, "pay", text)
    _names_out, complete = _resolve(tmp_path, "pay-invoice")
    assert complete is False


def test_a_call_by_a_directory_name_is_resolved_whatever_another_skill_holds(tmp_path):
    _skill(tmp_path, "pay", "---\nname: [weird]\n---\n")
    _skill(tmp_path, "report", "# no front matter\n")
    names, complete = _resolve(tmp_path, "report")
    assert complete is True and "report" in names


def test_an_empty_or_null_name_is_no_name(tmp_path):
    _skill(tmp_path, "pay", "---\nname:\n---\n")
    _skill(tmp_path, "refund", "---\nname: ~\n---\n")
    assert _resolve(tmp_path, "something-else")[1] is True


def test_a_config_dir_moved_by_dot_env_is_searched_too(tmp_path):
    home, config = tmp_path / "home", tmp_path / "cfg"
    (home / ".claude" / "skills").mkdir(parents=True)
    d = config / "skills" / "pay"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pay-invoice\n---\n")
    names, complete = _resolve(home, "pay-invoice", str(config))
    assert complete and "pay" in names


def _definition(home: Path, rel: str, text: str) -> None:
    p = home / ".claude" / "agents" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_a_definition_with_a_long_description_is_still_read(tmp_path):
    _definition(tmp_path, "finance.md",
                "---\nname: finance\ndescription: " + "x" * (70 * 1024) + "\nskills: [pay-invoice]\n---\n")
    found = g.find_subagent("finance", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved, found.skills) == (True, True, ["pay-invoice"])


def test_a_folded_definition_name_is_found(tmp_path):
    _definition(tmp_path, "fin-agent.md", "---\nname: >-\n  finance\nskills: [pay-invoice]\n---\n")
    found = g.find_subagent("finance", str(tmp_path), str(tmp_path))
    assert (found.found, found.skills) == (True, ["pay-invoice"])


def test_a_definition_that_never_closes_its_front_matter_is_unresolved(tmp_path):
    _definition(tmp_path, "finance.md", "---\nname: finance\nskills: [pay-invoice]\n")
    found = g.find_subagent("finance", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved) == (True, False)


def test_a_definition_whose_name_cannot_be_read_may_be_the_one_asked_for(tmp_path):
    _definition(tmp_path, "x.md", "---\nname: [finance]\nskills: [pay-invoice]\n---\n")
    found = g.find_subagent("Explore", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved) == (False, False)


def test_a_file_with_no_front_matter_is_not_a_definition(tmp_path):
    _definition(tmp_path, "notes.md", "# just notes\n")
    found = g.find_subagent("Explore", str(tmp_path), str(tmp_path))
    assert (found.found, found.resolved) == (False, True)


def test_a_config_dir_moved_by_dot_env_holds_definitions_too(tmp_path):
    home, config = tmp_path / "home", tmp_path / "cfg"
    (home / ".claude" / "agents").mkdir(parents=True)
    p = config / "agents" / "finance.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: finance\nskills: [pay-invoice]\n---\n")
    found = g.find_subagent("finance", str(home), str(home), str(config))
    assert (found.found, found.skills) == (True, ["pay-invoice"])
