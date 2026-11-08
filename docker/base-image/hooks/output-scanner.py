#!/usr/bin/env python3
"""Keeps credentials in tool output from the model (GUARD-004, trinity-enterprise#165).

Registered for `Bash` (both events below), for `Read|Grep|WebFetch` and for
every `mcp__*` tool on `PostToolUse`, and for `mcp__*` on `PostToolUseFailure`.
The platform's own `trinity` MCP server is skipped (`is_platform_mcp_tool`):
its output is platform-authored and carries share links. Background Bash
output is read later through `Read`, so the `Read` registration covers it.
Strings that are binary payloads (`base64`, `image_data`, a base64 `source`'s
`data`) are never scanned or counted: a rewritten span would only break them.

For `Bash`:

* `PostToolUse` (the command exited 0): the matched spans are replaced with
  `[REDACTED:<shape>]` in a copy of `tool_response`, returned as
  `hookSpecificOutput.updatedToolOutput`. Every key, boolean and number is kept
  and every string leaf is scanned, lists included (`structuredContent`), because
  the CLI drops a rewrite that does not match the tool's output schema. Output
  the CLI persisted to `persistedOutputPath` is redacted in that file too. Clean
  output prints nothing.
* `PostToolUseFailure` (non-zero exit, or an MCP error result): its only
  decision field is `additionalContext`, so a hit adds a warning not to repeat
  it (decision D5). On the pinned CLI a failed Bash call ALSO fires
  `PostToolUse`, with
  `tool_response` the error text as a plain string ("Error: Exit code N\n...").
  The Bash schema wants an object there: a string rewrite is rejected and the
  whole result becomes a `tool_use_error`, while a Bash-shaped object is
  accepted. So a string Bash response is rewritten as `{stdout, stderr,
  interrupted}` (`_as_bash`); observed live on 2.1.281, see CLI_FACTS_TO_REAUDIT.
  An MCP error result does the same with a string; MCP output has no schema
  check, so that string is rewritten as a string.

Fails closed on content (D6): if the scan raises, its shapes cannot be loaded or
the output is over the cap, the result is withheld, in the shape of the tool
that produced it (`_withheld_other` for every tool but Bash). It never exits 2: on
`PostToolUse` that only adds stderr and the original output still reaches the
model. A timeout or a crash before `main` cannot be caught here; the CLI then
passes the original through.

The shapes are `credential_patterns` in the baseline: the known-prefix
partition of `utils/credential_sanitizer.SECRET_VALUE_PATTERNS` (D4), copied
because a `-I -S` hook cannot import it, behind the one `credential_boundary`
(not after a letter, digit or `-`, except where that character ends an escape:
`\\n`/`\\t`, `%XX`, an ANSI escape; `_` is a separator). A key that colour
codes split (`credential_escape` runs inside it) is found in the text with the
escape runs removed and redacted whole. Events go to the guardrails log with
shape names only, never a value.
"""
import bisect
import json
import os
import re
import stat
import sys
import tempfile

sys.path.insert(0, "/opt/trinity/hooks")
from lib import load_config, log_event, run_hook  # noqa: E402

# The CLI build these facts were read from. A test fails when the image pins
# another one, until each fact below is re-checked against it.
OUTPUT_SCANNER_CLI_VERIFIED = "2.1.281"
CLI_FACTS_TO_REAUDIT = (
    "PostToolUse hookSpecificOutput.updatedToolOutput replaces what the model sees for a built-in tool",
    "a rewrite that fails the tool's output schema is dropped and the original used (outputSchema.safeParse)",
    "the Bash output schema: stdout, stderr, interrupted required; isImage, structuredContent, "
    "persistedOutputPath, persistedOutputSize and the hint strings optional",
    "a non-empty structuredContent is shown to the model instead of stdout",
    "output over the inline limit is written to persistedOutputPath (a tool-results dir) before the hooks run, "
    "and the model sees a preview of stdout plus that path",
    "a Bash command that exits non-zero fires PostToolUseFailure with `error`, whose only decision field is "
    "additionalContext",
    "parallel PostToolUse rewrites all start from the original output: last write wins",
    "exit 2 on PostToolUse shows stderr to the model and the original output still goes through",
    "a failed Bash call fires PostToolUseFailure AND a managed PostToolUse whose tool_response is the error "
    "string; a string rewrite fails the schema and becomes a tool_use_error, a {stdout, stderr, interrupted} "
    "object is accepted (the model then sees the result without is_error)",
    "a matcher of only letters, digits, _ and | is an exact list of tool names; any other matcher is an "
    "UNANCHORED JavaScript regex, so `Read` as a regex would also match NotebookRead and ReadMcpResourceTool",
    "the Read output schema: a union on `type` (text: file.content; image and pdf: file.base64; notebook: "
    "file.cells with outputs[].image.image_data; parts; file_unchanged); the model sees file.content with "
    "line numbers the CLI adds after the hooks",
    "the WebFetch output schema: bytes, code, codeText, result, durationMs, url required; the model sees result",
    "the Grep output schema: mode, numFiles, filenames required, content optional; the model sees content in "
    "content mode and filenames otherwise",
    "an MCP tool's tool_response is a list of Anthropic content blocks (an image is {type: image, source: "
    "{type: base64, data}}) or a string, and a rewrite of it is not checked against any schema",
    "an MCP result with isError fires PostToolUseFailure AND PostToolUse, whose tool_response is the error "
    "string prefixed `Error: `; a string rewrite is accepted",
    "background Bash output is written to a tasks/*.output file the model is told to Read",
)

PLACEHOLDER = "[REDACTED:{name}]"
SCAN_CAP_CHARS = 8 * 1024 * 1024
PERSISTED_CAP_BYTES = 8 * 1024 * 1024
TOOL_RESULTS_ROOT = "/home/developer"
TOOL_RESULTS_DIR = "tool-results"
_NAME = re.compile(r"[a-z0-9_]{1,32}")
PLATFORM_MCP_PREFIX = "mcp__trinity__"
# Strings that are binary payloads: Read's image/pdf `base64`, a notebook
# output's `image_data`, and an MCP image block's `source.data`.
BINARY_KEYS = frozenset({"base64", "image_data"})
_OUTPUT_KIND = {"Bash": "bash", "Read": "read", "Grep": "grep", "WebFetch": "webfetch"}

_DO_NOT_RERUN = "The command already ran — do not run it again to see its output.]"
WITHHELD_ERROR = "[Output withheld: Trinity's credential check failed. " + _DO_NOT_RERUN
WITHHELD_OVERSIZE = ("[Output withheld: the output is too large for Trinity's credential check. "
                     + _DO_NOT_RERUN)
_DO_NOT_REFETCH = "Do not try to get this output another way.]"
WITHHELD_ERROR_OTHER = "[Output withheld: Trinity's credential check failed. " + _DO_NOT_REFETCH
WITHHELD_OVERSIZE_OTHER = ("[Output withheld: the output is too large for Trinity's credential check. "
                           + _DO_NOT_REFETCH)
FAILURE_WARNING = ("The output of this failed call held a credential ({shapes}). Trinity hides it "
                   "where it can; if you can still see it, do not repeat, store or send it.")
FAILURE_UNCHECKED = ("Trinity could not check the output above for credentials ({why}). "
                     "If it contains one, do not repeat, store or send it.")


class _Oversize(Exception):
    pass


class _Unverifiable(Exception):
    pass


def _shapes(config):
    """One matcher for every shape: `credential_boundary`, then each entry's
    `pattern` as the named group `s<index>`; the list of shape names; and a
    matcher for a run of `credential_escape` sequences.

    One pass finds the leftmost key, so a shape that starts inside another key's
    random body (with `_` a left boundary, `trinity_mcp_ab_sk-...`) is part of
    that key instead of cutting it short, and the boundary is tested once per
    position instead of once per shape."""
    boundary = config.get("credential_boundary")
    escape = config.get("credential_escape")
    entries = config.get("credential_patterns")
    if not isinstance(boundary, str) or not boundary:
        raise ValueError("no credential_boundary")
    if not isinstance(escape, str) or not escape:
        raise ValueError("no credential_escape")
    if not isinstance(entries, list) or not entries:
        raise ValueError("no credential_patterns")
    names, groups = [], []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(pattern, str):
            raise ValueError("malformed credential_patterns entry")
        re.compile(pattern)
        groups.append(f"(?P<s{len(names)}>{pattern})")
        names.append(name)
    return re.compile(boundary + "(?:" + "|".join(groups) + ")"), names, re.compile(f"(?:{escape})+")


def is_platform_mcp_tool(tool):
    """A tool of the platform's own `trinity` MCP server. The same test as
    turn-tier-guard.py: a server called `trinity_` or `trinity__x` shares the
    prefix, and neither leaves a remainder that is one plain tool name."""
    rest = tool[len(PLATFORM_MCP_PREFIX):] if tool.startswith(PLATFORM_MCP_PREFIX) else ""
    return bool(rest) and not rest.startswith("_") and "__" not in rest


def _kind(tool):
    return "mcp" if tool.startswith("mcp__") else _OUTPUT_KIND.get(tool, "tool")


def _is_binary(container, key):
    return key in BINARY_KEYS or (key == "data" and container.get("type") == "base64")


def _text_size(value):
    total, stack = 0, [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            stack.extend(v for k, v in item.items() if not _is_binary(item, k))
        elif isinstance(item, list):
            stack.extend(item)
    return total


def _shape_name(match, names):
    return names[int(match.lastgroup[1:])]


def _split_keys(text, runs, find):
    """Keys that escape runs split, as (start, end, tag) spans of `text`.

    `find` is run on `text` with every escape run removed (what a terminal
    shows) and yields (start, end, tag) there; a match is kept only when an
    escape run sat strictly inside it, and is mapped back onto `text`. The
    same function is in both copies of `utils/credential_sanitizer.py` (D4)."""
    if "\x1b" not in text and "\\" not in text:
        return []
    at, shift, parts, kept, removed = [], [], [], 0, 0
    for run in runs.finditer(text):
        parts.append(text[kept:run.start()])
        at.append(run.start() - removed)
        removed += run.end() - run.start()
        shift.append(removed)
        kept = run.end()
    if not at:
        return []
    parts.append(text[kept:])
    spans = []
    for start, end, tag in find("".join(parts)):
        first, last = bisect.bisect_right(at, start), bisect.bisect_left(at, end)
        if first < last:
            spans.append((start + (shift[first - 1] if first else 0), end + shift[last - 1], tag))
    return spans


def _spans(text, shapes):
    """Every key in `text` as (start, end, name), in order, not overlapping.

    Two looks: the text as it is (a key after an escape, the boundary's `lead`,
    is found here), and the text with its escape runs removed, where a key that
    colour codes split (`grep --color=always trinity_mcp` prints
    `trinity_mcp\\x1b[m_...`) is whole again. Spans that overlap become one,
    named by the leftmost."""
    matcher, names, runs = shapes

    def find(source):
        return [(m.start(m.lastgroup), m.end(), _shape_name(m, names)) for m in matcher.finditer(source)]

    merged = []
    for start, end, name in sorted(find(text) + _split_keys(text, runs, find), key=lambda s: (s[0], -s[1])):
        if merged and start < merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end, merged[-1][2])
        else:
            merged.append((start, end, name))
    return merged


def _redact_text(text, shapes, hits):
    """Each key replaced by `[REDACTED:<shape>]`. Only the key's own characters
    go: an escape in front of it (the boundary's `lead`) stays, and the escape
    runs a split key held are written back after the placeholder, in order, so
    the colour state of the output is unchanged."""
    spans = _spans(text, shapes)
    if not spans:
        return text
    out, kept = [], 0
    for start, end, name in spans:
        hits.add(name)
        out += [text[kept:start], PLACEHOLDER.format(name=name)]
        out += [run.group() for run in shapes[2].finditer(text, start, end)]
        kept = end
    out.append(text[kept:])
    return "".join(out)


def _redact(value, shapes, hits, skip=()):
    if isinstance(value, str):
        return _redact_text(value, shapes, hits)
    if isinstance(value, list):
        return [_redact(item, shapes, hits) for item in value]
    if isinstance(value, dict):
        return {k: v if k in skip or _is_binary(value, k) else _redact(v, shapes, hits)
                for k, v in value.items()}
    return value


def _as_bash(response, tool):
    """A plain-string Bash response (a failed call) as the object the Bash
    output schema accepts; anything else unchanged."""
    if tool == "Bash" and isinstance(response, str):
        return {"stdout": response, "stderr": "", "interrupted": False}
    return response


def _withheld(response, notice):
    """The result with every top-level text field blanked and `stdout` carrying
    the notice. Booleans, numbers and nested client-only objects are kept so it
    still matches the Bash output schema."""
    if isinstance(response, str):
        return notice
    out = {}
    if isinstance(response, dict):
        for key, value in response.items():
            if isinstance(value, str):
                out[key] = ""
            elif key == "structuredContent" and isinstance(value, list):
                out[key] = []
            else:
                out[key] = value
    out["stdout"] = notice
    out.setdefault("stderr", "")
    out.setdefault("interrupted", False)
    return out


def _blank(value):
    """Every text leaf emptied, binary payloads and everything else kept."""
    if isinstance(value, str):
        return ""
    if isinstance(value, list):
        return [_blank(item) for item in value]
    if isinstance(value, dict):
        return {k: v if _is_binary(value, k) else _blank(v) for k, v in value.items()}
    return value


def _withheld_other(response, tool, notice):
    """The withhold for a tool other than Bash, in a shape its output schema
    accepts, with the notice where the model reads that tool's text."""
    if tool == "Read":
        file = response.get("file") if isinstance(response, dict) else None
        path = file.get("filePath") if isinstance(file, dict) else None
        return {"type": "text", "file": {"filePath": path if isinstance(path, str) else "",
                                         "content": notice, "numLines": 1, "startLine": 1, "totalLines": 1}}
    if tool == "Grep":
        return {"mode": "content", "numFiles": 0, "filenames": [], "content": notice, "numLines": 1}
    if tool == "WebFetch":
        out = _blank(response) if isinstance(response, dict) else {}
        for key in ("bytes", "code", "durationMs"):
            if not isinstance(out.get(key), (int, float)) or isinstance(out.get(key), bool):
                out[key] = 0
        out.setdefault("codeText", "")
        out.setdefault("url", "")
        out["result"] = notice
        return out
    if isinstance(response, str):
        return notice
    if isinstance(response, dict) and not tool.startswith("mcp__"):
        return _blank(response)
    return [{"type": "text", "text": notice}]


def _persisted_path(response):
    """The file the CLI saved the full output to, or None. Only a regular file in
    a `tool-results` directory under the agent's home, reached with no symlink,
    is accepted; anything else raises `_Unverifiable`."""
    path = response.get("persistedOutputPath") if isinstance(response, dict) else None
    if not path:
        return None
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _Unverifiable("persisted path is not absolute")
    real = os.path.realpath(path)
    root = os.path.realpath(TOOL_RESULTS_ROOT)
    if (real != os.path.normpath(path) or not real.startswith(root + os.sep)
            or os.path.basename(os.path.dirname(real)) != TOOL_RESULTS_DIR):
        raise _Unverifiable("persisted path is outside a tool-results directory")
    if not stat.S_ISREG(os.lstat(real).st_mode):
        raise _Unverifiable("persisted path is not a regular file")
    return real


def _replace(path, data, mode):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".trinity-redact-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), stat.S_IMODE(mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _cut_to_notice(path, notice):
    """Truncate the persisted file to the withhold notice, in place."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "wb") as f:
        f.write(notice.encode())


def _redact_persisted(path, shapes, hits):
    """Redact the persisted file in place. Returns "redacted", "withheld" or None."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        raw = f.read(PERSISTED_CAP_BYTES + 1) if st.st_size <= PERSISTED_CAP_BYTES else None
    if raw is None or len(raw) > PERSISTED_CAP_BYTES:
        _cut_to_notice(path, WITHHELD_OVERSIZE)
        return "withheld"
    text = raw.decode("utf-8", "surrogateescape")
    redacted = _redact_text(text, shapes, hits)
    if redacted == text:
        return None
    try:
        _replace(path, redacted.encode("utf-8", "surrogateescape"), st.st_mode)
    except OSError:
        _cut_to_notice(path, WITHHELD_ERROR)
        return "withheld"
    return "redacted"


def _emit(event, **fields):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))
    sys.stdout.flush()


def _ids(data):
    return {"session_id": data.get("session_id"), "tool_use_id": data.get("tool_use_id")}


def _on_success(data, shapes):
    tool = data.get("tool_name", "")
    response = data.get("tool_response", data.get("tool_output"))
    if response is None:
        return
    if _text_size(response) > SCAN_CAP_CHARS:
        raise _Oversize()
    path = _persisted_path(response)
    skip = ("stdout",) if isinstance(response, dict) and response.get("isImage") is True else ()
    hits = set()
    rewritten = _redact(response, shapes, hits, skip)
    persisted = _redact_persisted(path, shapes, hits) if path else None
    if persisted == "withheld":
        log_event("scanner_oversize", tool=tool, code="persisted_output_withheld",
                  persisted=True, **_ids(data))
    if hits:
        log_event("credential_redacted", tool=tool, code=_kind(tool) + "_output",
                  patterns=sorted(hits), persisted=persisted == "redacted", **_ids(data))
    if rewritten != response:
        _emit("PostToolUse", updatedToolOutput=_as_bash(rewritten, tool))


def _withhold_persisted(response):
    """On a withhold, the file the CLI named must not keep the original either."""
    try:
        path = _persisted_path(response)
        if path:
            _cut_to_notice(path, WITHHELD_ERROR)
    except Exception as exc:
        log_event("scanner_error", code="persisted_output_untouched", error=type(exc).__name__)


def _on_failure(data, shapes):
    error = data.get("error")
    text = error if isinstance(error, str) else ""
    if len(text) > SCAN_CAP_CHARS:
        raise _Oversize()
    hits = {name for _, _, name in _spans(text, shapes)}
    if hits:
        tool = data.get("tool_name", "")
        log_event("credential_unredacted", tool=tool, code=_kind(tool) + "_failure_output",
                  patterns=sorted(hits), **_ids(data))
        _emit("PostToolUseFailure", additionalContext=FAILURE_WARNING.format(shapes=", ".join(sorted(hits))))


def _withhold(response, tool, oversize):
    if tool == "Bash" or tool == "":
        return _withheld(response, WITHHELD_OVERSIZE if oversize else WITHHELD_ERROR)
    return _withheld_other(response, tool, WITHHELD_OVERSIZE_OTHER if oversize else WITHHELD_ERROR_OTHER)


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read())
        if not isinstance(data, dict):
            raise ValueError("hook input is not an object")
    except Exception as exc:
        log_event("scanner_error", code="input_unreadable", error=type(exc).__name__)
        _emit("PostToolUse", updatedToolOutput=_withheld(None, WITHHELD_ERROR))
        return

    failure = data.get("hook_event_name") == "PostToolUseFailure"
    tool = data.get("tool_name", "")
    if not isinstance(tool, str):
        tool = ""
    if is_platform_mcp_tool(tool):
        return
    try:
        shapes = _shapes(load_config())
        (_on_failure if failure else _on_success)(data, shapes)
    except _Oversize:
        log_event("scanner_oversize", tool=tool, code="output_too_large", **_ids(data))
        if failure:
            _emit("PostToolUseFailure", additionalContext=FAILURE_UNCHECKED.format(why="too large to check"))
        else:
            response = _as_bash(data.get("tool_response", data.get("tool_output")), tool)
            _withhold_persisted(response)
            _emit("PostToolUse", updatedToolOutput=_withhold(response, tool, oversize=True))
    except Exception as exc:
        code = "persisted_output_unverifiable" if isinstance(exc, _Unverifiable) else "scanner_exception"
        log_event("scanner_error", tool=tool, code=code, error=type(exc).__name__, **_ids(data))
        if failure:
            _emit("PostToolUseFailure", additionalContext=FAILURE_UNCHECKED.format(why="the check failed"))
        else:
            response = _as_bash(data.get("tool_response", data.get("tool_output")), tool)
            if not isinstance(exc, _Unverifiable):
                _withhold_persisted(response)
            _emit("PostToolUse", updatedToolOutput=_withhold(response, tool, oversize=False))


if __name__ == "__main__":
    run_hook(main)
