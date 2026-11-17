"""trinity-enterprise#1066 — the paste guard's `KEY=value` walk is linear by construction.

CodeQL (`py/polynomial-redos`) flagged `secret_shape`'s two regex calls: the
guard fed a Workspace chat message — user-provided text — through the
redaction's `_KV_LINE_RE` / `_KV_PAIR_RE`, whose unbounded `[^…]+` runs it
reads as polynomial on many repetitions of `!`. In CPython those two are
linear (the lookbehind pins each key to the start of its run, #1661), and the
first test class below shows it at 1M characters; but "linear because of an
engine detail a scanner cannot see" is not a property the guard should rest
on. The guard now finds each `=` with `str.find`, walks back to the key's
start over text no earlier key can claim, and checks the value with one
fixed-width match — no unbounded quantifier anywhere on the path.

Two directions are pinned: the timing bound on the shapes CodeQL names, and
detection — every real token still refused, prose still admitted, including a
token sitting at the END of a 1M-character message (no length cap that could
drop it).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

from utils import credential_sanitizer as cs  # noqa: E402

N = (10_000, 100_000, 1_000_000)

# The shapes CodeQL names (many `!`) and the ones that stress a key walk:
# runs with no `=`, `=` at either end, a key per two characters, quoted
# values that never close, credential-named keys with short values.
SHAPES = {
    "bang": lambda n: "!" * n,
    "eq-then-bang": lambda n: "=" + "!" * n,
    "bang-then-eq": lambda n: "!" * n + "=",
    "bang-eq-pairs": lambda n: "!=" * (n // 2),
    "key-per-three": lambda n: "k=!" * (n // 3),
    "unclosed-quote": lambda n: 'note="' + "!" * n,
    "sensitive-short": lambda n: "token=! " * (n // 8),
    "spaced-bang": lambda n: "! " * (n // 2),
}


def _secs(text: str) -> float:
    t = time.perf_counter()
    cs.secret_shape(text)
    return time.perf_counter() - t


class TestTimingBound:

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_a_million_characters_is_bounded_and_scales_linearly(self, shape):
        make = SHAPES[shape]
        small, big = (min(_secs(make(n)) for _ in range(3)) for n in (N[1], N[2]))
        # Absolute: generous for a slow CI runner, far below a quadratic run
        # (1M**2 steps). Relative: 10x the input may cost ~10x, never ~100x.
        assert big < 3.0, f"{shape}: {big:.2f}s at 1M"
        assert big < 30 * max(small, 1e-3), f"{shape}: {small:.4f}s -> {big:.4f}s"

    def test_ten_thousand_is_instant(self):
        for shape, make in SHAPES.items():
            assert _secs(make(N[0])) < 0.25, shape

    def test_the_walk_uses_no_unbounded_regex(self):
        """Pin, on top of the timing above: the walk no longer reaches the
        redaction's unbounded patterns, and the only pattern it does use is
        fixed-width."""
        names = cs.secret_shape.__code__.co_names
        assert "_KV_LINE_RE" not in names and "_KV_PAIR_RE" not in names
        pat = cs._GUARD_VALUE_RE.pattern
        assert "+" not in pat and "*" not in pat and ",}" not in pat


class TestDetection:

    TAIL = " MY_API_KEY=abcdefgh12345678"

    @pytest.mark.parametrize("text", [
        "VAULT_TOKEN=hvs.AAAAAAAAAAAAAAAAAAAAAAAA",
        'export MY_API_KEY="qwertyuiop1234567890"',
        "DB_PASSWORD='correct-horse-battery'",
        "user=a&password=hunter2hunter2",
        "--env=GITHUB_TOKEN=abcdefgh1234",
        "https://example.com/cb?client=me&access_token=abcdefgh1234",
    ], ids=["plain", "double-quoted", "single-quoted", "chained-after-harmless",
            "flag-chained", "url-query"])
    def test_a_sensitive_pair_is_refused(self, text):
        assert cs.secret_shape(text) == "env_pair"

    def test_an_unclosed_quote_is_still_a_secret(self):
        """A truncated paste is still the secret. The redaction's pair shape
        needed the closing quote; the guard needs eight characters."""
        assert cs.secret_shape('API_KEY="abcdefgh12345678') == "env_pair"

    def test_a_very_long_key_still_counts(self):
        """No key-length cap: a long URL ending in a credential parameter."""
        url = "https://example.com/" + "p/" * 3000 + "?access_token=abcdefgh1234"
        assert cs.secret_shape(url) == "env_pair"

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_a_pair_at_the_end_of_a_million_characters_is_found(self, shape):
        assert cs.secret_shape(SHAPES[shape](N[2]) + self.TAIL) == "env_pair"

    def test_a_prefixed_token_at_the_end_of_a_million_characters_is_found(self):
        tok = "ghp_" + "a" * 36
        assert cs.secret_shape("!" * N[2] + " " + tok) == "github"

    @pytest.mark.parametrize("text", [
        "set API_KEY=short",
        'TOKEN="a phrase with spaces"',
        "author=jane.doe.example",
        "tokens_used=123456789",
        "x==y and a=b",
        "API_KEY=",
        "=abcdefghijk",
        "please add a Bearer token header",
    ])
    def test_prose_and_short_values_are_admitted(self, text):
        assert cs.secret_shape(text) is None

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_the_adversarial_shapes_alone_are_not_secrets(self, shape):
        assert cs.secret_shape(SHAPES[shape](N[1])) is None
