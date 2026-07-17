"""Unit tests for the linear KEY=value redaction in both credential sanitizers (#1661).

The sanitizers' name patterns describe VARIABLE NAMES (`.*TOKEN.*` = "a name
containing TOKEN"), but stage 3 composed each one into a LINE-scanning regex:

    (.*TOKEN.*)=(["\']?)([^\s"\']+)\2

Two unbounded `.*` around a literal, re-scanned from every offset of the line.
That had two consequences, and this module pins both fixes:

1. **Cost** — superlinear in line length (~6.8s for `.*TOKEN.*` alone on an 8 KB
   line; ~10 CPU-minutes on a 44 KB tool-result line). Agent-side that pegged a
   core: the reader thread was not blocked on a pipe, it was *computing* — which
   is why the #728/#1502 pipe fixes never covered this path, and why the spin
   "self-cleared" once the regex finally finished.

2. **Correctness (security)** — greedy `.*` spans to the LAST `=` on the line, so
   on a multi-pair line the old code redacted the WRONG pair and left the real
   credential in place. `GH_TOKEN=supersecret PLAIN=harmless` redacted
   `harmless`. Stages 1-2 (known values / value-shaped patterns) hide this for
   many real keys, which is why it went unnoticed.

Both sanitizer copies (backend + agent-server) carried the same flaw and are
tested here together.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parents[2]


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(_project_root / path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT = _load(
    "docker/base-image/agent_server/utils/credential_sanitizer.py",
    "_cs_agent_1661",
)
BACKEND = _load("src/backend/utils/credential_sanitizer.py", "_cs_backend_1661")

BOTH = [pytest.param(AGENT, id="agent"), pytest.param(BACKEND, id="backend")]


def _big_line(n_chars: int) -> str:
    """A tool-result-shaped line: prose with env-ish tokens, no real secrets."""
    unit = "Loaded DATABASE_URL config and GH_TOKEN refs while scanning. "
    return (unit * ((n_chars // len(unit)) + 1))[:n_chars]


class TestLinearCost:
    @pytest.mark.parametrize("mod", BOTH)
    def test_44kb_line_is_fast(self, mod):
        """The #1661 line size. Was ~10 CPU-minutes; the budget here is
        deliberately loose (1s) — it fails on a return to superlinear cost, not
        on ordinary machine noise."""
        line = _big_line(44_000)
        start = time.perf_counter()
        mod.sanitize_text(line)
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("mod", BOTH)
    def test_cost_stays_linear_as_the_line_grows(self, mod):
        """8x the input must not cost ~300x the time (the old curve was ~n^2.5).

        Asserts the SHAPE, not a wall-clock number, so it holds on slow CI."""
        small = _big_line(8_000)
        large = _big_line(64_000)

        t0 = time.perf_counter()
        for _ in range(3):
            mod.sanitize_text(small)
        small_dt = (time.perf_counter() - t0) / 3

        t0 = time.perf_counter()
        for _ in range(3):
            mod.sanitize_text(large)
        large_dt = (time.perf_counter() - t0) / 3

        # Linear would be ~8x. Allow 40x for constant factors/noise; the old
        # implementation was ~300x+ here and would blow this budget outright.
        assert large_dt < max(small_dt * 40, 0.5)

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "attack",
        [
            pytest.param(lambda n: "!" * n, id="many-bangs"),
            pytest.param(lambda n: "!=" + "!=!" * (n // 3), id="bang-equals-bang"),
            pytest.param(lambda n: 'K="' + "x" * n, id="unterminated-quote"),
            pytest.param(lambda n: "x" * n + "=", id="trailing-equals-no-value"),
            pytest.param(lambda n: "a= " * (n // 3), id="missing-values"),
            pytest.param(lambda n: '=' * n, id="all-equals"),
        ],
    )
    def test_adversarial_inputs_stay_linear(self, mod, attack):
        """Guards the ReDoS surface of `_KV_LINE_RE` — including the two strings
        CodeQL's `py/polynomial-redos` names (`!`*n, and `!=` + `!=!`*n).

        The linearity comes from the lookbehind: it stops the engine retrying
        the key at every offset inside a long token, which is what would make
        `([^\s"\'=]+)=` quadratic on a non-matching run. CodeQL does not model
        the lookbehind, so it reports this as polynomial; these cases are the
        executable proof that it is not — and the reason the lookbehind must not
        be "simplified away" by a later edit.
        """
        start = time.perf_counter()
        mod.sanitize_text(attack(64_000))
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("mod", BOTH)
    def test_one_giant_unbroken_token(self, mod):
        """A 200 KB base64-ish blob with no delimiters. The key charset is
        delimiter-bounded, so the engine must not retry every offset inside it."""
        start = time.perf_counter()
        mod.sanitize_text("A" * 200_000)
        assert time.perf_counter() - start < 1.0


class TestMultiPairRedaction:
    """The security half: every sensitive pair on a line is redacted, and only
    those. The old code redacted the last pair on the line instead."""

    @pytest.mark.parametrize("mod", BOTH)
    def test_secret_redacted_and_harmless_kept(self, mod):
        out = mod.sanitize_text("GH_TOKEN=supersecret PLAIN=harmless")
        assert "supersecret" not in out
        assert "PLAIN=harmless" in out

    @pytest.mark.parametrize("mod", BOTH)
    def test_realistic_env_dump_line(self, mod):
        out = mod.sanitize_text(
            "env: ANTHROPIC_API_KEY=sk-ant-realkey123456 LOG_LEVEL=debug"
        )
        assert "sk-ant-realkey123456" not in out
        assert "LOG_LEVEL=debug" in out

    @pytest.mark.parametrize("mod", BOTH)
    def test_every_sensitive_pair_on_the_line(self, mod):
        out = mod.sanitize_text(
            "DB_PASSWORD=hunter2 HOST=localhost GH_TOKEN=ghp_abc PORT=5432"
        )
        assert "hunter2" not in out
        assert "ghp_abc" not in out
        assert "HOST=localhost" in out
        assert "PORT=5432" in out


class TestPreservedSemantics:
    """Behaviour pinned from the pre-#1661 implementation on the single-pair
    lines it handled correctly — a faster filter that redacts LESS is a leak."""

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("GH_TOKEN=abc123secret", "GH_TOKEN=***REDACTED***"),
            # `ANTHROPIC_.*` matches a SUFFIX of the key — the old composed
            # regex could start matching mid-token, so this was redacted before
            # and must stay redacted. (A `fullmatch` rewrite would quietly stop
            # redacting it: less redaction is a leak, not a speedup.)
            ("MY_ANTHROPIC_KEY=secret1", "MY_ANTHROPIC_KEY=***REDACTED***"),
            ("some prose GH_TOKEN=abc123 more", "some prose GH_TOKEN=***REDACTED*** more"),
            # Non-identifier characters in the key.
            ("my.token=abc123", "my.token=***REDACTED***"),
            # Case-insensitive name matching.
            ("lowercase_token=abc123", "lowercase_token=***REDACTED***"),
            ("TOKEN=abc", "TOKEN=***REDACTED***"),
            # Not a credential name — left alone.
            ("NOTHING_SENSITIVE=plainvalue", "NOTHING_SENSITIVE=plainvalue"),
        ],
    )
    def test_single_pair_contract(self, mod, text, expected):
        assert mod.sanitize_text(text) == expected

    @pytest.mark.parametrize("mod", BOTH)
    def test_quoted_value_with_space_is_unchanged(self, mod):
        """Documents a PRE-EXISTING gap rather than a new one: the value charset
        `[^\\s"\\']+` never matched across a space, so a quoted multi-word value
        was not redacted before this change either. Pinned so the behaviour is a
        decision, not a surprise (stages 1-2 still catch value-shaped secrets)."""
        assert mod.sanitize_text('GH_TOKEN="quoted secret"') == 'GH_TOKEN="quoted secret"'

    @pytest.mark.parametrize("mod", BOTH)
    def test_key_suffix_matching_helper(self, mod):
        assert mod._is_sensitive_kv_key("GH_TOKEN") is True
        assert mod._is_sensitive_kv_key("ANTHROPIC_API_KEY") is True
        # Suffix, not whole-key: the name pattern starts matching mid-token.
        assert mod._is_sensitive_kv_key("MY_ANTHROPIC_KEY") is True
        assert mod._is_sensitive_kv_key("HOST") is False
        assert mod._is_sensitive_kv_key("PORT") is False

    def test_the_two_copies_carry_different_pattern_lists(self):
        """Not a bug this issue fixes — pinned so the asymmetry is visible.

        The agent-side list is a superset (it adds `DB_.*`, `REDIS_.*`,
        `SLACK_.*`, ...), so `MY_DB_PASS=x` is redacted agent-side and NOT by
        the backend layer. Both copies got the identical #1661 treatment; the
        lists themselves are untouched here, since widening the backend's list
        changes what gets redacted and belongs in its own change.
        """
        assert AGENT._is_sensitive_kv_key("MY_DB_PASS") is True
        assert BACKEND._is_sensitive_kv_key("MY_DB_PASS") is False
