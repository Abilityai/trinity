"""
Gated skills, the in-container hook — the hook itself (trinity-enterprise#752).

Target: ``docker/base-image/hooks/skill-gate.py`` (the bootstrap Claude Code
runs) and ``_skill_gate.py`` (the decision), executed for real in their own
process under ``-I -S`` through the import-level seam in
``_ent752_hook_harness.py``, against a stub platform on 127.0.0.1 and a fake
``/proc``.

The contract the CLI (2.1.281) imposes, and why every test below exists:
  * exit 2 blocks the tool; exit 0 lets it run; exit 1, a traceback, a crash,
    a spawn failure or the CLI's own hook timeout ALL let it run. So the hook
    never exits 1: every path ends in 0 or 2, under its own deadline.
  * only a definitive answer from the platform decides; anything else falls to
    the root-owned marker — present (the agent has gates) refuses, absent
    allows, so an agent with no gates keeps its skills through an outage.
  * identity comes from the claude process's launch env and the container's,
    never from the hook's own env, which repo settings `env` can reach.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent752_hook_harness as H  # noqa: E402

pytestmark = pytest.mark.unit

COULD_NOT_CHECK = (
    "Trinity could not be reached to check whether this skill needs approval, so it was not "
    "run. Do not carry out its steps another way; tell whoever asked that it could not be checked."
)
ALLOW = {"allowed": True, "gated": False, "message": None}
REFUSE = {"allowed": False, "gated": True, "message": "Refused: ask through Trinity."}


@pytest.fixture
def world(tmp_path):
    w = H.HookWorld(tmp_path)
    yield w
    w.close()


# ---------------------------------------------------------------------------
# The verdict decides
# ---------------------------------------------------------------------------

def test_an_allowed_skill_runs_and_the_question_carries_the_platform_identity(world):
    world.skill("pay-invoice")
    world.backend.answer(200, ALLOW)
    out = world.run(H.skill_payload("  /pay-invoice "))
    assert (out.returncode, out.stderr) == (0, "")
    [req] = world.backend.requests
    assert req.path == "/api/skill-gate/check"
    assert req.headers["authorization"] == f"Bearer {H.KEY}"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.body)
    assert body == {"via": "skill_tool", "invoked": "pay-invoice", "names": ["pay-invoice"],
                    "resolved": True, "subagent": None, "execution_id": H.EXECUTION_ID,
                    "marker": False}


def test_a_refusal_blocks_with_the_platforms_words(world):
    world.backend.answer(200, REFUSE)
    out = world.run(H.skill_payload())
    assert out.returncode == 2
    assert out.stderr.strip() == REFUSE["message"]


def test_a_refusal_without_words_still_blocks(world):
    world.backend.answer(200, {"allowed": False})
    out = world.run(H.skill_payload())
    assert out.returncode == 2
    assert out.stderr.strip()


def test_the_marker_is_reported_to_the_platform(world):
    world.set_marker(True)
    world.run(H.skill_payload())
    assert world.backend.bodies()[0]["marker"] is True


@pytest.mark.parametrize("tool", ["Bash", "Write", "Read", "mcp__trinity__chat_with_agent"])
def test_other_tools_are_not_this_hooks_business(world, tool):
    world.set_marker(True)
    payload = H.skill_payload()
    payload["tool_name"] = tool
    out = world.run(payload)
    assert out.returncode == 0
    assert world.backend.requests == []


# ---------------------------------------------------------------------------
# No verdict → the marker decides (and a gated agent fails closed)
# ---------------------------------------------------------------------------

_NO_VERDICT = [
    pytest.param(("status", 500, {"detail": "boom"}), id="5xx"),
    pytest.param(("status", 503, {"detail": {"code": "gate_unavailable"}}), id="gate-unavailable"),
    pytest.param(("status", 404, {"detail": "Not Found"}), id="an-older-backend"),
    pytest.param(("status", 401, {"detail": "no"}), id="401"),
    pytest.param(("status", 403, {"detail": {"code": "agent_identity_required"}}), id="403"),
    pytest.param(("status", 422, {"detail": []}), id="422"),
    pytest.param(("status", 200, b"<html>not json</html>"), id="malformed-200"),
    pytest.param(("status", 200, {"allowed": "true"}), id="allowed-not-a-bool"),
    pytest.param(("status", 200, {"gated": False}), id="allowed-missing"),
    pytest.param(("status", 200, ["allowed", True]), id="not-an-object"),
    pytest.param(("status", 302, {"location": "elsewhere"}), id="a-redirect"),
]


@pytest.mark.parametrize("answer", _NO_VERDICT)
@pytest.mark.parametrize("marker", [False, True], ids=["no-gates", "gated-agent"])
def test_anything_but_a_verdict_falls_to_the_marker(world, answer, marker):
    world.set_marker(marker)
    world.backend.script(answer)
    out = world.run(H.skill_payload())
    if marker:
        assert out.returncode == 2
        assert out.stderr.strip() == COULD_NOT_CHECK
    else:
        assert (out.returncode, out.stderr) == (0, "")


@pytest.mark.parametrize("marker", [False, True], ids=["no-gates", "gated-agent"])
def test_an_unreachable_platform_falls_to_the_marker(world, marker):
    world.set_marker(marker)
    world.pid1_env["TRINITY_BACKEND_URL"] = H.closed_port_url()
    out = world.run(H.skill_payload())
    assert out.returncode == (2 if marker else 0)


def test_a_platform_that_never_answers_is_cut_off_by_the_deadline(world):
    world.set_marker(True)
    world.backend.script(("hang", 30))
    started = time.monotonic()
    out = world.run(H.skill_payload(), DEADLINE_SECONDS=1.5, HTTP_TIMEOUT_SECONDS=60.0)
    assert out.returncode == 2
    assert out.stderr.strip() == COULD_NOT_CHECK
    assert time.monotonic() - started < 10


def test_one_dropped_connection_is_retried_once(world):
    world.backend.script(("close",), ("status", 200, ALLOW))
    out = world.run(H.skill_payload())
    assert out.returncode == 0
    assert len(world.backend.requests) == 2


def test_a_second_drop_is_not_retried_again(world):
    world.set_marker(True)
    world.backend.script(("close",), ("close",), ("status", 200, ALLOW))
    out = world.run(H.skill_payload())
    assert out.returncode == 2
    assert len(world.backend.requests) == 2


def test_an_error_answer_is_not_retried(world):
    world.backend.script(("status", 500, {}), ("status", 200, ALLOW))
    world.run(H.skill_payload())
    assert len(world.backend.requests) == 1


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"tool_name": "Skill"'])
@pytest.mark.parametrize("marker", [False, True], ids=["no-gates", "gated-agent"])
def test_unreadable_hook_input_falls_to_the_marker(world, raw, marker):
    world.set_marker(marker)
    out = world.run(None, raw_stdin=raw)
    assert out.returncode == (2 if marker else 0)
    assert world.backend.requests == []


# ---------------------------------------------------------------------------
# Identity — /proc, never the hook's own environment
# ---------------------------------------------------------------------------

def test_the_hooks_own_environment_is_ignored(world):
    """Repo settings `env` reaches a hook's environment; none of it may steer
    the question — not the run, not the platform, not a proxy, not the key."""
    hostile = {
        "PATH": os.environ.get("PATH", ""),
        "TRINITY_EXECUTION_ID": "exec-forged",
        "TRINITY_BACKEND_URL": H.closed_port_url(),
        "TRINITY_MCP_API_KEY": "forged-key",
        "HTTP_PROXY": H.closed_port_url(), "http_proxy": H.closed_port_url(),
        "HOSTALIASES": "/nonexistent", "LOCALDOMAIN": "evil.example",
        "AGENT_NAME": "someone-else",
    }
    world.backend.answer(200, ALLOW)
    out = world.run(H.skill_payload(), env=hostile)
    assert out.returncode == 0
    [req] = world.backend.requests
    assert req.headers["authorization"] == f"Bearer {H.KEY}"
    assert json.loads(req.body)["execution_id"] == H.EXECUTION_ID


def test_the_hook_empties_its_own_environment_before_deciding(world, tmp_path):
    """Defence in depth under the registration's `env -i`: `getaddrinfo` reads
    HOSTALIASES / RES_OPTIONS / LOCALDOMAIN and TLS reads SSL_CERT_FILE from the
    environment, so nothing of it may survive into the decision."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(H.HOOKS)!r})\n"
        "import lib\n"
        f"lib.LOG_PATH = {str(world.log)!r}\n"
        "import _skill_gate as g\n"
        f"for k, v in json.loads({json.dumps(json.dumps(world.overrides()))}).items():\n"
        "    setattr(g, k, v)\n"
        "code = g.main([])\n"
        "sys.stdout.write(json.dumps(sorted(os.environ)))\n"
        "sys.stdout.flush()\n"
        "os._exit(code)\n")
    world._write_proc()
    out = subprocess.run([sys.executable, "-I", "-S", str(probe)], input=json.dumps(H.skill_payload()),
                         capture_output=True, text=True, timeout=40,
                         env={"PATH": os.environ.get("PATH", ""), "HOSTALIASES": "/x",
                              "RES_OPTIONS": "ndots:9", "SSL_CERT_FILE": "/x"})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_no_agent_name_is_ever_sent(world):
    world.run(H.skill_payload())
    body = world.backend.requests[0].body.decode()
    assert "a-stale-name-the-hook-must-not-use" not in body
    assert "agent_name" not in body


@pytest.mark.parametrize("marker", [False, True], ids=["no-gates", "gated-agent"])
def test_no_key_means_no_question_and_the_marker_decides(world, marker):
    world.set_marker(marker)
    world.pid1_env["TRINITY_MCP_API_KEY"] = None
    out = world.run(H.skill_payload())
    assert out.returncode == (2 if marker else 0)
    assert world.backend.requests == []


def test_an_unreadable_container_environment_means_no_question(world):
    world.set_marker(True)
    world.pid1_env = None
    out = world.run(H.skill_payload())
    assert out.returncode == 2
    assert world.backend.requests == []


def test_a_session_with_no_run_still_asks_and_says_so(world):
    """Web terminal / SSH: no execution id. The platform answers (refusing a
    gated skill, D2); an agent with no gates keeps working."""
    world.parent_env.pop("TRINITY_EXECUTION_ID")
    world.backend.answer(200, ALLOW)
    assert world.run(H.skill_payload()).returncode == 0
    assert world.backend.bodies()[0]["execution_id"] is None


def test_an_unreadable_claude_environment_asks_without_a_run(world):
    world.parent_env = None
    world.run(H.skill_payload())
    assert world.backend.bodies()[0]["execution_id"] is None


def test_an_oversized_execution_id_is_never_sent(world):
    world.parent_env["TRINITY_EXECUTION_ID"] = "x" * 500
    world.run(H.skill_payload())
    assert world.backend.bodies()[0]["execution_id"] is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the real /proc")
def test_the_environ_reader_reads_a_real_processs_launch_environment():
    g = H.load_module()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             env={"TRINITY_EXECUTION_ID": "exec-real-752", "OTHER": "a=b=c"})
    try:
        deadline = time.monotonic() + 5
        env = {}
        while time.monotonic() < deadline:
            env = g.read_environ(child.pid)
            if env.get("TRINITY_EXECUTION_ID"):
                break
            time.sleep(0.05)
        assert env["TRINITY_EXECUTION_ID"] == "exec-real-752"
        assert env["OTHER"] == "a=b=c"
    finally:
        child.kill()
        child.wait()


# ---------------------------------------------------------------------------
# What it asks about
# ---------------------------------------------------------------------------

def test_every_name_the_skill_answers_to_is_sent(world):
    world.skill("pay", "name: pay-invoice")
    world.run(H.skill_payload("pay-invoice"))
    body = world.backend.bodies()[0]
    assert set(body["names"]) >= {"pay", "pay-invoice"}


def test_a_plugin_skill_is_asked_about_by_its_last_segment_too(world):
    world.run(H.skill_payload("acme:pay-invoice"))
    body = world.backend.bodies()[0]
    assert body["invoked"] == "acme:pay-invoice"
    assert set(body["names"]) >= {"acme:pay-invoice", "pay-invoice"}


@pytest.mark.parametrize("bad", [None, "", "   ", "/", 42, ["pay-invoice"], {"x": 1}])
def test_a_skill_call_that_names_nothing_usable_is_unresolved(world, bad):
    world.run(H.skill_payload(bad))
    body = world.backend.bodies()[0]
    assert body["resolved"] is False
    assert body["names"] == [] and body["invoked"] is None


def test_an_overlong_skill_name_is_unresolved_not_truncated(world):
    world.run(H.skill_payload("p" * 300))
    body = world.backend.bodies()[0]
    assert body["resolved"] is False
    assert all(len(n) <= 256 for n in body["names"])
    assert body["invoked"] is None or len(body["invoked"]) <= 256


def test_a_subagent_that_preloads_a_skill_is_asked_about(world):
    world.subagent("team/money.md", "name: finance\ndescription: pays\nskills:\n  - pay-invoice")
    world.backend.answer(200, REFUSE)
    out = world.run(H.agent_payload("finance"))
    assert out.returncode == 2
    body = world.backend.bodies()[0]
    assert (body["via"], body["subagent"], body["invoked"]) == ("subagent_preload", "finance", None)
    assert "pay-invoice" in body["names"] and body["resolved"] is True


def test_the_legacy_task_tool_name_is_checked_the_same_way(world):
    world.subagent("finance.md", "name: finance\nskills: [pay-invoice]")
    world.run(H.agent_payload("finance", tool_name="Task"))
    assert world.backend.bodies()[0]["via"] == "subagent_preload"


def test_a_project_subagent_is_found_from_the_claude_processs_directory(world, tmp_path):
    world.project = tmp_path / "repo"
    world.subagent("finance.md", "name: finance\nskills: pay-invoice",
                   root=world.project / ".claude" / "agents")
    world.run(H.agent_payload("finance"))
    assert "pay-invoice" in world.backend.bodies()[0]["names"]


@pytest.mark.parametrize("subagent_type", ["Explore", "general-purpose", None])
def test_a_subagent_with_no_definition_preloads_nothing_and_costs_no_call(world, subagent_type):
    world.set_marker(True)
    out = world.run(H.agent_payload(subagent_type))
    assert out.returncode == 0
    assert world.backend.requests == []


def test_an_omitted_subagent_type_is_the_general_purpose_agent_a_definition_may_override(world):
    """No `subagent_type` falls back to `general-purpose`, and a user or project
    definition of that name overrides the built-in — with its preloads."""
    world.subagent("gp.md", "name: general-purpose\nskills: [pay-invoice]")
    world.run(H.agent_payload(None))
    body = world.backend.bodies()[0]
    assert (body["subagent"], body["via"]) == ("general-purpose", "subagent_preload")
    assert "pay-invoice" in body["names"]


def test_a_config_dir_from_the_claude_processs_environment_is_searched(world, tmp_path):
    config = tmp_path / "cfg"
    world.skill("pay", "name: pay-invoice", root=config / "skills")
    world.parent_env["CLAUDE_CONFIG_DIR"] = str(config)
    world.run(H.skill_payload("pay-invoice"))
    assert "pay" in world.backend.bodies()[0]["names"]


def test_a_subagent_that_preloads_nothing_costs_no_call(world):
    world.set_marker(True)
    world.subagent("finance.md", "name: finance\ndescription: no skills here")
    assert world.run(H.agent_payload("finance")).returncode == 0
    assert world.backend.requests == []


def test_a_plugin_subagent_whose_definition_is_missing_is_unresolved(world):
    world.run(H.agent_payload("acme:finance"))
    body = world.backend.bodies()[0]
    assert (body["resolved"], body["subagent"]) == (False, "acme:finance")


def test_a_plugin_subagent_is_found_under_the_plugins_tree(world):
    world.subagent("finance.md", "name: finance\nskills: [pay-invoice]",
                   root=world.home / ".claude" / "plugins" / "cache" / "mkt" / "acme" / "1.0" / "agents")
    world.run(H.agent_payload("acme:finance"))
    body = world.backend.bodies()[0]
    assert body["resolved"] is True and "pay-invoice" in body["names"]


def test_an_unparseable_skills_list_is_unresolved(world):
    world.subagent("finance.md", "name: finance\nskills: {pay-invoice: yes}")
    world.run(H.agent_payload("finance"))
    assert world.backend.bodies()[0]["resolved"] is False


def test_a_non_string_subagent_type_is_unresolved(world):
    world.run(H.agent_payload(["finance"]))
    assert world.backend.bodies()[0]["resolved"] is False


# ---------------------------------------------------------------------------
# Hostile files cannot hang it
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_where_a_definition_should_be_is_never_opened(world):
    d = world.home / ".claude" / "skills" / "trap"
    d.mkdir(parents=True)
    os.mkfifo(d / "SKILL.md")
    os.mkfifo(world.home / ".claude" / "agents" / "trap.md")
    world.backend.answer(200, ALLOW)
    started = time.monotonic()
    assert world.run(H.skill_payload("trap"), timeout=20).returncode == 0
    assert world.run(H.agent_payload("trap"), timeout=20).returncode == 0
    assert time.monotonic() - started < 15


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_log_path_never_changes_the_exit_code(world):
    os.mkfifo(world.log)
    world.backend.answer(200, REFUSE)
    started = time.monotonic()
    out = world.run(H.skill_payload(), timeout=20)
    assert out.returncode == 2
    assert time.monotonic() - started < 15


def test_more_skills_than_the_scan_bound_is_unresolved(world):
    root = world.home / ".claude" / "skills"
    for i in range(H.load_module().MAX_ENTRIES + 5):
        (root / f"s{i:05d}").mkdir()
    world.run(H.skill_payload("pay-invoice"))
    assert world.backend.bodies()[0]["resolved"] is False


# ---------------------------------------------------------------------------
# The log — names and outcome, never the key; a failed write changes nothing
# ---------------------------------------------------------------------------

def test_each_outcome_is_logged_without_the_key(world):
    world.backend.answer(200, ALLOW)
    world.run(H.skill_payload())
    world.backend.answer(200, REFUSE)
    world.run(H.skill_payload())
    world.backend.answer(500, {})
    world.run(H.skill_payload())
    events = world.log_events()
    assert [e["event"] for e in events] == ["skill_gate_allow", "skill_gate_deny", "skill_gate_unknown"]
    assert events[0]["via"] == "skill_tool" and "pay-invoice" in events[0]["names"]
    assert events[2]["reason"]
    assert H.KEY not in world.log.read_text()


@pytest.mark.parametrize("answer, code", [(REFUSE, 2), (ALLOW, 0)], ids=["refusal", "allow"])
def test_a_logger_that_cannot_even_start_never_changes_the_verdict(world, answer, code):
    """Thread creation failing after the verdict (resource exhaustion) must not
    reach the bootstrap's catch-all: with no marker that would turn the
    platform's explicit refusal into an allow."""
    world.backend.answer(200, answer)
    out = world.run(H.skill_payload(), LOG_JOIN_SECONDS="not-a-number")
    assert out.returncode == code


def test_an_unwritable_log_never_changes_the_exit_code(world):
    world.log.mkdir()          # a directory where the file should be
    world.backend.answer(200, REFUSE)
    assert world.run(H.skill_payload()).returncode == 2
    world.backend.answer(200, ALLOW)
    assert world.run(H.skill_payload()).returncode == 0


# ---------------------------------------------------------------------------
# The bootstrap — whatever the module does, the exit is 0 or 2
# ---------------------------------------------------------------------------

_BOOT = r'''
import os, runpy, sys, types
_marker = {marker!r}
_real = os.path.lexists
os.path.lexists = lambda p: _marker if p == "/opt/trinity/skill-gates-active" else _real(p)
{prelude}
sys.argv = [{boot!r}] + {argv!r}
runpy.run_path({boot!r}, run_name="__main__")
'''


def _boot(tmp_path, prelude: str, *, marker: bool, argv=(), stdin="{}"):
    script = tmp_path / "boot.py"
    script.write_text(_BOOT.format(marker=marker, prelude=prelude, boot=str(H.BOOTSTRAP),
                                   argv=list(argv)))
    return subprocess.run([sys.executable, "-I", "-S", str(script)], input=stdin,
                          capture_output=True, text=True, timeout=30)


_CRASHES = {
    "the-module-will-not-import": 'sys.modules["_skill_gate"] = None',
    "main-raises": 'sys.modules["_skill_gate"] = types.SimpleNamespace(main=lambda argv: 1 / 0)',
    "main-exits-1": ('def _m(argv):\n    raise SystemExit(1)\n'
                     'sys.modules["_skill_gate"] = types.SimpleNamespace(main=_m)'),
    "main-returns-garbage": 'sys.modules["_skill_gate"] = types.SimpleNamespace(main=lambda argv: 7)',
}


@pytest.mark.parametrize("crash", sorted(_CRASHES))
@pytest.mark.parametrize("marker", [False, True], ids=["no-gates", "gated-agent"])
def test_a_crashing_module_never_lets_a_gated_skill_through(tmp_path, crash, marker):
    out = _boot(tmp_path, _CRASHES[crash], marker=marker)
    assert out.returncode == (2 if marker else 0), out.stderr
    if marker:
        assert out.stderr.strip() == COULD_NOT_CHECK


def test_the_bootstrap_runs_the_real_module(tmp_path):
    """No seam at all: the real module, found on the real path layout. The
    image path does not exist here, so the module comes from the repo copy
    that sits behind it on sys.path."""
    prelude = f"sys.path.append({str(H.HOOKS)!r})"
    out = _boot(tmp_path, prelude, marker=False,
                stdin=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": "true"}}))
    assert (out.returncode, out.stderr) == (0, "")


def test_the_self_test_passes_on_the_real_module(tmp_path):
    out = _boot(tmp_path, f"sys.path.append({str(H.HOOKS)!r})", marker=False, argv=["--self-test"])
    assert out.returncode == 0, out.stderr


def test_the_self_test_fails_the_build_when_the_module_cannot_load(tmp_path):
    out = _boot(tmp_path, 'sys.modules["_skill_gate"] = None', marker=False, argv=["--self-test"])
    assert out.returncode not in (0, 2)


def test_the_bootstrap_and_the_module_agree_on_the_marker_and_the_words():
    """The bootstrap must decide on its own when the module cannot load, so it
    carries its own copy of the two values; they must never drift apart."""
    import ast
    g = H.load_module()
    consts = {}
    for node in ast.parse(H.BOOTSTRAP.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    assert g.MARKER == consts["MARKER"] == "/opt/trinity/skill-gates-active"
    assert g.COULD_NOT_CHECK == consts["COULD_NOT_CHECK"] == COULD_NOT_CHECK


def test_the_hook_deadline_sits_well_inside_its_registered_timeout():
    g = H.load_module()
    registration = json.loads((H.HOOKS / "managed-settings.d" / "50-skill-gate.json").read_text())
    [entry] = registration["hooks"]["PreToolUse"]
    [hook] = entry["hooks"]
    assert g.DEADLINE_SECONDS + 5 <= hook["timeout"]
    assert 2 * g.HTTP_TIMEOUT_SECONDS <= g.DEADLINE_SECONDS
