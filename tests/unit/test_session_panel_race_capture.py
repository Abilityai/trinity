"""
Issue #789 — Session tab follow-ups for #782:
  1. KeepAlive `onActivated` TOCTOU race against Vue prop reactivity
  2. Missing optimistic user-message insert

These guards pin the two structural invariants that prevent the bugs
from regressing in a future refactor. Both are JS/Vue source files, so
the existing AST-only test convention (test_session_inflight_sentinel.py)
is extended here with text/regex parsing rather than Python's `ast`
module — the codebase has no JS test runner and Trinity's Python `ast`
can't parse Vue/JS.

Guards:

1. **`onActivated` captures `props.agentName` BEFORE the first `await`** —
   `AgentDetail`'s route watcher updates `agent.value` asynchronously,
   so `SessionPanel.props.agentName` can flip mid-await. Reading
   `props.agentName` after an await would re-introduce the cross-agent
   404 polling bug observed live and documented in #789.

2. **`onActivated` does not re-read `props.agentName` after the await** —
   any post-await usage indicates the capture pattern was undone.

3. **`sendMessage` mutates `messagesBySession` BEFORE the `axios.post`
   call** — the optimistic insert must happen synchronously when the
   user clicks send. If reordered after the await, the user stares at
   a bare spinner for the full turn duration (the #782 regression).

4. **The optimistic insert uses array spread, not assignment** — pins
   the `[...existing, ...]` pattern. A future refactor that re-assigns
   to a single-element array would erase prior conversation history
   on every send.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSION_PANEL_VUE = (
    _PROJECT_ROOT / "src" / "frontend" / "src" / "components" / "SessionPanel.vue"
)
_SESSIONS_STORE_JS = (
    _PROJECT_ROOT / "src" / "frontend" / "src" / "stores" / "sessions.js"
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text()


def _extract_balanced_block(text: str, opener_pattern: str) -> str:
    """Return the substring from the first match of `opener_pattern`
    through the matching closing brace. Brace-balanced (counts `{`/`}`
    only — fine for Vue/JS that doesn't put braces in single-line
    strings within these specific blocks).
    """
    m = re.search(opener_pattern, text)
    assert m is not None, f"opener pattern not found: {opener_pattern!r}"
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"unbalanced braces after {opener_pattern!r}"
    return text[start : i - 1]


def _strip_js_comments(src: str) -> str:
    """Replace JS/Vue line and block comments with whitespace of the same
    length so character offsets are preserved for downstream ordering
    asserts. We replace rather than delete because the regression tests
    assert `match.start() < other.start()` — corrupting offsets would
    make those asserts misleading.
    """
    # Block comments /* ... */
    src = re.sub(
        r"/\*.*?\*/",
        lambda m: re.sub(r"[^\n]", " ", m.group(0)),
        src,
        flags=re.DOTALL,
    )
    # Line comments // ...
    src = re.sub(
        r"//[^\n]*",
        lambda m: " " * len(m.group(0)),
        src,
    )
    return src


# ---------------------------------------------------------------------------
# Guards on SessionPanel.vue — onActivated capture-before-await
# ---------------------------------------------------------------------------


def test_onactivated_captures_agentname_before_first_await():
    """The TOCTOU fix. `const agentName = props.agentName` must appear
    inside `onActivated` before any `await` keyword. Without this, a
    Vue prop update during the await pairs the new agent name with the
    previous session id and the polling closure 404s forever."""
    src = _strip_js_comments(_read(_SESSION_PANEL_VUE))
    body = _extract_balanced_block(src, r"onActivated\s*\(\s*async\s*\(\s*\)\s*=>\s*\{")

    capture_match = re.search(
        r"const\s+agentName\s*=\s*props\.agentName\b", body
    )
    assert capture_match is not None, (
        "onActivated must capture `const agentName = props.agentName` "
        "into a local before any await. See #789 — without the capture, "
        "props.agentName can update mid-await and pair the new agent "
        "with the previous session id, 404'ing polling indefinitely."
    )

    first_await = re.search(r"\bawait\b", body)
    assert first_await is not None, "onActivated body must contain at least one await"
    assert capture_match.start() < first_await.start(), (
        "agentName capture must come BEFORE the first `await` in onActivated. "
        "Current ordering re-introduces the #789 race."
    )


def test_onactivated_does_not_reread_props_agentname_after_capture():
    """Defensive guard: any `props.agentName` read inside the
    onActivated body (after the capture line) defeats the purpose of
    the capture. The captured local `agentName` should be the only
    name used in subsequent calls."""
    src = _strip_js_comments(_read(_SESSION_PANEL_VUE))
    body = _extract_balanced_block(src, r"onActivated\s*\(\s*async\s*\(\s*\)\s*=>\s*\{")

    # Drop the capture line itself before searching for additional reads.
    body_after_capture = re.sub(
        r"const\s+agentName\s*=\s*props\.agentName\s*", "", body, count=1
    )
    stray = re.search(r"\bprops\.agentName\b", body_after_capture)
    # An additional read of props.agentName inside this hook is allowed
    # only on a pre-await control-flow guard line (e.g.
    # `if (props.agentStatus !== 'running') return`). agentName itself
    # must not be re-read.
    assert stray is None, (
        f"onActivated re-reads `props.agentName` at offset {stray.start()} "
        "after the capture. This re-opens the #789 race — use the captured "
        "`agentName` local instead."
    )


# ---------------------------------------------------------------------------
# Guards on stores/sessions.js — optimistic insert ordering + shape
# ---------------------------------------------------------------------------


def test_sendmessage_optimistic_insert_precedes_axios_post():
    """The user-message echo fix. `messagesBySession[sessionId] = [...]`
    must run BEFORE the `axios.post(...)` for the turn endpoint. If
    re-ordered, the user sees only a bare spinner for the full turn
    duration (the #782 regression that #789 reverses)."""
    src = _strip_js_comments(_read(_SESSIONS_STORE_JS))
    body = _extract_balanced_block(
        src, r"async\s+sendMessage\s*\(\s*agentName,\s*sessionId[^)]*\)\s*\{"
    )

    optimistic_match = re.search(
        r"this\.messagesBySession\s*\[\s*sessionId\s*\]\s*=\s*\[", body
    )
    assert optimistic_match is not None, (
        "sendMessage must assign `this.messagesBySession[sessionId] = [...]` "
        "to perform the optimistic user-message insert. Without it the user "
        "sees only a spinner for the full turn duration (#789)."
    )

    post_match = re.search(r"axios\.post\s*\(", body)
    assert post_match is not None, (
        "sendMessage must POST to the turn endpoint — pattern `axios.post(` not found"
    )
    assert optimistic_match.start() < post_match.start(), (
        "Optimistic insert into messagesBySession must precede axios.post(). "
        "Current ordering recreates the #789 'bare spinner' bug."
    )


def test_optimistic_insert_uses_array_spread_not_replace():
    """The spread pattern `[...existing, { ... }]` preserves prior
    conversation history. A future refactor to `= [{ ... }]` (no
    spread) would erase the entire session history on every send."""
    src = _strip_js_comments(_read(_SESSIONS_STORE_JS))
    body = _extract_balanced_block(
        src, r"async\s+sendMessage\s*\(\s*agentName,\s*sessionId[^)]*\)\s*\{"
    )

    # Find the assignment block. Match across newlines.
    assign_match = re.search(
        r"this\.messagesBySession\s*\[\s*sessionId\s*\]\s*=\s*\[(.+?)\]",
        body,
        re.DOTALL,
    )
    assert assign_match is not None, (
        "Optimistic-insert assignment to messagesBySession[sessionId] not found"
    )
    rhs = assign_match.group(1)
    assert "...existing" in rhs or re.search(r"\.\.\.[A-Za-z_]\w*", rhs), (
        "Optimistic insert must spread the existing array (e.g. `[...existing, "
        "{...}]`). A bare `[{...}]` would erase prior conversation history "
        "on every send."
    )
    # And the new message must have the four fields the renderer reads.
    for field in ("role", "content", "timestamp"):
        assert re.search(rf"\b{field}\b\s*:", rhs), (
            f"Optimistic message must include `{field}:` — read by "
            f"SessionPanel.vue's messages computed."
        )
    # The role must literally be 'user'.
    assert re.search(r"role\s*:\s*['\"]user['\"]", rhs), (
        "Optimistic message role must be the literal 'user' — the "
        "renderer differentiates user vs assistant by this field."
    )
