"""GUARD-004: the output scanner covers Read, Grep, WebFetch and owner MCP tools
(trinity-enterprise#165, slice S5).

S3 made `output-scanner.py` redact credentials in Bash output, and registered it
for `Bash` only. Everything else the model reads reached it raw:

  * `Read` of any file, the agent's own `.env`, `.mcp.json` and credentials
    file included, and the `tasks/*.output` file a `run_in_background` Bash
    call writes (the CLI tells the model to `Read` it);
  * `Grep` (when the CLI ships it), `WebFetch`, and every owner MCP tool,
    including an MCP error result (which fires `PostToolUseFailure` too).

What is pinned here:

  * Each test runs the hook only if the managed file's matchers select it for
    that tool and event, judged by a port of CLI 2.1.281's matcher rule: a
    matcher of letters, digits, `_` and `|` is an exact list, anything else an
    UNANCHORED regex. A tool the registration does not reach goes unscanned.
  * Per-tool shape: only text leaves change; binary payloads (Read's image and
    PDF `base64`, a notebook output's `image_data`, an MCP image's
    `source.data`) are never rewritten and never counted against the scan cap.
  * The platform's own `trinity` MCP server is not scanned, by the same
    predicate turn-tier-guard uses.
  * A withhold has the shape of the tool that produced it, with the notice in
    the field the model reads.
  * The write-back guard steers a model that saw a placeholder to Edit the lines
    it needs, instead of rewriting a file without the hidden value.

The shapes were captured from the real CLI 2.1.281 in the agent base image (see
the slice report); Grep's comes from that binary's output schema, because the
image's CLI ships no Grep tool (search runs through Bash, which S3 covers).
Related flow: docs/memory/feature-flows/agent-guardrails.md
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
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
_SLACK = "xoxb-1234567890-" + "H" * 24
_MCP_KEY = "trinity_mcp_" + "G" * 24
_OAUTH = "sk-ant-oat01-" + "Z" * 80
# Base64 that the `google` and `aws` shapes DO match (asserted below), the way
# a real image or PDF can: rewriting a span of it would only corrupt the payload.
_B64 = "iVBORw0KGgo+AIza" + "Cq9" * 12 + "w/AKIA" + "QZ7X" * 4 + "+Jggg=="

_PLATFORM = ["mcp__trinity__chat_with_agent", "mcp__trinity__list_agents"]
_NOT_PLATFORM = [
    "mcp__trinity___x",
    "mcp__trinity__fs__read",
    "mcp__trinityx__read",
    "mcp__owner__fetch",
]


# --- the CLI's matcher rule (2.1.281, `BRe`/`Zho`) -----------------------------


def _matcher_selects(matcher: str, tool: str) -> bool:
    if not matcher or matcher == "*":
        return True
    if re.fullmatch(r"[a-zA-Z0-9_|]+", matcher):
        return tool in [m.strip() for m in matcher.split("|") if m.strip()]
    return re.search(matcher, tool) is not None


def _scanner_entries(event: str) -> list:
    return [
        e
        for e in H.registration()["hooks"].get(event, [])
        if any(
            h.get("args", [""])[-1] == "/opt/trinity/hooks/output-scanner.py"
            for h in e["hooks"]
        )
    ]


def _cli_runs_scanner(event: str, tool: str) -> bool:
    return any(_matcher_selects(e["matcher"], tool) for e in _scanner_entries(event))


def _scan(tmp_path, payload, overrides=None, baseline=None):
    """What the CLI would get back: None when no registration selects the
    scanner for this tool and event (the model then sees the raw output)."""
    if not _cli_runs_scanner(payload["hook_event_name"], payload["tool_name"]):
        return None
    if baseline is None:
        return H.run_hook(
            _SCANNER,
            payload,
            flags=H.registered_flags(_SCANNER),
            env=H.registered_env(_SCANNER),
            tmp=tmp_path,
            overrides=overrides,
        )
    import subprocess

    cfg = tmp_path / "baseline.json"
    cfg.write_text(json.dumps(baseline))
    wrapper = H._WRAPPER.format(
        hooks=str(H.HOOKS),
        runtime=str(tmp_path / "no-runtime.json"),
        baseline=str(cfg),
        log=str(tmp_path / "guardrails.jsonl"),
        script=str(H.HOOKS / _SCANNER),
        over=json.dumps(overrides or {}),
    )
    return subprocess.run(
        [H.PYTHON, *H.registered_flags(_SCANNER), "-c", wrapper],
        input=json.dumps(payload),
        env=H.registered_env(_SCANNER),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _rewrite(result):
    assert (
        result is not None
    ), "the managed settings do not run the scanner for this tool"
    assert result.returncode == 0, result.stderr
    assert result.stdout, "the scanner printed no rewrite"
    hso = json.loads(result.stdout)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


def _untouched(result):
    """Selected by the registration, and left the output as it was."""
    assert (
        result is not None
    ), "the managed settings do not run the scanner for this tool"
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout == ""
    ), f"rewrote output it should leave alone: {result.stdout[:300]}"


def _call(tool, response, tool_input=None, event="PostToolUse"):
    return {
        "session_id": "s-165",
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input or {},
        "tool_use_id": "toolu_s5",
        "tool_response": response,
    }


def _read_text(content, path="/home/developer/app/config.yaml"):
    n = content.count("\n") + 1
    return _call(
        "Read",
        {
            "type": "text",
            "file": {
                "filePath": path,
                "content": content,
                "numLines": n,
                "startLine": 1,
                "totalLines": n,
            },
        },
        {"file_path": path},
    )


def _events(tmp_path, name):
    return [e for e in H.log_events(tmp_path) if e["event"] == name]


def test_the_base64_fixture_is_one_the_shapes_would_damage():
    """Precondition for every binary-skip test: the payload is not vacuous.
    Each shape is compiled behind the shared left boundary, as the hook does."""
    baseline = json.loads(H.BASELINE.read_text())
    shapes = [
        re.compile(baseline["credential_boundary"] + "(?:" + e["pattern"] + ")")
        for e in baseline["credential_patterns"]
    ]
    assert sum(1 for p in shapes if p.search(_B64)) >= 2


# --- registration ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        "Read",
        "Grep",
        "WebFetch",
        "Bash",
        "mcp__owner__fetch",
        "mcp__github__get_file_contents",
        *_PLATFORM,
    ],
)
def test_the_scanner_is_registered_for_the_tools_the_model_reads(tool):
    """The platform's tools are selected too and skipped inside the hook, by
    the same predicate turn-tier-guard uses (a matcher regex cannot say
    "a server called trinity, but not trinity__x")."""
    assert _cli_runs_scanner("PostToolUse", tool)


@pytest.mark.parametrize(
    "tool",
    [
        "NotebookRead",
        "ReadMcpResourceTool",
        "FrameRead",
        "Write",
        "Edit",
        "Glob",
        "my_mcp__tool",
        "ToolSearch",
    ],
)
def test_the_registration_does_not_reach_tools_it_was_not_written_for(tool):
    """An unanchored regex such as `Read|Grep|WebFetch|mcp__.*` would run the
    scanner on every tool whose name merely CONTAINS one of those words."""
    assert not _cli_runs_scanner("PostToolUse", tool)
    assert _matcher_selects("Read|Grep|WebFetch|mcp__.*", tool) == (
        tool in ("NotebookRead", "ReadMcpResourceTool", "FrameRead", "my_mcp__tool")
    )


def test_an_mcp_error_result_reaches_the_failure_hook():
    assert _cli_runs_scanner("PostToolUseFailure", "mcp__owner__fetch")
    assert _cli_runs_scanner("PostToolUseFailure", "Bash")


def test_every_scanner_registration_uses_the_registered_exec_form():
    expected = {
        "type": "command",
        "command": "/usr/bin/env",
        "timeout": 30,
        "args": [
            "-i",
            "HOME=/home/developer",
            "/usr/local/bin/python3",
            "-I",
            "-S",
            "/opt/trinity/hooks/output-scanner.py",
        ],
    }
    for event in ("PostToolUse", "PostToolUseFailure"):
        for entry in _scanner_entries(event):
            assert entry["hooks"] == [expected], (event, entry["matcher"])
    assert sorted(e["matcher"] for e in _scanner_entries("PostToolUse")) == [
        "Bash",
        "Read|Grep|WebFetch",
        "^mcp__",
    ]
    assert sorted(e["matcher"] for e in _scanner_entries("PostToolUseFailure")) == [
        "Bash",
        "^mcp__",
    ]


# --- Read --------------------------------------------------------------------------


def test_a_key_in_a_read_file_is_redacted_and_the_line_count_kept(tmp_path):
    payload = _read_text(f"name: demo\nslack_token: {_SLACK}\ndebug: false\n")
    rewritten = _rewrite(_scan(tmp_path, payload))
    file = payload["tool_response"]["file"]
    assert rewritten == {
        "type": "text",
        "file": {
            **file,
            "content": "name: demo\nslack_token: [REDACTED:slack]\ndebug: false\n",
        },
    }
    events = _events(tmp_path, "credential_redacted")
    assert [(e["tool"], e["code"], e["patterns"]) for e in events] == [
        ("Read", "read_output", ["slack"])
    ]
    assert _SLACK not in (tmp_path / "guardrails.jsonl").read_text()


@pytest.mark.parametrize(
    "path,content,shown",
    [
        (
            "/home/developer/.env",
            f"ANTHROPIC_API_KEY={_SECRET}\nGITHUB_TOKEN={_GITHUB}\nDEBUG=false\n",
            "ANTHROPIC_API_KEY=[REDACTED:sk]\nGITHUB_TOKEN=[REDACTED:github]\nDEBUG=false\n",
        ),
        (
            "/home/developer/.mcp.json",
            '{"mcpServers": {"trinity": {"headers": {"Authorization": "Bearer '
            + _MCP_KEY
            + '"}}}}',
            '{"mcpServers": {"trinity": {"headers": {"Authorization": "Bearer '
            '[REDACTED:trinity_mcp]"}}}}',
        ),
        (
            "/home/developer/.claude/.credentials.json",
            '{"claudeAiOauth": {"accessToken": "' + _OAUTH + '"}}',
            '{"claudeAiOauth": {"accessToken": "[REDACTED:sk]"}}',
        ),
    ],
    ids=["env", "mcp-json", "credentials-json"],
)
def test_reading_the_agents_own_config_is_allowed_and_redacted(
    tmp_path, path, content, shown
):
    """The agent may read its own config (the names, the structure, the
    non-secret values); the known-prefix values are hidden (D4)."""
    rewritten = _rewrite(_scan(tmp_path, _read_text(content, path)))
    assert rewritten["file"]["content"] == shown


def test_background_bash_output_read_through_read_is_redacted(tmp_path):
    """A `run_in_background` call returns at once; its output lands in a
    tasks/*.output file the CLI tells the model to Read."""
    path = "/tmp/claude-1000/-home-developer/0b1c/tasks/b98axh2wm.output"
    rewritten = _rewrite(
        _scan(
            tmp_path, _read_text(f"bg key: {_SECRET}\n\n[exited with code 0]\n", path)
        )
    )
    assert (
        rewritten["file"]["content"]
        == "bg key: [REDACTED:sk]\n\n[exited with code 0]\n"
    )


BENIGN_FILE = (
    "image: registry.example.com/task-runner-deployment:7d9f8c6b5\n"
    "SLACK_BOT_TOKEN=xoxb-test\nSLACK_BOT_TOKEN=xoxb-your-token-here\n"
    "OPENAI_API_KEY=sk-your-openai-key\n"
    "share: https://trinity.example.com/api/files/9f1c2e/report.pdf?sig=Xq3vNw8pLr2TzYb6KdHj0MfA5sUeCo1gWi7RnQ4lVyE\n"
    "pip install flask-sqlalchemy-utilities-extra==3.1.0\n"
)


def test_a_source_file_with_fixtures_and_prefixes_is_byte_identical(tmp_path):
    _untouched(
        _scan(
            tmp_path, _read_text(BENIGN_FILE, "/home/developer/app/tests/conftest.py")
        )
    )


@pytest.mark.parametrize(
    "response",
    [
        {
            "type": "image",
            "file": {"base64": _B64, "type": "image/png", "originalSize": 73},
        },
        {
            "type": "pdf",
            "file": {
                "filePath": "/home/developer/a.pdf",
                "base64": _B64,
                "originalSize": 73,
            },
        },
        {
            "type": "parts",
            "file": {
                "filePath": "/home/developer/a.pdf",
                "originalSize": 73,
                "count": 1,
                "outputDir": "/tmp/p",
            },
            "pages": [{"base64": _B64, "mediaType": "image/png"}],
        },
        {"type": "file_unchanged", "file": {"filePath": "/home/developer/a.txt"}},
    ],
    ids=["image", "pdf", "parts", "file-unchanged"],
)
def test_binary_read_payloads_are_never_rewritten(tmp_path, response):
    _untouched(
        _scan(tmp_path, _call("Read", response, {"file_path": "/home/developer/a"}))
    )


def test_a_notebook_output_is_redacted_and_its_image_kept(tmp_path):
    cells = [
        {
            "cellType": "code",
            "source": "print(token)\n",
            "cell_id": "c1",
            "language": "python",
            "outputs": [
                {"output_type": "stream", "text": f"token: {_GITHUB}\n"},
                {
                    "output_type": "display_data",
                    "image": {"image_data": _B64, "media_type": "image/png"},
                },
            ],
        }
    ]
    payload = _call(
        "Read",
        {
            "type": "notebook",
            "file": {"filePath": "/home/developer/n.ipynb", "cells": cells},
        },
    )
    rewritten = _rewrite(_scan(tmp_path, payload))
    outputs = rewritten["file"]["cells"][0]["outputs"]
    assert outputs[0]["text"] == "token: [REDACTED:github]\n"
    assert outputs[1]["image"]["image_data"] == _B64


def test_a_large_image_is_not_withheld_as_oversize_text(tmp_path):
    """The scan cap counts text; an image's base64 is not text the model reads."""
    response = {
        "type": "image",
        "file": {"base64": _B64 * 40, "type": "image/png", "originalSize": 9},
    }
    _untouched(
        _scan(tmp_path, _call("Read", response), overrides={"SCAN_CAP_CHARS": 1000})
    )


# --- Grep (output schema read from the 2.1.281 binary) --------------------------


def test_grep_content_and_file_names_are_redacted(tmp_path):
    response = {
        "mode": "content",
        "numFiles": 1,
        "filenames": [],
        "content": f".env:2:GITHUB_TOKEN={_GITHUB}\nsettings.yaml:2:slack_token: {_SLACK}",
        "numLines": 2,
    }
    rewritten = _rewrite(
        _scan(tmp_path, _call("Grep", response, {"pattern": "TOKEN|token"}))
    )
    assert rewritten == {
        **response,
        "content": ".env:2:GITHUB_TOKEN=[REDACTED:github]\n"
        "settings.yaml:2:slack_token: [REDACTED:slack]",
    }
    assert _events(tmp_path, "credential_redacted")[0]["code"] == "grep_output"


def test_grep_file_list_mode_is_untouched_when_clean(tmp_path):
    response = {
        "mode": "files_with_matches",
        "numFiles": 2,
        "filenames": ["/home/developer/.env", "a.py"],
    }
    _untouched(_scan(tmp_path, _call("Grep", response, {"pattern": "sk-ant"})))


# --- WebFetch ----------------------------------------------------------------------


def test_a_key_in_a_fetched_page_result_is_redacted_and_the_numbers_kept(tmp_path):
    response = {
        "bytes": 121,
        "code": 200,
        "codeText": "OK",
        "result": f"The page says: {_SECRET}",
        "durationMs": 1730,
        "url": "https://example.com/build/42",
    }
    rewritten = _rewrite(
        _scan(tmp_path, _call("WebFetch", response, {"url": response["url"]}))
    )
    assert rewritten == {**response, "result": "The page says: [REDACTED:sk]"}
    assert _events(tmp_path, "credential_redacted")[0]["code"] == "webfetch_output"


# --- MCP ---------------------------------------------------------------------------


def test_an_owner_mcp_text_block_is_redacted_and_its_image_kept(tmp_path):
    response = [
        {"type": "text", "text": f"probe image key: AKIA{'Q' * 16}"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _B64},
        },
    ]
    rewritten = _rewrite(_scan(tmp_path, _call("mcp__owner__fetch", response)))
    assert rewritten == [
        {"type": "text", "text": "probe image key: [REDACTED:aws]"},
        response[1],
    ]
    events = _events(tmp_path, "credential_redacted")
    assert [(e["tool"], e["code"]) for e in events] == [
        ("mcp__owner__fetch", "mcp_output")
    ]


def test_an_mcp_error_result_seen_by_post_tool_use_stays_a_string(tmp_path):
    """Observed on 2.1.281: an `isError` result also fires PostToolUse with the
    error as a string `Error: ...`. MCP output has no schema check."""
    payload = _call("mcp__owner__fetch", f"Error: probe mcp error key: {_SECRET}")
    assert (
        _rewrite(_scan(tmp_path, payload))
        == "Error: probe mcp error key: [REDACTED:sk]"
    )


def test_an_mcp_error_result_warns_the_model_on_the_failure_event(tmp_path):
    payload = {
        **_call("mcp__owner__fetch", None, event="PostToolUseFailure"),
        "error": f"probe mcp error key: {_SECRET}",
        "is_interrupt": False,
    }
    payload.pop("tool_response")
    result = _scan(tmp_path, payload)
    assert result is not None and result.returncode == 0, result and result.stderr
    hso = json.loads(result.stdout)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    assert "sk" in hso["additionalContext"] and _SECRET not in result.stdout
    events = _events(tmp_path, "credential_unredacted")
    assert [(e["tool"], e["code"]) for e in events] == [
        ("mcp__owner__fetch", "mcp_failure_output")
    ]


@pytest.mark.parametrize("tool", _PLATFORM)
@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
def test_the_platform_mcp_server_is_not_scanned(tmp_path, tool, event):
    payload = {
        **_call(tool, [{"type": "text", "text": f"share {_SECRET}"}], event=event),
        "error": f"key {_SECRET}",
    }
    _untouched(_scan(tmp_path, payload))
    assert H.log_events(tmp_path) == []


@pytest.mark.parametrize("tool", _NOT_PLATFORM)
def test_a_server_that_only_shares_the_prefix_is_scanned(tmp_path, tool):
    rewritten = _rewrite(
        _scan(tmp_path, _call(tool, [{"type": "text", "text": f"k {_SECRET}"}]))
    )
    assert rewritten == [{"type": "text", "text": "k [REDACTED:sk]"}]


def _load_module(name, path, mp):
    mp.syspath_prepend(str(H.HOOKS))
    lib_spec = importlib.util.spec_from_file_location("lib", H.HOOKS / "lib.py")
    lib = importlib.util.module_from_spec(lib_spec)
    lib_spec.loader.exec_module(lib)
    mp.setitem(sys.modules, "lib", lib)
    policy_spec = importlib.util.spec_from_file_location(
        "file_path_policy", H.HOOKS / "file_path_policy.py"
    )
    policy = importlib.util.module_from_spec(policy_spec)
    policy_spec.loader.exec_module(policy)
    mp.setitem(sys.modules, "file_path_policy", policy)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_platform_test_is_the_one_turn_tier_guard_uses():
    with pytest.MonkeyPatch.context() as mp:
        scanner = _load_module("_s5_scanner", H.HOOKS / _SCANNER, mp)
        turn_tier = _load_module("_s5_turn_tier", H.HOOKS / "turn-tier-guard.py", mp)
    corpus = [
        *_PLATFORM,
        *_NOT_PLATFORM,
        "mcp__trinity__",
        "mcp__trinity",
        "Read",
        "mcp__trinity__a_b",
    ]
    assert [scanner.is_platform_mcp_tool(t) for t in corpus] == [
        turn_tier.is_platform_mcp_tool(t) for t in corpus
    ]
    assert [t for t in corpus if scanner.is_platform_mcp_tool(t)] == [
        *_PLATFORM,
        "mcp__trinity__a_b",
    ]


# --- withhold, in each tool's shape -------------------------------------------------

_BAD_BASELINE = {"credential_patterns": []}
_NOTICE = "[Output withheld: Trinity's credential check failed."


def _assert_a_text_read(rewritten):
    """A withheld Read must still parse as Read's text variant, number types
    included: the CLI drops a rewrite that fails the output schema and shows the
    original output instead."""
    file = rewritten["file"]
    assert rewritten["type"] == "text"
    assert isinstance(file["filePath"], str) and isinstance(file["content"], str)
    assert all(
        type(file[k]) is int for k in ("numLines", "startLine", "totalLines")
    ), file


def test_a_withheld_read_is_a_text_read_with_the_notice_as_its_content(tmp_path):
    payload = _read_text(f"k={_SECRET}\n", "/home/developer/app/c.yaml")
    rewritten = _rewrite(_scan(tmp_path, payload, baseline=_BAD_BASELINE))
    assert rewritten["type"] == "text"
    assert set(rewritten["file"]) == {
        "filePath",
        "content",
        "numLines",
        "startLine",
        "totalLines",
    }
    assert rewritten["file"]["filePath"] == "/home/developer/app/c.yaml"
    assert rewritten["file"]["content"].startswith(_NOTICE)
    _assert_a_text_read(rewritten)
    assert "another way" in rewritten["file"]["content"]
    assert _SECRET not in json.dumps(rewritten)


def test_a_withheld_image_read_is_a_text_read_too(tmp_path):
    payload = _call(
        "Read",
        {
            "type": "image",
            "file": {"base64": _B64, "type": "image/png", "originalSize": 1},
        },
    )
    rewritten = _rewrite(_scan(tmp_path, payload, baseline=_BAD_BASELINE))
    assert rewritten["type"] == "text" and rewritten["file"]["content"].startswith(
        _NOTICE
    )
    _assert_a_text_read(rewritten)


def test_a_withheld_grep_shows_the_notice_as_content(tmp_path):
    payload = _call(
        "Grep", {"mode": "files_with_matches", "numFiles": 1, "filenames": ["/x/.env"]}
    )
    rewritten = _rewrite(_scan(tmp_path, payload, baseline=_BAD_BASELINE))
    assert (
        rewritten["mode"] == "content"
        and rewritten["filenames"] == []
        and rewritten["numFiles"] == 0
    )
    assert rewritten["content"].startswith(_NOTICE)


def test_a_withheld_webfetch_keeps_its_numbers_and_shows_the_notice_as_result(tmp_path):
    response = {
        "bytes": 121,
        "code": 200,
        "codeText": "OK",
        "result": f"key {_SECRET}",
        "durationMs": 9,
        "url": "https://example.com/x",
    }
    rewritten = _rewrite(
        _scan(tmp_path, _call("WebFetch", response), baseline=_BAD_BASELINE)
    )
    assert {k: rewritten[k] for k in ("bytes", "code", "durationMs")} == {
        "bytes": 121,
        "code": 200,
        "durationMs": 9,
    }
    assert rewritten["codeText"] == "" and rewritten["url"] == ""
    assert rewritten["result"].startswith(_NOTICE)


def test_a_withheld_webfetch_missing_its_numbers_gets_integer_ones(tmp_path):
    """WebFetch's schema requires bytes, code and durationMs as numbers; a
    withhold that filled them with anything else would be dropped by the CLI."""
    response = {
        "result": f"key {_SECRET}",
        "url": "https://example.com/x",
        "code": True,
    }
    rewritten = _rewrite(
        _scan(tmp_path, _call("WebFetch", response), baseline=_BAD_BASELINE)
    )
    assert all(
        type(rewritten[k]) is int for k in ("bytes", "code", "durationMs")
    ), rewritten
    assert isinstance(rewritten["codeText"], str) and isinstance(rewritten["url"], str)
    assert rewritten["result"].startswith(_NOTICE)


@pytest.mark.parametrize(
    "response,expected_type",
    [
        ([{"type": "text", "text": f"k {_SECRET}"}], list),
        (f"Error: k {_SECRET}", str),
    ],
    ids=["blocks", "error-string"],
)
def test_a_withheld_mcp_result_keeps_its_form(tmp_path, response, expected_type):
    rewritten = _rewrite(
        _scan(tmp_path, _call("mcp__owner__fetch", response), baseline=_BAD_BASELINE)
    )
    assert isinstance(rewritten, expected_type)
    text = rewritten if isinstance(rewritten, str) else rewritten[0]["text"]
    assert text.startswith(_NOTICE) and _SECRET not in json.dumps(rewritten)


def test_an_mcp_result_over_the_cap_is_withheld(tmp_path):
    response = [{"type": "text", "text": "x" * 5000}]
    rewritten = _rewrite(
        _scan(
            tmp_path,
            _call("mcp__owner__dump", response),
            overrides={"SCAN_CAP_CHARS": 1000},
        )
    )
    assert rewritten == [{"type": "text", "text": rewritten[0]["text"]}]
    assert rewritten[0]["text"].startswith("[Output withheld: the output is too large")


# --- the write-back guard after a redacted Read ---------------------------------------


def _guard(hook, tmp_path, payload):
    return H.run_hook(
        hook,
        payload,
        flags=H.registered_flags(hook),
        env=H.registered_env(hook),
        tmp=tmp_path,
    )


def test_rewriting_a_redacted_file_is_refused_and_steers_to_edit(tmp_path):
    """Read hid the key; a whole-file Write carries the placeholder. The refusal
    must not tell the model to "leave the value out": a Write without that line
    deletes the real credential from the file."""
    content = "name: demo\nslack_token: [REDACTED:slack]\ndebug: true\n"
    result = _guard(
        "file-guardrail.py",
        tmp_path,
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/home/developer/app/settings.yaml",
                "content": content,
            },
        },
    )
    assert result.returncode == 2, result.stderr
    assert "Edit" in result.stderr
    assert "Leave that value out" not in result.stderr


def test_a_shell_rewrite_carrying_the_placeholder_steers_to_editing_lines(tmp_path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "cat > settings.yaml <<'EOF'\nslack_token: [REDACTED:slack]\nEOF"
        },
    }
    result = _guard("bash-guardrail.py", tmp_path, payload)
    assert result.returncode == 2, result.stderr
    assert "Leave that value out" not in result.stderr
    assert "only the lines" in result.stderr


def test_an_edit_of_another_line_passes(tmp_path):
    result = _guard(
        "file-guardrail.py",
        tmp_path,
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/home/developer/app/settings.yaml",
                "old_string": "debug: false",
                "new_string": "debug: true",
            },
        },
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "/home/developer/.env",
        "/home/developer/.mcp.json",
        "/home/developer/.claude/.credentials.json",
    ],
)
def test_the_agents_own_credential_files_are_not_written_through_write_or_edit(
    tmp_path, path
):
    """Unchanged by S5, and the reason a redacted Read of them cannot be written
    back over the real values: the path rule refuses any Write/Edit to them."""
    result = _guard(
        "file-guardrail.py",
        tmp_path,
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": path,
                "old_string": "DEBUG=false",
                "new_string": "DEBUG=true",
            },
        },
    )
    assert result.returncode == 2, result.stderr


# --- the image-build smoke --------------------------------------------------------------


def _smoke(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "_guard002_smoke_s5", H.HOOKS / "guard002-smoke.py"
    )
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke.SMOKE_HOME = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    return smoke


def test_the_build_smoke_checks_the_scanner_covers_every_tool(tmp_path):
    smoke = _smoke(tmp_path)
    assert smoke._check_scanner_coverage(H.registration()) == []
    bash_only = json.loads(H.MANAGED.read_text())
    bash_only["hooks"]["PostToolUse"] = [
        e for e in bash_only["hooks"]["PostToolUse"] if e["matcher"] == "Bash"
    ]
    bash_only["hooks"]["PostToolUseFailure"] = [
        e for e in bash_only["hooks"]["PostToolUseFailure"] if e["matcher"] == "Bash"
    ]
    missing = smoke._check_scanner_coverage(bash_only)
    assert len(missing) == 5, missing


def test_the_build_smoke_runs_the_scanner_on_read_and_mcp(tmp_path):
    smoke = _smoke(tmp_path)
    wrapper = H._WRAPPER.format(
        hooks=str(H.HOOKS),
        runtime=str(tmp_path / "no-runtime.json"),
        baseline=str(H.BASELINE),
        log=str(tmp_path / "guardrails.jsonl"),
        script=str(H.HOOKS / _SCANNER),
        over=json.dumps({"TOOL_RESULTS_ROOT": str(tmp_path / "home")}),
    )
    argv = [H.PYTHON, *H.registered_flags(_SCANNER), "-c", wrapper]
    failures = []
    smoke._check_output_scanner(argv, failures)
    assert failures == []
    # A scanner without the platform exemption fails the smoke.
    source = (H.HOOKS / _SCANNER).read_text()
    exemption = "    if is_platform_mcp_tool(tool):\n        return\n"
    assert source.count(exemption) == 1
    s3 = tmp_path / "no-exemption-scanner.py"
    s3.write_text(source.replace(exemption, ""))
    argv_s3 = [
        H.PYTHON,
        *H.registered_flags(_SCANNER),
        "-c",
        wrapper.replace(str(H.HOOKS / _SCANNER), str(s3)),
    ]
    failures = []
    smoke._check_output_scanner(argv_s3, failures)
    assert any("platform" in f for f in failures), failures
