"""
Unit tests for Slack rich text formatting (#223 → re-targeted #293).

Originally tested the third-party `slackify-markdown` library directly.
After #293 replaced that library with our own `services.slack_mrkdwn`
renderer (because slackify-markdown 0.2.2 dropped nested-list indent,
swallowed blank lines before headings, prefixed only the first blockquote
line, and passed tables through verbatim), these tests were re-targeted
at the new renderer.

For the comprehensive #293 test suite covering all five bug categories,
see `tests/unit/test_slack_mrkdwn.py`. This file remains as the smaller
inline-formatting smoke test originally written for #223.

Module: src/backend/services/slack_mrkdwn.py
Issues: https://github.com/abilityai/trinity/issues/223
        https://github.com/abilityai/trinity/issues/293
"""

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.slack_mrkdwn import to_slack_mrkdwn


def slackify(text):
    """Compat shim: keep the old test method names readable."""
    return to_slack_mrkdwn(text)


class TestSlackifyMarkdown:
    """Test our renderer converts standard markdown to Slack mrkdwn."""

    def test_bold(self):
        assert "*bold*" in slackify("**bold**")

    def test_link(self):
        result = slackify("[Google](https://google.com)")
        assert "<https://google.com|Google>" in result

    def test_inline_code(self):
        result = slackify("use `pip install`")
        assert "`pip install`" in result

    def test_code_block(self):
        result = slackify("```python\nprint('hello')\n```")
        assert "```" in result
        assert "print('hello')" in result

    def test_heading_becomes_bold(self):
        result = slackify("# Main Title")
        assert "Main Title" in result

    def test_unordered_list(self):
        result = slackify("- item one\n- item two")
        assert "item one" in result
        assert "item two" in result

    def test_plain_text_unchanged(self):
        text = "Hello, how can I help you?"
        assert slackify(text).strip() == text

    def test_mixed_formatting(self):
        text = "**Important**: Check [docs](https://example.com) for `details`"
        result = slackify(text)
        assert "*Important*" in result
        assert "<https://example.com|docs>" in result
        assert "`details`" in result

    def test_empty_string(self):
        assert slackify("") == ""

    def test_multiline(self):
        text = "First line\n\nSecond line"
        result = slackify(text)
        assert "First line" in result
        assert "Second line" in result

    def test_real_agent_response(self):
        """Test with an actual agent response format."""
        text = (
            "**What I can determine:**\n"
            "- It's a **security alert** from GitHub\n"
            "- **Severity:** Critical\n"
            "- Check the [documentation](https://docs.github.com) for details"
        )
        result = slackify(text)
        # Bold converted
        assert "**" not in result
        assert "*What I can determine:*" in result
        # Link converted
        assert "<https://docs.github.com|documentation>" in result
        # List items preserved
        assert "security alert" in result


class TestFormatResponseContract:
    """Test the format_response contract: default is passthrough, Slack overrides."""

    def test_slack_does_not_passthrough(self):
        """Slack's format_response must change the text (not passthrough)."""
        text = "**bold** and [link](https://example.com)"
        result = slackify(text)
        assert result != text  # Slack adapter must transform, not passthrough

    def test_slack_override_converts(self):
        """Slack's format_response converts markdown to mrkdwn."""
        text = "**bold** and [link](https://example.com)"
        result = slackify(text)
        assert "**" not in result
        assert "*bold*" in result
        assert "<https://example.com|link>" in result
