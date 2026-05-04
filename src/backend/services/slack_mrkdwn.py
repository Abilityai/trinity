"""
Convert standard Markdown → Slack mrkdwn (#293).

Replaces the third-party `slackify-markdown==0.2.2` library which dropped
nested-list indentation, swallowed blank lines before headings, applied
the blockquote `>` prefix only to the first line, and passed Markdown
tables through unchanged. All five problems compounded into the "ugly
formatting" reported in #293.

Public API
----------
    to_slack_mrkdwn(text: str) -> str

Renders to Slack's `mrkdwn` text format (the default rendering for the
`text` field of `chat.postMessage`). Output is safe to send directly via
`slack_service.send_message(text=..., ...)`.

Slack mrkdwn syntax (https://api.slack.com/reference/surfaces/formatting):
    bold:           *text*           (single-asterisk, not **double**)
    italic:         _text_
    strikethrough:  ~text~
    inline code:    `text`
    code block:     ```text```       (no language fence)
    link:           <url|label>
    bulleted list:  • item           (uses bullet character, no leading dash)
    blockquote:     > line           (one `>` per line — no exceptions)
    headings:       not supported    (we render as bold + blank lines for spacing)
    tables:         not supported    (we render as fenced code-block with aligned columns)
    HR:             not supported    (we emit a unicode rule)

Architecture
------------
Stateful AST walker over `markdown-it-py` tokens (the same parser the old
library used internally — proven correct at the parse layer; the bugs were
all in the rendering layer). The walker keeps a small stack:

  - list_stack:     current nesting depth + ordered/unordered + counter
  - in_blockquote:  toggle that prefixes every emitted line
  - inline_buffer:  collect inline tokens then flush as one line

Adding a new token type = adding one method on the walker (Open-Closed).
"""
from __future__ import annotations

from typing import List, Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token


# Single shared parser — markdown-it-py is thread-safe for parsing.
_MD = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def to_slack_mrkdwn(text: str) -> str:
    """Convert a Markdown string to Slack mrkdwn.

    Empty / non-string input returns the input unchanged (str-coerced),
    so callers can pass agent output verbatim without nil-checking.
    """
    if not text:
        return text or ""
    if not isinstance(text, str):
        text = str(text)
    tokens = _MD.parse(text)
    walker = _SlackMrkdwnWalker()
    walker.walk(tokens)
    return walker.result()


# ---------------------------------------------------------------------------
# Internal: stateful AST walker
# ---------------------------------------------------------------------------


class _ListFrame:
    """One entry on the list-nesting stack."""

    __slots__ = ("ordered", "counter")

    def __init__(self, ordered: bool):
        self.ordered = ordered
        self.counter = 1  # next number to emit (ordered lists only)


class _SlackMrkdwnWalker:
    """Walks markdown-it tokens and emits Slack mrkdwn line by line.

    Output is built into `self._lines` (list of strings, each one a line
    WITHOUT trailing newline). `result()` joins with `\n` and adds a single
    trailing newline — Slack tolerates trailing whitespace.

    Heading spacing rule:
        Headings render as `*text*` on their own line WITH a blank line
        before AND after (when not at top-of-document). The third-party
        library only emitted the blank line AFTER, which crammed headings
        against any preceding content.
    """

    # The bullet character Slack renders. Real Slack clients render `•`
    # natively, so we don't need a leading hyphen — and a single space after
    # is the convention (the previous library used three spaces for no clear
    # reason).
    BULLET = "•"

    # Horizontal rule — Markdown `---` becomes a unicode separator line so
    # the structural break the author intended is visible in Slack.
    HR = "──────────"

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._list_stack: List[_ListFrame] = []
        # Blockquote depth (Markdown allows nested blockquotes — we flatten
        # to a single `>` prefix because Slack doesn't render nesting).
        self._blockquote_depth = 0
        # Inline buffer accumulates `text`/`em`/`strong`/`link` etc. inside
        # a paragraph, list item, heading, or blockquote so we emit one
        # final line with all the inline formatting applied at once.
        self._inline: List[str] = []
        # Pending blank line: set by handlers that want a blank line before
        # the NEXT block emit (e.g. heading_open). Avoids leading blank lines
        # at the top of the document.
        self._want_blank_before_next = False
        # Suppress next paragraph's natural trailing blank — used inside
        # tight lists where extra blanks look wrong.
        self._tight_list_depth = 0

    # ---- public ---------------------------------------------------------

    def walk(self, tokens: List[Token]) -> None:
        for tok in tokens:
            self._dispatch(tok)

    def result(self) -> str:
        # Strip trailing blanks, add a single trailing newline.
        while self._lines and self._lines[-1] == "":
            self._lines.pop()
        return "\n".join(self._lines) + "\n"

    # ---- emission helpers ----------------------------------------------

    def _emit_line(self, line: str) -> None:
        if self._want_blank_before_next and self._lines:
            # Don't double-blank: if the previous emit already added a blank
            # (e.g. trailing blank from a closing paragraph), this is a no-op.
            if self._lines[-1] != "":
                self._lines.append("")
            self._want_blank_before_next = False
        prefix = self._line_prefix()
        self._lines.append(prefix + line)

    def _emit_blank(self) -> None:
        if self._tight_list_depth:
            return
        if not self._lines or self._lines[-1] != "":
            self._lines.append("")

    def _line_prefix(self) -> str:
        """Compute the leading prefix for the current logical line.

        Order matters: blockquote `> ` wraps everything (including list
        markers); list indentation is two spaces per nesting level outside
        the blockquote. Slack renders 4-space-indented bullets correctly
        as nested lists.
        """
        parts: List[str] = []
        if self._blockquote_depth:
            parts.append("> ")
        # List nesting (indentation only — the bullet/number is inserted
        # by `list_item_open`)
        if self._list_stack:
            parts.append("    " * (len(self._list_stack) - 1))
        return "".join(parts)

    def _flush_inline(self) -> str:
        out = "".join(self._inline)
        self._inline = []
        return out

    # ---- dispatch -------------------------------------------------------

    def _dispatch(self, tok: Token) -> None:
        # `inline` tokens are containers — recurse into their children
        if tok.type == "inline":
            for child in tok.children or []:
                self._dispatch_inline(child)
            return
        handler = getattr(self, f"_block_{tok.type}", None)
        if handler:
            handler(tok)

    def _dispatch_inline(self, tok: Token) -> None:
        handler = getattr(self, f"_inline_{tok.type}", None)
        if handler:
            handler(tok)

    # ---- block handlers -------------------------------------------------

    def _block_paragraph_open(self, tok: Token) -> None:
        pass

    def _block_paragraph_close(self, tok: Token) -> None:
        line = self._flush_inline()
        if line:
            self._emit_line(line)
        # Trailing blank between paragraphs — but not inside tight lists or
        # blockquote continuations.
        if not self._tight_list_depth:
            self._emit_blank()

    def _block_heading_open(self, tok: Token) -> None:
        # Blank line BEFORE the heading (the bug we're fixing) — only if
        # we've already emitted content.
        if self._lines:
            self._want_blank_before_next = True
        self._inline.append("*")

    def _block_heading_close(self, tok: Token) -> None:
        self._inline.append("*")
        line = self._flush_inline()
        if line:
            self._emit_line(line)
        self._emit_blank()

    def _block_bullet_list_open(self, tok: Token) -> None:
        # Tight list = no blank lines between items. markdown-it sets
        # `meta` or hidden attrs; the simplest signal is that no `paragraph`
        # token appears inside list items in tight lists. We treat ALL
        # nested lists as tight by default — looks better in Slack.
        self._list_stack.append(_ListFrame(ordered=False))
        self._tight_list_depth += 1

    def _block_bullet_list_close(self, tok: Token) -> None:
        if self._list_stack:
            self._list_stack.pop()
        self._tight_list_depth = max(0, self._tight_list_depth - 1)
        if not self._list_stack:
            self._emit_blank()

    def _block_ordered_list_open(self, tok: Token) -> None:
        self._list_stack.append(_ListFrame(ordered=True))
        self._tight_list_depth += 1

    def _block_ordered_list_close(self, tok: Token) -> None:
        if self._list_stack:
            self._list_stack.pop()
        self._tight_list_depth = max(0, self._tight_list_depth - 1)
        if not self._list_stack:
            self._emit_blank()

    def _block_list_item_open(self, tok: Token) -> None:
        if not self._list_stack:
            return
        frame = self._list_stack[-1]
        if frame.ordered:
            marker = f"{frame.counter}. "
            frame.counter += 1
        else:
            marker = f"{self.BULLET} "
        # Marker is the first thing in the inline buffer for this item's
        # paragraph.
        self._inline.append(marker)

    def _block_list_item_close(self, tok: Token) -> None:
        # Any unflushed inline (item without a wrapping paragraph) emits now
        line = self._flush_inline()
        if line:
            self._emit_line(line)

    def _block_blockquote_open(self, tok: Token) -> None:
        self._blockquote_depth += 1
        # Preceding blank — blockquotes need visual separation
        if self._lines and self._lines[-1] != "":
            self._emit_blank()

    def _block_blockquote_close(self, tok: Token) -> None:
        self._blockquote_depth = max(0, self._blockquote_depth - 1)
        if not self._blockquote_depth:
            self._emit_blank()

    def _block_fence(self, tok: Token) -> None:
        # Fenced code block. Slack ignores language fences; we drop them.
        self._emit_blank()
        # Open fence on its own line (no language)
        self._emit_line("```")
        # Body — keep all internal lines verbatim, including leading spaces.
        # Strip exactly one trailing newline (markdown-it always adds one).
        body = tok.content
        if body.endswith("\n"):
            body = body[:-1]
        for content_line in body.split("\n"):
            self._emit_line(content_line)
        self._emit_line("```")
        self._emit_blank()

    def _block_code_block(self, tok: Token) -> None:
        # Indented code block — same rendering as fenced.
        self._block_fence(tok)

    def _block_hr(self, tok: Token) -> None:
        self._emit_blank()
        self._emit_line(self.HR)
        self._emit_blank()

    # ---- table handling ------------------------------------------------
    #
    # Slack has no table primitive. We collect rows, compute per-column
    # max width, and emit a fenced code block with monospace alignment —
    # the closest Slack can render to the author's intent. Matches what
    # GitHub/Discord do when their renderer can't.

    def _block_table_open(self, tok: Token) -> None:
        self._table_rows: List[List[str]] = []
        self._table_current_row: Optional[List[str]] = None
        self._table_current_cell: Optional[List[str]] = None

    def _block_thead_open(self, tok: Token) -> None:
        pass

    def _block_thead_close(self, tok: Token) -> None:
        pass

    def _block_tbody_open(self, tok: Token) -> None:
        pass

    def _block_tbody_close(self, tok: Token) -> None:
        pass

    def _block_tr_open(self, tok: Token) -> None:
        self._table_current_row = []

    def _block_tr_close(self, tok: Token) -> None:
        if self._table_current_row is not None:
            self._table_rows.append(self._table_current_row)
            self._table_current_row = None

    def _block_th_open(self, tok: Token) -> None:
        self._table_current_cell = []
        self._inline = self._table_current_cell  # redirect inline buffer to cell

    def _block_th_close(self, tok: Token) -> None:
        if self._table_current_row is not None and self._table_current_cell is not None:
            self._table_current_row.append("".join(self._table_current_cell))
        self._table_current_cell = None
        self._inline = []  # reset to default buffer

    def _block_td_open(self, tok: Token) -> None:
        self._block_th_open(tok)

    def _block_td_close(self, tok: Token) -> None:
        self._block_th_close(tok)

    def _block_table_close(self, tok: Token) -> None:
        if not self._table_rows:
            return
        # Compute column widths
        ncols = max(len(r) for r in self._table_rows)
        widths = [0] * ncols
        for row in self._table_rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        # Emit as fenced code block (monospace = aligned)
        self._emit_blank()
        self._emit_line("```")
        # Header row + separator + body rows. First row treated as header.
        for ridx, row in enumerate(self._table_rows):
            padded = [
                (row[i] if i < len(row) else "").ljust(widths[i])
                for i in range(ncols)
            ]
            self._emit_line("  ".join(padded).rstrip())
            if ridx == 0:
                # Separator
                self._emit_line("  ".join("-" * w for w in widths))
        self._emit_line("```")
        self._emit_blank()
        self._table_rows = []

    # ---- inline handlers ------------------------------------------------

    def _inline_text(self, tok: Token) -> None:
        self._inline.append(tok.content)

    def _inline_softbreak(self, tok: Token) -> None:
        # Soft break inside a paragraph — flush current line, start a new
        # one with the same prefix. This preserves user-intended line breaks
        # within a paragraph (e.g. inside a blockquote or list item).
        line = self._flush_inline()
        if line:
            self._emit_line(line)

    def _inline_hardbreak(self, tok: Token) -> None:
        self._inline_softbreak(tok)

    def _inline_strong_open(self, tok: Token) -> None:
        self._inline.append("*")

    def _inline_strong_close(self, tok: Token) -> None:
        self._inline.append("*")

    def _inline_em_open(self, tok: Token) -> None:
        self._inline.append("_")

    def _inline_em_close(self, tok: Token) -> None:
        self._inline.append("_")

    def _inline_s_open(self, tok: Token) -> None:
        self._inline.append("~")

    def _inline_s_close(self, tok: Token) -> None:
        self._inline.append("~")

    def _inline_code_inline(self, tok: Token) -> None:
        self._inline.append(f"`{tok.content}`")

    def _inline_link_open(self, tok: Token) -> None:
        href = tok.attrGet("href") or ""
        # Defer label collection to a small per-link buffer so we can emit
        # the Slack-format `<url|label>` atom on link_close.
        self._link_href = href
        self._link_label_start = len(self._inline)

    def _inline_link_close(self, tok: Token) -> None:
        # Pull collected label text, replace it with the Slack atom
        label_parts = self._inline[self._link_label_start:]
        del self._inline[self._link_label_start:]
        label = "".join(label_parts)
        href = getattr(self, "_link_href", "")
        # Slack mrkdwn link: <url|label> — if label == url, omit the pipe
        if label == href:
            self._inline.append(f"<{href}>")
        else:
            self._inline.append(f"<{href}|{label}>")
        self._link_href = ""

    def _inline_image(self, tok: Token) -> None:
        # Slack doesn't inline images in mrkdwn; emit the alt text + link.
        href = tok.attrGet("src") or ""
        alt = (tok.content or tok.attrGet("alt") or "image").strip()
        if href:
            self._inline.append(f"<{href}|{alt}>")
        else:
            self._inline.append(alt)

    def _inline_html_inline(self, tok: Token) -> None:
        # HTML disabled in the parser config; this should not fire. If it
        # does (raw HTML in a code-context boundary case), pass through.
        self._inline.append(tok.content)
