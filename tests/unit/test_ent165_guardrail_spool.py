"""trinity-enterprise#165 S2 — the guardrail spool record.

Every guardrail hook appends to `/logs/guardrails.jsonl` through
`lib.log_event`. Before this, a record said what happened but not which hook
wrote it, which turn it belonged to, or a stable reason, and nothing ever read
the file. This pins the versioned record the agent server's forwarder ships:
`v`, `event_id`, a derived `kind` and `hook`, a stable `code` at every deny
call site, and the turn's `execution_id` — added to the existing fields, which
stay as they were.

Everything here runs the SHIPPED hooks from `docker/base-image/hooks/`.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "docker" / "base-image" / "hooks"
BASELINE = HOOKS / "guardrails-baseline.json"
PYTHON = getattr(sys, "_base_executable", None) or sys.executable
CODE_SHAPE = r"^[a-z0-9_]{1,64}$"
FAKE_KEY = "sk-ant-api03-" + "Q" * 93


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lib(monkeypatch, tmp_path):
    """A fresh `lib` writing to a tmp spool, with `/proc` pointed at tmp."""
    mod = _load("lib", HOOKS / "lib.py")
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "guardrails.jsonl"))
    monkeypatch.setattr(mod, "PROC_ROOT", str(tmp_path / "proc"), raising=False)
    monkeypatch.setitem(sys.modules, "lib", mod)
    return mod


def _records(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _parent_environ(tmp_path, monkeypatch, env: dict) -> None:
    """The claude process's launch environment, as `/proc/<ppid>/environ`."""
    d = tmp_path / "proc" / str(os.getppid())
    d.mkdir(parents=True, exist_ok=True)
    (d / "environ").write_bytes(
        b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
    )


# ---- the record ----------------------------------------------------------------------------


def test_a_deny_record_carries_the_versioned_contract(lib, tmp_path, monkeypatch):
    _parent_environ(
        tmp_path, monkeypatch, {"TRINITY_EXECUTION_ID": "exec-165-spool", "HOME": "/x"}
    )
    monkeypatch.setattr(sys, "argv", ["/opt/trinity/hooks/bash-guardrail.py"])
    with pytest.raises(SystemExit):
        lib.deny(
            "matches a rule",
            tool="Bash",
            pattern="rm",
            command_prefix="rm -rf /",
            code="bash_deny_pattern",
        )
    [rec] = _records(lib.LOG_PATH)
    assert rec["v"] == 1
    assert str(uuid.UUID(rec["event_id"])) == rec["event_id"]
    assert rec["kind"] == "deny"
    assert rec["hook"] == "bash_guardrail"
    assert rec["code"] == "bash_deny_pattern"
    assert rec["execution_id"] == "exec-165-spool"


def test_the_existing_fields_are_unchanged(lib, monkeypatch):
    """Additive only: `ts`, `event`, `reason` and the call site's fields are
    exactly what they were before the contract was added."""
    monkeypatch.setattr(sys, "argv", ["/opt/trinity/hooks/file-guardrail.py"])
    with pytest.raises(SystemExit):
        lib.deny(
            "protected path (x)",
            tool="Write",
            path="/home/developer/.ssh/id_rsa",
            pattern="/home/developer/.ssh/*",
            code="protected_path",
        )
    [rec] = _records(lib.LOG_PATH)
    assert isinstance(rec["ts"], float)
    assert rec["event"] == "deny"
    assert rec["reason"] == "protected path (x)"
    assert rec["tool"] == "Write" and rec["path"] == "/home/developer/.ssh/id_rsa"
    assert rec["pattern"] == "/home/developer/.ssh/*"


def test_each_record_gets_its_own_event_id(lib):
    lib.log_event("hook_error", traceback="x")
    lib.log_event("hook_error", traceback="y")
    ids = [r["event_id"] for r in _records(lib.LOG_PATH)]
    assert len(set(ids)) == 2


def test_a_record_without_a_code_carries_its_event_name(lib):
    lib.log_event("config_missing")
    [rec] = _records(lib.LOG_PATH)
    assert rec["code"] == "config_missing"


# ---- kind: derived through one frozen table --------------------------------------------------


@pytest.mark.parametrize(
    "event, fields, kind",
    [
        ("deny", {}, "deny"),
        (
            "deny",
            {"code": "redaction_writeback_refused"},
            "redaction_writeback_refused",
        ),
        ("credential_redacted", {}, "credential_redacted"),
        ("credential_unredacted", {}, "credential_unredacted"),
        ("scanner_error", {}, "scanner_error"),
        ("scanner_oversize", {}, "scanner_error"),
        ("hook_error", {}, "hook_error"),
        ("hook_stdin_parse_error", {}, "hook_error"),
        ("config_load_error", {}, "config_error"),
        ("config_missing", {}, "config_error"),
        ("bad_baseline_regex", {}, "config_error"),
        ("read_only_config_load_error", {}, "config_error"),
        ("skill_gate_deny", {}, "deny"),
        ("skill_gate_unknown", {"marker": True}, "deny"),
        ("skill_gate_unknown", {"marker": False}, None),
        ("skill_gate_allow", {}, None),
        ("turn_tier_owner", {}, None),
        ("credential_pattern_in_output", {}, None),
        ("something_new", {}, None),
    ],
)
def test_kind_is_derived_from_the_legacy_event_name(lib, event, fields, kind):
    lib.log_event(event, **fields)
    [rec] = _records(lib.LOG_PATH)
    assert rec["kind"] == kind
    assert kind is None or kind in lib.EVENT_KINDS


def test_a_caller_cannot_overwrite_a_derived_field(lib, monkeypatch):
    """`kind` is the event kind. A caller that passes the turn's kind under that
    name (the turn-tier guard did) must not change it."""
    monkeypatch.setattr(sys, "argv", ["/opt/trinity/hooks/turn-tier-guard.py"])
    lib.log_event(
        "deny",
        kind="visitor",
        hook="forged",
        v=7,
        event_id="forged",
        execution_id="forged",
    )
    [rec] = _records(lib.LOG_PATH)
    assert (rec["kind"], rec["hook"], rec["v"]) == ("deny", "turn_tier_guard", 1)
    assert rec["event_id"] != "forged" and rec["execution_id"] is None


# ---- hook: derived from the script that runs ---------------------------------------------------


@pytest.mark.parametrize(
    "script, hook",
    [
        ("bash-guardrail.py", "bash_guardrail"),
        ("file-guardrail.py", "file_guardrail"),
        ("read-only-guard.py", "read_only_guard"),
        ("turn-tier-guard.py", "turn_tier_guard"),
        ("skill-gate.py", "skill_gate"),
        ("output-scanner.py", "output_scanner"),
        ("something-else.py", None),
    ],
)
def test_hook_is_derived_from_the_script(lib, monkeypatch, script, hook):
    monkeypatch.setattr(sys, "argv", ["/opt/trinity/hooks/" + script])
    lib.log_event("deny")
    [rec] = _records(lib.LOG_PATH)
    assert rec["hook"] == hook
    assert hook is None or hook in lib.EVENT_HOOKS


def test_the_enums_are_the_contract_values(lib):
    """The backend keeps its own copy of these two sets (ent#165 S1)."""
    assert tuple(lib.EVENT_KINDS) == (
        "deny",
        "credential_redacted",
        "credential_unredacted",
        "redaction_writeback_refused",
        "scanner_error",
        "hook_error",
        "config_error",
    )
    assert tuple(lib.EVENT_HOOKS) == (
        "bash_guardrail",
        "file_guardrail",
        "read_only_guard",
        "turn_tier_guard",
        "skill_gate",
        "output_scanner",
    )
    assert lib.SPOOL_SCHEMA_VERSION == 1


# ---- the turn's identity ------------------------------------------------------------------------


def test_no_platform_turn_means_no_execution_id(lib, tmp_path, monkeypatch):
    _parent_environ(tmp_path, monkeypatch, {"HOME": "/x"})
    lib.log_event("deny")
    assert _records(lib.LOG_PATH)[0]["execution_id"] is None


def test_an_over_long_execution_id_is_dropped(lib, tmp_path, monkeypatch):
    _parent_environ(tmp_path, monkeypatch, {"TRINITY_EXECUTION_ID": "e" * 129})
    lib.log_event("deny")
    assert _records(lib.LOG_PATH)[0]["execution_id"] is None


def test_the_execution_id_is_read_from_the_parent_never_the_hooks_env(
    lib, tmp_path, monkeypatch
):
    monkeypatch.setenv("TRINITY_EXECUTION_ID", "from-the-hook-env")
    _parent_environ(tmp_path, monkeypatch, {"TRINITY_EXECUTION_ID": "from-the-parent"})
    lib.log_event("deny")
    assert _records(lib.LOG_PATH)[0]["execution_id"] == "from-the-parent"


def test_session_and_tool_use_ids_come_from_the_hook_input(lib, monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "session_id": "sess-165",
                    "tool_use_id": "toolu_165",
                }
            )
        ),
    )
    lib.read_stdin_json()
    lib.log_event("deny")
    rec = _records(lib.LOG_PATH)[0]
    assert (rec["session_id"], rec["tool_use_id"]) == ("sess-165", "toolu_165")


def test_a_broken_derivation_still_writes_the_legacy_record(lib, monkeypatch):
    """The spool fails open: a fault computing the new fields never costs the record."""

    def boom():
        raise RuntimeError("derivation fault")

    monkeypatch.setattr(lib, "_derived_fields", lambda event, fields: boom())
    lib.log_event("deny", tool="Bash")
    [rec] = _records(lib.LOG_PATH)
    assert rec["event"] == "deny" and rec["tool"] == "Bash" and "v" not in rec


# ---- every call site: a code, and no reserved field ------------------------------------------

RESERVED = {"v", "event_id", "kind", "hook", "execution_id"}


def _calls(tree, names):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.id
                if isinstance(f, ast.Name)
                else f.attr if isinstance(f, ast.Attribute) else None
            )
            if name in names:
                yield node


def _hook_sources():
    return sorted(p for p in HOOKS.glob("*.py") if p.name != "lib.py")


def test_the_scan_sees_the_hooks():
    names = {p.name for p in _hook_sources()}
    assert {
        "bash-guardrail.py",
        "file-guardrail.py",
        "read-only-guard.py",
        "turn-tier-guard.py",
        "_skill_gate.py",
    } <= names


@pytest.mark.parametrize("path", _hook_sources(), ids=lambda p: p.name)
def test_every_deny_call_passes_a_code(path):
    tree = ast.parse(path.read_text())
    for call in _calls(tree, {"deny"}):
        kws = {k.arg: k.value for k in call.keywords}
        assert "code" in kws, f"{path.name}:{call.lineno} denies without code="
        value = kws["code"]
        if isinstance(value, ast.Constant):
            import re

            assert re.match(
                CODE_SHAPE, value.value
            ), f"{path.name}:{call.lineno} code {value.value!r}"


@pytest.mark.parametrize("path", _hook_sources(), ids=lambda p: p.name)
def test_no_call_passes_a_reserved_field(path):
    tree = ast.parse(path.read_text())
    for call in _calls(tree, {"deny", "log_event"}):
        bad = {k.arg for k in call.keywords} & RESERVED
        assert not bad, f"{path.name}:{call.lineno} passes reserved {sorted(bad)}"


# ---- each shipped hook, run as its own script --------------------------------------------------

_WRAPPER = r"""
import importlib.util, json, sys
sys.path.insert(0, {hooks!r})
sys.argv[0] = {script!r}
import lib
lib.RUNTIME_CONFIG_PATH = {runtime!r}
lib.BASELINE_CONFIG_PATH = {baseline!r}
lib.LOG_PATH = {log!r}
lib.PROC_ROOT = {proc!r}
spec = importlib.util.spec_from_file_location("guard_hook", {script!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for _k, _v in json.loads({over!r}).items():
    setattr(mod, _k, _v)
lib.run_hook(mod.main)
"""


def _run_script(tmp_path, hook, payload, *, runtime=None, overrides=None):
    log = tmp_path / "guardrails.jsonl"
    rt = tmp_path / "runtime.json"
    if runtime is not None:
        rt.write_text(json.dumps({**json.loads(BASELINE.read_text()), **runtime}))
    wrapper = _WRAPPER.format(
        hooks=str(HOOKS),
        script=str(HOOKS / hook),
        runtime=str(rt),
        baseline=str(BASELINE),
        log=str(log),
        proc=str(tmp_path / "proc"),
        over=json.dumps(overrides or {}),
    )
    r = subprocess.run(
        [PYTHON, "-I", "-S", "-c", wrapper],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    return r, _records(log)


@pytest.mark.parametrize(
    "hook, payload, runtime, overrides, code",
    [
        (
            "bash-guardrail.py",
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
            None,
            None,
            "bash_deny_pattern",
        ),
        (
            "bash-guardrail.py",
            {"tool_name": "Bash", "tool_input": {"command": "make deploy-prod"}},
            {"extra_bash_deny": ["deploy-prod"]},
            None,
            "bash_agent_rule",
        ),
        (
            "file-guardrail.py",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/home/developer/.ssh/id_rsa"},
            },
            None,
            None,
            "protected_path",
        ),
        (
            "file-guardrail.py",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/home/developer/secret-notes.md"},
            },
            {"extra_path_deny": ["secret-notes"]},
            None,
            "path_agent_rule",
        ),
    ],
)
def test_a_shipped_hook_denies_with_its_code(
    tmp_path, hook, payload, runtime, overrides, code
):
    r, records = _run_script(
        tmp_path, hook, payload, runtime=runtime, overrides=overrides
    )
    assert r.returncode == 2, r.stderr
    [rec] = records
    assert rec["code"] == code and rec["kind"] == "deny"
    assert rec["hook"] == hook[:-3].replace("-", "_")


def test_the_bash_deny_names_the_rule_that_fired(tmp_path):
    r, [rec] = _run_script(
        tmp_path,
        "bash-guardrail.py",
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
    )
    assert rec["rule"] == rec["pattern"] and rec["rule"]


def test_the_read_only_guard_denies_with_its_code(tmp_path):
    cfg = tmp_path / "read-only-config.json"
    cfg.write_text(
        json.dumps(
            {"enabled": True, "blocked_patterns": ["*.py"], "allowed_patterns": []}
        )
    )
    r, records = _run_script(
        tmp_path,
        "read-only-guard.py",
        {"tool_name": "Write", "tool_input": {"file_path": "/home/developer/app.py"}},
        overrides={"_CONFIG_PATH": str(cfg), "_LEGACY_CONFIG_PATH": str(cfg)},
    )
    assert r.returncode == 2, r.stderr
    [rec] = records
    assert (rec["code"], rec["kind"], rec["hook"]) == (
        "read_only_path",
        "deny",
        "read_only_guard",
    )


# ---- turn-tier: codes, and the turn's kind under its own name -----------------------------------


@pytest.fixture
def turn_tier(lib, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setitem(
        sys.modules,
        "file_path_policy",
        _load("file_path_policy", HOOKS / "file_path_policy.py"),
    )
    monkeypatch.setattr(sys, "argv", [str(HOOKS / "turn-tier-guard.py")])
    return _load("_turn_tier_guard_165", HOOKS / "turn-tier-guard.py")


def _turn(hook, monkeypatch, tool, tool_input, *, tier, shell=False, kind=None):
    monkeypatch.setattr(
        hook, "read_stdin_json", lambda: {"tool_name": tool, "tool_input": tool_input}
    )
    monkeypatch.setattr(
        hook,
        "read_environ",
        lambda pid: (
            {"TRINITY_EXECUTION_ID": "exec-1"}
            if pid != 1
            else {"AGENT_NAME": "agent-a", "TRINITY_MCP_API_KEY": "k"}
        ),
    )
    # (tier, shell, kind, schedule): trinity#4281 B added the schedule detail.
    monkeypatch.setattr(hook, "ask_turn", lambda c, a, r: (tier, shell, kind, None))
    monkeypatch.setattr(hook, "ask_tier", lambda c, a, r: tier)
    # The image's /home/developer, whatever the host does with /home (macOS
    # resolves it under /System/Volumes/Data).
    monkeypatch.setattr(hook, "resolve", os.path.normpath)
    with pytest.raises(SystemExit) as exc:
        hook.run_hook(hook.main)
    return exc.value.code


@pytest.mark.parametrize(
    "tool, tool_input, tier, kind, code",
    [
        ("Bash", {"command": "ls"}, "share", "person", "shell_owner_only"),
        ("Bash", {"command": "ls"}, "share", "visitor", "shell_visitor_off"),
        ("mcp__filesystem__read_file", {}, "share", "person", "mcp_owner_only"),
        ("mcp__filesystem__read_file", {}, "share", "visitor", "mcp_visitor_off"),
        ("Bash", {"command": "ls"}, "unreachable", None, "tier_unreachable"),
        (
            "Write",
            {"file_path": "/home/developer/CLAUDE.md"},
            "share",
            None,
            "owner_only_path",
        ),
        (
            "Write",
            {"file_path": "/home/developer/CLAUDE.md"},
            "unreachable",
            None,
            "tier_unreachable",
        ),
        ("Grep", {"pattern": "x"}, "share", None, "owner_only_search"),
    ],
)
def test_turn_tier_denies_with_its_code(
    turn_tier, lib, monkeypatch, tool, tool_input, tier, kind, code
):
    assert _turn(turn_tier, monkeypatch, tool, tool_input, tier=tier, kind=kind) == 2
    [rec] = _records(lib.LOG_PATH)
    assert (rec["code"], rec["kind"], rec["hook"]) == (code, "deny", "turn_tier_guard")


@pytest.mark.parametrize("turn_kind", ["person", "visitor"])
def test_turn_tier_records_the_turn_kind_as_turn_kind(
    turn_tier, lib, monkeypatch, turn_kind
):
    """ent#165 reserves `kind` for the event kind (trinity#4281 review R8)."""
    _turn(
        turn_tier, monkeypatch, "Bash", {"command": "ls"}, tier="share", kind=turn_kind
    )
    [rec] = _records(lib.LOG_PATH)
    assert rec["turn_kind"] == turn_kind and rec["kind"] == "deny"


# ---- the skill gate --------------------------------------------------------------------------


@pytest.fixture
def skill_gate(lib, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    return _load("_skill_gate_165", HOOKS / "_skill_gate.py")


def test_a_skill_refusal_carries_its_code(skill_gate, lib, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [str(HOOKS / "skill-gate.py")])
    call = skill_gate.Call("skill_tool", "pay-invoice", ["pay-invoice"], True, None)
    out = skill_gate._verdict(
        200, json.dumps({"allowed": False, "message": "no"}).encode(), call
    )
    assert out.fields["code"] == "skill_refused"
    lib.log_event(out.event, **out.fields)
    [rec] = _records(lib.LOG_PATH)
    assert (rec["kind"], rec["hook"], rec["code"]) == (
        "deny",
        "skill_gate",
        "skill_refused",
    )


@pytest.mark.parametrize("marker, kind", [(True, "deny"), (False, None)])
def test_a_skill_check_with_no_verdict_is_a_deny_only_when_it_refused(
    skill_gate, lib, monkeypatch, tmp_path, marker, kind
):
    marker_path = tmp_path / "skill-gates-active"
    if marker:
        marker_path.write_text("1\n")
    monkeypatch.setattr(skill_gate, "MARKER", str(marker_path))
    call = skill_gate.Call("skill_tool", "pay-invoice", ["pay-invoice"], True, None)
    out = skill_gate._verdict(503, b"{}", call)
    assert out.fields["code"] == "skill_check_unreachable"
    lib.log_event(out.event, **out.fields)
    assert _records(lib.LOG_PATH)[0]["kind"] == kind


def test_the_skill_gate_log_comment_no_longer_claims_vector():
    src = (HOOKS / "_skill_gate.py").read_text()
    assert "(Vector)" not in src


# ---- the image smoke checks the record too ------------------------------------------------------


def _load_smoke():
    return _load("guard002_smoke_165", HOOKS / "guard002-smoke.py")


def test_the_image_smoke_fails_a_deny_record_without_the_contract(tmp_path):
    smoke = _load_smoke()
    smoke.LOG_PATH = str(tmp_path / "guardrails.jsonl")
    Path(smoke.LOG_PATH).write_text(
        json.dumps({"ts": 1.0, "event": "deny", "reason": "x"}) + "\n"
    )
    failures = []
    smoke._check_deny_record(failures)
    assert len(failures) == 1 and "v/event_id/kind" in failures[0]


def test_the_image_smoke_passes_a_real_deny_record(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(HOOKS / "bash-guardrail.py")])
    with pytest.raises(SystemExit):
        lib.deny("x", tool="Bash", code="bash_deny_pattern")
    smoke = _load_smoke()
    smoke.LOG_PATH = lib.LOG_PATH
    failures = []
    smoke._check_deny_record(failures)
    assert failures == []


@pytest.mark.parametrize("missing", ["v", "event_id", "kind", "hook", "code"])
def test_the_image_smoke_requires_each_contract_field(lib, tmp_path, monkeypatch, missing):
    monkeypatch.setattr(sys, "argv", [str(HOOKS / "bash-guardrail.py")])
    with pytest.raises(SystemExit):
        lib.deny("x", tool="Bash", code="bash_deny_pattern")
    [rec] = _records(lib.LOG_PATH)
    del rec[missing]
    smoke = _load_smoke()
    smoke.LOG_PATH = str(tmp_path / "one-record.jsonl")
    Path(smoke.LOG_PATH).write_text(json.dumps(rec) + "\n")
    failures = []
    smoke._check_deny_record(failures)
    assert len(failures) == 1


def test_the_image_smoke_checks_the_record_after_the_bash_rows():
    """The check is only proof at image build if the smoke's main() calls it."""
    tree = ast.parse((HOOKS / "guard002-smoke.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    branches = [
        n for n in ast.walk(main)
        if isinstance(n, ast.If) and "'bash-guardrail.py'" in ast.unparse(n.test)
    ]
    assert len(branches) == 1
    calls = [ast.unparse(s) for s in branches[0].body]
    assert "_check_deny_record(failures)" in calls
    assert calls.index("_check_gated_rows(argv, failures)") < calls.index(
        "_check_deny_record(failures)"
    )


def test_the_fake_key_in_a_command_stays_only_in_the_local_spool(tmp_path):
    """The local record keeps `command_prefix` for debugging (unchanged); the
    forwarder's projection is what keeps it in the container
    (test_ent165_guardrail_forwarder.py)."""
    r, [rec] = _run_script(
        tmp_path,
        "bash-guardrail.py",
        {"tool_name": "Bash", "tool_input": {"command": f"rm -rf / {FAKE_KEY}"}},
    )
    assert FAKE_KEY[:40] in rec["command_prefix"]
