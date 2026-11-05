"""WhatsApp markdown conversion runs in linear time (trinity-enterprise#1117).

`WhatsAppAdapter._markdown_to_whatsapp` converts the agent's reply before it is
split into 1600-char Twilio messages. The reply reaches it uncapped from the
channel router and from proactive delivery, and the conversion is synchronous
inside the async send path, so its cost is paid on the backend event loop.

Two of its regexes were quadratic in the reply length:

* the header rule `^[ \\t]*#{1,6}[ \\t]+(.+?)\\s*$` -- the lazy capture retried
  `\\s*$` across the whole run of trailing whitespace at every step;
* the link rule `\\[([^\\]]+)\\]\\(([^)]+)\\)` -- every `[` (and every `[x](`)
  rescanned to the end of the text when no closing bracket followed.

Measured on the old code: 8-11 s of CPU for one 64 KB reply. This module pins:

1. growth, not a single threshold: per-call time at 4 / 16 / 64 KB must scale
   about linearly (~4x per 4x of input; quadratic is ~16x), on the pump
   strings CodeQL names plus the worst shapes measured, with a coarse absolute
   ceiling at 64 KB;
2. links whose URL contains brackets still convert (IPv6 hosts, `?filter[]=`);
3. the conversion entry clamps its regex input to the largest reply any caller
   can hand it, and passes any excess through unchanged rather than dropping it;
4. on realistic agent markdown the output equals the previous implementation;
   the only intentional differences are pinned by name.
"""

from __future__ import annotations

import ast
import os
import random
import re
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _PROJECT_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def wa():
    import adapters.whatsapp_adapter as module

    return module


def _convert(module, text):
    return module.WhatsAppAdapter._markdown_to_whatsapp(text)


# ---------------------------------------------------------------------------
# Timing harness: per-call time, measured over enough calls to be stable
# ---------------------------------------------------------------------------

_SIZES = (4_000, 16_000, 64_000)
# 4 KB -> 64 KB is 16x the input: linear grows ~16x, quadratic ~256x. 64x is
# the geometric middle -- 4x of slack for noise (CI runs this under xdist) on
# either side.
_MAX_GROWTH_4K_TO_64K = 64.0
# Per call at 64 KB. The quadratic regexes took 1.6-11 s here; linear takes
# a few milliseconds. Coarse on purpose: the ratio is the real check.
_CEILING_64K_S = 0.5


def _per_call_seconds(fn, arg, *, min_total_s=0.02, reps=3):
    """Best-of-`reps` per-call time, each rep looping until it lasts min_total_s."""
    best = float("inf")
    for _ in range(reps):
        loops = 1
        while True:
            start = time.perf_counter()
            for _ in range(loops):
                fn(arg)
            elapsed = time.perf_counter() - start
            if elapsed >= min_total_s:
                break
            loops *= 4
        best = min(best, elapsed / loops)
    return best


def _assert_linear_growth(fn, make, label):
    """Time at 4 / 16 / 64 KB must grow about linearly.

    A quadratic shape is stopped after 16 KB when it is already both clearly
    superlinear and expensive, so a regression fails in seconds rather than
    spending ~10 s per shape at 64 KB."""
    times = {}
    for size in _SIZES:
        times[size] = _per_call_seconds(fn, make(size))
        detail = ", ".join(f"{n} chars: {t * 1e3:.3f} ms" for n, t in times.items())
        if size == 16_000:
            step = times[16_000] / times[4_000]
            assert not (step > 12 and times[16_000] > 0.05), (
                f"{label}: {step:.1f}x for 4x input (linear ~4x, quadratic ~16x); {detail}")
    growth = times[64_000] / times[4_000]
    assert growth < _MAX_GROWTH_4K_TO_64K, (
        f"{label}: {growth:.0f}x for 16x input (linear ~16x, quadratic ~256x); {detail}")
    assert times[64_000] < _CEILING_64K_S, f"{label}: too slow at 64 KB; {detail}"


# Pump strings: CodeQL's (from the alert text) and the worst shapes measured.
_PUMPS = {
    "#163 header, trailing spaces '# a'+' '*n+'b'": lambda n: "# a" + " " * n + "b",
    "#163 header, CodeQL tab pump '# a'+'\\t\\t'*n+'b'": lambda n: "# a"
    + "\t\t" * (n // 2)
    + "b",
    "#164 link, unclosed text '['*n": lambda n: "[" * n,
    "#164 link, CodeQL pump '['+'[\\\\'*n": lambda n: "[" + "[\\" * (n // 2),
    "#164 link, CodeQL pump '[\\\\]('+'[(](('*n": lambda n: "[\\]("
    + "[(]((" * (n // 5),
    "#164 link, unclosed URL '[x]('*n": lambda n: "[x](" * (n // 4),
}


class TestLinearCost:
    @pytest.mark.parametrize("label", list(_PUMPS))
    def test_cost_grows_linearly(self, wa, label):
        _assert_linear_growth(lambda s: _convert(wa, s), _PUMPS[label], label)


# ---------------------------------------------------------------------------
# Links whose URL contains brackets keep converting
# ---------------------------------------------------------------------------


class TestBracketUrlsStillConvert:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("[local](http://[::1]:8000/health)", "local (http://[::1]:8000/health)"),
            (
                "[ipv6](http://[2001:db8::1]:443/path)",
                "ipv6 (http://[2001:db8::1]:443/path)",
            ),
            (
                "[items](https://api.example.com/items?filter[]=a&filter[]=b)",
                "items (https://api.example.com/items?filter[]=a&filter[]=b)",
            ),
            ("[nested](https://x.io/?a[b][c]=d)", "nested (https://x.io/?a[b][c]=d)"),
            ("[ends](http://x/[1])", "ends (http://x/[1])"),
            (
                "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))",
                "wiki (https://en.wikipedia.org/wiki/Foo_(bar))",
            ),
            (
                "See [a](u1) and [b](http://[::1]/x?y[]=z).",
                "See a (u1) and b (http://[::1]/x?y[]=z).",
            ),
        ],
    )
    def test_link_converts(self, wa, text, expected):
        assert _convert(wa, text) == expected


# ---------------------------------------------------------------------------
# The conversion entry clamps its regex input
# ---------------------------------------------------------------------------


def _module_int_constant(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


class TestConversionClamp:
    def test_clamp_is_the_largest_reply_the_platform_accepts(self, wa):
        """The largest agent response any path accepts is the result-callback
        cap; the clamp is sized to it so no legitimate reply is affected."""
        callback_cap = _module_int_constant(
            _BACKEND / "routers" / "agents.py", "_MAX_CALLBACK_RESPONSE_CHARS"
        )
        assert wa._MARKDOWN_CONVERSION_MAX_CHARS == callback_cap

    def test_text_past_the_clamp_passes_through_unchanged(self, wa, monkeypatch):
        monkeypatch.setattr(wa, "_MARKDOWN_CONVERSION_MAX_CHARS", 20)
        text = "**a** [l](u) " * 6  # 78 chars
        out = _convert(wa, text)
        head, tail = text[:20], text[20:]
        assert out == _convert_unclamped(head) + tail
        assert out.endswith(tail)  # nothing past the clamp is dropped

    def test_reply_at_the_clamp_converts_in_bounded_time(self, wa):
        """The clamp bounds the work, sized to the largest caller input. Gated
        on the 64 KB case first so a quadratic regression fails fast instead of
        running for hours at 4 M chars."""
        cap = wa._MARKDOWN_CONVERSION_MAX_CHARS
        for label in (
            "#164 link, unclosed URL '[x]('*n",
            "#164 link, unclosed text '['*n",
            "#163 header, trailing spaces '# a'+' '*n+'b'",
        ):
            make = _PUMPS[label]
            assert (
                _per_call_seconds(lambda s: _convert(wa, s), make(64_000), reps=1)
                < _CEILING_64K_S
            )
            big = make(cap + 1000)
            start = time.perf_counter()
            _convert(wa, big)
            assert time.perf_counter() - start < 5.0, label


# ---------------------------------------------------------------------------
# Same output as before on realistic markdown; the differences are named
# ---------------------------------------------------------------------------


def _convert_unclamped(text):
    """The conversion as it was before the linear rewrite -- the oracle."""
    if not text:
        return text
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(.+?)\s*$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    return text


_WORDS = "the agent ran a task and found two issues with config files plus logs".split()
_URLS = [
    "https://example.com/docs",
    "http://localhost:8000/api/x?y=1",
    "http://[::1]:8000/health",
    "https://api.x.io/v1/items?filter[]=a&filter[]=b",
    "https://en.wikipedia.org/wiki/Foo_(bar)",
    "https://github.com/o/r/pull/42#issuecomment-1",
    "http://[fe80::1%25en0]/",
    "https://x.io/a?b[c][d]=e",
    "mailto:user@example.com",
    "https://x.io/path_(with)_parens?q=(1)",
]


def _realistic_replies(count, seed=1117):
    rng = random.Random(seed)

    def phrase(k):
        return " ".join(rng.choice(_WORDS) for _ in range(k))

    def inline():
        r = rng.random()
        if r < 0.25:
            return f"[{phrase(rng.randint(1, 3))}]({rng.choice(_URLS)})"
        if r < 0.40:
            return f"**{phrase(rng.randint(1, 3))}**"
        if r < 0.50:
            return f"__{phrase(2)}__"
        if r < 0.58:
            return f"~~{phrase(2)}~~"
        if r < 0.66:
            return f"`{phrase(2)}`"
        if r < 0.72:
            return f"_{phrase(2)}_"
        return phrase(rng.randint(1, 6))

    def line():
        r = rng.random()
        prefix = ""
        if r < 0.15:
            prefix = rng.choice(["# ", "## ", "### ", "  ## ", "#### ", "#\t"])
        elif r < 0.35:
            prefix = rng.choice(["- ", "* ", "1. ", "> "])
        body = " ".join(inline() for _ in range(rng.randint(1, 4)))
        return prefix + body + rng.choice(["", "", " ", "  ", "\r"])

    return ["\n".join(line() for _ in range(rng.randint(1, 12))) for _ in range(count)]


class TestEquivalence:
    def test_realistic_replies_convert_exactly_as_before(self, wa):
        replies = _realistic_replies(2000)
        diffs = [r for r in replies if _convert(wa, r) != _convert_unclamped(r)]
        assert not diffs, f"{len(diffs)} realistic replies changed, e.g. {diffs[0]!r}"

    @pytest.mark.parametrize(
        "text, before, after",
        [
            # Link text may no longer contain `[`: the innermost bracket-free link
            # converts instead of one link spanning both brackets.
            ("[a [b](u)", "a [b (u)", "[a b (u)"),
            # A URL may no longer contain `](` (the start of the next link).
            ("[a](x](y)", "a (x](y)", "[a](x](y)"),
            # A header with only whitespace after the marks stays as it is
            # instead of becoming an empty bold `* *`.
            ("#  \nnext", "* *\nnext", "#  \nnext"),
        ],
    )
    def test_intentional_differences(self, wa, text, before, after):
        assert _convert_unclamped(text) == before
        assert _convert(wa, text) == after
