"""Escape-first Telegram markdown→HTML conversion + entity-safe splitting (#2277).

The property that matters most here is the **escape-first rule**: every send
path uses parse_mode=HTML, so any raw `<`, `>`, `&` an agent emits (type hints,
tracebacks, pasted XML) used to trip Telegram's "can't parse entities" and the
adapter's fallback then stripped ALL formatting — precisely the messages that
contain code arrived as plain text. The converter now escapes everything
outside stashed code spans BEFORE styling, so those literals survive.

Second property: `_split_message` must never emit a chunk that fails HTML
parse on its own — a cut inside an open tag makes Telegram reject the
continuation chunk. Tags open at a cut are closed there and reopened in the
next chunk.

Module: src/backend/adapters/telegram_adapter.py (+ the proactive path in
        services/proactive_message_service.py now routes through the same
        converter, and services/channel_completion_report.py dropped its
        pre-escape workaround so it no longer double-escapes)
Issue:  https://github.com/Abilityai/trinity/issues/2277
"""

import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from adapters.telegram_adapter import (  # noqa: E402
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramAdapter,
)

md = TelegramAdapter._markdown_to_html
split = TelegramAdapter._split_message
strip = TelegramAdapter._strip_html


def _balanced(chunk: str) -> bool:
    """Telegram-style entity check: every tag closed, properly nested."""
    stack = []
    for m in TelegramAdapter._HTML_TAG_RE.finditer(chunk):
        if m.group(1):
            if not stack or stack[-1] != m.group(2).lower():
                return False
            stack.pop()
        else:
            stack.append(m.group(2).lower())
    return not stack


def _no_raw_angles(chunk: str) -> bool:
    """Outside of tags, no bare < or > may remain."""
    outside = re.sub(r"<[^>]+>", "", chunk)
    return "<" not in outside and ">" not in outside


# ---------------------------------------------------------------- escaping ---


def test_raw_angle_brackets_survive_as_escaped_literals():
    out = md("error in Optional<str> -> None & co")
    assert "Optional&lt;str&gt;" in out
    assert "&amp; co" in out
    assert _balanced(out) and _no_raw_angles(out)


def test_plain_text_passthrough():
    assert md("hello") == "hello"


def test_strip_html_unescapes_for_plain_fallback():
    # The old fallback DELETED `<...>` substrings from failure reports.
    assert (
        strip(md("err: <class 'ValueError'> & `x<1`"))
        == "err: <class 'ValueError'> & x<1"
    )


# ------------------------------------------------------------ inline styles ---


def test_inline_styles():
    out = md("**b** and *i* and ~~s~~ and ||sp|| but file_name and 2*3*4 stay")
    assert "<b>b</b>" in out
    assert "<i>i</i>" in out
    assert "<s>s</s>" in out
    assert "<tg-spoiler>sp</tg-spoiler>" in out
    assert "file_name" in out and "2*3*4" in out


def test_headers():
    out = md("# Big\n## Also big\n### Small\nplain # not a header")
    assert "<b><u>Big</u></b>" in out
    assert "<b><u>Also big</u></b>" in out
    assert "<b>Small</b>" in out
    assert "plain # not a header" in out


def test_links_with_query_string():
    out = md("see [docs](https://example.com/a?b=1&c=2) ok")
    assert '<a href="https://example.com/a?b=1&amp;c=2">docs</a>' in out


def test_bullets_and_hr_do_not_become_emphasis():
    out = md("- item one\n* item two\n---\ndone")
    assert "• item one" in out and "• item two" in out
    assert "<i>" not in out


# -------------------------------------------------------------- code spans ---


def test_fence_keeps_language_and_escapes_content():
    out = md("```python\nif a < b:\n    print('x & y')\n```")
    assert '<pre><code class="language-python">' in out
    assert "a &lt; b" in out and "x &amp; y" in out


def test_bare_fence():
    out = md("```\nplain <code>\n```")
    assert out.startswith("<pre><code>")
    assert "&lt;code&gt;" in out


def test_inline_code_protected_from_styling():
    out = md("run `pip install **notbold** <x>` now")
    assert "<code>pip install **notbold** &lt;x&gt;</code>" in out


# -------------------------------------------------------------- blockquotes ---


def test_short_blockquote():
    out = md("> one\n> two\nafter")
    assert "<blockquote>one\ntwo</blockquote>" in out
    assert " expandable" not in out


def test_long_blockquote_collapses():
    out = md("\n".join(f"> line {i}" for i in range(8)))
    assert "<blockquote expandable>" in out


def test_styled_quote_nests():
    out = md("> **hot** take")
    assert "<blockquote><b>hot</b> take</blockquote>" in out


# ------------------------------------------------------------------- tables ---


def test_table_becomes_pre_without_nested_tags():
    out = md("| a | b |\n|---|---|\n| **x** | <y> |\n")
    assert out.strip().startswith("<pre>")
    assert "**x**" in out  # cells are NOT styled — nested tags are invalid in <pre>
    assert "&lt;y&gt;" in out
    assert _balanced(out)


# ---------------------------------------------------------------- splitting ---


def test_split_is_entity_safe_across_pre_block():
    big = (
        "# Report\n```python\n"
        + "\n".join(f"value_{i} = {i}  # < & >" for i in range(400))
        + "\n```\ntail **bold**"
    )
    chunks = split(md(big))
    assert len(chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_MESSAGE_LENGTH for c in chunks)
    assert all(_balanced(c) for c in chunks)
    assert all(_no_raw_angles(c) for c in chunks)
    # the cut closed <pre><code> and the continuation reopened it
    assert chunks[1].startswith("<pre><code")
    # nothing was duplicated or lost across the cut
    text_only = "".join(re.sub(r"</?[a-zA-Z][^>]*>", "", c) for c in chunks)
    assert text_only.count("value_399") == 1


def test_plain_long_text_splits_at_paragraphs():
    chunks = split(("para. " * 300 + "\n\n") * 4)
    assert len(chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_MESSAGE_LENGTH for c in chunks)


def test_short_text_is_single_chunk():
    assert split("hello") == ["hello"]


# ------------------------------------------------------------ whole pipeline ---


def test_realistic_agent_report_parses_clean():
    report = (
        "# Fleet brief\n\n**2 agents** need attention:\n\n"
        "| agent | status |\n|---|---|\n| pm | DORMANT |\n\n"
        "> Root cause: token expired -> 403 <org disabled>\n> Fix: stop+start\n\n"
        "Run `trinity restart pm` or see [runbook](https://docs.ability.ai/guides).\n\n"
        '```bash\ndocker restart agent-pm && echo "ok < done"\n```\n'
    )
    out = md(report)
    assert _balanced(out) and _no_raw_angles(out)
    assert "<b>2 agents</b>" in out
    assert "<blockquote>" in out
    assert '<a href="https://docs.ability.ai/guides">runbook</a>' in out
    assert 'class="language-bash"' in out
    assert "&lt;org disabled&gt;" in out
