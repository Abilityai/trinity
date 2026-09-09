"""#2585 — PR-authored text must not be able to speak in the bot's voice.

Both nightlies splice a downloaded diff artifact straight into a sticky comment
authored by `github-actions[bot]`. The artifact is produced by
`scripts/ci/diff-pytest-failures.py` running over the MERGED PR TREE, so its
content is chosen by whoever opened the PR — and this is a public repo that
evaluates fork PRs.

The impact is not RCE on the runner. It is that the bot's comment can be made to
say something its author never wrote — a forged all-clear, a link presented as
the workflow's own output. The bot's voice is the only reason anyone trusts that
comment.

── WHERE THE FIX WENT, AND WHY NOT WHERE THE ISSUE SAID ───────────────────────
The issue asks for a fence around the diff text in each workflow. Reading the
producer first changed the answer: the artifact is STRUCTURED markdown —
headings, a per-XML totals table, bullet lists — and wrapping it in a code fence
would turn the table into monospace text, destroying the thing that makes the
comment useful, on every ordinary nightly, forever.

What is actually PR-authored is narrower than the issue states: only the pytest
node id. There is no assertion text in this document (verified — `_render_summary`
emits ids and counts, never a failure message), and the other interpolations are
workflow-owned: the per-XML rows carry `s.path.name`, i.e. the
`junit-base-pr<N>-<seed>.xml` names the workflow itself templates.

So the neutralisation is at the node id, where the untrusted input actually
enters, and both nightlies inherit it because both run this one producer. The
comment keeps rendering as a table.

── THE SEED, CONFIRMED RATHER THAN ASSUMED (AC item 3) ────────────────────────
`--randomly-seed=${regressed[0].seed}` is a shell command a human is invited to
paste. The seeds come from the workflow-level constant
`NIGHTLY_SEEDS: '12345 67890 99999'` and reach the verdict through the build
matrix — they are never PR-derived, so there is no untrusted token in that line.
Pinned below so a future change that makes seeds dynamic fails here.
"""
import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "ci" / "diff-pytest-failures.py"


@pytest.fixture(scope="module")
def dpf():
    """Import the hyphenated CLI script as a module."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "dpf_2585.py"
        shutil.copy(_SCRIPT, target)
        spec = importlib.util.spec_from_file_location("dpf_2585", target)
        mod = importlib.util.module_from_spec(spec)
        # `MonkeyPatch.context()`, not the `monkeypatch` fixture: this fixture is
        # module-scoped and that one is function-scoped. Same mechanism, same
        # automatic unwind — which is what the #762 sys.modules lint asks for,
        # and what a bare assign + `pop` in a `finally` only approximates (it
        # deletes the name even if an earlier test had legitimately bound it).
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "dpf_2585", mod)
            spec.loader.exec_module(mod)
            yield mod


def _spans(text):
    """The opening backtick runs in `text`, longest first."""
    return sorted((len(m) for m in re.findall(r"`+", text)), reverse=True)


class TestTheCommonMarkPropertyStatedDirectly:
    """A code span closes on the first backtick run EQUAL to the opening one, so
    the opening run must be strictly longer than anything inside."""

    @pytest.mark.parametrize("payload", [
        "a`b",
        "a``b",
        "a```b",
        "`" * 12,
        "x`y``z```w",
        "``",
        "`",
    ])
    def test_no_inner_run_can_close_the_span(self, dpf, payload):
        out = dpf._inline_code(payload)
        runs = _spans(out)
        opening = runs[0]
        # The opening (and closing) run is the longest in the output, and it is
        # strictly longer than every run the payload contributed.
        inner = max((len(m) for m in re.findall(r"`+", payload)), default=0)
        assert opening > inner
        assert out.startswith("`" * opening)
        assert out.endswith("`" * opening)

    @pytest.mark.parametrize("payload", [
        "t::test_a\n## Forged heading",
        "t::test_a\r\n- [x] approved",
        "t::test_a\x00b",
    ])
    def test_a_newline_cannot_break_out_of_the_bullet(self, dpf, payload):
        # The span rule alone is not enough: a code span cannot contain a blank
        # line, and a bullet must stay one bullet. An id carrying `\n## ` would
        # otherwise render as a heading however the backticks are counted.
        out = dpf._inline_code(payload)
        assert "\n" not in out
        assert "\r" not in out

    def test_the_rendered_id_carries_the_payload_inertly(self, dpf):
        out = dpf._format_test_id(("t", "x`y", "failure"))
        assert "x`y" in out          # still legible to a reviewer
        assert out.startswith("[F] ")  # the sigil stays outside the span


class TestTheWiringNotJustTheHelper:
    """The assertion that cannot be bypassed.

    Found by mutation: deleting the `_inline_code(...)` call from
    `_format_test_id` left every CommonMark test above GREEN, because they
    exercise the helper directly. A neutraliser nothing calls is not a fix, so
    the property is asserted on the DOCUMENT the workflow actually splices —
    driven end to end from JUnit XML, the way the nightly produces it.
    """

    def _md_for(self, dpf, hostile_name):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.xml"
            head = Path(tmp) / "head.xml"
            base.write_text(dpf._build_xml(""))
            head.write_text(dpf._build_xml(
                f'<testcase classname="t" name="{hostile_name}"><failure/></testcase>'
            ))
            md, _ = dpf.diff([base], [head])
        return md

    def test_a_hostile_node_id_is_inert_in_the_rendered_document(self, dpf):
        # A `@pytest.mark.parametrize` value is an arbitrary author-chosen
        # string, and it reaches the XML `name` attribute verbatim.
        md = self._md_for(dpf, "test_x[a`b]")
        line = next(ln for ln in md.splitlines() if "test_x" in ln)
        # Everything after the sigil is inside a span the payload cannot close.
        span = line.split("] ", 1)[1]
        opening = len(re.match(r"`+", span).group(0))
        assert opening > 1                      # widened because of the payload
        assert span.endswith("`" * opening)

    def test_a_newline_bearing_node_id_cannot_forge_a_heading(self, dpf):
        # `&#10;` is a legal XML attribute escape, so this is what a hostile id
        # actually looks like on the wire.
        md = self._md_for(dpf, "test_x&#10;## Nightly suite clean")
        assert "\n## Nightly suite clean" not in md, (
            "a node id broke out of its bullet and rendered as a heading in a "
            "comment authored by github-actions[bot]"
        )


class TestTheControlOrdinaryOutputStillReadsNormally:
    """A hardening that makes every normal comment worse is a bad trade."""

    def test_an_ordinary_id_is_a_plain_single_backtick_span(self, dpf):
        assert dpf._format_test_id(
            ("tests/unit/test_x.py", "test_ok", "failure")
        ) == "[F] `tests/unit/test_x.py::test_ok`"

    def test_the_error_sigil_is_preserved(self, dpf):
        assert dpf._format_test_id(("t", "boom", "error")) == "[E] `t::boom`"

    def test_the_document_still_renders_as_a_table_not_a_code_block(self, dpf):
        # The reason the fix is at the node id and not a fence around the whole
        # artifact: this table is the comment's value.
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.xml"
            head = Path(tmp) / "head.xml"
            base.write_text(dpf._build_xml('<testcase classname="t" name="ok"/>'))
            head.write_text(dpf._build_xml(
                '<testcase classname="t" name="ok"/>'
                '<testcase classname="t" name="bad"><failure/></testcase>'
            ))
            md, code = dpf.diff([base], [head])
        assert code == 1
        assert "| Side | Path | Total |" in md      # the table survives
        assert "```" not in md                       # no fenced block was added


class TestPaddingKeepsTheSpanUnambiguous:
    @pytest.mark.parametrize("payload", ["`lead", "trail`", "`both`"])
    def test_an_edge_backtick_is_padded(self, dpf, payload):
        out = dpf._inline_code(payload)
        body = out.strip("`")
        assert body.startswith(" ") and body.endswith(" ")

    def test_padding_is_absent_when_it_is_not_needed(self, dpf):
        assert dpf._inline_code("plain") == "`plain`"


class TestBounds:
    def test_a_huge_id_is_truncated_not_splice_bombed(self, dpf):
        out = dpf._inline_code("z" * 100_000)
        assert len(out) < dpf._MAX_TEST_ID_CHARS + 10

    @pytest.mark.parametrize("payload", ["", "   ", "\n\n"])
    def test_an_empty_id_never_produces_an_unterminated_span(self, dpf, payload):
        out = dpf._inline_code(payload)
        assert out == "`(empty)`"


class TestTheSeedIsNotPrAuthored:
    """AC item 3 — confirmed, not assumed."""

    def test_the_seed_set_is_a_workflow_constant(self):
        wf = (_REPO / ".github/workflows/backend-unit-nightly.yml").read_text()
        assert re.search(r"NIGHTLY_SEEDS:\s*'[\d ]+'", wf), (
            "seeds are no longer a literal workflow constant — the "
            "`--randomly-seed=` line in the sticky comment now interpolates a "
            "value that may be PR-derived, and needs allowlist validation (#2585)"
        )


class TestBothNightliesInheritTheFix:
    def test_both_workflows_use_this_one_producer(self):
        for wf in ("backend-unit-nightly.yml", "integration-nightly.yml"):
            src = (_REPO / ".github/workflows" / wf).read_text()
            assert "diff-pytest-failures.py" in src, (
                f"{wf} no longer sources its diff from the hardened producer — "
                "its comment may be splicing unneutralised PR text again"
            )
