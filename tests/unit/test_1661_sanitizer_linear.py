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


@pytest.fixture(autouse=True)
def _no_ambient_credential_values(monkeypatch):
    """Pin the agent sanitizer's known-VALUE cache to empty.

    Stage 1 redacts every exact value of a sensitive-named variable in the
    process environment (and `~/.env`). This module pins stage 3 — the KEY=value
    pass — so an ambient value must not get there first: `tests/setup-env.sh`
    exports `TRINITY_TEST_PASSWORD`, and on an install whose admin password is
    the literal word `password` stage 1 rewrote the KEY in `password=hunter2xyz`
    to `***REDACTED***=hunter2xyz`, failing 15 cases that pass in a clean shell.
    """
    monkeypatch.setattr(AGENT, "_credential_values", set())


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


class TestChainedPairs:
    """#3311: a sensitive pair chained after a harmless one is still redacted.

    #1670's single regex took the key up to the FIRST `=` and the value as
    everything up to whitespace, so in `user=a&password=X` the key was `user`
    (not sensitive) and `a&password=X` was consumed as its value and returned
    verbatim — the sensitive pair was never examined. A harmless pair must not
    consume its value; only a sensitive one does.
    """

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "text,expected",
        [
            # The issue's table, row by row.
            ("password=hunter2xyz", "password=***REDACTED***"),
            ("user=a&password=hunter2xyz", "user=a&password=***REDACTED***"),
            (
                "https://host/?client=me&access_token=s3cr3tv4lue",
                "https://host/?client=me&access_token=***REDACTED***",
            ),
            # Not token-shaped, so only the KEY=value pass can catch it.
            ("--env=GITHUB_TOKEN=plainsecret", "--env=GITHUB_TOKEN=***REDACTED***"),
            # Every separator the AC names (`&` is the row above).
            ("user=a;password=hunter2xyz", "user=a;password=***REDACTED***"),
            ("user=a,password=hunter2xyz", "user=a,password=***REDACTED***"),
            ("user=a password=hunter2xyz", "user=a password=***REDACTED***"),
            ("user=a\tpassword=hunter2xyz", "user=a\tpassword=***REDACTED***"),
            # Several harmless pairs ahead of it are all kept verbatim.
            (
                "a=1&b=2;c=3,API_KEY=k3yk3yk3y",
                "a=1&b=2;c=3,API_KEY=***REDACTED***",
            ),
            # Two sensitive pairs in one chain.
            (
                "user=a&password=p1&x=1;secret=p2",
                "user=a&password=***REDACTED***",
            ),
        ],
    )
    def test_sensitive_pair_anywhere_in_a_chain_is_redacted(self, mod, text, expected):
        assert mod.sanitize_text(text) == expected

    @pytest.mark.parametrize("mod", BOTH)
    def test_a_sensitive_value_is_never_split_at_a_separator(self, mod):
        """The value of a sensitive pair still runs to whitespace — a password
        containing `&`, `;` or `,` is redacted whole, never partly leaked.
        (Trailing pairs after a sensitive one are therefore redacted with it:
        over-redaction is the safe direction.)"""
        out = mod.sanitize_text("password=ab;cd,ef&gh next=1")
        assert out == "password=***REDACTED*** next=1"

    @pytest.mark.parametrize("mod", BOTH)
    def test_harmless_chain_is_untouched(self, mod):
        text = "https://host/path?page=2&sort=asc;lang=en,fmt=json --flag=x=y"
        assert mod.sanitize_text(text) == text

    # --- the merge-train finding on #3335 ---------------------------------
    # Walking the chain made the SUBSTRING key rule (`.*AUTH.*`, `.*TOKEN.*`)
    # apply to keys `dev` never examined, so everyday query parameters that
    # merely CONTAIN a sensitive word were redacted — and, because a sensitive
    # value runs to whitespace, took the rest of the URL with them. AC1 says
    # the harmless pairs are kept.

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "text",
        [
            # The three shapes named in the finding.
            "https://example.com/org/repo/issues?q=is:open&author=bob&page=2",
            "https://example.com/usage?page=2&tokens_used=10&model=x",
            "https://example.com/login?sort=asc&passwordless=true&lang=en",
            # Neighbours: the same words in their other everyday forms.
            "?page=2&authors=bob,alice&lang=en",
            "?page=2&coauthor=bob&lang=en",
            "?page=2&authority=example.com&lang=en",
            "?page=2&author_id=7&lang=en",
            "?page=2&token_count=10&model=x",
            "?model=x&max_tokens=100&input_tokens=7&output_tokens=9",
            "?model=x&prompt_tokens=7;completion_tokens=9,total_tokens=16",
            "?page=2&tokenizer=bpe&tokenized=true",
            "?page=2&secretary=bob&lang=en",
            "sort=asc;AUTHOR=bob,PASSWORDLESS=1",
            "--env=AUTHOR=bob",
            'curl -d "user=a&author=bob" https://example.com',
            'q="x&author=bob"',
        ],
    )
    def test_a_harmless_word_containing_a_sensitive_one_is_kept(self, mod, text):
        assert mod.sanitize_text(text) == text

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("FOO=bar API_KEY=k3yk3yk3y", "FOO=bar API_KEY=***REDACTED***"),
            ("a=1&access_token=abc", "a=1&access_token=***REDACTED***"),
            ("x=1&password=hunter2", "x=1&password=***REDACTED***"),
            # The exemption is a closed list of WORDS, not a word-boundary
            # rule: every other key containing a sensitive literal — glued,
            # camelCase, plural, suffixed — is still redacted mid-chain.
            ("a=1&auth=abc", "a=1&auth=***REDACTED***"),
            ("a=1&authorization=abc", "a=1&authorization=***REDACTED***"),
            ("a=1&authorisation=abc", "a=1&authorisation=***REDACTED***"),
            ("a=1&authentication=abc", "a=1&authentication=***REDACTED***"),
            ("a=1&authkey=abc", "a=1&authkey=***REDACTED***"),
            ("a=1&oauth=abc", "a=1&oauth=***REDACTED***"),
            ("a=1&accessToken=abc", "a=1&accessToken=***REDACTED***"),
            ("a=1&csrftoken=abc", "a=1&csrftoken=***REDACTED***"),
            ("a=1&token=abc", "a=1&token=***REDACTED***"),
            ("a=1&tokens=abc", "a=1&tokens=***REDACTED***"),
            ("a=1&access_tokens=abc", "a=1&access_tokens=***REDACTED***"),
            ("a=1&token_value=abc", "a=1&token_value=***REDACTED***"),
            ("a=1&secretkey=abc", "a=1&secretkey=***REDACTED***"),
            ("a=1&secrets=abc", "a=1&secrets=***REDACTED***"),
            ("a=1&passwords=abc", "a=1&passwords=***REDACTED***"),
            ("a=1&password_confirmation=abc", "a=1&password_confirmation=***REDACTED***"),
            ("a=1&credentials=abc", "a=1&credentials=***REDACTED***"),
            # A harmless word does not launder a sensitive one beside it.
            ("a=1&author_token=abc", "a=1&author_token=***REDACTED***"),
            ("a=1&passwordless_secret=abc", "a=1&passwordless_secret=***REDACTED***"),
            ("a=1&max_tokens_secret=abc", "a=1&max_tokens_secret=***REDACTED***"),
            ("a=1&tokens_used_password=abc", "a=1&tokens_used_password=***REDACTED***"),
            ("a=1&max_tokensecret=abc", "a=1&max_tokensecret=***REDACTED***"),
        ],
    )
    def test_the_harmless_words_do_not_shelter_a_real_credential(self, mod, text, expected):
        assert mod.sanitize_text(text) == expected

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("author=bob", "author=***REDACTED***"),
            ("page=2 tokens_used=10", "page=2 tokens_used=***REDACTED***"),
            ('{"passwordless=true"}', '{"passwordless=***REDACTED***"}'),
            # After a separator, but not inside a harmless pair's value: `dev`
            # read `x,author` / `y&authority` as ONE key and redacted it.
            ("x,author=bob", "x,author=***REDACTED***"),
            ("q=x y&authority=bob", "q=x y&authority=***REDACTED***"),
            # Straight after a redacted pair `dev` resumed with `&author` as
            # the key, so the exemption does not carry across one.
            ('token="x"&author=bob', "token=***REDACTED***&author=***REDACTED***"),
        ],
    )
    def test_a_key_dev_already_examined_keeps_the_dev_rule(self, mod, text, expected):
        """The exemption is ONLY for a key sitting inside what #1670 consumed
        as a harmless pair's value — the keys this fix newly examines. Every
        key `dev` already tested with plain containment keeps that verdict:
        this is a fix for a regression, not a second opinion on the key rule,
        and it must never redact LESS than `dev` did."""
        assert mod.sanitize_text(text) == expected

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize(
        "attack",
        [
            pytest.param(lambda n: "a=1&" * (n // 4), id="many-harmless-pairs"),
            pytest.param(lambda n: "a=" * (n // 2), id="equals-chain"),
            pytest.param(lambda n: "&" * n, id="all-separators"),
            pytest.param(lambda n: "TOKEN=" + ",a" * (n // 2), id="separator-value"),
            pytest.param(lambda n: "TOKEN='TOKEN=\"" * (n // 14), id="mixed-quotes"),
            pytest.param(lambda n: 'TOKEN="' + "x" * n, id="sensitive-unterminated-quote"),
            pytest.param(lambda n: "a=1&" + "AUTHOR" * (n // 6) + "=x", id="harmless-word-key"),
            pytest.param(lambda n: "a=1&" + "MAX_TOKENS" * (n // 10) + "=x", id="harmless-count-key"),
            pytest.param(lambda n: "a=1&" + "AUTHORI" * (n // 7) + "=x", id="harmless-near-miss-key"),
            pytest.param(lambda n: "a=1&" + "_" * n + "TOKENS=x", id="separator-run-key"),
            pytest.param(lambda n: "a=1&" + "author=1&" * (n // 9), id="many-harmless-word-pairs"),
            pytest.param(lambda n: ("a=" + "x" * 200 + "&author=1 ") * (n // 212), id="long-gaps"),
        ],
    )
    def test_chain_shapes_stay_linear(self, mod, attack):
        start = time.perf_counter()
        mod.sanitize_text(attack(64_000))
        assert time.perf_counter() - start < 1.0
