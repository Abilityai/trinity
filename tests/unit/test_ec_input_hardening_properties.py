"""/edge-cases 2026-10-06 — Hypothesis properties for input hardening (slug `ec_input_hardening`).

Randomized arm of ``test_ec_input_hardening_edges.py`` (read that docstring for
scope, the module-loading rationale and the mirror/parity note). Properties:

safe_yaml
  * ROUND-TRIP / ORACLE — for any alias-free JSON-like document, the hardened
    loader returns exactly what ``yaml.safe_load`` returns, under BOTH policies.
    A guard that changed the meaning of an honest document is an outage that
    reads as hardening (the #1932 lesson cited in ent#314's tests).
  * NO-CRASH-TOTAL — over printable text (the reader's own printable set) of
    bounded nesting, the loader either returns or raises ``HardenedYamlError``;
    never a bare ``YAMLError`` or any other type. (The non-printable and
    deep-nesting escapes are strict-xfail rows in the edges file.)
  * METAMORPHIC — the byte cap is a threshold in exactly ``len(utf8)``; the
    expansion budget is monotone (admitted at N ⇒ admitted at N+1); REJECT
    accepts a subset of what BUDGET accepts, with the same value.

credential_paths
  * TOTALITY over arbitrary objects, and the STRUCTURAL rejections (absolute,
    any ``""``/``.``/``..`` segment, backslash, NUL) for any path containing them.
  * DENY PRECEDENCE — a cert-shaped basename under any root deny dir is refused.
  * ``disallowed_paths`` is the order-preserving complement of the predicate.

credential_sanitizer
  * IDEMPOTENCE — ``sanitize_text(sanitize_text(x)) == sanitize_text(x)``.
  * NO SECRET SURVIVES — every token family, embedded at arbitrary boundaries
    in arbitrary unicode text, is absent from the output; the same for
    ``sanitize_dict`` within ``max_depth`` and for ``scrub_secret``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st

_ROOT = Path(__file__).resolve().parents[2]

# tests/lint_sys_modules.py pattern: every sys.modules entry this file installs
# is named here and restored after each test so nothing leaks across files.
_STUBBED_MODULE_NAMES = ['_ec_input_hardening_p_safe_yaml', '_ec_input_hardening_p_cred_paths', '_ec_input_hardening_p_sanitizer']


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SY = _load("src/backend/utils/safe_yaml.py", "_ec_input_hardening_p_safe_yaml")
CP = _load("src/backend/services/credential_paths.py", "_ec_input_hardening_p_cred_paths")
CS = _load("src/backend/utils/credential_sanitizer.py", "_ec_input_hardening_p_sanitizer")

B = SY.AliasPolicy.BUDGET
R = SY.AliasPolicy.REJECT
HYE = SY.HardenedYamlError
RED = CS.REDACTION_PLACEHOLDER

PROPS = settings(max_examples=200, deadline=None,
                 suppress_health_check=[HealthCheck.too_slow])


# ===========================================================================
# safe_yaml
# ===========================================================================

class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):  # noqa: D401 — never emit an anchor
        return True


_scalars = (
    st.none() | st.booleans() | st.integers(-10**12, 10**12)
    | st.floats(allow_nan=False, allow_infinity=True) | st.text(max_size=20)
)
_json_like = st.recursive(
    _scalars,
    lambda kids: st.lists(kids, max_size=4)
    | st.dictionaries(st.text(max_size=8), kids, max_size=4),
    max_leaves=25,
)


@PROPS
@given(_json_like)
@example({"credentials": {"env_file": ["A"]}, "name": "x"})
@example({"true": 1, "yes": 2, "1": 3, "1.0": 4})  # string keys that LOOK like collapsing ones
@example("﻿")
def test_honest_documents_mean_exactly_what_safe_load_says(data):
    doc = yaml.dump(data, Dumper=_NoAliasDumper, allow_unicode=True)
    expected = yaml.safe_load(doc)
    for policy in (B, R):
        got = SY.load_hardened_yaml(doc, kind="k", alias_policy=policy,
                                    max_bytes=10**7)
        assert got == expected or (got != got and expected != expected)


# PyYAML Reader's printable set, so the totality property does not re-find the
# already-xfailed control-character escape on every run.
_printable = st.characters(
    codec="utf-8",
    exclude_categories=("Cs",),
).filter(lambda c: c in "\t\n\r\x85" or (
    "\x20" <= c <= "\x7e" or "\xa0" <= c <= "퟿"
    or "" <= c <= "�" or c >= "\U00010000"))
_yamlish = st.lists(
    st.sampled_from(["a", ": ", "- ", "\n", "  ", "&x ", "*x", "[", "]", "{", "}",
                     ",", "'", '"', "!!str ", "!t ", "? ", "<<: ", "---\n", "#", "1", "~"])
    | _printable, max_size=60,
).map("".join)


@PROPS
@given(_yamlish)
@example("a: &x [1]\nb: *x\n")
@example("? [a]\n: 1\n")
@example("{a: 1, a: 2}")
@example("[[[[1]]]]")
def test_loader_is_total_over_printable_text(text):
    assume(text.count("[") + text.count("{") < 200)  # deep nesting: see edges Y10 xfail
    for policy in (B, R):
        try:
            SY.load_hardened_yaml(text, kind="k", alias_policy=policy)
        except HYE as e:
            assert e.code.startswith("k_") and e.code[2:] in {
                "too_large", "alias_not_permitted", "alias_budget_exceeded",
                "duplicate_key", "yaml_invalid"}


@PROPS
@given(st.text(alphabet=_printable.filter(lambda c: c not in "\x85\u2028\u2029"),
               min_size=1, max_size=40))  # YAML folds these line breaks inside a scalar
@example("é" * 7)
@example("\U0001F600")
def test_byte_cap_threshold_is_exactly_the_utf8_length(value):
    # Raw (not \u-escaped) so multi-byte chars hit the byte count; escaped
    # astral chars would decode to lone surrogates under PyYAML (it does not
    # pair JSON-style `\ud83d\ude00` escapes) — a test-construction trap.
    doc = json.dumps({"a": value}, ensure_ascii=False)
    n = len(doc.encode("utf-8"))
    assert SY.load_hardened_yaml(doc, kind="k", alias_policy=B, max_bytes=n) == {"a": value}
    with pytest.raises(HYE) as exc:
        SY.load_hardened_yaml(doc, kind="k", alias_policy=B, max_bytes=n - 1)
    assert exc.value.code == "k_too_large"


def _pyramid(width: int, levels: int) -> str:
    lines = ["a0: &a0 [" + ", ".join(["x"] * width) + "]"]
    for i in range(1, levels + 1):
        lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i-1}"] * width) + "]")
    return "\n".join(lines) + "\n"


@PROPS
@given(st.integers(1, 4), st.integers(1, 3), st.integers(1, 3000))
@example(2, 1, 13)
def test_expansion_budget_is_monotone_and_reject_is_a_subset(width, levels, budget):
    doc = _pyramid(width, levels)

    def admitted(n):
        try:
            return True, SY.load_hardened_yaml(doc, kind="k", alias_policy=B,
                                               max_expanded_nodes=n)
        except HYE as e:
            assert e.code == "k_alias_budget_exceeded"
            return False, None

    ok_n, val_n = admitted(budget)
    ok_n1, val_n1 = admitted(budget + 1)
    if ok_n:
        assert ok_n1 and val_n1 == val_n
    # REJECT never admits an aliased document, whatever the budget
    with pytest.raises(HYE):
        SY.load_hardened_yaml(doc, kind="k", alias_policy=R)


# ===========================================================================
# credential_paths
# ===========================================================================

ok = CP.is_allowed_credential_path

_seg = st.text(alphabet=st.characters(exclude_characters="/\\\x00"), min_size=1, max_size=12)
_rel = st.lists(_seg | st.sampled_from([".ssh", ".config", "gcloud", ".claude", "id_rsa",
                                        "x.pem", ".env", ".kube", "config"]),
                min_size=1, max_size=5).map("/".join)


@PROPS
@given(st.one_of(st.none(), st.integers(), st.binary(max_size=10), st.text(max_size=40),
                 st.lists(st.text(max_size=5), max_size=2), _rel))
def test_policy_is_total_and_boolean(obj):
    assert ok(obj) in (True, False)


@PROPS
@given(_rel, st.sampled_from(["prefix-abs", "dotdot", "dot", "empty", "trailing",
                              "backslash", "nul"]),
       st.integers(0, 4))
@example(".env", "trailing", 0)
@example(".ssh/id_rsa", "dotdot", 1)
@example(".config/gcloud/a.json", "dot", 2)
def test_structural_poison_anywhere_is_refused(path, poison, at):
    segs = path.split("/")
    at = min(at, len(segs))
    if poison == "prefix-abs":
        bad = "/" + path
    elif poison in ("dotdot", "dot", "empty"):
        token = {"dotdot": "..", "dot": ".", "empty": ""}[poison]
        bad = "/".join(segs[:at] + [token] + segs[at:])
    elif poison == "trailing":
        bad = path + "/"
    elif poison == "backslash":
        bad = path[: at] + "\\" + path[at:]
    else:
        bad = path[: at] + "\x00" + path[at:]
    assert ok(bad) is False


_ROOT_DENY_DIRS = [".claude", ".git", "bin", ".local/bin", ".config/fish",
                   ".config/systemd", ".config/environment.d", "node_modules",
                   "site-packages", ".local", ".venv", "venv", ".cache", "go/pkg"]


@PROPS
@given(st.sampled_from(_ROOT_DENY_DIRS),
       st.lists(st.sampled_from(["a", "b", "x.y"]), max_size=2),
       st.sampled_from(["k.pem", "k.key", "k.crt", "k.cert", "k.p12", "k.pfx"]))
def test_deny_dirs_beat_cert_shaped_allow_globs(deny_dir, middle, base):
    assert ok("/".join([deny_dir, *middle, base])) is False


@PROPS
@given(st.lists(_rel | st.sampled_from(["../x", ".bashrc", ".env", "x.pem"]), max_size=8))
def test_disallowed_paths_is_the_ordered_complement(paths):
    assert CP.disallowed_paths(paths) == [p for p in paths if not ok(p)]


# ===========================================================================
# credential_sanitizer
# ===========================================================================

_alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _tok(prefix, alphabet, lo, hi):
    return st.text(alphabet=alphabet, min_size=lo, max_size=hi).map(lambda s: prefix + s)


_tokens = st.one_of(
    st.tuples(st.sampled_from(_alnum), st.text(alphabet=_alnum + "_-", min_size=19, max_size=40))
    .map(lambda t: "sk-" + t[0] + t[1]),
    *[_tok(p, _alnum, 36, 44) for p in ("ghp_", "gho_", "ghs_", "ghr_")],
    _tok("github_pat_", _alnum + "_", 22, 40),
    *[_tok(p, _alnum + "-", 1, 30) for p in ("xoxb-", "xoxp-", "xoxa-")],
    _tok("AKIA", _UP, 16, 16),
    _tok("trinity_mcp_", _alnum, 16, 30),
)
# Boundary characters a secret is realistically wedged between. Deliberately
# NOT `=` (a `k=` prefix is the separate KEY=value machinery) and not alnum
# (a glued alnum suffix only lengthens the match, which the edges file pins).
_sep = st.sampled_from(list(" \n\t\"'(),;:<>[]{}|`") + ["é", "→", "​", "　"])
_noise = st.text(max_size=30)


@PROPS
@given(_noise, _sep, _tokens, _sep, _noise)
@example("", " ", "sk-" + "a" * 20, " ", "")
@example("Bearer", " ", "ghp_" + "a" * 36, ")", "")
@example("", "'", "AKIA" + "A" * 16, "'", "")
def test_no_token_survives_at_any_boundary(pre, s1, tok, s2, post):
    out = CS.sanitize_text(pre + s1 + tok + s2 + post)
    assert tok not in out


@PROPS
@given(st.text(max_size=120) | st.lists(_noise | _tokens | st.sampled_from(
    ["TOKEN=", "a=", "PASSWORD='", '"', "Bearer ", "Basic ", "=", " ", "&", "\n",
     RED, "https://u:p@h/"]), max_size=10).map("".join))
@example("TOKEN=***REDACTED***x")
@example("Basic abc=TOKEN=x")
@example("xoxb-sk-" + "a" * 30)
def test_sanitize_text_is_idempotent(text):
    once = CS.sanitize_text(text)
    assert CS.sanitize_text(once) == once


_kids = st.recursive(
    _noise | _tokens | st.integers() | st.none(),
    lambda k: st.lists(k, max_size=3) | st.dictionaries(st.sampled_from(["a", "b", "input"]), k, max_size=3),
    max_leaves=12,
)


def _depth(x) -> int:
    if isinstance(x, dict):
        return 1 + max((_depth(v) for v in x.values()), default=0)
    if isinstance(x, list):
        return 1 + max((_depth(v) for v in x), default=0)
    return 0


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


@PROPS
@given(st.dictionaries(st.sampled_from(["x", "y", "z"]), _kids, max_size=3))
def test_no_token_survives_sanitize_dict_within_max_depth(data):
    assume(_depth(data) <= 11)  # the >max_depth fail-open is the edges S12 xfail
    out = json.dumps(CS.sanitize_dict(data))
    for s in _strings(data):
        if any(p.fullmatch(s) for p in CS._secret_value_re):
            assert s not in out


@PROPS
@given(_kids)
def test_json_entry_point_agrees_with_the_structured_one(data):
    raw = json.dumps(data)
    got = CS.sanitize_json_string(raw)
    if isinstance(data, dict):
        assert got == json.dumps(CS.sanitize_dict(data))
    elif isinstance(data, list):
        assert got == json.dumps(CS.sanitize_list(data))
    else:
        assert got == (CS.sanitize_text(raw) if raw else raw)


@PROPS
@given(st.text(max_size=80), st.text(alphabet=st.characters(exclude_characters="*",
                                                            exclude_categories=("Cs",)),
                                     min_size=1, max_size=12))
@example("aaaa", "aa")
@example("xabcxabc", "abc")
def test_scrub_secret_leaves_no_occurrence(text, secret):
    """Holds for any secret without `*` (a secret made of `*` can reappear
    inside the `***` replacement — no real token has one). Lone surrogates are
    excluded: the b64 pass encodes the secret, and every caller's secret is
    an ASCII PAT/API token."""
    out = CS.scrub_secret(text + secret + text, secret)
    assert secret not in out


@PROPS
@given(st.text(max_size=80))
@example("https://a:b@h/x https://c@h2")
def test_redact_url_userinfo_is_idempotent(text):
    once = CS.redact_url_userinfo(text)
    assert CS.redact_url_userinfo(once) == once
