"""GUARD-004: the Bash output scanner keeps credentials from the model
(trinity-enterprise#165, slice S3).

`output-scanner.py` used to log the names of the shapes it matched and exit 0,
so the model read every key a command printed. It now:

  * returns `hookSpecificOutput.updatedToolOutput` on `PostToolUse` — a copy of
    `tool_response` with only the matched spans replaced by `[REDACTED:<shape>]`,
    every key, boolean and number kept, every string leaf scanned;
  * withholds the result (never exits 2) when the scan raises, cannot load its
    shapes, or meets output over the scan cap;
  * redacts the file a large result is persisted to (`persistedOutputPath`);
  * on `PostToolUseFailure` (a command that exits non-zero, whose output no hook
    can rewrite) tells the model what it holds and records the event.

The shapes are the known-prefix partition of `SECRET_VALUE_PATTERNS` (D4),
each with a left boundary and the Slack forms with a length floor, so
legitimate output that only LOOKS like a prefix stays byte-identical.

Every hook here runs as the managed file registers it (`-I -S`, `env -i`), with
`lib`'s config and log paths pointed at test files (`_ent787_hook_harness`).
CI does not build the image; the batch verify checks the rewrite reaches the
model in a real agent on the pinned CLI.
Related flow: docs/memory/feature-flows/agent-guardrails.md
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _ent787_hook_harness as H  # noqa: E402

pytestmark = pytest.mark.unit

_SCANNER = "output-scanner.py"
_SECRET = "sk-ant-api03-" + "A" * 93
_GITHUB = "ghp_" + "b" * 36
_GOOGLE = "AIza" + "C" * 35
# The left boundary every model-facing shape gets: not after a letter, digit
# or `-`, or after an escape that ends in one (a literal `\n`/`\r`/`\t`, a
# `%XX` URL escape, an ANSI escape sequence, raw or written as an escape); `_`
# counts as a separator. The escape is consumed as `lead` and
# written back unchanged.
_BOUNDARY = (r"(?:(?<![A-Za-z0-9-])|(?<=\\[nrt])|(?<=%[0-9A-Fa-f]{2})"
             r"|(?P<lead>(?:\x1b|\\(?:u001[bB]|x1[bB]|033|e))(?:\[[0-?]*[ -/]*[@-~]|[ -/]*[0-~])))")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, H.REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BACKEND_CS = "src/backend/utils/credential_sanitizer.py"
_AGENT_CS = "docker/base-image/agent_server/utils/credential_sanitizer.py"


def _bash(stdout="", stderr="", **extra) -> dict:
    response = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False,
                "noOutputExpected": False, **extra}
    return {"session_id": "s-165", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "cat x"}, "tool_use_id": "toolu_165",
            "tool_response": response}


def _failure(error: str) -> dict:
    return {"session_id": "s-165", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "make test"}, "tool_use_id": "toolu_165f",
            "error": error, "is_interrupt": False}


def _scan(tmp_path, payload, overrides=None, baseline=None) -> subprocess.CompletedProcess:
    """Run the scanner exactly as registered; ``baseline`` swaps the config file."""
    if baseline is None:
        return H.run_hook(_SCANNER, payload, flags=H.registered_flags(_SCANNER),
                          env=H.registered_env(_SCANNER), tmp=tmp_path, overrides=overrides)
    cfg = tmp_path / "baseline.json"
    cfg.write_text(json.dumps(baseline))
    wrapper = H._WRAPPER.format(
        hooks=str(H.HOOKS), runtime=str(tmp_path / "no-runtime.json"), baseline=str(cfg),
        log=str(tmp_path / "guardrails.jsonl"), script=str(H.HOOKS / _SCANNER),
        over=json.dumps(overrides or {}))
    return subprocess.run([H.PYTHON, *H.registered_flags(_SCANNER), "-c", wrapper],
                          input=json.dumps(payload), env=H.registered_env(_SCANNER),
                          capture_output=True, text=True, timeout=60)


def _rewrite(result) -> dict:
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


def _events(tmp_path, name):
    return [e for e in H.log_events(tmp_path) if e["event"] == name]


# --- redaction on a successful call -----------------------------------------

def test_a_key_on_stdout_is_redacted_and_the_structure_is_kept(tmp_path):
    payload = _bash(stdout=f"export KEY={_SECRET}\nok\n")
    result = _scan(tmp_path, payload)
    assert _SECRET not in result.stdout
    rewritten = _rewrite(result)
    assert rewritten == {**payload["tool_response"], "stdout": "export KEY=[REDACTED:sk]\nok\n"}
    events = _events(tmp_path, "credential_redacted")
    assert [e["patterns"] for e in events] == [["sk"]]
    assert events[0]["tool"] == "Bash" and events[0]["persisted"] is False
    assert _SECRET not in (tmp_path / "guardrails.jsonl").read_text()


def test_a_key_on_stderr_is_redacted(tmp_path):
    payload = _bash(stdout="done", stderr=f"warning: token {_GITHUB} expires soon")
    rewritten = _rewrite(_scan(tmp_path, payload))
    assert rewritten["stdout"] == "done"
    assert rewritten["stderr"] == "warning: token [REDACTED:github] expires soon"


def test_every_string_leaf_is_scanned_including_lists(tmp_path):
    """The pinned Bash schema carries `structuredContent`, a list of blocks the
    model is shown INSTEAD of stdout when it is non-empty."""
    blocks = [{"type": "text", "text": f"key {_GOOGLE}"}, {"type": "text", "text": "plain"}]
    rewritten = _rewrite(_scan(tmp_path, _bash(structuredContent=blocks)))
    assert rewritten["structuredContent"] == [
        {"type": "text", "text": "key [REDACTED:google]"}, {"type": "text", "text": "plain"}]


def test_a_failed_bash_call_string_response_is_rewritten_as_a_bash_object(tmp_path):
    """Observed on CLI 2.1.281 in the agent image: a failed Bash call also fires
    PostToolUse with `tool_response` the error STRING. A string rewrite fails
    the Bash schema and the whole result becomes a tool_use_error; this object
    shape was accepted and the model saw the redacted text."""
    payload = {**_bash(), "tool_response": f"Error: Exit code 3\nfailing key: {_SECRET}"}
    assert _rewrite(_scan(tmp_path, payload)) == {
        "stdout": "Error: Exit code 3\nfailing key: [REDACTED:sk]", "stderr": "", "interrupted": False}


def test_a_string_response_from_another_tool_stays_a_string(tmp_path):
    payload = {**_bash(), "tool_name": "mcp__owner__fetch", "tool_response": f"key={_SECRET}"}
    assert _rewrite(_scan(tmp_path, payload)) == "key=[REDACTED:sk]"


def test_a_withheld_failed_bash_call_is_a_bash_object(tmp_path):
    payload = {**_bash(), "tool_response": "Error: Exit code 1\n" + "z" * 50}
    rewritten = _rewrite(_scan(tmp_path, payload, overrides={"SCAN_CAP_CHARS": 10}))
    assert rewritten == {"stdout": rewritten["stdout"], "stderr": "", "interrupted": False}
    assert rewritten["stdout"].startswith(_TOO_LARGE)


@pytest.mark.parametrize("payload", [
    _bash(stdout="all tests passed\n"),
    _bash(),
    {**_bash(), "tool_response": None},
    {**_bash(), "tool_response": ""},
], ids=["clean", "empty", "none", "empty-string"])
def test_clean_output_prints_nothing_and_logs_nothing(tmp_path, payload):
    result = _scan(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert H.log_events(tmp_path) == []


# Legitimate output that carries a credential PREFIX but no credential. The
# untightened shapes damage the first four (the precondition test below).
BENIGN = [
    "pod/task-runner-deployment-7d9f8c6b5-x2z9q   1/1   Running   0   3d",
    "Successfully installed flask-sqlalchemy-utilities-extra-3.1.0",
    "/dev/nvme0n1p1 disk-usage-report-for-volume-mounted-at-data 87%",
    "* feature/ent165-mask-credentials-in-output-scanner-for-bash",
    "SLACK_BOT_TOKEN=xoxb-test",
    "SLACK_BOT_TOKEN=xoxb-your-token-here",
    "OPENAI_API_KEY=sk-your-openai-key\nGITHUB_TOKEN=ghp_your_token_here\nANTHROPIC_API_KEY=sk-ant-your-key-here",
    "Download: https://trinity.example.com/api/files/9f1c2e/report.pdf"
    "?sig=Xq3vNw8pLr2TzYb6KdHj0MfA5sUeCo1gWi7RnQ4lVyE",
    "The redaction placeholder looks like [REDACTED:sk]; logs use ***REDACTED***.",
    "AWS_REGION=eu-west-1 AKIA_ROTATION_DAYS=90",
    # The escape and `_` left contexts in front of the same prefix-only words.
    "\x1b[32mpod/task-runner-deployment-7d9f8c6b5-x2z9q\x1b[0m   1/1   Running",
    "\x1b[01;31m\x1b[Kxoxb-test\x1b[m\x1b[K and KEY_xoxb-your-token-here",
    '{"env": "A=1\\nOPENAI_API_KEY=sk-your-openai-key\\tKEY_sk-your-openai-key"}',
    "https://x.example/cb?next%3Dtask-runner-deployment-7d9f8c6b5-x2z9q&k%3Dsk-your-openai-key",
    # Identifiers that start with the Trinity key prefix are code.
    "def test_trinity_mcp_injection_writes_the_opt_out(tmp_path):",
    "def test_trinity_mcp_key_is_injected_into_the_agent_environment_at_start(tmp_path):",
    "def test_trinity_mcp_API_key_is_injected_into_the_agent_environment_at_start(tmp_path):",
]
_MCP_IDENTIFIERS = BENIGN[-3:]


@pytest.mark.parametrize("text", BENIGN)
def test_benign_output_is_byte_identical(tmp_path, text):
    result = _scan(tmp_path, _bash(stdout=text, stderr=text))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"rewrote benign output: {result.stdout[:300]}"


def test_the_untightened_shapes_damage_that_corpus():
    """Precondition: the corpus is not vacuous. The sanitizer's own `sk-` and
    Slack forms (no left boundary, no floor) do match these lines."""
    cs = _load(_BACKEND_CS, "_cs165_backend")
    damaged = [t for t in BENIGN[:6]
               if any(re.search(p, t) for p in cs.SECRET_VALUE_PATTERNS
                      if p.startswith(("sk-", "xox")))]
    assert damaged == BENIGN[:6]


def test_a_key_after_a_word_character_is_not_a_key_but_after_punctuation_it_is(tmp_path):
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=f"ask-{_SECRET[3:]} '{_SECRET}' ={_SECRET}")))
    assert rewritten["stdout"] == f"ask-{_SECRET[3:]} '[REDACTED:sk]' =[REDACTED:sk]"


def test_image_stdout_is_not_rewritten(tmp_path):
    """`isImage` stdout is image data the model sees as an image; a rewrite of
    the base64 would only break it."""
    payload = _bash(stdout="iVBORw0KGgo+" + _SECRET, isImage=True)
    result = _scan(tmp_path, payload)
    assert result.returncode == 0 and result.stdout == ""


# --- withhold: scanner error and oversize ------------------------------------

_WITHHELD = "[Output withheld: Trinity's credential check failed."
_TOO_LARGE = "[Output withheld: the output is too large for Trinity's credential check."


def _assert_withheld(rewritten, original, notice):
    assert rewritten["stdout"].startswith(notice)
    assert "do not run it again" in rewritten["stdout"]
    for key, value in original.items():
        if key == "stdout":
            continue
        if isinstance(value, str):
            assert rewritten[key] == "", key
        elif key == "structuredContent":
            assert rewritten[key] == []
        else:
            assert rewritten[key] == value, key


def test_a_scanner_exception_withholds_stdout_stderr_and_structured_content(tmp_path):
    """Forced through the placeholder format: the scan raises on the first hit."""
    payload = _bash(stdout=f"a {_SECRET}", stderr=f"b {_SECRET}", backgroundCwdHint="hint",
                    structuredContent=[{"type": "text", "text": _SECRET}])
    result = _scan(tmp_path, payload, overrides={"PLACEHOLDER": 0})
    assert result.returncode == 0, result.stderr   # never exit 2: that shows the original
    assert _SECRET not in result.stdout
    _assert_withheld(_rewrite(result), payload["tool_response"], _WITHHELD)
    assert _events(tmp_path, "scanner_error")


@pytest.mark.parametrize("baseline", [
    {},
    {"credential_boundary": _BOUNDARY, "credential_patterns": []},
    {"credential_boundary": _BOUNDARY, "credential_patterns": [{"name": "sk", "pattern": "sk-[A-Z"}]},
    {"credential_boundary": _BOUNDARY, "credential_patterns": [{"name": "Not A Name", "pattern": "x"}]},
    {"credential_patterns": [{"name": "sk", "pattern": "sk-[A-Za-z0-9]{20,}"}]},
    {"credential_boundary": "(?<!", "credential_patterns": [{"name": "sk", "pattern": "sk-[A-Za-z0-9]{20,}"}]},
], ids=["no-key", "empty", "bad-regex", "bad-name", "no-boundary", "bad-boundary"])
def test_shapes_that_cannot_be_loaded_withhold(tmp_path, baseline):
    payload = _bash(stdout=f"k={_SECRET}")
    result = _scan(tmp_path, payload, baseline=baseline)
    assert _SECRET not in result.stdout
    _assert_withheld(_rewrite(result), payload["tool_response"], _WITHHELD)
    assert _events(tmp_path, "scanner_error")


def test_unreadable_input_withholds_in_the_bash_shape(tmp_path):
    wrapper = H._WRAPPER.format(
        hooks=str(H.HOOKS), runtime=str(tmp_path / "x.json"), baseline=str(H.BASELINE),
        log=str(tmp_path / "guardrails.jsonl"), script=str(H.HOOKS / _SCANNER), over="{}")
    result = subprocess.run([H.PYTHON, *H.registered_flags(_SCANNER), "-c", wrapper],
                            input="{not json", env=H.registered_env(_SCANNER),
                            capture_output=True, text=True, timeout=30)
    rewritten = _rewrite(result)
    assert rewritten["stdout"].startswith(_WITHHELD)
    assert rewritten["stderr"] == "" and rewritten["interrupted"] is False


def test_output_over_the_cap_is_withheld(tmp_path):
    payload = _bash(stdout="x" * 600, stderr="y" * 600)
    result = _scan(tmp_path, payload, overrides={"SCAN_CAP_CHARS": 1000})
    _assert_withheld(_rewrite(result), payload["tool_response"], _TOO_LARGE)
    assert _events(tmp_path, "scanner_oversize")


def test_eight_mib_is_scanned_well_inside_the_hook_timeout(tmp_path):
    """The registered timeout is 30 s and a timed-out hook passes the original."""
    filler = ("2026-11-08T10:00:00Z INFO task-runner-deployment ok id=7d9f8c6b5 " * 140_000)
    stdout = filler[: 8 * 1024 * 1024 - 200] + " " + _SECRET
    started = time.monotonic()
    result = _scan(tmp_path, _bash(stdout=stdout))
    elapsed = time.monotonic() - started
    rewritten = _rewrite(result)
    assert rewritten["stdout"].endswith(" [REDACTED:sk]") and _SECRET not in result.stdout
    assert elapsed < 10, f"8 MiB took {elapsed:.1f}s"


def test_the_default_cap_is_eight_mib(tmp_path):
    payload = _bash(stdout="z" * (8 * 1024 * 1024 + 1))
    _assert_withheld(_rewrite(_scan(tmp_path, payload)), payload["tool_response"], _TOO_LARGE)


# --- persisted large output ---------------------------------------------------

def _persisted(tmp_path, body: bytes, name="b7x.txt") -> Path:
    path = tmp_path / "home" / ".claude" / "projects" / "-home-developer" / "s-165" / "tool-results" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    return path


def _root(tmp_path):
    return {"TOOL_RESULTS_ROOT": str(tmp_path / "home")}


def test_a_key_past_the_inline_window_is_redacted_in_the_persisted_file(tmp_path):
    head = ("line of build output\n" * 2000).encode()
    body = head + f"export KEY={_SECRET}\n".encode() + b"tail\xff\xfe\n"
    path = _persisted(tmp_path, body)
    payload = _bash(stdout=head[:30000].decode(), persistedOutputPath=str(path),
                    persistedOutputSize=len(body))
    result = _scan(tmp_path, payload, overrides=_root(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""            # the inline part was clean
    assert path.read_bytes() == head + b"export KEY=[REDACTED:sk]\n" + b"tail\xff\xfe\n"
    events = _events(tmp_path, "credential_redacted")
    assert events and events[0]["persisted"] is True and events[0]["patterns"] == ["sk"]


def test_a_clean_persisted_file_is_not_touched(tmp_path):
    path = _persisted(tmp_path, b"clean\n" * 10000)
    before = os.stat(path)
    result = _scan(tmp_path, _bash(stdout="clean\n", persistedOutputPath=str(path)),
                   overrides=_root(tmp_path))
    assert result.stdout == ""
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_persisted_file_over_the_cap_is_cut_to_the_notice(tmp_path):
    path = _persisted(tmp_path, b"q" * 5000 + _SECRET.encode())
    _scan(tmp_path, _bash(stdout="q" * 100, persistedOutputPath=str(path)),
          overrides={**_root(tmp_path), "PERSISTED_CAP_BYTES": 1000})
    assert path.read_text().startswith(_TOO_LARGE)
    assert _events(tmp_path, "scanner_oversize")


@pytest.mark.parametrize("where", ["outside-root", "not-tool-results", "symlink"])
def test_a_persisted_path_that_cannot_be_verified_withholds_and_is_not_touched(tmp_path, where):
    target = tmp_path / "elsewhere.txt"
    target.write_text(f"k={_SECRET}")
    if where == "outside-root":
        path = target
    elif where == "not-tool-results":
        path = tmp_path / "home" / "notes" / "x.txt"
        path.parent.mkdir(parents=True)
        path.write_text(f"k={_SECRET}")
    else:
        path = _persisted(tmp_path, b"", name="link.txt")
        path.unlink()
        path.symlink_to(target)
    original = path.read_text()
    payload = _bash(stdout="preview", persistedOutputPath=str(path))
    result = _scan(tmp_path, payload, overrides=_root(tmp_path))
    rewritten = _rewrite(result)
    assert rewritten["stdout"].startswith(_WITHHELD) and rewritten["persistedOutputPath"] == ""
    assert path.read_text() == original and target.read_text() == f"k={_SECRET}"


# --- failing commands (PostToolUseFailure) ------------------------------------

def test_a_failing_command_with_a_key_warns_the_model_and_records_it(tmp_path):
    result = _scan(tmp_path, _failure(f"Exit code 1\nAuthorization: token {_GITHUB}\nFAIL"))
    assert result.returncode == 0, result.stderr
    hso = json.loads(result.stdout)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    assert set(hso) == {"hookEventName", "additionalContext"}
    assert "held a credential (github)" in hso["additionalContext"]
    assert "do not repeat" in hso["additionalContext"].lower()
    assert _GITHUB not in result.stdout
    events = _events(tmp_path, "credential_unredacted")
    assert [e["patterns"] for e in events] == [["github"]]


def test_a_failing_command_without_a_key_prints_nothing(tmp_path):
    result = _scan(tmp_path, _failure("Exit code 2\nmake: *** [test] Error 2"))
    assert result.returncode == 0 and result.stdout == "" and H.log_events(tmp_path) == []


def test_a_failure_the_scanner_cannot_check_still_warns(tmp_path):
    result = _scan(tmp_path, _failure(f"Exit code 1\n{_SECRET}"), overrides={"SCAN_CAP_CHARS": 10})
    hso = json.loads(result.stdout)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure" and "could not check" in hso["additionalContext"]
    assert _SECRET not in result.stdout


# --- the shapes: one source, three classes -----------------------------------

# Named-and-tightened: the source form, then exactly how the hook tightens it
# beyond the left boundary every shape gets.
TIGHTENED = {
    r'xoxb-[a-zA-Z0-9\-]+': (r'xoxb-[a-zA-Z0-9\-]{20,}', "length floor 20"),
    r'xoxp-[a-zA-Z0-9\-]+': (r'xoxp-[a-zA-Z0-9\-]{20,}', "length floor 20"),
    r'xoxa-[a-zA-Z0-9\-]+': (r'xoxa-[a-zA-Z0-9\-]{20,}', "length floor 20"),
}
EXCLUDED = {r'Bearer\s+[a-zA-Z0-9\-_.]+', r'Basic\s+[a-zA-Z0-9+/=]+'}


def _scanner_shapes():
    return json.loads(H.BASELINE.read_text())["credential_patterns"]


def test_every_shape_shares_the_one_left_boundary():
    """The hook compiles `credential_boundary` once in front of all the shapes."""
    assert json.loads(H.BASELINE.read_text())["credential_boundary"] == _BOUNDARY


def test_every_sanitizer_shape_is_named_tightened_or_excluded():
    cs = _load(_BACKEND_CS, "_cs165_partition")
    shapes = _scanner_shapes()
    sources = [s["source"] for s in shapes]
    assert len(sources) == len(set(sources))
    assert set(sources) | EXCLUDED == set(cs.SECRET_VALUE_PATTERNS)
    assert not set(sources) & EXCLUDED
    for shape in shapes:
        if shape["source"] in TIGHTENED:
            body, how = TIGHTENED[shape["source"]]
            assert shape["pattern"] == body and shape["tightening"] == how, shape
        else:
            assert shape["pattern"] == shape["source"] and "tightening" not in shape, shape
        # One vocabulary with the Workspace composer guard (ent#1066).
        assert shape["name"] == cs._GUARD_SHAPES[shape["source"]], shape
        re.compile(shape["pattern"])


def test_the_google_key_shape_is_in_both_sanitizer_copies_and_redacted_from_logs():
    """D9: the scanner's `AIza` shape moves into `SECRET_VALUE_PATTERNS` (both
    copies, Invariant #5), so logs learn it in the same change."""
    for rel in (_BACKEND_CS, _AGENT_CS):
        cs = _load(rel, "_cs165_" + rel.replace("/", "_").replace(".", "_"))
        assert r'AIza[0-9A-Za-z_-]{35}' in cs.SECRET_VALUE_PATTERNS, rel
        assert _GOOGLE not in cs.sanitize_text(f"maps key {_GOOGLE} here"), rel
    backend = _load(_BACKEND_CS, "_cs165_shape")
    assert backend.secret_shape(f"use {_GOOGLE}") == "google"


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
def test_a_google_consent_link_still_survives_the_new_shape(rel):
    cs = _load(rel, "_cs165_consent_" + rel.replace("/", "_").replace(".", "_"))
    if hasattr(cs, "_credential_values"):
        cs._credential_values = set()
    url = ("https://accounts.google.com/o/oauth2/auth?access_type=offline"
           "&client_id=123-example.apps.googleusercontent.com"
           "&redirect_uri=http%3A%2F%2F127.0.0.1%3A34837%2Foauth2%2Fcallback&response_type=code"
           "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar&state=AIzaPublicState"
           "&prompt=consent&include_granted_scopes=true")
    assert cs.sanitize_text("Open " + url) == "Open " + url


# --- registration, D11 and the CLI pin ------------------------------------------

def _all_settings():
    files = [H.MANAGED, *sorted((H.HOOKS / "managed-settings.d").glob("*.json"))]
    return {p.name: json.loads(p.read_text()) for p in files}


def test_the_scanner_is_registered_on_the_failure_event_too():
    phases = H.registration()["hooks"]
    for phase in ("PostToolUse", "PostToolUseFailure"):
        entries = [h for e in phases[phase] if e["matcher"] == "Bash" for h in e["hooks"]]
        assert [h["args"][-1] for h in entries] == ["/opt/trinity/hooks/output-scanner.py"], phase


def test_allow_managed_hooks_only_stays_unset():
    """D11: left unset on purpose (setting it would switch off every template's
    own hooks). The docs say a workspace PostToolUse hook can undo a redaction;
    they stay true only while no managed file sets it."""
    def keys(value):
        if isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from keys(v)
        elif isinstance(value, list):
            for v in value:
                yield from keys(v)
    for name, settings in _all_settings().items():
        assert "allowManagedHooksOnly" not in set(keys(settings)), name
    assert "allowManagedHooksOnly" not in (H.BASE / "Dockerfile").read_text()


def _scanner_constant(name):
    for node in ast.parse((H.HOOKS / _SCANNER).read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined in {_SCANNER}")


def test_the_scanner_was_verified_against_the_cli_the_image_pins():
    pinned = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$",
                       (H.BASE / "Dockerfile").read_text(), re.M).group(1)
    verified = _scanner_constant("OUTPUT_SCANNER_CLI_VERIFIED")
    assert verified == pinned, (
        f"The image now pins Claude Code {pinned}, but the output scanner's CLI facts were read "
        f"from {verified}. Re-check every item in CLI_FACTS_TO_REAUDIT against {pinned} "
        f"(docker/base-image/hooks/output-scanner.py), then move OUTPUT_SCANNER_CLI_VERIFIED.")


def test_the_reaudit_list_names_what_to_check():
    facts = _scanner_constant("CLI_FACTS_TO_REAUDIT")
    text = " ".join(facts).lower()
    for topic in ("updatedtooloutput", "schema", "posttoolusefailure", "persistedoutputpath",
                  "structuredcontent", "last write wins", "exit 2"):
        assert topic in text, topic


# --- the write-back guard ------------------------------------------------------

def _guard(hook, tmp_path, payload):
    return H.run_hook(hook, payload, flags=H.registered_flags(hook),
                      env=H.registered_env(hook), tmp=tmp_path)


@pytest.mark.parametrize("tool_input", [
    {"tool": "Write", "file_path": "/home/developer/app/.envrc.local", "content": "K=[REDACTED:sk]\n"},
    {"tool": "Edit", "file_path": "/home/developer/cfg.yaml", "old_string": "key: x",
     "new_string": "key: [REDACTED:github]"},
    {"tool": "MultiEdit", "file_path": "/home/developer/cfg.yaml",
     "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "[REDACTED:aws]"}]},
    {"tool": "NotebookEdit", "notebook_path": "/home/developer/n.ipynb", "new_source": "k='[REDACTED:sk]'"},
], ids=["write", "edit", "multiedit", "notebookedit"])
def test_writing_the_placeholder_into_a_file_is_refused(tmp_path, tool_input):
    tool = tool_input.pop("tool")
    result = _guard("file-guardrail.py", tmp_path, {"tool_name": tool, "tool_input": tool_input})
    assert result.returncode == 2, result.stderr
    assert "[REDACTED:" in result.stderr
    denies = [e for e in H.log_events(tmp_path) if e["event"] == "deny"]
    assert [e.get("code") for e in denies] == ["redaction_writeback_refused"]


@pytest.mark.parametrize("tool_input", [
    {"tool": "Write", "file_path": "/home/developer/notes.md", "content": "logs use ***REDACTED***"},
    {"tool": "Edit", "file_path": "/home/developer/cfg.yaml", "old_string": "key: [REDACTED:sk]",
     "new_string": "key: ${API_KEY}"},
], ids=["log-placeholder", "removing-it"])
def test_ordinary_writes_and_removing_the_placeholder_pass(tmp_path, tool_input):
    tool = tool_input.pop("tool")
    result = _guard("file-guardrail.py", tmp_path, {"tool_name": tool, "tool_input": tool_input})
    assert result.returncode == 0, result.stderr


def test_a_command_carrying_the_placeholder_is_refused(tmp_path):
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "echo 'OPENAI_API_KEY=[REDACTED:sk]' >> .env.local"}}
    result = _guard("bash-guardrail.py", tmp_path, payload)
    assert result.returncode == 2, result.stderr
    denies = [e for e in H.log_events(tmp_path) if e["event"] == "deny"]
    assert [e.get("code") for e in denies] == ["redaction_writeback_refused"]


def test_an_ordinary_command_still_runs(tmp_path):
    result = _guard("bash-guardrail.py", tmp_path,
                    {"tool_name": "Bash", "tool_input": {"command": "grep -c REDACTED app.log"}})
    assert result.returncode == 0, result.stderr


# --- the image-build smoke checks the rewrite, not only the exit code ----------

def _smoke(tmp_path):
    smoke = _load("docker/base-image/hooks/guard002-smoke.py", "_guard002_smoke_165")
    smoke.SMOKE_HOME = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    return smoke


def _hook_argv(tmp_path, script: Path) -> list:
    wrapper = H._WRAPPER.format(
        hooks=str(H.HOOKS), runtime=str(tmp_path / "no-runtime.json"), baseline=str(H.BASELINE),
        log=str(tmp_path / "guardrails.jsonl"), script=str(script),
        over=json.dumps({"TOOL_RESULTS_ROOT": str(tmp_path / "home")}))
    return [H.PYTHON, *H.registered_flags(_SCANNER), "-c", wrapper]


def test_the_build_smoke_passes_the_shipped_scanner(tmp_path):
    failures = []
    _smoke(tmp_path)._check_output_scanner(_hook_argv(tmp_path, H.HOOKS / _SCANNER), failures)
    assert failures == []
    assert list((tmp_path / "home").iterdir()) == []   # the smoke cleans up after itself


def test_the_build_smoke_fails_a_log_only_scanner(tmp_path):
    """The old scanner exited 0 and printed nothing; the old smoke checked only
    that, so a scanner that hid nothing passed the build."""
    log_only = tmp_path / "log-only.py"
    log_only.write_text("def main():\n    import sys; sys.stdin.read()\n")
    failures = []
    _smoke(tmp_path)._check_output_scanner(_hook_argv(tmp_path, log_only), failures)
    assert failures, "a scanner that hides nothing passed the smoke"


# --- a Trinity MCP key is recognised whole ------------------------------------
# Keys are `trinity_mcp_` + `secrets.token_urlsafe(32)`: 43 base64url
# characters, so about 40% hold a `-` or `_`. The old body `[a-zA-Z0-9]{16,}`
# missed a key with one in its first 16 characters and stopped at the first one
# after that, leaving the key's tail in clear (an agent's own key reached the
# model on a read of its MCP config).

_MCP_MINT = "src/backend/db/mcp_keys.py"
_MCP_SOURCE = r'trinity_mcp_(?:[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])|[a-zA-Z0-9]{16,})'


def _minted_mcp_keys(count=2000, seed=165):
    """Keys built the way the backend mints them (`token_urlsafe(32)` is the
    unpadded base64url of 32 random bytes), from a seeded generator so a
    failure names a reproducible key."""
    rng = random.Random(seed)
    return ["trinity_mcp_" + base64.urlsafe_b64encode(rng.randbytes(32)).rstrip(b"=").decode()
            for _ in range(count)]


def test_the_keys_under_test_are_minted_like_the_backend_mints_them():
    assert 'f"trinity_mcp_{secrets.token_urlsafe(32)}"' in (H.REPO / _MCP_MINT).read_text()
    keys = _minted_mcp_keys()
    assert {len(k) for k in keys} == {12 + 43}
    old = re.compile(r'trinity_mcp_[a-zA-Z0-9]{16,}')
    missed = [k for k in keys if not old.search(k)]
    cut = [k for k in keys if old.search(k) and old.search(k).group() != k]
    # Not vacuous: the sample holds both failure kinds the old body had.
    assert len(missed) > 300 and len(cut) > 300


# Valid mint outputs (each body is the base64url of exactly 32 bytes) with no
# uppercase letter, and an older alnum form longer than 43 characters: a rule
# that keys on character classes or stops at 43 leaves these in clear.
_MCP_EDGE_KEYS = [
    "trinity_mcp_" + "a_" * 21 + "g",
    "trinity_mcp_" + "a" * 20 + "_" + "b" * 21 + "g",
    "trinity_mcp_" + "-" * 42 + "w",
    "trinity_mcp_" + "A" * 64,
]


def test_the_edge_keys_are_what_they_claim():
    for key in _MCP_EDGE_KEYS[:3]:
        body = key[len("trinity_mcp_"):]
        assert base64.urlsafe_b64encode(base64.urlsafe_b64decode(body + "=")).rstrip(b"=").decode() == body
        assert len(base64.urlsafe_b64decode(body + "=")) == 32


@pytest.mark.parametrize("key", _MCP_EDGE_KEYS, ids=["no-upper", "no-upper-underscore", "dashes", "alnum-64"])
def test_an_mcp_key_of_any_characters_or_the_older_length_is_redacted_whole(tmp_path, key):
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=f'"Bearer {key}"')))
    assert rewritten["stdout"] == '"Bearer [REDACTED:trinity_mcp]"'
    for rel in (_BACKEND_CS, _AGENT_CS):
        cs = _load(rel, "_cs165_r1_edge_" + rel.replace("/", "_").replace(".", "_"))
        assert cs.sanitize_text(f"key {key} end") == f"key {cs.REDACTION_PLACEHOLDER} end", rel


def test_every_minted_mcp_key_is_redacted_whole_by_the_scanner(tmp_path):
    keys = _minted_mcp_keys()
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout="\n".join(f"key {k} end" for k in keys))))
    assert rewritten["stdout"] == "\n".join("key [REDACTED:trinity_mcp] end" for _ in keys)


def test_the_agent_mcp_config_read_back_hides_its_own_key(tmp_path):
    """The case as it was found: `cat ~/.mcp.json` printed the agent's own key."""
    key = "trinity_mcp_Df81fwt_" + "Q" * 35
    config = json.dumps({"mcpServers": {"trinity": {"type": "http", "url": "http://mcp-server:8080/mcp",
                                                     "headers": {"Authorization": f"Bearer {key}"}}}})
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=config)))
    assert rewritten["stdout"] == config.replace(key, "[REDACTED:trinity_mcp]")


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
def test_every_minted_mcp_key_is_redacted_whole_from_logs(rel):
    cs = _load(rel, "_cs165_r1_" + rel.replace("/", "_").replace(".", "_"))
    keys = _minted_mcp_keys()
    assert cs.sanitize_text(" | ".join(keys)) == " | ".join([cs.REDACTION_PLACEHOLDER] * len(keys))
    assert _MCP_SOURCE in cs.SECRET_VALUE_PATTERNS, rel


def test_the_composer_guard_knows_every_minted_mcp_key():
    cs = _load(_BACKEND_CS, "_cs165_r1_guard")
    assert [k for k in _minted_mcp_keys() if cs.secret_shape(f"here: {k}") is None] == []
    assert cs._GUARD_SHAPES[_MCP_SOURCE] == "trinity_mcp"


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
def test_a_key_that_holds_another_shape_is_redacted_whole_from_logs(rel):
    """`sk-` inside a key's random body: the `sk` rule ran first and the MCP rule
    then saw too short a head, which stayed in clear."""
    cs = _load(rel, "_cs165_r1_nested_" + rel.replace("/", "_").replace(".", "_"))
    key = "trinity_mcp_ab" + "sk-" + "Q" * 38
    assert cs.sanitize_text(f"key {key} end") == f"key {cs.REDACTION_PLACEHOLDER} end"


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
def test_identifiers_that_start_with_the_mcp_prefix_stay_in_logs(rel):
    cs = _load(rel, "_cs165_r1_ident_" + rel.replace("/", "_").replace(".", "_"))
    for line in _MCP_IDENTIFIERS:
        assert cs.sanitize_text(line) == line, line


# --- a key after an escape or an underscore is still a key --------------------
# These reached the model unchanged: the character before
# the key was the last letter of an escape, or `_`.

_CSI_GREP = "\x1b[01;31m\x1b[K"
_CSI_END = "\x1b[m\x1b[K"


@pytest.mark.parametrize("before, after", [
    (_CSI_GREP + _SECRET + _CSI_END, _CSI_GREP + "[REDACTED:sk]" + _CSI_END),
    ("config.py:3:KEY = \x1b[0m\x1b[1m\x1b[31m" + _SECRET + "\x1b[0m",
     "config.py:3:KEY = \x1b[0m\x1b[1m\x1b[31m[REDACTED:sk]\x1b[0m"),
    ('{"keys": "first\\n' + _SECRET + '"}', '{"keys": "first\\n[REDACTED:sk]"}'),
    ("a\\t" + _GITHUB, "a\\t[REDACTED:github]"),
    ("https://x.example/cb?token%3D" + _SECRET, "https://x.example/cb?token%3D[REDACTED:sk]"),
    ("Authorization%3A%20Bearer%20" + _GITHUB, "Authorization%3A%20Bearer%20[REDACTED:github]"),
    ("KEY_" + _SECRET, "KEY_[REDACTED:sk]"),
    ("prefix_" + _GITHUB, "prefix_[REDACTED:github]"),
    ('"line": "\\u001b[31m' + _GOOGLE + '\\u001b[0m"', '"line": "\\u001b[31m[REDACTED:google]\\u001b[0m"'),
    ("\x1b[1;32m" + "trinity_mcp_Df81fwt_" + "Q" * 35, "\x1b[1;32m[REDACTED:trinity_mcp]"),
    ("\x1b(B" + _GITHUB + "\x1b(B\x1b[m", "\x1b(B[REDACTED:github]\x1b(B\x1b[m"),
    ("\x1b7" + _SECRET + "\x1b8", "\x1b7[REDACTED:sk]\x1b8"),
], ids=["grep-color", "rg-color", "json-newline", "json-tab", "url-eq", "url-space",
        "underscore-key", "underscore-ghp", "json-escaped-csi", "csi-mcp", "charset-escape", "cursor-save"])
def test_a_key_after_an_escape_or_an_underscore_is_redacted_and_the_escape_kept(tmp_path, before, after):
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=before, stderr=before)))
    assert rewritten["stdout"] == after and rewritten["stderr"] == after


@pytest.mark.parametrize("key, name", [
    ("trinity_mcp_ab_" + "sk-" + "Q" * 37, "trinity_mcp"),
    ("AIza" + "Q" * 10 + "_sk-" + "R" * 21, "google"),
], ids=["sk-in-mcp", "sk-in-google"])
def test_a_key_whose_body_holds_another_shape_after_an_underscore_is_redacted_whole(tmp_path, key, name):
    """With `_` a boundary, a shape can start inside another key's random body.
    Each shape matches on the original text and overlapping matches become one
    placeholder, so the outer key is not cut short whichever shape runs first."""
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=f"key {key} end")))
    assert rewritten["stdout"] == f"key [REDACTED:{name}] end"


def test_a_key_failing_with_an_escape_in_front_still_warns(tmp_path):
    result = _scan(tmp_path, _failure("Error: Exit code 1\n" + _CSI_GREP + _SECRET + _CSI_END))
    assert "credential" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("unit", [
    "\x1b[01;31m\x1b[Ktask-runner\x1b[m\x1b[K a%3Dflask-x \\nmask-y KEY_disk-z ",
    "\\", "\x1b[",
], ids=["mixed", "backslashes", "csi-starts"])
def test_eight_mib_of_escapes_is_scanned_well_inside_the_hook_timeout(tmp_path, unit):
    """The boundary's escape branches stay cheap: output dense with colour codes,
    URL escapes and `\\n` escapes, and the slowest input found (every position
    a backslash or an unfinished CSI), scan well inside the 30 s timeout."""
    filler = unit * (8 * 1024 * 1024 // len(unit) + 1)
    stdout = filler[: 8 * 1024 * 1024 - 200] + " " + _CSI_GREP + _SECRET
    started = time.monotonic()
    result = _scan(tmp_path, _bash(stdout=stdout))
    elapsed = time.monotonic() - started
    assert _rewrite(result)["stdout"].endswith(_CSI_GREP + "[REDACTED:sk]")
    assert elapsed < 10, f"8 MiB took {elapsed:.1f}s"


# --- a key that output colouring splits is still one key ----------------------
# `grep --color=always <prefix>` wraps the matched part of a key in colour
# codes, so the escape sits INSIDE the key (`trinity_mcp\x1b[m_PPP...`) and no
# shape matched the contiguous text: the rest of the key reached the model, the
# stored execution log and the transcript in clear. Every escape run is removed
# for a second look, and a key found there that had an escape run inside it is
# redacted whole; the escapes it held are kept after the placeholder, so the
# colour state of the output is unchanged.

_GREP_ON, _GREP_OFF = "\x1b[01;31m\x1b[K", "\x1b[m\x1b[K"       # GNU grep's defaults
_IMAGE_ON, _IMAGE_OFF = "\x1b[m\x1b[1;31m", "\x1b[m"           # as the agent image's grep wrote it
_RG_ON, _RG_OFF = "\x1b[0m\x1b[1m\x1b[31m", "\x1b[0m"           # ripgrep
_TPUT_ON, _TPUT_OFF = "\x1b[1m\x1b[31m", "\x1b(B\x1b[m"           # `tput bold; tput setaf 1` ... `tput sgr0`
_SPLIT_MCP = "trinity_mcp_PPP-PPPP_" + "P" * 34
_MCP_HOLDING_SK = "trinity_mcp_ab_sk-" + "Q" * 37
_AKIA = "AKIA" + "Z" * 16
_SLACK = "xoxb-" + "1234567890-" * 3
_ESCAPE = (r"(?:\x1b|\\(?:u001[bB]|x1[bB]|033|e))(?:\[[0-?]*[ -/]*[@-~]|[ -/]*[0-~])")


def _coloured(key, needle, on, off, placeholder):
    """`key` as grep colours every `needle` in it, and what the redaction of
    that must be: the placeholder, with the colour codes the key held kept in
    order after it (the leading one stays in front when the key starts with
    the needle)."""
    count = key.count(needle)
    assert count, (key, needle)
    if key.startswith(needle):
        want = on + placeholder + off + (on + off) * (count - 1)
    else:
        want = placeholder + (on + off) * count
    return key.replace(needle, on + needle + off), want


def test_the_key_as_the_live_turn_printed_it_is_redacted_whole(tmp_path):
    """The bytes the model received from `grep --color=always trinity_mcp`."""
    before = "cfg \x1b[m\x1b[1;31mtrinity_mcp\x1b[m_PPP-PPPP_" + "P" * 34 + " end\x1b[m"
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=before, stderr=before)))
    after = "cfg \x1b[m\x1b[1;31m[REDACTED:trinity_mcp]\x1b[m end\x1b[m"
    assert rewritten["stdout"] == after and rewritten["stderr"] == after
    assert [e["patterns"] for e in _events(tmp_path, "credential_redacted")] == [["trinity_mcp"]]


_SPLITS = [
    (_SECRET, "sk-ant", "sk"), (_SECRET, "sk", "sk"), (_SECRET, "api03", "sk"), (_SECRET, "AAAA", "sk"),
    (_GITHUB, "ghp", "github"), (_GITHUB, "bbbbbbbbbbbb", "github"),
    (_GOOGLE, "AIza", "google"), (_GOOGLE, "CC", "google"),
    (_AKIA, "AKIA", "aws"), (_SLACK, "xoxb", "slack"), (_SLACK, "-", "slack"),
    (_SPLIT_MCP, "trinity_mcp", "trinity_mcp"), (_SPLIT_MCP, "trinity", "trinity_mcp"),
    (_SPLIT_MCP, "mcp_PPP", "trinity_mcp"), (_SPLIT_MCP, "_", "trinity_mcp"),
    (_MCP_HOLDING_SK, "QQQQ", "trinity_mcp"),
]
_SPLIT_IDS = ["sk-prefix", "sk", "sk-mid", "sk-every-4", "ghp-prefix", "ghp-mid", "google-prefix",
              "google-every-2", "aws-prefix", "slack-prefix", "slack-every-dash", "mcp-prefix",
              "mcp-part-prefix", "mcp-across-prefix", "mcp-every-underscore", "mcp-holding-sk"]
_COLOURS = [(_GREP_ON, _GREP_OFF), (_IMAGE_ON, _IMAGE_OFF), (_RG_ON, _RG_OFF), (_TPUT_ON, _TPUT_OFF)]


@pytest.mark.parametrize("colour", _COLOURS, ids=["grep", "image-grep", "rg", "tput"])
@pytest.mark.parametrize("key, needle, name", _SPLITS, ids=_SPLIT_IDS)
def test_a_key_split_by_colour_codes_is_redacted_whole_and_the_codes_kept(tmp_path, key, needle, name, colour):
    split, want = _coloured(key, needle, *colour, f"[REDACTED:{name}]")
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=f"cfg: {split} end", stderr=split)))
    assert rewritten["stdout"] == f"cfg: {want} end"
    assert rewritten["stderr"] == want


@pytest.mark.parametrize("key, needle, name", _SPLITS[::3], ids=_SPLIT_IDS[::3])
def test_a_split_key_in_json_text_is_redacted_whole(tmp_path, key, needle, name):
    """A log or transcript read back as JSON writes the escape as `\\u001b`."""
    split, want = _coloured(key, needle, _GREP_ON, _GREP_OFF, f"[REDACTED:{name}]")
    before = json.dumps({"line": f"x {split} y"})
    assert "\\u001b" in before
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=before)))
    assert rewritten["stdout"] == json.dumps({"line": f"x {want} y"})


def test_every_minted_mcp_key_split_at_its_prefix_is_redacted_whole(tmp_path):
    keys = _minted_mcp_keys(500)
    lines = [f"cfg {_IMAGE_ON}trinity_mcp{_IMAGE_OFF}{k[len('trinity_mcp'):]} end" for k in keys]
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout="\n".join(lines))))
    want = f"cfg {_IMAGE_ON}[REDACTED:trinity_mcp]{_IMAGE_OFF} end"
    assert rewritten["stdout"] == "\n".join([want] * len(keys))


def test_a_key_coloured_only_at_its_end_is_one_placeholder(tmp_path):
    """The head before the colour codes is a key on its own (the first look
    finds it) and the whole key is one too (the second look): one placeholder,
    not a hidden head followed by the tail in clear."""
    split = _SECRET[:-4] + _GREP_ON + _SECRET[-4:] + _GREP_OFF
    rewritten = _rewrite(_scan(tmp_path, _bash(stdout=f"k {split} e")))
    assert rewritten["stdout"] == f"k [REDACTED:sk]{_GREP_ON}{_GREP_OFF} e"
    for rel in (_BACKEND_CS, _AGENT_CS):
        cs = _load(rel, "_cs165_g1_tail_" + rel.replace("/", "_").replace(".", "_"))
        if hasattr(cs, "_credential_values"):
            cs._credential_values = set()
        assert cs.sanitize_text(f"k {split} e") == f"k {cs.REDACTION_PLACEHOLDER}{_GREP_ON}{_GREP_OFF} e", rel


def test_a_failing_command_with_a_split_key_still_warns(tmp_path):
    split, _ = _coloured(_GITHUB, "ghp", _GREP_ON, _GREP_OFF, "")
    result = _scan(tmp_path, _failure("Error: Exit code 1\n" + split))
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "credential (github)" in context


_COLOURED_BENIGN_NEEDLES = ("task", "flask", "disk", "mask", "sk", "xoxb", "ghp", "AKIA", "ROTATION",
                            "trinity_mcp", "mcp", "key", "your", "-")


def _coloured_benign():
    lines = []
    for text in BENIGN:
        for needle in _COLOURED_BENIGN_NEEDLES:
            if needle in text:
                lines.append(text.replace(needle, _GREP_ON + needle + _GREP_OFF))
    return lines


def test_the_coloured_benign_corpus_splits_words():
    """Precondition: these lines do put colour codes inside words."""
    inside = re.compile(f"[A-Za-z0-9_-](?:{_ESCAPE})+[A-Za-z0-9_-]")
    assert len(_coloured_benign()) > 40
    assert sum(1 for line in _coloured_benign() if inside.search(line)) > 30


@pytest.mark.parametrize("text", _coloured_benign())
def test_coloured_benign_output_is_byte_identical(tmp_path, text):
    result = _scan(tmp_path, _bash(stdout=text, stderr=json.dumps(text)))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"rewrote benign output: {result.stdout[:300]}"


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
@pytest.mark.parametrize("key, needle, name", _SPLITS, ids=_SPLIT_IDS)
def test_a_split_key_is_redacted_whole_from_logs(rel, key, needle, name):
    cs = _load(rel, "_cs165_g1_" + rel.replace("/", "_").replace(".", "_"))
    if hasattr(cs, "_credential_values"):
        cs._credential_values = set()
    for on, off in _COLOURS:
        split, want = _coloured(key, needle, on, off, cs.REDACTION_PLACEHOLDER)
        assert cs.sanitize_text(f"cfg: {split} end") == f"cfg: {want} end", (on, off)
        written = json.dumps(f"cfg: {split} end")[1:-1]
        assert cs.sanitize_text(written) == json.dumps(f"cfg: {want} end")[1:-1], (on, off)


def test_the_stored_execution_log_holds_no_part_of_a_split_key():
    """The backend's last pass before the execution log is stored, and the agent
    side's per-line pass over the stream it sends."""
    line = "cfg \x1b[m\x1b[1;31mtrinity_mcp\x1b[m_PPP-PPPP_" + "P" * 34 + " end\x1b[m"
    entry = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": line}]}}
    backend = _load(_BACKEND_CS, "_cs165_g1_log_backend")
    stored = backend.sanitize_execution_log(json.dumps([entry]))
    agent = _load(_AGENT_CS, "_cs165_g1_log_agent")
    agent._credential_values = set()
    streamed = agent.sanitize_subprocess_line(json.dumps(entry))
    for out in (stored, streamed):
        assert "PPPP" not in out and "***REDACTED***" in out
        assert json.loads(out)[0 if out.startswith("[") else "type"] is not None
    assert agent.sanitize_text(line) == "cfg \x1b[m\x1b[1;31m***REDACTED***\x1b[m end\x1b[m"


@pytest.mark.parametrize("rel", [_BACKEND_CS, _AGENT_CS])
def test_a_bearer_token_whose_scheme_grep_coloured_is_redacted_from_logs(rel):
    cs = _load(rel, "_cs165_g1_bearer_" + rel.replace("/", "_").replace(".", "_"))
    if hasattr(cs, "_credential_values"):
        cs._credential_values = set()
    line = f"Authorization: {_GREP_ON}Bearer{_GREP_OFF} abcDEF123.ghiJKL456.mnoPQR789"
    assert cs.sanitize_text(line) == f"Authorization: {_GREP_ON}***REDACTED***{_GREP_OFF}"


def test_the_escape_grammar_is_one_definition_in_the_scanner_and_both_sanitizers():
    baseline = json.loads(H.BASELINE.read_text())
    assert baseline["credential_escape"] == _ESCAPE
    assert f"(?P<lead>{_ESCAPE})" in baseline["credential_boundary"]
    for rel in (_BACKEND_CS, _AGENT_CS):
        cs = _load(rel, "_cs165_g1_parity_" + rel.replace("/", "_").replace(".", "_"))
        assert cs.ESCAPE_SEQUENCE == _ESCAPE, rel


def test_eight_mib_of_split_words_is_scanned_well_inside_the_hook_timeout(tmp_path):
    """The most second looks one output can need: an escape run inside every word."""
    unit = "ab" + _GREP_ON + "cd" + _GREP_OFF + "ef "
    filler = unit * (8 * 1024 * 1024 // len(unit))
    split, want = _coloured(_SECRET, "sk-ant", _GREP_ON, _GREP_OFF, "[REDACTED:sk]")
    stdout = filler[: 8 * 1024 * 1024 - 300] + " " + split
    started = time.monotonic()
    result = _scan(tmp_path, _bash(stdout=stdout))
    elapsed = time.monotonic() - started
    assert _rewrite(result)["stdout"].endswith(" " + want)
    assert elapsed < 10, f"8 MiB took {elapsed:.1f}s"


def test_the_split_key_search_is_one_function_in_the_scanner_and_both_sanitizers():
    """D4: the scanner cannot import the sanitizer, so `_split_keys` is copied;
    the copies must not drift (docstrings aside)."""
    def body(rel):
        for node in ast.parse((H.REPO / rel).read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name == "_split_keys":
                stmts = node.body[1:] if isinstance(node.body[0], ast.Expr) else node.body
                return [ast.dump(s) for s in stmts], ast.dump(node.args)
        raise AssertionError(f"no _split_keys in {rel}")
    scanner = body("docker/base-image/hooks/" + _SCANNER)
    assert body(_BACKEND_CS) == scanner and body(_AGENT_CS) == scanner
