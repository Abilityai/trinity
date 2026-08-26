"""#2398 — the credential-sanitizer KEY test must be linear, and unchanged.

#1670 fixed the LINE scan (#1661) and left the KEY test quadratic one call
deeper: `_is_sensitive_kv_key` ran `re.search(r'(?:.*TOKEN.*)\\Z', key)` per
pattern, which retries `.*` from every offset — O(len(key)^2).

Two facts made that lethal rather than theoretical:

* the "key" is whatever preceded an `=` in `_KV_LINE_RE`'s `([^\\s"'=]+)`, which
  is UNBOUNDED — a base64 blob or a tool-result payload arrives here as a
  "key", not the short env-var name the docstring claimed;
* measured on 3.13, a 44 KB key cost ~40 CPU-seconds for ONE key, per match,
  per line.

py-spy caught the burn 14 times across three agents (cmo, copywriter,
cornelius), every dump on this stack.

These tests pin BOTH halves: the verdicts must not move (a faster check that
redacts less is a credential leak), and the cost must stay linear.
"""
import re
import time

import pytest

from utils.credential_sanitizer import (
    SENSITIVE_KEY_PATTERNS,
    _SENSITIVE_KEY_LITERALS,
    _is_sensitive_kv_key,
    _literal_from_pattern,
    sanitize_text,
)

pytestmark = pytest.mark.unit


def _old_implementation(key: str) -> bool:
    """The pre-#2398 test, verbatim, as the equivalence oracle."""
    compiled = [
        re.compile(r'(?:' + p + r')\Z', re.IGNORECASE) for p in SENSITIVE_KEY_PATTERNS
    ]
    return any(r.search(key) for r in compiled)


@pytest.mark.parametrize("key", [
    "ANTHROPIC_API_KEY", "MY_DB_PASSWORD", "github_token", "TRINITY_MCP_API_KEY",
    "prefix_TOKEN_suffix", "tokEN", "AUTHORIZATION", "AWS_", "OPENAI_ORG",
    # ...and the ones that must stay UNREDACTED
    "PATH", "HOME", "MY_DB_PASS", "NOTASECRET", "x", "",
])
def test_verdicts_are_unchanged(key):
    """A faster check that redacts LESS is a leak, not a speedup."""
    assert _is_sensitive_kv_key(key) == _old_implementation(key), key


def test_the_suffix_semantics_that_1670_preserved_still_hold():
    """`DB_.*` redacting `MY_DB_PASS=x` was deliberate (#1670): a name pattern
    matches a SUFFIX of the key, never only the whole key. Containment keeps
    that — this pins it so a later `fullmatch` "simplification" cannot quietly
    narrow the redaction."""
    assert _is_sensitive_kv_key("MY_DB_PASSWORD")
    assert _is_sensitive_kv_key("SOME_PREFIX_ANTHROPIC_API_KEY")


def test_the_key_test_is_linear():
    """The regression itself. Quadratic growth is what pegged the core.

    Generous absolute bound so this cannot flake on a loaded runner: the old
    implementation took ~40s at 44 KB, the new one ~0.4ms. Anything under a
    second at 64 KB proves the shape changed.
    """
    key = "A" * 65536
    started = time.perf_counter()
    assert _is_sensitive_kv_key(key) is False
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, (
        f"the key test took {elapsed:.2f}s on a 64 KB key — the quadratic "
        f"backtracking of #2398 is back. It is reached from sanitize_text via "
        f"an UNBOUNDED `([^\\s\"'=]+)=` capture, so this is not a synthetic input."
    )


def test_a_long_kv_line_sanitizes_quickly_end_to_end():
    """Through the public entry point, on the shape the agents actually hit:
    stream-json with a long unbroken token before an `=`."""
    line = "prefix " + ("A" * 44000) + "=value TOKEN=abc123"
    started = time.perf_counter()
    out = sanitize_text(line)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"sanitize_text took {elapsed:.2f}s on a 44 KB token"
    # ...and it still did its job.
    assert "abc123" not in out


def test_a_pattern_that_is_not_a_plain_literal_fails_loudly():
    """The derivation is only safe while every pattern is `.*LITERAL.*`.

    A future entry needing real regex semantics must raise at import rather
    than be silently reduced to a literal that matches less — the failure mode
    would be a credential that stops being redacted, with no test going red.
    """
    with pytest.raises(ValueError, match="not a plain"):
        _literal_from_pattern(r'.*(TOKEN|SECRET).*')
    with pytest.raises(ValueError, match="not a plain"):
        _literal_from_pattern(r'.*.*')


def test_every_shipped_pattern_derives_a_literal():
    assert len(_SENSITIVE_KEY_LITERALS) == len(SENSITIVE_KEY_PATTERNS)
    assert all(lit and lit.isupper() for lit in _SENSITIVE_KEY_LITERALS)


def test_the_vendored_agent_copy_got_the_same_fix():
    """Invariant #5. The py-spy stacks are all in the AGENT-SERVER copy —
    `agent_server/utils/credential_sanitizer.py` — so fixing only the backend
    would have left every dumped burn exactly where it was.

    The two files are vendored in SHAPE, not byte-identically: the agent list
    is `SENSITIVE_VAR_PATTERNS` and carries 30 entries to the backend's 13. The
    derivation is what is shared, which is why it is asserted here rather than
    diffed.
    """
    from pathlib import Path
    agent = Path(__file__).resolve().parents[2] / (
        "docker/base-image/agent_server/utils/credential_sanitizer.py")
    body = agent.read_text()
    assert "_SENSITIVE_KEY_LITERALS" in body, (
        "the agent-server copy still runs the quadratic regex test — that is "
        "the copy every py-spy dump was taken in"
    )
    assert "_literal_from_pattern" in body
    assert "any(r.search(key) for r in" not in body
