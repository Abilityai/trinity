#!/usr/bin/env python3
"""Keeps credentials in Bash output from the model (GUARD-004, trinity-enterprise#165).

Registered twice for `Bash` in the managed settings:

* `PostToolUse` (the command exited 0): the matched spans are replaced with
  `[REDACTED:<shape>]` in a copy of `tool_response`, returned as
  `hookSpecificOutput.updatedToolOutput`. Every key, boolean and number is kept
  and every string leaf is scanned, lists included (`structuredContent`), because
  the CLI drops a rewrite that does not match the tool's output schema. Output
  the CLI persisted to `persistedOutputPath` is redacted in that file too. Clean
  output prints nothing.
* `PostToolUseFailure` (non-zero exit): no hook can rewrite that output, so a hit
  only adds `additionalContext` telling the model not to repeat it (decision D5).

Fails closed on content (D6): if the scan raises, its shapes cannot be loaded or
the output is over the cap, the result is withheld. It never exits 2: on
`PostToolUse` that only adds stderr and the original output still reaches the
model. A timeout or a crash before `main` cannot be caught here; the CLI then
passes the original through.

The shapes are `credential_patterns` in the baseline: the known-prefix
partition of `utils/credential_sanitizer.SECRET_VALUE_PATTERNS` (D4), copied
because a `-I -S` hook cannot import it. Events go to the guardrails log with
shape names only, never a value.
"""
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
)

PLACEHOLDER = "[REDACTED:{name}]"
SCAN_CAP_CHARS = 8 * 1024 * 1024
PERSISTED_CAP_BYTES = 8 * 1024 * 1024
TOOL_RESULTS_ROOT = "/home/developer"
TOOL_RESULTS_DIR = "tool-results"
_NAME = re.compile(r"[a-z0-9_]{1,32}")

_DO_NOT_RERUN = "The command already ran — do not run it again to see its output.]"
WITHHELD_ERROR = "[Output withheld: Trinity's credential check failed. " + _DO_NOT_RERUN
WITHHELD_OVERSIZE = ("[Output withheld: the output is too large for Trinity's credential check. "
                     + _DO_NOT_RERUN)
FAILURE_WARNING = ("The output above contains a credential Trinity could not hide ({shapes}). "
                   "Do not repeat, store or send it.")
FAILURE_UNCHECKED = ("Trinity could not check the output above for credentials ({why}). "
                     "If it contains one, do not repeat, store or send it.")


class _Oversize(Exception):
    pass


class _Unverifiable(Exception):
    pass


def _shapes(config):
    entries = config.get("credential_patterns")
    if not isinstance(entries, list) or not entries:
        raise ValueError("no credential_patterns")
    shapes = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(pattern, str):
            raise ValueError("malformed credential_patterns entry")
        shapes.append((re.compile(pattern), name))
    return shapes


def _text_size(value):
    total, stack = 0, [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return total


def _redact_text(text, shapes, hits):
    for pattern, name in shapes:
        text, count = pattern.subn(PLACEHOLDER.format(name=name), text)
        if count:
            hits.add(name)
    return text


def _redact(value, shapes, hits, skip=()):
    if isinstance(value, str):
        return _redact_text(value, shapes, hits)
    if isinstance(value, list):
        return [_redact(item, shapes, hits) for item in value]
    if isinstance(value, dict):
        return {k: v if k in skip else _redact(v, shapes, hits) for k, v in value.items()}
    return value


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
        log_event("scanner_oversize", tool=data.get("tool_name", ""), code="persisted_output_withheld",
                  persisted=True, **_ids(data))
    if hits:
        log_event("credential_redacted", tool=data.get("tool_name", ""), code="bash_output",
                  patterns=sorted(hits), persisted=persisted == "redacted", **_ids(data))
    if rewritten != response:
        _emit("PostToolUse", updatedToolOutput=rewritten)


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
    hits = {name for pattern, name in shapes if pattern.search(text)}
    if hits:
        log_event("credential_unredacted", tool=data.get("tool_name", ""), code="bash_failure_output",
                  patterns=sorted(hits), **_ids(data))
        _emit("PostToolUseFailure", additionalContext=FAILURE_WARNING.format(shapes=", ".join(sorted(hits))))


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
    try:
        shapes = _shapes(load_config())
        (_on_failure if failure else _on_success)(data, shapes)
    except _Oversize:
        log_event("scanner_oversize", tool=tool, code="output_too_large", **_ids(data))
        if failure:
            _emit("PostToolUseFailure", additionalContext=FAILURE_UNCHECKED.format(why="too large to check"))
        else:
            response = data.get("tool_response", data.get("tool_output"))
            _withhold_persisted(response)
            _emit("PostToolUse", updatedToolOutput=_withheld(response, WITHHELD_OVERSIZE))
    except Exception as exc:
        code = "persisted_output_unverifiable" if isinstance(exc, _Unverifiable) else "scanner_exception"
        log_event("scanner_error", tool=tool, code=code, error=type(exc).__name__, **_ids(data))
        if failure:
            _emit("PostToolUseFailure", additionalContext=FAILURE_UNCHECKED.format(why="the check failed"))
        else:
            response = data.get("tool_response", data.get("tool_output"))
            if not isinstance(exc, _Unverifiable):
                _withhold_persisted(response)
            _emit("PostToolUse", updatedToolOutput=_withheld(response, WITHHELD_ERROR))


if __name__ == "__main__":
    run_hook(main)
