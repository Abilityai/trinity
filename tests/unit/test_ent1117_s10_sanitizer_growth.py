"""Growth-ratio pins for the credential sanitizer's key=value scan (trinity-enterprise#1117).

CodeQL's `py/polynomial-redos` flags three regexes in `utils/credential_sanitizer.py`
(and they live in the agent server's copy too):

* `_KV_LINE_RE` searched from a moving `pos` -- pump `'!'*n`;
* `_KV_PAIR_RE.match(text, pos)` -- pumps `'!'*n` and `'!='+'!=!'*n`;
* the Google-consent `client_id` check, a `re.fullmatch` -- pump `'-'*n`.

They are linear: the negative lookbehind pins each key to the start of its run,
`.match` at a fixed position cannot retry later offsets, and `fullmatch` is
anchored at both ends. CodeQL's model ignores all three. These tests turn that
claim into something CI re-checks: per-call time at 4 / 16 / 64 KB on CodeQL's
own pump strings must grow about linearly (~4x per 4x of input, where quadratic
is ~16x), with a coarse ceiling.

They are characterization tests -- the code is already linear, so they cannot be
red today. `TestHarnessSensitivity` shows the same harness does go red on a
known-quadratic regex, so a green run means something.
"""
from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(_PROJECT_ROOT / path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT = _load("docker/base-image/agent_server/utils/credential_sanitizer.py", "_cs_agent_ent1117")
BACKEND = _load("src/backend/utils/credential_sanitizer.py", "_cs_backend_ent1117")
BOTH = [pytest.param(AGENT, id="agent"), pytest.param(BACKEND, id="backend")]


@pytest.fixture(autouse=True)
def _no_ambient_credential_values(monkeypatch):
    """Keep stage 1 (known env values) out of the measurement, as test_1661 does."""
    monkeypatch.setattr(AGENT, "_credential_values", set())


_SIZES = (4_000, 16_000, 64_000)
# Smallest -> largest size is 16x the input: linear grows ~16x, quadratic ~256x.
# 64x is the geometric middle, leaving 4x of slack for noise on either side.
_MAX_GROWTH = 64.0
_CEILING_64K_S = 0.5    # per call at 64 KB; measured ~1-15 ms


def _per_call_seconds(fn, arg, *, min_total_s=0.02, reps=3):
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


def _growth(fn, make, sizes=_SIZES):
    """(per-call times, largest/smallest time ratio) over `sizes`."""
    times = [_per_call_seconds(fn, make(n)) for n in sizes]
    return times, times[-1] / times[0]


def _assert_linear(fn, make, label, sizes=_SIZES):
    times = []
    for n in sizes:
        times.append(_per_call_seconds(fn, make(n)))
        detail = ", ".join(f"{k}: {t * 1e3:.3f} ms" for k, t in zip(sizes, times))
        # Stop a regression early: clearly quadratic and already expensive at
        # the middle size, rather than paying ~15 s per rep at the largest.
        if len(times) == 2:
            step = times[1] / times[0]
            assert not (step > 12 and times[1] > 0.05), f"{label}: {step:.1f}x for 4x input ({detail})"
    growth = times[-1] / times[0]
    assert growth < _MAX_GROWTH, (
        f"{label}: {growth:.0f}x for {sizes[-1] // sizes[0]}x input ({detail})")
    assert times[-1] < _CEILING_64K_S, f"{label}: too slow at the largest size ({detail})"


def _walk_keys(mod):
    def walk(text):
        pos = 0
        while True:
            m = mod._KV_LINE_RE.search(text, pos)
            if m is None:
                return
            pos = m.end()
    return walk


def _consent_url(client_id):
    query = {"client_id": client_id, "redirect_uri": "http://127.0.0.1:34837/oauth2/callback",
             "response_type": "code", "scope": "email", "state": "s"}
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(query)


class TestCodeqlPumpsGrowLinearly:
    @pytest.mark.parametrize("mod", BOTH)
    def test_388_key_scan(self, mod):
        _assert_linear(_walk_keys(mod), lambda n: "!" * n, "#388 _KV_LINE_RE '!'*n")

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize("label, make", [
        ("'!'*n", lambda n: "!" * n),
        ("'!='+'!=!'*n", lambda n: "!=" + "!=!" * (n // 3)),
        ("unclosed quote TOKEN='x...", lambda n: "TOKEN='" + "x" * n),
    ])
    def test_384_pair_match(self, mod, label, make):
        _assert_linear(lambda s: mod._KV_PAIR_RE.match(s, 0), make, f"#384 _KV_PAIR_RE {label}")

    @pytest.mark.parametrize("mod", BOTH)
    @pytest.mark.parametrize("label, make", [
        ("'!'*n", lambda n: "!" * n),
        ("'!='+'!=!'*n", lambda n: "!=" + "!=!" * (n // 3)),
        ("a=1& chain", lambda n: "a=1&" * (n // 4)),
        ("TOKEN='&TOKEN= chain", lambda n: "TOKEN='" + "&TOKEN=" * (n // 7)),
    ])
    def test_full_sanitize_text(self, mod, label, make):
        _assert_linear(mod.sanitize_text, make, f"sanitize_text {label}")


class TestConsentClientIdCheck:
    """#389: the `client_id` `fullmatch` runs only on consent URLs of at most
    8192 chars (`_is_public_google_consent_match` returns early above that), so
    its growth is measured at 0.5 / 2 / 8 KB through the real entry point."""

    @pytest.mark.parametrize("mod", BOTH)
    def test_389_client_id_fullmatch(self, mod):
        sizes = (500, 2_000, 8_000)
        for n in sizes:  # the path is really reached: a valid id of this size survives
            ok = _consent_url("a" * n + ".apps.googleusercontent.com")
            assert mod.sanitize_text(ok) == ok
        _assert_linear(mod.sanitize_text, lambda n: _consent_url("-" * n),
                       "#389 consent client_id '-'*n", sizes=sizes)


class TestHarnessSensitivity:
    """The growth check above must be able to fail. Run the same harness on the
    counterfactuals: #389's regex without anchoring, and #388's key regex without
    its lookbehind. Both are quadratic; both must read as superlinear here.
    1 KB -> 16 KB keeps it cheap with the same 16x input ratio."""

    @pytest.mark.parametrize("label, pattern, make", [
        ("#389 regex via unanchored search", r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com",
         lambda n: "-" * n),
        ("#388 key regex without lookbehind", r"""([^\s"'=&;,]+)=""", lambda n: "!" * n),
    ])
    def test_harness_flags_a_quadratic_regex(self, label, pattern, make):
        rx = re.compile(pattern)
        times, growth = _growth(lambda s: rx.search(s), make, sizes=(1_000, 16_000))
        assert growth > _MAX_GROWTH, f"{label}: harness read only {growth:.0f}x for 16x input"
