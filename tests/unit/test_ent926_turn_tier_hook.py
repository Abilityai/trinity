"""The in-container guard: a shared person's turn does not do through the agent
what the Files API refuses them (ent#926).

Drives `docker/base-image/hooks/turn-tier-guard.py` the way Claude Code does
(stdin JSON, exit 0 = allow, 2 = deny) with the platform's answer and the
claude process's launch environment stubbed, plus one real HTTP round trip for
the lookup itself. The policy it enforces is the Files API's own
(`file_path_policy.py`, a byte-identical third copy).
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_HOOKS = _REPO / "docker/base-image/hooks"
_BACKEND_POLICY = _REPO / "src/backend/services/file_path_policy.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hook():
    """The hook as the image runs it. Its `lib` and `file_path_policy` imports are
    served from the repo copies for the length of the module only: monkeypatch
    puts back whatever was in sys.modules, so nothing leaks into other tests."""
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(_HOOKS))
        mp.setitem(sys.modules, "lib", _load("lib", _HOOKS / "lib.py"))
        mp.setitem(sys.modules, "file_path_policy", _load("file_path_policy", _HOOKS / "file_path_policy.py"))
        yield _load("_turn_tier_guard", _HOOKS / "turn-tier-guard.py")


def run(hook, monkeypatch, tool, tool_input, *, run_id="exec-1", tier="share", shell=None, kind=None):
    """Returns (exit code, asked) where asked says whether the platform was asked.
    `shell` is the platform's shell answer; by default only the owner's turn has it."""
    shell = (tier == "owner") if shell is None else shell
    asked = []
    monkeypatch.setattr(hook, "read_stdin_json", lambda: {"tool_name": tool, "tool_input": tool_input})
    monkeypatch.setattr(hook, "read_environ", lambda pid: (
        {"TRINITY_EXECUTION_ID": run_id} if run_id else {}) if pid != 1 else
        {"AGENT_NAME": "agent-a", "TRINITY_MCP_API_KEY": "k"})

    def fake_ask(container, agent, run_):
        asked.append((agent, run_))
        return tier
    monkeypatch.setattr(hook, "ask_tier", fake_ask)

    def fake_turn(container, agent, run_):
        asked.append((agent, run_))
        return tier, shell, kind
    monkeypatch.setattr(hook, "ask_turn", fake_turn)
    monkeypatch.setattr(hook, "log_event", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        hook.run_hook(hook.main)
    return exc.value.code, asked


# ---- the decision ------------------------------------------------------------------------

def test_an_ordinary_write_is_allowed_without_asking_anyone(hook, monkeypatch):
    code, asked = run(hook, monkeypatch, "Write", {"file_path": "/home/developer/workspace/a.py"})
    assert (code, asked) == (0, [])


@pytest.mark.parametrize("path", ["/home/developer/CLAUDE.md", "/home/developer/.claude/agents/a.md",
                                  "/home/developer/.claude/skills/x/SKILL.md", "/home/developer/.bashrc",
                                  "/home/developer/.local/lib/python3.13/site-packages/zz.pth"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_a_shared_persons_turn_cannot_rewrite_instructions_skills_or_startup_files(hook, monkeypatch, tool, path):
    code, asked = run(hook, monkeypatch, tool, {"file_path": path}, tier="share")
    assert code == 2 and asked == [("agent-a", "exec-1")]


@pytest.mark.parametrize("path", ["/home/developer/CLAUDE.md", "/home/developer/.claude/skills/x/SKILL.md"])
def test_the_owners_turn_may_edit_them(hook, monkeypatch, path):
    code, asked = run(hook, monkeypatch, "Edit", {"file_path": path}, tier="owner")
    assert code == 0 and len(asked) == 1


@pytest.mark.parametrize("path", [
    "/home/developer/.claude/projects/-home-developer/9f2c.jsonl",
    "/home/developer/.claude/history.jsonl",
    "/home/developer/.codex/sessions/2026/10/16/rollout.jsonl",
    "/home/developer/.netrc", "/home/developer/.env"])
@pytest.mark.parametrize("tool", ["Read", "NotebookRead"])
def test_a_shared_persons_turn_cannot_read_transcripts_or_credentials(hook, monkeypatch, tool, path):
    code, _ = run(hook, monkeypatch, tool, {"file_path": path}, tier="share")
    assert code == 2


def test_the_owners_turn_may_read_a_transcript(hook, monkeypatch):
    code, _ = run(hook, monkeypatch, "Read", {"file_path": "/home/developer/.claude/history.jsonl"}, tier="owner")
    assert code == 0


def test_project_memory_stays_readable(hook, monkeypatch):
    code, asked = run(hook, monkeypatch, "Read",
                      {"file_path": "/home/developer/.claude/projects/-home-developer/memory/notes.md"})
    assert (code, asked) == (0, [])


@pytest.mark.parametrize("tool,tool_input", [
    ("Grep", {"path": "/home/developer/.claude/projects", "pattern": "x"}),
    ("Glob", {"path": "/home/developer/.claude", "pattern": "**/*.jsonl"}),
    ("Grep", {"path": "/home/developer/.claude/history.jsonl", "pattern": "x"}),
])
def test_search_tools_aimed_at_owner_only_paths_are_refused_for_a_shared_turn(hook, monkeypatch, tool, tool_input):
    code, _ = run(hook, monkeypatch, tool, tool_input, tier="share")
    assert code == 2


# ---- searches (PR #3778 review): a search reads everything under its root ---------------
# Judging Grep/Glob by `path` alone let a search with no path (it runs in the home) or one
# rooted at the home read every transcript and `.env`, since Grep includes dotfiles.

@pytest.mark.parametrize("tool,tool_input", [
    ("Grep", {"pattern": "x"}),                                         # no path: the home
    ("Glob", {"pattern": ".claude/projects/**"}),
    ("Grep", {"pattern": "x", "path": "/home/developer"}),
    ("Grep", {"pattern": "x", "path": "~"}),
    ("Grep", {"pattern": "x", "path": "/home"}),
    ("Grep", {"pattern": "x", "path": "/"}),
    ("Glob", {"pattern": "**/*.jsonl", "path": "/home/developer"}),
    ("Grep", {"pattern": "x", "path": "/var/lib"}),                     # holds the token
    ("Grep", {"pattern": "x", "path": "/home/developer/workspace", "glob": "../.env"}),
    ("Glob", {"pattern": "/home/developer/.claude/projects/*.jsonl", "path": "/tmp"}),
    ("Glob", {"pattern": "~/.env", "path": "/tmp"}),
])
def test_a_search_that_reaches_owner_only_files_is_refused_for_a_shared_turn(hook, monkeypatch, tool, tool_input):
    code, asked = run(hook, monkeypatch, tool, tool_input, tier="share")
    assert code == 2 and asked == [("agent-a", "exec-1")]


@pytest.mark.parametrize("tool_input", [{"pattern": "x"}, {"pattern": "x", "path": "/home/developer"}])
def test_the_owners_turn_may_search_the_home(hook, monkeypatch, tool_input):
    code, asked = run(hook, monkeypatch, "Grep", tool_input, tier="owner")
    assert code == 0 and len(asked) == 1


def test_a_search_with_no_path_outside_a_platform_turn_asks_nobody(hook, monkeypatch):
    assert run(hook, monkeypatch, "Grep", {"pattern": "x"}, run_id=None) == (0, [])


def _tmp_home(hook, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "workspace" / "src").mkdir(parents=True)
    (home / "workspace" / "src" / "a.py").write_text("x")
    monkeypatch.setattr(hook, "HOME", str(home))
    import file_path_policy as policy
    real = policy.decide
    monkeypatch.setattr(policy, "decide", lambda tier, verb, path, home_=str(home): real(tier, verb, path, home_))
    monkeypatch.setattr(policy, "HOME", str(home))
    return home


def test_a_search_of_an_ordinary_folder_is_allowed_without_asking(hook, monkeypatch, tmp_path):
    home = _tmp_home(hook, monkeypatch, tmp_path)
    for tool in ("Grep", "Glob"):
        code, asked = run(hook, monkeypatch, tool, {"pattern": "*", "path": str(home / "workspace")})
        assert (code, asked) == (0, []), tool


@pytest.mark.parametrize("secret", ["src/.env", "deploy/server.pem", "keys/id.key"])
def test_a_search_of_a_folder_holding_an_owner_only_file_is_refused(hook, monkeypatch, tmp_path, secret):
    home = _tmp_home(hook, monkeypatch, tmp_path)
    target = home / "workspace" / secret
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1")
    code, asked = run(hook, monkeypatch, "Grep", {"pattern": "SECRET", "path": str(home / "workspace")})
    assert code == 2 and asked


def test_a_search_of_a_folder_with_a_link_out_is_refused(hook, monkeypatch, tmp_path):
    home = _tmp_home(hook, monkeypatch, tmp_path)
    (home / ".env").write_text("SECRET=1")
    (home / "workspace" / "out").symlink_to(home)
    code, asked = run(hook, monkeypatch, "Grep", {"pattern": "SECRET", "path": str(home / "workspace")})
    assert code == 2 and asked


def test_a_search_too_large_to_check_is_refused_not_waved_through(hook, monkeypatch, tmp_path):
    home = _tmp_home(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "SEARCH_WALK_MAX_ENTRIES", 1)
    (home / "workspace" / "src" / "b.py").write_text("x")
    code, asked = run(hook, monkeypatch, "Grep", {"pattern": "x", "path": str(home / "workspace")})
    assert code == 2 and asked


@pytest.mark.parametrize("answer", ["unknown", "unreachable"])
def test_an_unknown_turn_or_no_answer_fails_closed_on_an_owner_only_path(hook, monkeypatch, answer):
    code, _ = run(hook, monkeypatch, "Write", {"file_path": "/home/developer/CLAUDE.md"}, tier=answer)
    assert code == 2


def test_no_answer_never_blocks_an_ordinary_path(hook, monkeypatch):
    code, asked = run(hook, monkeypatch, "Write", {"file_path": "/home/developer/notes.md"}, tier="unreachable")
    assert (code, asked) == (0, [])


def test_no_execution_id_means_no_platform_turn_and_allows(hook, monkeypatch):
    code, asked = run(hook, monkeypatch, "Write", {"file_path": "/home/developer/CLAUDE.md"}, run_id=None)
    assert (code, asked) == (0, [])


def test_a_relative_path_is_anchored_at_home(hook, monkeypatch):
    code, _ = run(hook, monkeypatch, "Write", {"file_path": "CLAUDE.md"}, tier="share")
    assert code == 2


def test_tilde_and_dotdot_spellings_do_not_get_around_it(hook, monkeypatch):
    for raw in ("~/CLAUDE.md", "/home/developer/workspace/../CLAUDE.md", "//home/developer/CLAUDE.md"):
        code, _ = run(hook, monkeypatch, "Write", {"file_path": raw}, tier="share")
        assert code == 2, raw


def test_a_link_into_an_owner_only_path_is_judged_by_where_it_lands(hook, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("x")
    (home / "workspace" / "link").symlink_to(home / "CLAUDE.md")
    monkeypatch.setattr(hook, "HOME", str(home))
    import file_path_policy as policy
    real = policy.decide
    monkeypatch.setattr(policy, "decide", lambda tier, verb, path, home_=str(home): real(tier, verb, path, home_))
    code, _ = run(hook, monkeypatch, "Write", {"file_path": str(home / "workspace" / "link")}, tier="share")
    assert code == 2


def test_other_tools_are_none_of_its_business(hook, monkeypatch):
    assert run(hook, monkeypatch, "WebFetch", {"url": "https://example.com"}) == (0, [])


# ---- the shell (ent#926 ruling 2026-10-19: a shared person's turn gets no shell) ----------
# A command reaches files the file tools never see (`echo x >> CLAUDE.md` landed in the
# 10-16 live run), and no path filter over a command string is sound. The container
# cannot host an OS sandbox (no user namespaces), so the shell itself is owner tier.

@pytest.mark.parametrize("command", ["ls", "echo x >> ~/CLAUDE.md", "python3 -c 'print(1)'"])
def test_a_shared_persons_turn_gets_no_shell(hook, monkeypatch, command):
    code, asked = run(hook, monkeypatch, "Bash", {"command": command}, tier="share")
    assert code == 2 and asked == [("agent-a", "exec-1")]


def test_the_owners_turn_keeps_the_shell(hook, monkeypatch):
    assert run(hook, monkeypatch, "Bash", {"command": "echo x >> ~/CLAUDE.md"}, tier="owner") == (
        0, [("agent-a", "exec-1")])


@pytest.mark.parametrize("answer", ["unknown", "unreachable"])
def test_no_answer_about_the_turn_keeps_the_shell_closed(hook, monkeypatch, answer):
    code, _ = run(hook, monkeypatch, "Bash", {"command": "ls"}, tier=answer)
    assert code == 2


def test_a_shell_outside_a_platform_turn_asks_nobody(hook, monkeypatch):
    assert run(hook, monkeypatch, "Bash", {"command": "ls"}, run_id="") == (0, [])


# ---- the lookup itself, over real HTTP -----------------------------------------------------

class _Platform(http.server.BaseHTTPRequestHandler):
    status, body, seen = 200, b'{"tier": "share"}', []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def platform():
    _Platform.status, _Platform.body, _Platform.seen = 200, b'{"tier": "share"}', []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Platform)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _container(url):
    return {"TRINITY_MCP_API_KEY": "the-key", "TRINITY_BACKEND_URL": url}


def test_the_lookup_sends_the_agents_own_key_to_the_turn_tier_route(hook, platform):
    _, url = platform
    assert hook.ask_tier(_container(url), "agent-a", "exec-1") == "share"
    assert _Platform.seen == [("/api/agents/agent-a/executions/exec-1/turn-tier", "Bearer the-key")]


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"tier": "owner"}', "owner"),
    (404, b'{"detail": "x"}', "unknown"),
    (403, b"{}", "unreachable"),
    (500, b"{}", "unreachable"),
    (200, b"not json", "unreachable"),
    (200, b'{"tier": "admin"}', "unreachable"),
    (200, b'["owner"]', "unreachable"),
])
def test_the_lookup_reads_only_a_clean_owner_or_share_answer(hook, platform, status, body, expected):
    _, url = platform
    _Platform.status, _Platform.body = status, body
    assert hook.ask_tier(_container(url), "agent-a", "exec-1") == expected


def test_an_unreachable_platform_is_no_answer_not_an_owner(hook):
    assert hook.ask_tier(_container("http://127.0.0.1:9"), "agent-a", "exec-1") == "unreachable"


def test_a_missing_key_or_agent_is_no_answer(hook):
    assert hook.ask_tier({}, "agent-a", "e") == "unreachable"
    assert hook.ask_tier({"TRINITY_MCP_API_KEY": "k"}, "", "e") == "unreachable"


def test_ids_are_percent_encoded_into_the_path(hook, platform):
    _, url = platform
    hook.ask_tier(_container(url), "agent-a", "../../x?y")
    assert _Platform.seen[-1][0] == "/api/agents/agent-a/executions/..%2F..%2Fx%3Fy/turn-tier"


# ---- wiring ---------------------------------------------------------------------------------

def test_the_hooks_policy_copy_is_byte_identical_to_the_backends():
    assert (_HOOKS / "file_path_policy.py").read_bytes() == _BACKEND_POLICY.read_bytes(), (
        "copy src/backend/services/file_path_policy.py to docker/base-image/hooks/file_path_policy.py")


def test_the_drop_in_registers_the_guard_in_exec_form_for_the_file_tools_the_shell_and_mcp():
    drop_in = json.loads((_HOOKS / "managed-settings.d/60-turn-tier-guard.json").read_text())
    (entry,) = drop_in["hooks"]["PreToolUse"]
    assert set(entry["matcher"].split("|")) == {"Edit", "Write", "NotebookEdit", "MultiEdit",
                                                "Read", "NotebookRead", "Grep", "Glob", "Bash",
                                                "mcp__.*"}
    (h,) = entry["hooks"]
    assert h["command"] == "/usr/bin/env"
    assert h["args"] == ["-i", "HOME=/home/developer", "/usr/local/bin/python3", "-I", "-S",
                         "/opt/trinity/hooks/turn-tier-guard.py"]


def test_the_image_ships_the_hook_its_policy_and_the_drop_in_root_owned():
    dockerfile = (_REPO / "docker/base-image/Dockerfile").read_text()
    for line in ("COPY ./hooks/turn-tier-guard.py /opt/trinity/hooks/turn-tier-guard.py",
                 "COPY ./hooks/file_path_policy.py /opt/trinity/hooks/file_path_policy.py",
                 "COPY ./hooks/managed-settings.d/60-turn-tier-guard.json "
                 "/etc/claude-code/managed-settings.d/60-turn-tier-guard.json",
                 "/etc/claude-code/managed-settings.d/60-turn-tier-guard.json"):
        assert line in dockerfile, line
    assert "chmod 0555 /opt/trinity/hooks/*.py" in dockerfile and "chown -R root:root /opt/trinity" in dockerfile


# ---- agent-owned paths outside the home (ent#926 review 2026-10-19) -----------------------
# The agent user owns /app (on PYTHONPATH: a `sitecustomize.py` there runs in every python
# process; startup.sh runs at boot), /var/lib/trinity (the subscription token, #1089) and
# /logs (this guard's own evidence). None of them is under the home, so none matched a class.

@pytest.mark.parametrize("path", ["/app/sitecustomize.py", "/app/startup.sh",
                                  "/app/agent_server/services/turn_scope.py"])
def test_a_shared_persons_turn_cannot_write_code_the_agent_runs(hook, monkeypatch, path):
    code, asked = run(hook, monkeypatch, "Write", {"file_path": path})
    assert code == 2 and asked


def test_the_owners_turn_may_write_under_app(hook, monkeypatch):
    assert run(hook, monkeypatch, "Write", {"file_path": "/app/x.py"}, tier="owner")[0] == 0


def test_reading_the_agent_server_code_stays_open(hook, monkeypatch):
    assert run(hook, monkeypatch, "Read", {"file_path": "/app/startup.sh"}) == (0, [])


@pytest.mark.parametrize("tool,verb_input", [("Read", "file_path"), ("Write", "file_path")])
def test_a_shared_persons_turn_cannot_touch_the_subscription_token(hook, monkeypatch, tool, verb_input):
    code, asked = run(hook, monkeypatch, tool, {verb_input: "/var/lib/trinity/oauth-token"})
    assert code == 2 and asked


def test_a_shared_persons_turn_cannot_forge_the_guard_log(hook, monkeypatch):
    code, asked = run(hook, monkeypatch, "Write", {"file_path": "/logs/guardrails.jsonl"})
    assert code == 2 and asked


def test_a_channel_visitor_keeps_the_shell_while_the_owner_allows_it(hook, monkeypatch):
    """`share` + shell: a Slack/Telegram/public-link turn on an agent whose owner
    left `visitor_shell` on (the default). File paths still follow the share tier."""
    assert run(hook, monkeypatch, "Bash", {"command": "ls"}, tier="share", shell=True)[0] == 0
    code, _ = run(hook, monkeypatch, "Write", {"file_path": "/home/developer/CLAUDE.md"},
                  tier="share", shell=True)
    assert code == 2


@pytest.mark.parametrize("body,expected", [
    (b'{"tier": "share", "kind": "visitor", "shell": true}', ("share", True, "visitor")),
    (b'{"tier": "share", "kind": "person", "shell": "yes"}', ("share", False, "person")),  # strict True
    (b'{"tier": "owner"}', ("owner", False, None)),             # no answer is no shell
    (b'{"tier": "share", "kind": "robot"}', ("share", False, None)),
    (b'{"tier": "admin", "shell": true}', ("unreachable", False, None)),
])
def test_the_shell_answer_is_read_as_a_strict_true(hook, platform, body, expected):
    _, url = platform
    _Platform.body = body
    assert hook.ask_turn(_container(url), "agent-a", "e1") == expected


@pytest.mark.parametrize("kind,expected", [("visitor", "channel or a public link"),
                                           ("person", "shared with")])
def test_the_shell_refusal_names_who_the_turn_is_for(hook, monkeypatch, capsys, kind, expected):
    """Found live 2026-10-19: a public-link visitor was told the turn was for
    'a person the agent is shared with'."""
    code, _ = run(hook, monkeypatch, "Bash", {"command": "ls"}, tier="share", shell=False, kind=kind)
    assert code == 2
    assert expected in capsys.readouterr().err


# ---- MCP servers the owner configured (PR #3778 review) ----------------------------------
# The drop-in covered built-in tools only, so a filesystem or exec MCP server in the
# owner's `.mcp.json` ran unguarded in a shared person's turn: the shell by another name.
# Such a tool is held to the shell's line; the platform's own `trinity` server is fenced
# per route by the backend (the turn header) and passes without a lookup.

@pytest.mark.parametrize("tool", ["mcp__filesystem__read_file", "mcp__shell__exec",
                                  "mcp__plugin_x_y__write",
                                  "mcp__trinity___x",          # a server named `trinity_`
                                  "mcp__trinity__fs__read"])   # a server named `trinity__fs`
def test_a_shared_persons_turn_cannot_use_the_owners_mcp_servers(hook, monkeypatch, tool):
    code, asked = run(hook, monkeypatch, tool, {"path": "/home/developer/CLAUDE.md"}, tier="share")
    assert code == 2 and asked == [("agent-a", "exec-1")]


@pytest.mark.parametrize("tier,shell", [("owner", True), ("share", True)])
def test_a_turn_that_may_use_the_shell_may_use_them(hook, monkeypatch, tier, shell):
    code, asked = run(hook, monkeypatch, "mcp__filesystem__read_file", {}, tier=tier, shell=shell,
                      kind="visitor" if tier == "share" else None)
    assert code == 0 and asked


@pytest.mark.parametrize("answer", ["unknown", "unreachable"])
def test_no_answer_keeps_the_owners_mcp_servers_closed(hook, monkeypatch, answer):
    assert run(hook, monkeypatch, "mcp__filesystem__read_file", {}, tier=answer)[0] == 2


@pytest.mark.parametrize("tool", ["mcp__trinity__chat_with_agent", "mcp__trinity__list_agents"])
def test_the_platforms_own_server_passes_without_asking(hook, monkeypatch, tool):
    assert run(hook, monkeypatch, tool, {"agent_name": "b"}, tier="share") == (0, [])


def test_an_mcp_tool_outside_a_platform_turn_asks_nobody(hook, monkeypatch):
    assert run(hook, monkeypatch, "mcp__filesystem__read_file", {}, run_id="") == (0, [])


def test_the_mcp_refusal_names_the_owners_servers(hook, monkeypatch, capsys):
    code, _ = run(hook, monkeypatch, "mcp__filesystem__read_file", {}, tier="share", kind="person")
    assert code == 2 and "MCP" in capsys.readouterr().err
