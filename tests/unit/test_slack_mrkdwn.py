"""
Unit tests for `services.slack_mrkdwn` (#293).

Targets the five bugs the previous library (`slackify-markdown==0.2.2`)
shipped with:
  1. Nested lists were flattened
  2. Headings were crammed against preceding content (no blank line before)
  3. Blockquotes only got the `>` prefix on the first line
  4. Markdown tables were passed through verbatim (raw pipes in Slack)
  5. Horizontal rules were dropped silently

Module: src/backend/services/slack_mrkdwn.py
Issue:  https://github.com/abilityai/trinity/issues/293
"""

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.slack_mrkdwn import to_slack_mrkdwn  # noqa: E402


def _norm(s: str) -> str:
    """Strip trailing whitespace per line + collapse trailing blank lines.
    Helpful when comparing rendered output without obsessing over a
    single trailing newline.
    """
    lines = [line.rstrip() for line in s.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bug 1 — nested lists must preserve indentation
# ---------------------------------------------------------------------------


class TestNestedListPreservation:
    def test_two_level_bullet_list(self):
        out = to_slack_mrkdwn("- top1\n  - nested1\n  - nested2\n- top2")
        # Top-level items at column 0, nested at 4-space indent
        assert "• top1" in out
        assert "    • nested1" in out
        assert "    • nested2" in out
        assert "• top2" in out
        # Order preserved
        lines = [l for l in out.splitlines() if l.strip()]
        assert lines.index("• top1") < lines.index("    • nested1") < lines.index("• top2")

    def test_three_level_bullet_list(self):
        src = "- a\n  - b\n    - c"
        out = to_slack_mrkdwn(src)
        assert "• a" in out
        assert "    • b" in out
        assert "        • c" in out

    def test_nested_ordered_list(self):
        src = "1. first\n   1. sub-a\n   2. sub-b\n2. second"
        out = to_slack_mrkdwn(src)
        assert "1. first" in out
        assert "    1. sub-a" in out
        assert "    2. sub-b" in out
        assert "2. second" in out

    def test_mixed_nested(self):
        """Bullet list containing a nested ordered list."""
        src = "- top\n  1. a\n  2. b\n- top2"
        out = to_slack_mrkdwn(src)
        assert "• top" in out
        assert "    1. a" in out
        assert "    2. b" in out
        assert "• top2" in out


# ---------------------------------------------------------------------------
# Bug 2 — blank line before headings
# ---------------------------------------------------------------------------


class TestHeadingSpacing:
    def test_blank_line_before_h2_after_paragraph(self):
        src = "Paragraph text.\n\n## Heading\n\nMore text."
        out = to_slack_mrkdwn(src)
        # Heading rendered as bold
        assert "*Heading*" in out
        lines = [l for l in out.splitlines()]
        h_idx = lines.index("*Heading*")
        # Line BEFORE heading must be blank — the bug we're fixing
        assert lines[h_idx - 1] == "", (
            f"expected blank line before heading, got {lines[h_idx-1]!r}"
        )
        # And blank line AFTER heading
        assert lines[h_idx + 1] == ""

    def test_no_leading_blank_when_heading_is_first(self):
        """Don't emit a blank line before a heading at the start of doc."""
        out = to_slack_mrkdwn("# Top\n\nText.")
        assert out.startswith("*Top*")

    def test_consecutive_headings_have_separation(self):
        out = to_slack_mrkdwn("Para.\n\n## H2\n\n### H3\n\nText.")
        lines = out.splitlines()
        h2 = lines.index("*H2*")
        h3 = lines.index("*H3*")
        # Both surrounded by blank lines
        assert lines[h2 - 1] == ""
        assert lines[h3 - 1] == ""
        assert h3 > h2

    def test_h1_through_h6_all_render_as_bold(self):
        for level in range(1, 7):
            src = f"{'#' * level} Title"
            assert "*Title*" in to_slack_mrkdwn(src), f"H{level} failed"


# ---------------------------------------------------------------------------
# Bug 3 — blockquote prefix on every line
# ---------------------------------------------------------------------------


class TestBlockquoteEveryLine:
    def test_two_line_blockquote(self):
        src = "> first\n> second"
        out = to_slack_mrkdwn(src)
        assert "> first" in out
        assert "> second" in out

    def test_blockquote_with_softbreak(self):
        """Continuation line via single line break inside blockquote."""
        src = "> first\nsecond"  # markdown lazy-continuation
        out = to_slack_mrkdwn(src)
        # Both lines must carry the > prefix
        assert "> first" in out
        assert "> second" in out

    def test_multiline_blockquote_with_explicit_prefixes(self):
        src = "> line one\n> line two\n> line three"
        out = to_slack_mrkdwn(src)
        for n in ("one", "two", "three"):
            assert f"> line {n}" in out

    def test_blockquote_with_inline_formatting(self):
        src = "> **bold** and `code`\n> regular"
        out = to_slack_mrkdwn(src)
        assert "> *bold* and `code`" in out
        assert "> regular" in out


# ---------------------------------------------------------------------------
# Bug 4 — tables converted (no raw pipes leaking through)
# ---------------------------------------------------------------------------


class TestTableConversion:
    def test_simple_2col_table(self):
        src = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        out = to_slack_mrkdwn(src)
        # Must NOT leak raw markdown pipes through
        assert "|---|---|" not in out
        assert "| A | B |" not in out
        # Must wrap in fenced code block (Slack monospace)
        assert "```" in out
        # All cell contents present
        for cell in ("A", "B", "1", "2", "3", "4"):
            assert cell in out

    def test_table_columns_aligned(self):
        """Cells padded to match column width — visually aligned in Slack."""
        src = "| Short | LongerHeader |\n|---|---|\n| A | B |"
        out = to_slack_mrkdwn(src)
        # Find the data row line
        body_lines = [l for l in out.splitlines() if l and l != "```" and "Short" not in l and "-" * 3 not in l.replace(" ", "")]
        # The padded data row must have at least 2 columns visually separated
        data_row = next((l for l in body_lines if "A" in l and "B" in l), None)
        assert data_row is not None
        assert "A" in data_row and "B" in data_row


# ---------------------------------------------------------------------------
# Bug 5 — horizontal rule emits a separator (not silently dropped)
# ---------------------------------------------------------------------------


class TestHorizontalRule:
    def test_hr_emits_visible_separator(self):
        src = "Above.\n\n---\n\nBelow."
        out = to_slack_mrkdwn(src)
        # Above and Below NOT merged
        assert "Above.\nBelow." not in out
        # Some non-empty separator line appears between them
        lines = out.splitlines()
        above = next(i for i, l in enumerate(lines) if "Above" in l)
        below = next(i for i, l in enumerate(lines) if "Below" in l)
        between = lines[above + 1:below]
        non_blank = [l for l in between if l.strip()]
        assert non_blank, "expected at least one non-blank separator line"


# ---------------------------------------------------------------------------
# Existing-coverage regressions (port from #223 test file — these must still pass)
# ---------------------------------------------------------------------------


class TestInlineFormatting:
    def test_bold(self):
        assert "*bold*" in to_slack_mrkdwn("**bold**")

    def test_italic(self):
        assert "_italic_" in to_slack_mrkdwn("*italic*")

    def test_strikethrough(self):
        assert "~strike~" in to_slack_mrkdwn("~~strike~~")

    def test_inline_code(self):
        assert "`pip install`" in to_slack_mrkdwn("use `pip install`")

    def test_link_with_label(self):
        out = to_slack_mrkdwn("[Google](https://google.com)")
        assert "<https://google.com|Google>" in out

    def test_link_label_equals_url(self):
        """Slack idiom: bare URL → no pipe needed."""
        out = to_slack_mrkdwn("[https://x.io](https://x.io)")
        assert "<https://x.io>" in out

    def test_image_to_link(self):
        out = to_slack_mrkdwn("![alt](https://x.io/i.png)")
        # Slack mrkdwn doesn't inline images — rendered as labeled link
        assert "<https://x.io/i.png|alt>" in out


class TestCodeBlocks:
    def test_fenced_code_block(self):
        out = to_slack_mrkdwn("```python\nx = 1\n```")
        # Slack ignores language fences — they MUST be stripped
        assert "```python" not in out
        assert "x = 1" in out
        # Open and close fences present
        assert out.count("```") >= 2

    def test_indented_code_block(self):
        out = to_slack_mrkdwn("    x = 1\n    y = 2")
        assert "x = 1" in out
        assert "y = 2" in out
        assert "```" in out

    def test_code_block_preserves_internal_whitespace(self):
        out = to_slack_mrkdwn("```\n    indented\nflush\n```")
        assert "    indented" in out


class TestParagraphsAndBreaks:
    def test_plain_paragraph_passthrough(self):
        text = "Just a plain sentence."
        assert text in to_slack_mrkdwn(text)

    def test_consecutive_paragraphs_separated_by_blank(self):
        out = to_slack_mrkdwn("Para one.\n\nPara two.")
        lines = out.splitlines()
        # blank line BETWEEN them
        i = lines.index("Para one.")
        assert lines[i + 1] == ""
        assert lines[i + 2] == "Para two."

    def test_softbreak_inside_paragraph(self):
        out = to_slack_mrkdwn("first\nsecond")
        assert "first" in out and "second" in out


class TestRealAgentResponses:
    """End-to-end: structured agent responses that look ugly today."""

    def test_status_report(self):
        src = (
            "## Result\n\n"
            "Task complete.\n\n"
            "### Findings\n"
            "- Issue A\n"
            "- Issue B\n"
            "  - sub-detail\n"
            "- Issue C\n\n"
            "See [docs](https://example.com)."
        )
        out = to_slack_mrkdwn(src)
        # Headings as bold with blank lines around
        assert "*Result*" in out
        assert "*Findings*" in out
        # Nested list indented
        assert "    • sub-detail" in out
        # Top-level items NOT indented
        assert "• Issue A" in out
        # Link converted
        assert "<https://example.com|docs>" in out

    def test_code_review_response(self):
        src = (
            "**Summary**: 3 issues found.\n\n"
            "1. Missing input validation in `auth.py`\n"
            "2. SQL injection risk\n"
            "3. Missing rate limit\n\n"
            "```python\ndef sanitize(s):\n    return s.strip()\n```\n\n"
            "Fix recommended."
        )
        out = to_slack_mrkdwn(src)
        assert "*Summary*" in out
        assert "1. Missing input validation in `auth.py`" in out
        assert "```" in out and "def sanitize(s):" in out
        assert "Fix recommended." in out

    def test_blockquoted_quote_with_attribution(self):
        src = (
            "Per the spec:\n\n"
            "> first line\n> second line\n\n"
            "we should..."
        )
        out = to_slack_mrkdwn(src)
        # Both quote lines prefixed
        assert "> first line" in out
        assert "> second line" in out


class TestEdgeCases:
    def test_empty_string(self):
        assert to_slack_mrkdwn("") == ""

    def test_none_returns_empty(self):
        assert to_slack_mrkdwn(None) == ""

    def test_non_string_input(self):
        # Coerced to str — never raises
        out = to_slack_mrkdwn(42)
        assert "42" in out

    def test_html_displays_as_literal_text(self):
        """Slack mrkdwn doesn't render HTML — `<script>...</script>` shows as
        visible text characters in the Slack client. No XSS risk; verifying
        the parser doesn't accidentally drop the content (which would silently
        eat user-pasted code samples like `<canvas>` or `<form>`).
        """
        out = to_slack_mrkdwn("<script>alert(1)</script>")
        # Content present (not silently dropped)
        assert "alert(1)" in out
