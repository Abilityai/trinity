"""The agent server's file verbs never act through a link that changes what a
path is (abilityai/trinity-enterprise#820).

The backend decides the tier on the path the caller NAMED. A link the agent
made (on request, in chat) could make that name reach another class:
`workspace/x/CLAUDE.md` with `x -> ../.claude`. PUT, mkdir, DELETE and the list
root resolve once, refuse a resolved path whose class set differs from the
named one, then walk without following links. A link that stays in the same
class keeps working, so templates that link inside the home are unaffected.

Also pinned here: resolved-path containment on list and delete (#3032), the
list never descends a link and stops at a node cap, DELETE of a link removes
the link only, the compare-and-swap read does not follow a link, a crashed
tmp file does not block the next write, a file keeps its mode, the read
routes report the link count, and every platform writer still succeeds.

Loads the SHIPPED router by path (it must stay standalone-importable, #1795)
and drives it through a real FastAPI app over a temporary home.
"""
import hashlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER = _REPO_ROOT / "docker/base-image/agent_server/routers/files.py"


def _load():
    spec = importlib.util.spec_from_file_location("_test820_agent_files", str(_ROUTER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary home with instructions, a workspace, a credential, and three
    links: `x -> .claude` (changes class), `docs -> workspace/docs` (same
    class), `envlink -> .env` (a final-component link)."""
    mod = _load()
    base = (tmp_path / "home").resolve()
    (base / ".claude/skills").mkdir(parents=True)
    (base / ".claude/CLAUDE.md").write_text("instructions")
    (base / "CLAUDE.md").write_text("top instructions")
    (base / "workspace/docs").mkdir(parents=True)
    (base / "workspace/docs/a.md").write_text("a")
    (base / ".env").write_text("SECRET=1")
    (base / "x").symlink_to(base / ".claude")
    (base / "docs").symlink_to(base / "workspace/docs")
    (base / "envlink").symlink_to(base / ".env")
    monkeypatch.setattr(mod, "_HOME", base)
    app = FastAPI()
    app.include_router(mod.router)
    return mod, base, TestClient(app, raise_server_exceptions=True)


def _put(client, path, content="new", **params):
    return client.put("/api/files", params={"path": path, **params}, json={"content": content})


def _is_link_refusal(r):
    return r.status_code == 403 and r.json()["detail"]["code"] == "resolved_path_mismatch"


# ---- a link that changes the class is refused on every verb ------------------

def test_a_write_through_a_link_into_the_instructions_is_refused(home):
    _mod, base, client = home
    r = _put(client, "x/CLAUDE.md")
    assert _is_link_refusal(r), r.text
    assert (base / ".claude/CLAUDE.md").read_text() == "instructions"


def test_mkdir_through_a_link_into_the_skills_dir_is_refused(home):
    _mod, base, client = home
    r = client.post("/api/files/mkdir", params={"path": "x/skills/evil"})
    assert _is_link_refusal(r), r.text
    assert not (base / ".claude/skills/evil").exists()


def test_delete_through_a_link_into_the_instructions_is_refused(home):
    _mod, base, client = home
    r = client.delete("/api/files", params={"path": "x/skills"})
    assert _is_link_refusal(r), r.text
    assert (base / ".claude/skills").is_dir()


def test_listing_a_link_root_into_a_hidden_dir_is_refused(home):
    _mod, _base, client = home
    r = client.get("/api/files", params={"path": "x"})
    assert _is_link_refusal(r), r.text


@pytest.mark.parametrize("rel", ["l/.ssh", "l/.aws", "l/.config", "l/.local", "l/.trinity", "l/.git"])
def test_a_link_to_the_home_cannot_delete_a_protected_or_startup_dir(home, rel):
    """`ws/l -> ~`: the named `l/.ssh` and the resolved `~/.ssh` are both just
    "hidden", but only the second holds a protected anchor; the verdicts
    differ, so the link is refused (found in review)."""
    _mod, base, client = home
    (base / "l").symlink_to(base)
    target = base / rel.split("/", 1)[1]
    target.mkdir(exist_ok=True)
    (target / "keep").write_text("k")
    r = client.delete("/api/files", params={"path": rel})
    assert r.status_code == 403, (rel, r.text)
    assert (target / "keep").exists()


@pytest.mark.parametrize("rel", ["l/.config/git/config", "l/.bashrc", "l/.ssh/authorized_keys"])
def test_a_link_to_the_home_cannot_write_a_protected_or_startup_file(home, rel):
    _mod, base, client = home
    (base / "l").symlink_to(base)
    r = _put(client, rel, "x")
    assert r.status_code == 403, (rel, r.text)
    assert not (base / rel.split("/", 1)[1]).exists()


def test_a_file_used_as_a_directory_is_a_400(home):
    _mod, _base, client = home
    r = _put(client, "workspace/docs/a.md/inner.md", "x")
    assert r.status_code == 400, r.text


# ---- a link in the same class keeps working ---------------------------------

def test_a_write_through_a_same_class_link_succeeds(home):
    _mod, base, client = home
    r = _put(client, "docs/b.md", "b")
    assert r.status_code == 200, r.text
    assert (base / "workspace/docs/b.md").read_text() == "b"


def test_a_final_component_link_is_never_written_through(home):
    _mod, base, client = home
    (base / "notes").symlink_to(base / "workspace/docs/a.md")
    r = _put(client, "notes", "clobber")
    assert r.status_code == 403, r.text
    assert (base / "workspace/docs/a.md").read_text() == "a"


def test_deleting_a_link_removes_the_link_not_its_target(home):
    _mod, base, client = home
    r = client.delete("/api/files", params={"path": "docs"})
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "link"
    assert not os.path.lexists(base / "docs")
    assert (base / "workspace/docs/a.md").read_text() == "a"


def test_deleting_a_directory_removes_it_and_counts_its_entries(home):
    _mod, base, client = home
    (base / "workspace/docs/sub").mkdir()
    (base / "workspace/docs/sub/c.md").write_text("c")
    r = client.delete("/api/files", params={"path": "workspace/docs"})
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "deleted": "workspace/docs", "type": "directory",
                        "file_count": 3}
    assert not (base / "workspace/docs").exists()


def test_the_directory_removal_resists_symlink_races():
    import shutil
    assert shutil.rmtree.avoids_symlink_attacks is True


# ---- containment (#3032) -----------------------------------------------------

def test_list_and_delete_refuse_a_sibling_that_shares_the_home_prefix(home, tmp_path):
    _mod, base, client = home
    sibling = Path(str(base) + "2")
    sibling.mkdir()
    (sibling / "f").write_text("f")
    for r in (client.get("/api/files", params={"path": str(sibling)}),
              client.delete("/api/files", params={"path": str(sibling / "f")})):
        assert r.status_code == 403, r.text
    assert (sibling / "f").exists()


# ---- the list ------------------------------------------------------------------

def test_the_list_shows_a_link_as_a_link_and_never_descends_it(home):
    _mod, base, client = home
    (base / "workspace/loop").symlink_to(base / "workspace")
    r = client.get("/api/files", params={"path": str(base), "show_hidden": "true"})
    assert r.status_code == 200, r.text
    nodes = {n["path"]: n for n in r.json()["tree"]}
    assert nodes["x"]["type"] == "link" and "children" not in nodes["x"]
    assert nodes["docs"]["type"] == "link"
    loop = [n for n in nodes["workspace"]["children"] if n["name"] == "loop"][0]
    assert loop["type"] == "link" and "children" not in loop


def test_the_list_stops_at_the_node_cap(home, monkeypatch):
    mod, base, client = home
    for i in range(12):
        (base / "workspace" / f"f{i}").write_text("x")
    monkeypatch.setattr(mod, "_MAX_LIST_NODES", 5)
    r = client.get("/api/files", params={"path": str(base)})
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True


def test_an_uncapped_list_is_not_marked_truncated(home):
    _mod, base, client = home
    r = client.get("/api/files", params={"path": str(base)})
    assert r.json()["truncated"] is False


# ---- write edge cases ------------------------------------------------------------

def test_the_compare_and_swap_does_not_hash_a_link_target(home):
    """A final link is refused before the CAS can read through it."""
    _mod, base, client = home
    (base / "q").symlink_to(base / "workspace/docs/a.md")
    sha = hashlib.sha256(b"a").hexdigest()
    r = _put(client, "q", "x", if_match=sha)
    assert r.status_code == 403
    assert (base / "workspace/docs/a.md").read_text() == "a"


def test_a_crashed_tmp_file_does_not_block_the_next_write(home):
    _mod, base, client = home
    (base / "workspace/.n.md.tmp-12345").write_text("left by a crash")
    r = _put(client, "workspace/n.md", "ok")
    assert r.status_code == 200, r.text
    assert (base / "workspace/n.md").read_text() == "ok"
    assert sorted(p.name for p in (base / "workspace").iterdir() if p.name.startswith(".n.md")) == [
        ".n.md.tmp-12345"]


def test_an_edited_script_keeps_its_mode(home):
    _mod, base, client = home
    script = base / "workspace/run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    assert _put(client, "workspace/run.sh", "#!/bin/sh\necho hi\n").status_code == 200
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_put_creates_missing_parents(home):
    _mod, base, client = home
    assert _put(client, "workspace/new/deep/f.md", "f").status_code == 200
    assert (base / "workspace/new/deep/f.md").read_text() == "f"


def test_mkdir_keeps_its_409s(home):
    _mod, base, client = home
    assert client.post("/api/files/mkdir", params={"path": "workspace/docs"}).status_code == 409
    assert client.post("/api/files/mkdir", params={"path": "workspace/docs/a.md"}).status_code == 409
    assert client.post("/api/files/mkdir", params={"path": "workspace/fresh/a"}).status_code == 200
    assert (base / "workspace/fresh/a").is_dir()


def test_mkdir_on_a_link_is_refused(home):
    _mod, _base, client = home
    r = client.post("/api/files/mkdir", params={"path": "docs"})
    assert r.status_code == 403, r.text


# ---- nested git metadata is the platform floor too --------------------------------

@pytest.mark.parametrize("rel", ["workspace/r/.git/config", "workspace/r/.git/hooks/pre-commit",
                                 "workspace/bare.git/hooks/post-update", "workspace/bare.git/config"])
def test_nested_and_bare_git_metadata_is_never_written(home, rel):
    _mod, base, client = home
    r = _put(client, rel, "x")
    assert r.status_code == 403, r.text
    assert not (base / rel).exists()


# ---- the read routes report the link count ------------------------------------------

def test_download_and_preview_report_the_link_count(home):
    _mod, base, client = home
    os.link(base / ".env", base / "workspace/n.txt")
    for route in ("/api/files/download", "/api/files/preview"):
        r = client.get(route, params={"path": "workspace/n.txt"})
        assert r.status_code == 200
        assert r.headers["X-File-Nlink"] == "2"
    r = client.get("/api/files/download", params={"path": "workspace/docs/a.md"})
    assert r.headers["X-File-Nlink"] == "1"


# ---- every platform writer still succeeds (R1) ----------------------------------------

@pytest.mark.parametrize("rel,params", [
    # read_only.py writes the settings file WITHOUT platform=true
    (".claude/settings.local.json", {}),
    # read_only.py / operator_queue_service write under .trinity with platform=true
    (".trinity/read-only-config.json", {"platform": "true"}),
    (".trinity/operator-queue.json", {"platform": "true"}),
    # skill_service writes skills and CLAUDE.md
    (".claude/skills/x/SKILL.md", {}),
    ("CLAUDE.md", {}),
])
def test_the_platform_writers_still_write(home, rel, params):
    _mod, base, client = home
    r = _put(client, rel, "platform", **params)
    assert r.status_code == 200, (rel, r.text)
    assert (base / rel).read_text() == "platform"


def test_the_operator_queue_compare_and_swap_still_works(home):
    _mod, base, client = home
    (base / ".trinity").mkdir()
    (base / ".trinity/operator-queue.json").write_text("[]")
    sha = hashlib.sha256(b"[]").hexdigest()
    assert _put(client, ".trinity/operator-queue.json", "[1]", platform="true",
                if_match=sha).status_code == 200
    assert _put(client, ".trinity/operator-queue.json", "[2]", platform="true",
                if_match=sha).status_code == 412


def test_skill_service_still_deletes_a_skill(home):
    _mod, base, client = home
    (base / ".claude/skills/old").mkdir()
    (base / ".claude/skills/old/SKILL.md").write_text("x")
    assert client.delete("/api/files", params={"path": ".claude/skills/old"}).status_code == 200
    assert not (base / ".claude/skills/old").exists()


def test_the_write_probe_token_is_in_the_shipped_router():
    """The backend's write-image probe greps the agent server's source for this
    name; renaming the walk without the backend constant would refuse every
    share holder's write as `agent_restart_required`."""
    src = _ROUTER.read_text()   # live consumer: the backend's grep probe
    assert "def _walk_dir_nofollow(" in src
    backend = (_REPO_ROOT / "src/backend/services/agent_service/files.py").read_text()
    assert '_WRITE_POLICY_PROBE_TOKEN = "_walk_dir_nofollow"' in backend
