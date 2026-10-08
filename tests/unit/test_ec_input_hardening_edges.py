"""/edge-cases 2026-10-06 — input-hardening boundaries (slug `ec_input_hardening`).

Three pure modules that sit between author/agent-controlled input and the
platform, worked at their BOUNDARIES rather than their headline cases:

* ``src/backend/utils/safe_yaml.py`` (ent#314) — the hardened YAML loader.
  ``test_ent314_hardened_yaml.py`` / ``test_1884_manifest_yaml_hardening.py``
  pin the bomb, the duplicate key and the oversize case. Nothing pinned the
  byte cap at EXACTLY-at vs cap+1 (or in bytes vs characters), the expansion
  budget at its own ``>`` comparison, the budget being per-parse, key
  EQUIVALENCE as a duplicate (``true``/``yes``, ``1``/``1.0``), non-mapping
  roots, BOM, NUL/control characters, or nesting depth.
* ``src/backend/services/credential_paths.py`` (#11) — the CRED-002 path
  policy. ``test_credential_inject_allowlist.py`` covers the named allow/deny
  list; this file adds the SHAPE boundaries: empty / non-str, trailing and
  doubled slashes, ``./`` and mid-path ``..``, unicode lookalikes and NFD,
  case, root-anchoring of the deny dirs.
* ``src/backend/utils/credential_sanitizer.py`` — the backend defense-in-depth
  redactor: secrets at string boundaries, exact-length token boundaries,
  overlap between value patterns and KEY=value, chained ``a=b&token=x`` pairs,
  the ``max_depth`` cut-off of ``sanitize_dict``, ``scrub_secret`` and
  ``redact_url_userinfo``.

Both ``safe_yaml.py`` and ``credential_paths.py`` have byte-identical vendored
mirrors under ``docker/base-image/agent_server/`` (Invariant #5); their parity
is already enforced by ``test_1965_agent_server_safe_yaml.py::
test_loader_copies_are_byte_identical`` and ``test_credential_paths_parity.py``,
so every test here targets the backend copy only. (The sanitizer has an agent
copy too, but it is NOT a byte mirror — see ``test_1661_sanitizer_linear.py``.)

Modules are loaded by FILE PATH (``importlib``), not by package: importing
``services.credential_paths`` drags in ``services/__init__`` → ``config``,
which refuses to import without a credentialed ``REDIS_URL``. All three
targets are pure (stdlib + PyYAML), so no harness is needed.

Real bugs are kept as ``xfail(strict=True)`` with the reachability evidence in
the test docstring; see the 2026-10-06 /edge-cases matrix. Matrix row
numbers are in each param id (``Y*`` safe_yaml, ``P*`` paths, ``S*`` sanitizer).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]

# tests/lint_sys_modules.py pattern: every sys.modules entry this file installs
# is named here and restored after each test so nothing leaks across files.
_STUBBED_MODULE_NAMES = ['_ec_input_hardening_safe_yaml', '_ec_input_hardening_cred_paths', '_ec_input_hardening_sanitizer']


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
    sys.modules[name] = mod  # dataclass/enum introspection needs a home
    spec.loader.exec_module(mod)
    return mod


SY = _load("src/backend/utils/safe_yaml.py", "_ec_input_hardening_safe_yaml")
CP = _load("src/backend/services/credential_paths.py", "_ec_input_hardening_cred_paths")
CS = _load(
    "src/backend/utils/credential_sanitizer.py", "_ec_input_hardening_sanitizer"
)

B = SY.AliasPolicy.BUDGET
R = SY.AliasPolicy.REJECT
HYE = SY.HardenedYamlError
RED = CS.REDACTION_PLACEHOLDER


def _load_yaml(text, *, policy=B, **kw):
    return SY.load_hardened_yaml(text, kind="k", alias_policy=policy, **kw)


def _code(text, *, policy=B, **kw):
    with pytest.raises(HYE) as exc:
        _load_yaml(text, policy=policy, **kw)
    return exc.value.code


# ===========================================================================
# safe_yaml — size cap (Y1-Y3)
# ===========================================================================

class TestSizeCapBoundary:
    """`len(text.encode("utf-8")) > max_bytes` — the cap is in BYTES and the
    comparison is strict. Existing tests only probe 300 KB vs 256 KiB."""

    @pytest.mark.parametrize("pad", [0, 1, 7], ids=["Y1-short", "Y1-short+1", "Y1-short+7"])
    def test_exactly_at_the_cap_parses_and_one_byte_over_refuses(self, pad):
        doc = "a: " + "x" * (10 + pad)
        cap = len(doc.encode())
        assert _load_yaml(doc, max_bytes=cap) == {"a": "x" * (10 + pad)}  # Y1
        assert _code(doc, max_bytes=cap - 1) == "k_too_large"  # Y2

    def test_the_cap_counts_utf8_bytes_not_characters(self):
        """Y3 — a 2-byte `é` costs two bytes: a char-count cap would admit a
        document up to 4x the intended size for astral-plane text."""
        doc = "a: " + "é" * 5  # 3 + 10 bytes, 8 characters
        assert len(doc) == 8 and len(doc.encode()) == 13
        assert _load_yaml(doc, max_bytes=13) == {"a": "é" * 5}
        assert _code(doc, max_bytes=12) == "k_too_large"
        # A char-count cap of 12 would have admitted it — prove the test can tell.
        assert len(doc) <= 12

    def test_four_byte_codepoints_are_counted_as_four(self):
        doc = "a: " + "\U0001F600" * 2  # 3 + 8 bytes
        assert _load_yaml(doc, max_bytes=11) == {"a": "\U0001F600" * 2}
        assert _code(doc, max_bytes=10) == "k_too_large"

    def test_too_large_fires_before_any_parsing(self):
        """Even an otherwise-invalid document reports `_too_large`, i.e. the
        cap is checked first and nothing is composed."""
        assert _code("{{{{ not yaml", max_bytes=3) == "k_too_large"

    def test_registry_and_template_helpers_carry_their_caps(self):
        over = "a: " + "x" * (SY.TEMPLATE_REGISTRY_MAX_BYTES)
        with pytest.raises(HYE) as exc:
            SY.load_template_registry_yaml(over)
        assert exc.value.code == "template_registry_too_large"
        with pytest.raises(HYE) as exc:
            SY.load_template_yaml("a: " + "x" * SY.TEMPLATE_YAML_MAX_BYTES)
        assert exc.value.code == "template_too_large"


# ===========================================================================
# safe_yaml — document shape (Y4-Y11, Y13-Y15, Y31-Y32)
# ===========================================================================

class TestDocumentShape:

    @pytest.mark.parametrize("doc", ["", "   ", "\n\n", "# only a comment\n", "---\n"],
                             ids=["Y4-empty", "Y5-spaces", "Y5-newlines", "Y5-comment", "Y5-marker"])
    def test_empty_documents_return_none_not_an_error(self, doc):
        assert _load_yaml(doc) is None

    @pytest.mark.parametrize("doc,expected", [
        ("- 1\n- 2\n", [1, 2]),
        ("just a string", "just a string"),
        ("42", 42),
        ("null", None),
        ("[a, {b: 1}]", ["a", {"b": 1}]),
    ], ids=["Y6-list", "Y6-str", "Y6-int", "Y6-null", "Y6-mixed"])
    def test_non_mapping_roots_are_returned_not_rejected(self, doc, expected):
        """The loader is shape-agnostic by design — every consumer does its own
        `isinstance(data, dict)` (template_service.py:1484, system_service.py
        parse_manifest, template_registry_service.parse_registry_document).
        Pinned so a 'helpful' root check never lands here and changes every
        caller's error code at once."""
        assert _load_yaml(doc) == expected

    def test_a_leading_bom_is_stripped(self):
        """Y7 — editors on Windows write a BOM; it must not become part of the
        first key (`'\\ufeffname'` would make `data["name"]` a KeyError)."""
        assert _load_yaml("\ufeffname: x\n") == {"name": "x"}

    @pytest.mark.xfail(strict=True, reason=(
        "BUG: NUL / C0 control chars raise yaml.ReaderError from the loader's "
        "constructor, OUTSIDE the try that maps YAMLError to `_yaml_invalid` — "
        "#3324"))
    @pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\x7f", "\ufffe"],
                             ids=["Y8-nul", "Y9-bel", "Y9-esc", "Y9-del", "Y9-fffe"])
    def test_control_characters_are_a_named_parse_error(self, ch):
        """Y8/Y9. The docstring contract is "Raises error_cls with a code of
        ... _yaml_invalid". `loader(text)` runs PyYAML's Reader.__init__, which
        checks printability eagerly, and that call sits ABOVE the `try`.

        Reachability: `system_service.deploy_system` (system_service.py:1815-1821)
        catches only `ManifestError` and `ValueError` around `parse_manifest`;
        `yaml.ReaderError` is neither, so a manifest with a stray NUL answers a
        500 instead of the named 400 (#1884's whole point). Template paths
        mostly fence with `except Exception` and degrade to the generic branch.
        """
        with pytest.raises(HYE) as exc:
            _load_yaml("name: x" + ch + "\n")
        assert exc.value.code == "k_yaml_invalid"

    def test_control_characters_do_not_parse_silently(self):
        """Companion to the xfail above: whatever the type, a NUL is REFUSED,
        never parsed into the value (the security half holds today)."""
        with pytest.raises(Exception):
            _load_yaml("name: x\x00\n")

    @pytest.mark.parametrize("ch", ["\t", "\r\n", "\x85", "\u2028"],
                             ids=["Y9-tab", "Y9-crlf", "Y9-nel", "Y9-lsep"])
    def test_printable_whitespace_variants_still_parse(self, ch):
        # (PyYAML refuses a bare tab as token whitespace, so the tab row puts it
        # inside a quoted scalar — the printable-reader check is what matters)
        out = (_load_yaml("a: 1" + ch + "b: 2" + ch) if ch != "\t"
               else _load_yaml('a: 1\nb: "x\ty"\n'))
        assert isinstance(out, dict) and out["a"] == 1

    @pytest.mark.xfail(strict=True, reason=(
        "BUG: deep flow nesting raises RecursionError (not HardenedYamlError) "
        "well under the byte cap — #3324"))
    def test_deep_nesting_is_a_named_refusal(self):
        """Y10. 6 KB of `[` — 2.3% of the 256 KiB cap — exhausts the Python
        stack inside PyYAML's recursive composer. The loader bounds bytes and
        alias EXPANSION but not DEPTH, so the refusal escapes as RuntimeError.

        Reachability: `deploy_system` (system_service.py:1815-1821) catches
        only ManifestError/ValueError → unnamed 500 for a creator-submitted
        manifest; `template_service._build_template` documents this exact
        escape and fences it with `except Exception` (template_service.py:1479-
        1483, ent#128) — i.e. callers are compensating for the loader."""
        doc = "[" * 3000 + "]" * 3000
        assert len(doc.encode()) < SY.DEFAULT_MAX_BYTES
        with pytest.raises(HYE):
            _load_yaml(doc)

    def test_moderate_nesting_parses(self):
        """Y11 — the depth gap must not be 'fixed' by a cap that breaks real
        documents: 50 levels of honest nesting parse."""
        doc = "[" * 50 + "1" + "]" * 50
        out = _load_yaml(doc)
        for _ in range(50):
            assert isinstance(out, list) and len(out) == 1
            out = out[0]
        assert out == 1

    @pytest.mark.parametrize("doc", [
        "a: !custom 1",
        "a: !!python/object/apply:os.system [ls]",
        "a: !!python/name:os.system",
        "!!python/object/new:dict {}",
    ], ids=["Y13-local-tag", "Y12-apply", "Y12-name", "Y12-new-root"])
    def test_unknown_and_python_tags_are_yaml_invalid(self, doc):
        assert _code(doc) == "k_yaml_invalid"

    def test_safe_core_tags_still_construct_non_json_types(self):
        """Y14 (UNSPECIFIED, pinned): SafeLoader's own `!!binary` and implicit
        timestamps construct `bytes` / `date`. Not a security hole, but a
        consumer that json-serializes the parse (the catalog) receives them."""
        import datetime as _dt
        assert _load_yaml("a: !!binary aGk=") == {"a": b"hi"}
        assert _load_yaml("a: 2001-12-14") == {"a": _dt.date(2001, 12, 14)}

    def test_multi_document_stream_is_refused(self):
        """Y15 — `get_single_data` refuses a second document; a two-document
        file must not silently mean 'the first one'."""
        assert _code("---\na: 1\n---\nb: 2\n") == "k_yaml_invalid"

    def test_lone_surrogate_raises_a_valueerror(self):
        """Y32 (UNSPECIFIED): a lone surrogate (reachable only via a JSON
        `\\ud800` escape in a request body) fails the byte-count `encode`
        before any guard, as `UnicodeEncodeError` — a ValueError subclass, so
        `deploy_system`'s `except ValueError` still answers 400, just unnamed."""
        with pytest.raises(ValueError):
            _load_yaml("a: \ud800")


# ===========================================================================
# safe_yaml — duplicate keys (Y16-Y24)
# ===========================================================================

class TestDuplicateKeys:

    @pytest.mark.parametrize("doc", [
        "{true: a, yes: b}",
        "{on: a, True: b}",
        "{1: a, 1.0: b}",
        "{0: a, false: b}",
        '"a": 1\na: 2\n',
        "'a': 1\n\"a\": 2\n",
        "~: 1\nnull: 2\n",
        "a: 1\n? a\n: 2\n",
        "- {x: 1, x: 2}\n",
        "outer:\n  - inner: {k: 1}\n    inner: {k: 2}\n",
        "0x10: a\n16: b\n",
    ], ids=["Y16-true-yes", "Y16-on-True", "Y17-1-1.0", "Y17-0-false",
            "Y18-quoted-plain", "Y18-single-double", "Y21-null",
            "Y18-explicit-key", "Y23-in-seq", "Y23-deep", "Y17-hex"])
    def test_keys_that_collapse_in_the_dict_are_duplicates(self, doc):
        """The guard compares CONSTRUCTED keys, so every spelling that would
        collapse into one dict slot (YAML 1.1 bools, int/float equality, quoting,
        hex ints) is caught — the 'show one value, mean another' class."""
        assert _code(doc) == "k_duplicate_key"

    @pytest.mark.parametrize("doc,expected", [
        ("a: 1\nA: 2\n", {"a": 1, "A": 2}),
        ('1: a\n"1": b\n', {1: "a", "1": "b"}),
        ("a: 1\na_: 2\n", {"a": 1, "a_": 2}),
        ("x: {k: 1}\ny: {k: 2}\n", {"x": {"k": 1}, "y": {"k": 2}}),
    ], ids=["Y19-case", "Y20-int-vs-str", "Y19-suffix", "Y23-sibling-scopes"])
    def test_distinct_keys_are_not_duplicates(self, doc, expected):
        assert _load_yaml(doc) == expected

    def test_unhashable_complex_key_is_invalid_not_a_crash(self):
        """Y22 — the guard swallows TypeError for unhashable keys and lets the
        SafeConstructor refuse it; must surface as the named parse error."""
        assert _code("? [a]\n: 1\n") == "k_yaml_invalid"
        assert _code("? [a]\n: 1\n? [a]\n: 2\n") == "k_yaml_invalid"

    @pytest.mark.parametrize("doc", ["{!custom k: 1}", "!custom k: 1\nk: 2\n"],
                             ids=["Y22-tagged-key", "Y22-tagged-key-then-dup"])
    def test_a_key_that_cannot_be_constructed_is_invalid(self, doc):
        """Y22b — the duplicate guard SKIPS a key whose construction raises
        (`except ConstructorError: continue`); the SafeConstructor must then
        refuse the mapping, so a skipped key can never smuggle a duplicate."""
        assert _code(doc) == "k_yaml_invalid"

    def test_duplicate_error_preserves_its_code_through_the_wrapper(self):
        """Y36 — `except err: raise` must sit before `except YAMLError`, or a
        duplicate (raised mid-construction) would be relabelled `_yaml_invalid`."""
        err = _code("a: 1\na: 2\n")
        assert err == "k_duplicate_key"

    def test_merge_key_then_explicit_override_is_not_a_duplicate(self):
        """Y24 (pinned): an explicit key overriding a `<<` merge is YAML's
        documented override, not two spellings of one key."""
        assert _load_yaml("<<: {a: 1, b: 1}\na: 2\n") == {"a": 2, "b": 1}


# ===========================================================================
# safe_yaml — alias budget (Y26-Y30)
# ===========================================================================

# root map (1) + key a (1) + seq (1) + 2 scalars (2) + key b (1) + alias(=seq cost 3)
_ONE_ALIAS = "a: &x [1, 2]\nb: *x\n"
_ONE_ALIAS_COST = 9


class TestAliasBudgetBoundary:

    def test_exactly_at_the_budget_parses_and_one_over_refuses(self):
        """Y26 — `self._expanded_nodes > max_expanded_nodes`, strict. No existing
        test sits on the comparison itself (they use 10x bombs)."""
        assert _load_yaml(_ONE_ALIAS, max_expanded_nodes=_ONE_ALIAS_COST) == {
            "a": [1, 2], "b": [1, 2]}
        assert _code(_ONE_ALIAS, max_expanded_nodes=_ONE_ALIAS_COST - 1) == \
            "k_alias_budget_exceeded"

    def test_an_alias_costs_its_full_subtree_not_one(self):
        """Y35 — doubling the anchored subtree must move the threshold by the
        subtree size (the 'alias count' shape the docstring rejects)."""
        doc = "a: &x [1, 2, 3, 4]\nb: *x\n"  # 1+1+(1+4)+1+5 = 13
        assert _load_yaml(doc, max_expanded_nodes=13)
        assert _code(doc, max_expanded_nodes=12) == "k_alias_budget_exceeded"

    def test_nested_aliases_compound(self):
        """An anchor that itself contains aliases is costed at its EXPANDED
        size: level-2 pyramid costs multiplicatively."""
        doc = "a: &a [1, 1]\nb: &b [*a, *a]\nc: *b\n"
        # root1 + ka1 + a(3) + kb1 + b(1 + 3 + 3 = 7) + kc1 + alias b(7) = 21
        assert _load_yaml(doc, max_expanded_nodes=21)
        assert _code(doc, max_expanded_nodes=20) == "k_alias_budget_exceeded"

    def test_the_budget_is_per_parse(self):
        """Y29 — a fresh loader class per call; a shared counter would make the
        Nth honest template on a catalog listing fail."""
        for _ in range(5):
            assert _load_yaml(_ONE_ALIAS, max_expanded_nodes=_ONE_ALIAS_COST)

    def test_anchor_redefinition_is_refused_by_the_composer(self):
        """Y28 — would the cost map follow a REDEFINED anchor? Moot: PyYAML's
        composer refuses a duplicate anchor outright, so `_anchor_cost[anchor]`
        can never be overwritten with a stale value. Pinned so a PyYAML upgrade
        that starts accepting redefinition (YAML 1.2 allows it) surfaces here
        rather than as a silently mis-costed alias."""
        assert _code("a: &x 1\nb: &x [1, 2, 3, 4, 5, 6, 7, 8]\nc: *x\n") == \
            "k_yaml_invalid"

    def test_undefined_alias_is_invalid(self):
        """Y27"""
        assert _code("a: *nope\n") == "k_yaml_invalid"

    def test_alias_free_documents_are_not_budgeted(self):
        """Y31 (pinned/UNSPECIFIED): the budget is only CHECKED at an alias
        event, so a large alias-free document never trips it, whatever the
        node count — the byte cap is its only bound."""
        doc = "[" + ", ".join(["1"] * 500) + "]"
        assert len(_load_yaml(doc, max_expanded_nodes=10)) == 500

    @pytest.mark.parametrize("doc", ["&a *x", "a: !!str *x", '"a" *x'],
                             ids=["Y25-anchor-on-alias", "Y25-tagged-alias", "Y25-glued-alias"])
    def test_reject_names_the_alias_even_when_the_parser_would_fail_first(self, doc):
        """Y25b — the SCANNER gate (`fetch_alias`) is the only one that sees an
        alias in a document the parser then rejects; without it these answer
        `_yaml_invalid` and the operator is told 'malformed' rather than
        'aliases are not permitted here'. Kills the mutant that drops the
        scanner gate (the compose gate alone never runs on these)."""
        assert _code(doc, policy=R) == "k_alias_not_permitted"

    def test_reject_refuses_what_budget_admits(self):
        assert _code(_ONE_ALIAS, policy=R) == "k_alias_not_permitted"


class TestErrorClassAndHelpers:

    class _MyErr(HYE):
        pass

    @pytest.mark.parametrize("doc,kw,code", [
        ("a: " + "x" * 50, {"max_bytes": 10}, "k_too_large"),
        ("a: 1\na: 2\n", {}, "k_duplicate_key"),
        (": : :\n  - ]", {}, "k_yaml_invalid"),
        (_ONE_ALIAS, {"max_expanded_nodes": 1}, "k_alias_budget_exceeded"),
    ], ids=["Y30-too-large", "Y30-dup", "Y30-invalid", "Y30-budget"])
    def test_custom_error_cls_is_raised_for_every_code(self, doc, kw, code):
        """Y30 — `error_cls` must be honoured on EVERY path, or a consumer's
        `except ManifestError` (and its router's code→400 map) misses one."""
        with pytest.raises(self._MyErr) as exc:
            SY.load_hardened_yaml(doc, kind="k", alias_policy=B,
                                  error_cls=self._MyErr, **kw)
        assert exc.value.code == code

    def test_custom_error_cls_on_reject_scanner_path(self):
        with pytest.raises(self._MyErr) as exc:
            SY.load_hardened_yaml("a: &x 1\nb: *x\n", kind="k", alias_policy=R,
                                  error_cls=self._MyErr)
        assert exc.value.code == "k_alias_not_permitted"

    def test_registry_helper_rejects_any_alias_with_its_prefix(self):
        """Y33 — the registry is REJECT and its codes are `template_registry_*`
        (template_registry_service maps exactly that prefix)."""
        with pytest.raises(HYE) as exc:
            SY.load_template_registry_yaml("a: &x 1\nb: *x\n")
        assert exc.value.code == "template_registry_alias_not_permitted"
        with pytest.raises(HYE) as exc:
            SY.load_template_registry_yaml("templates: []\ntemplates: []\n")
        assert exc.value.code == "template_registry_duplicate_key"
        assert SY.load_template_registry_yaml("templates: [{repo: a/b}]") == {
            "templates": [{"repo": "a/b"}]}

    def test_template_helper_is_budget_not_reject(self):
        """Y34"""
        assert SY.load_template_yaml(_ONE_ALIAS) == {"a": [1, 2], "b": [1, 2]}


# ===========================================================================
# credential_paths — shape boundaries (P1-P23)
# ===========================================================================

ok = CP.is_allowed_credential_path


class TestCredentialPathShapes:

    @pytest.mark.parametrize("path", ["", None, 0, 5, b".env", [".env"], {".env": 1}],
                             ids=["P1-empty", "P2-none", "P2-zero", "P2-int", "P2-bytes",
                                  "P2-list", "P2-dict"])
    def test_empty_and_non_string_are_refused_without_raising(self, path):
        assert ok(path) is False

    @pytest.mark.parametrize("path", [
        "./.env", "a/../.env", ".config/gcloud/../../.bashrc", ".ssh/./id_rsa",
        "..", ".", "../", "a/..",
    ], ids=["P4-dot-prefix", "P4-mid-dotdot", "P4-escape-via-allowed-dir",
            "P4-dot-seg", "P4-bare-dotdot", "P4-bare-dot", "P4-dotdot-slash", "P4-trailing-dotdot"])
    def test_dot_and_dotdot_segments_anywhere_are_refused(self, path):
        assert ok(path) is False

    @pytest.mark.parametrize("path", [
        ".env/", ".ssh/id_rsa/", ".config/gcloud/", "a//b.pem", "//x.pem", ".kube/config/",
    ], ids=["P5-env", "P5-ssh", "P5-gcloud-dir", "P6-double", "P6-leading-double", "P5-kube"])
    def test_trailing_and_doubled_slashes_are_refused(self, path):
        assert ok(path) is False

    @pytest.mark.parametrize("path,expected", [
        ("x.PEM", False),            # allow globs are case-SENSITIVE
        (".Env", False),
        (".Claude/x.pem", True),     # deny dirs are case-sensitive too (Linux fs)
        (".GIT/x.key", True),
        ("Claude.md", False),        # not on the allow list, so the case gap is inert
    ], ids=["P8-upper-ext", "P8-env-case", "P8-claude-case", "P8-git-case", "P8-claudemd-case"])
    def test_case_is_significant_in_both_directions(self, path, expected):
        """P8 (pinned/UNSPECIFIED): matching is `fnmatchcase`. Agent homes are
        Docker volumes on a case-sensitive filesystem, so `.Claude/` is a
        different directory that Claude Code never reads — the True rows are
        inert there. On a case-insensitive mount they would not be."""
        assert ok(path) is expected

    @pytest.mark.parametrize("path,expected", [
        ("\uff0e\uff0e/x.pem", True),   # FULLWIDTH FULL STOP x2 — a literal dir name
        ("..\u2215x.pem", True),         # DIVISION SLASH — one segment, not a separator
        (".e\u0301nv", False),          # NFD lookalike of nothing on the list
        ("..pem", True),                 # a FILENAME starting with dots, not traversal
        ("\u202ex.pem", True),           # RTL override in a basename
    ], ids=["P9-fullwidth-dots", "P9-division-slash", "P10-nfd", "P9-dotdot-prefix-name", "P9-rlo"])
    def test_unicode_lookalikes_are_literal_names_not_traversal(self, path, expected):
        """P9/P10 — the policy does no normalization, and the writer
        (`agent_server/routers/credentials.py::_safe_credential_target`)
        resolves with `Path.resolve()`, which does none either, so a lookalike
        is a harmless literal directory name. If either side ever adds NFKC
        normalization, `\\uff0e\\uff0e` becomes `..` and these rows must flip."""
        assert ok(path) is expected

    def test_backslash_anywhere_and_nul_anywhere(self):
        assert ok("a\\b.pem") is False
        assert ok("x.pem\x00") is False
        assert ok("\x00") is False

    @pytest.mark.parametrize("path,expected", [
        (".ssh/sub/id_rsa", False),
        (".ssh/.ssh/id_rsa", False),
        (".ssh/id_", True),
        (".ssh/id_rsa.pem", True),
        (".ssh/environment.pem", False),
        (".ssh", False),
        ("id_rsa", False),
    ], ids=["P13-nested", "P13-double-ssh", "P14-bare-prefix", "P14-pem", "P13-env-pem",
            "P13-dir", "P13-root-id"])
    def test_ssh_is_exactly_id_star_at_depth_one(self, path, expected):
        assert ok(path) is expected

    @pytest.mark.parametrize("path,expected", [
        (".config/gcloud", False),
        (".config/gcloud/a", True),
        (".config/gcloud/a/b/c.json", True),
        (".kube/config", True),
        (".kube/Config", False),
        (".mcp.json", True),
        ("sub/.mcp.json", False),
        (".env.local", False),
        ("sub/.env", False),
    ], ids=["P15-dir-itself", "P15-one", "P15-deep", "P16-kube", "P16-kube-case",
            "P20-root-mcp", "P20-nested-mcp", "P21-env-local", "P21-nested-env"])
    def test_allow_rules_at_their_edges(self, path, expected):
        """`**` is ONE-or-more segments, so the gcloud dir itself is not a
        writable file; exact entries are root-only."""
        assert ok(path) is expected

    @pytest.mark.parametrize("path,expected", [
        ("sub/.claude/x.pem", True),
        ("a/.git/x.pem", True),
        ("a/bin/x.pem", True),
        ("a/go/pkg/x.pem", True),
        ("a/node_modules/x.pem", False),
        ("CLAUDE.md/x.pem", True),
        (".bashrc/x.pem", True),
    ], ids=["P12-nested-claude", "P12-nested-git", "P12-nested-bin", "P17-nested-gopkg",
            "P17-nested-node-modules", "P23-claudemd-as-dir", "P23-bashrc-as-dir"])
    def test_deny_dirs_are_root_anchored(self, path, expected):
        """P12/P17/P23 (pinned/UNSPECIFIED): only `node_modules`/`site-packages`
        carry a `**/` variant; every other deny dir is anchored at the home
        root. Inert today because an ALLOW match still needs a cert-shaped
        basename, which nothing executes — but a future ALLOW glob broad enough
        to admit an executable name would reach `sub/.claude/` and `a/.git/`."""
        assert ok(path) is expected

    def test_glob_metacharacters_in_the_PATH_are_literal(self):
        """P18 — the path is the NAME side of fnmatch, so `*`/`[` in it never
        widen a deny/allow rule."""
        assert ok("*.pem") is True and ok("[a].pem") is True
        assert ok(".bash*") is False and ok(".claude*/x") is False

    def test_newline_inside_an_ssh_key_name(self):
        """P11 (pinned): fnmatch's `*` crosses `\\n`, so `.ssh/id_rsa\\n` is an
        `id_*` file. Harmless — sshd/ssh read neither — but it means the
        basename check is not a 'printable name' check."""
        assert ok(".ssh/id_rsa\n") is True
        assert ok("x.pem\n") is False

    def test_disallowed_paths_preserves_order_duplicates_and_accepts_iterables(self):
        """P19"""
        paths = ["../a", ".env", ".bashrc", "../a", "x.pem"]
        assert CP.disallowed_paths(paths) == ["../a", ".bashrc", "../a"]
        assert CP.disallowed_paths(iter(paths)) == ["../a", ".bashrc", "../a"]
        assert CP.disallowed_paths([]) == []
        # an empty path is DISALLOWED and must be reported, not dropped — a
        # filter that skipped falsy entries would let the caller treat
        # `{"": ...}` as all-clear
        assert CP.disallowed_paths(["", ".env"]) == [""]


# ===========================================================================
# credential_sanitizer (S1-S24)
# ===========================================================================

GHP = "ghp_" + "A1b2C3d4" * 4 + "Zz9Y"          # 36 after prefix
SK = "sk-proj-" + "abcDEF123_" * 3              # sk-family
AKIA = "AKIA" + "ABCDEFGH12345678"               # exactly 16
MCP = "trinity_mcp_" + "q" * 16


class TestSanitizerBoundaries:

    @pytest.mark.parametrize("text", [
        GHP, GHP + "\n", "\n" + GHP, "(" + GHP + ")", '"' + GHP + '"', "'" + SK + "'",
        "é" + AKIA + "ü", "token:" + MCP, "<" + SK + ">", "\t" + GHP + "\t",
        "x" + SK, AKIA + ".", "[" + GHP + "]," + SK,
    ], ids=["S2-whole", "S2-end-nl", "S2-start-nl", "S3-parens", "S3-dq", "S3-sq",
            "S19-unicode", "S3-colon", "S3-angle", "S3-tabs", "S2-alnum-prefix",
            "S2-dot-suffix", "S3-two"])
    def test_secret_at_any_boundary_is_gone(self, text):
        out = CS.sanitize_text(text)
        for tok in (GHP, SK, AKIA, MCP):
            assert tok not in out
        assert RED in out

    @pytest.mark.parametrize("text,redacted", [
        ("sk-" + "a" * 20, True),
        ("sk-" + "a" * 19, False),
        ("ghp_" + "a" * 36, True),
        ("ghp_" + "a" * 35, False),
        ("AKIA" + "A" * 16, True),
        ("AKIA" + "A" * 15, False),
        ("github_pat_" + "a" * 22, True),
        ("github_pat_" + "a" * 21, False),
        ("trinity_mcp_" + "a" * 16, True),
        ("trinity_mcp_" + "a" * 15, False),
        ("xoxb-1", True),
        ("xoxb-", False),
    ], ids=["S4-sk-20", "S4-sk-19", "S6-ghp-36", "S6-ghp-35", "S5-akia-16", "S5-akia-15",
            "S6-ghpat-22", "S6-ghpat-21", "S6-mcp-16", "S6-mcp-15", "S6-xoxb-1", "S6-xoxb-0"])
    def test_minimum_length_boundaries(self, text, redacted):
        """Exactly-at vs one-below each family's documented minimum."""
        assert (CS.sanitize_text(text) == RED) is redacted

    @pytest.mark.parametrize("text,expected", [
        ("Bearer " + SK, "Bearer " + RED),
        ("GH_TOKEN=" + GHP, "GH_TOKEN=" + RED),
        ("Authorization: Bearer abc.def-ghi", "Authorization: " + RED),
        ("API_KEY=" + RED, "API_KEY=" + RED),
    ], ids=["S7-bearer-sk", "S7-kv-ghp", "S7-bearer-jwtish", "S22-placeholder-stable"])
    def test_overlapping_rules_leave_one_placeholder_and_no_residue(self, text, expected):
        assert CS.sanitize_text(text) == expected

    def test_kv_pairs_separated_by_whitespace_are_each_redacted(self):
        out = CS.sanitize_text("FOO=bar GH_TOKEN=s1 PASSWORD=s2\nAPI_SECRET='s3'")
        assert out == f"FOO=bar GH_TOKEN={RED} PASSWORD={RED}\nAPI_SECRET={RED}"

    @pytest.mark.xfail(strict=True, reason=(
        "BUG: a non-sensitive `k=v` glued to a sensitive pair (`a=1&token=X`, "
        "`--env=GH_TOKEN=X`) swallows it — regression from #1670, the pre-#1661 "
        "regex redacted these — #3311"))
    @pytest.mark.parametrize("text,secret", [
        ("https://api.example.com/v1?client=me&access_token=s3cr3tv4lue", "s3cr3tv4lue"),
        ("curl -d user=alice&password=hunter2xyz https://x", "hunter2xyz"),
        ("docker run --env=GITHUB_TOKEN=plainsecret img", "plainsecret"),
        ("cfg=a,API_KEY=k3yk3yk3y", "k3yk3yk3y"),
    ], ids=["S8-query-string", "S8-form-body", "S9-flag-env", "S8-comma-list"])
    def test_a_sensitive_pair_chained_after_a_harmless_one_is_redacted(self, text, secret):
        """S8/S9. `_KV_LINE_RE` takes the key up to the FIRST `=` and the value
        as `[^\\s"']+`, so in `client=me&access_token=X` the key is `...?client`
        (not sensitive) and the value `me&access_token=X` is consumed whole and
        returned verbatim — the sensitive pair is never examined. The composed
        pre-#1661 regex (`(.*TOKEN.*)=...`, git 7670d6e35^) matched the greedy
        key `...&access_token` and redacted it, so this is a leak introduced by
        the linearization — exactly what `test_1661_sanitizer_linear.py::
        TestPreservedSemantics` says must not happen ("a faster filter that
        redacts LESS is a leak"), on a shape it never tried.

        Reachability: execution logs persisted via
        `chat_execution_service.py:434` (`sanitize_execution_log` →
        `sanitize_text` on every tool-input string, e.g. a Bash `curl` with a
        query-string token); also `error_handlers.py:54`, `a2a_client.py:601`.
        """
        assert secret not in CS.sanitize_text(text)

    def test_quoted_multiword_value_remains_a_known_gap(self):
        """S10 — already pinned by test_1661 as a documented pre-existing gap;
        restated only so the matrix row links to a test."""
        assert CS.sanitize_text('PASSWORD="two words"') == 'PASSWORD="two words"'

    def test_json_shaped_pairs_are_not_kv_redacted(self):
        """S11 (UNSPECIFIED): KEY-name redaction only understands `KEY=value`.
        A JSON/YAML-shaped `"PASSWORD": "x"` keeps its value unless the value
        itself is token-shaped."""
        assert CS.sanitize_text('{"PASSWORD": "hunter2"}') == '{"PASSWORD": "hunter2"}'
        assert CS.sanitize_text('PASSWORD: hunter2') == 'PASSWORD: hunter2'

    def test_very_long_input_redacts_a_secret_at_the_far_end(self):
        """S18 — performance is pinned by test_1661; this pins CORRECTNESS at the
        tail of a large line (a chunking/truncation regression would drop it)."""
        text = ("lorem ipsum " * 20_000) + GHP
        out = CS.sanitize_text(text)
        assert GHP not in out and out.endswith(RED)


def _nest(depth: int, leaf):
    """`depth` dict levels wrapping `leaf`: _nest(0, x) == x."""
    for _ in range(depth):
        leaf = {"n": leaf}
    return leaf


class TestSanitizeDictDepth:

    def test_secret_at_the_last_sanitized_depth_is_redacted(self):
        """S12 boundary — max_depth=10: the dict at depth 10 is still walked,
        so a string inside it (11 dict levels in total) is sanitized."""
        data = _nest(11, GHP)
        out = CS.sanitize_dict(data)
        assert GHP not in json.dumps(out)

    @pytest.mark.xfail(strict=True, reason=(
        "BUG: sanitize_dict/sanitize_list return the subtree UNSANITIZED past "
        "max_depth (fail-open) — #3312"))
    @pytest.mark.parametrize("shape", ["log", "lists"], ids=["S12-dict-log", "S12-list-nest"])
    def test_secret_below_max_depth_is_still_redacted(self, shape):
        """S12. `if depth > max_depth: return data` hands back the raw subtree,
        so any secret nested 12+ containers deep is persisted verbatim. The cap
        exists to bound recursion (existing test only asserts 'does not crash'),
        but the safe failure for a REDACTOR is to redact what it cannot walk
        (e.g. `sanitize_text(json.dumps(subtree))`), not to pass it through.

        Reachability: `chat_execution_service.py:434` →
        `sanitize_execution_log` → `sanitize_json_string` → `sanitize_list`.
        A stream-json log is list(0) → event(1) → message(2) → content(3) →
        tool_use(4) → input(5), so a tool input with ~6 levels of its own
        nesting (an MCP call carrying structured JSON, e.g. `report`/canvas
        payloads) crosses the cut-off. The agent-side copy has the same cap,
        so this layer is not backstopped there either."""
        if shape == "log":
            log = [{"message": {"content": [{"input": _nest(8, "key " + GHP)}]}}]
        else:
            log = [[[[[[[[[[[[["key " + GHP]]]]]]]]]]]]]
        out = CS.sanitize_execution_log(json.dumps(log))
        assert GHP not in out

    def test_list_and_dict_depth_count_together(self):
        """S23 — lists consume depth exactly like dicts."""
        data = {"l": [[[[{"s": GHP}]]]]}
        assert GHP not in json.dumps(CS.sanitize_dict(data))

    def test_non_string_scalars_pass_through_untouched(self):
        """S24"""
        data = {"a": 1, "b": None, "c": True, "d": 1.5, "e": [None, 2]}
        assert CS.sanitize_dict(data) == data

    def test_dict_keys_are_not_sanitized(self):
        """S13 (UNSPECIFIED): only values are walked. A token used as a KEY
        survives. Unlikely in tool payloads, but not impossible."""
        assert GHP in CS.sanitize_dict({GHP: 1})


class TestJsonEntryPoints:

    def test_scalar_json_string_falls_back_to_text(self):
        """S14 — a JSON document whose root is a string is sanitized as raw
        text (quotes included), not parsed-and-redumped."""
        raw = json.dumps(GHP)
        assert CS.sanitize_json_string(raw) == '"' + RED + '"'

    def test_redump_is_canonical_json(self):
        """S15 (pinned): a parsed log is re-serialized with `json.dumps`
        defaults — whitespace normalized, non-ASCII `\\u`-escaped. Callers
        persisting it must not expect byte-for-byte round trips."""
        out = CS.sanitize_json_string('{"a":  "é"}')
        assert out == '{"a": "\\u00e9"}'
        assert json.loads(out) == {"a": "é"}

    def test_invalid_json_with_a_secret_is_still_redacted(self):
        assert GHP not in CS.sanitize_json_string("{not json " + GHP)


class TestScrubAndUrlUserinfo:

    def test_scrub_secret_empty_and_none(self):
        """S20"""
        assert CS.scrub_secret(None, "x") == ""
        assert CS.scrub_secret("", "x") == ""
        assert CS.scrub_secret("abc", "") == "abc"
        assert CS.scrub_secret("abc", None) == "abc"

    def test_scrub_secret_removes_the_basic_auth_b64_form(self):
        secret = "tok_123"
        b64 = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        out = CS.scrub_secret(f"Authorization: basic {b64} and {secret}", secret)
        assert secret not in out and b64 not in out

    def test_scrub_secret_handles_adjacent_repeats(self):
        assert CS.scrub_secret("aaaa", "aa") == "******"

    @pytest.mark.parametrize("text,expected", [
        ("https://u:p@h/x and git://t@h2/y", "https://***@h/x and git://***@h2/y"),
        ("https://h/x", "https://h/x"),
        ("", ""),
        (None, None),
        ("ssh://git@github.com:22/a", "ssh://***@github.com:22/a"),
        ("mailto:me@example.com", "mailto:me@example.com"),
    ], ids=["S21-two", "S21-none", "S21-empty", "S21-None", "S21-ssh", "S21-mailto"])
    def test_redact_url_userinfo(self, text, expected):
        assert CS.redact_url_userinfo(text) == expected

    def test_redact_url_userinfo_userinfo_with_at_in_path(self):
        """Only the authority's userinfo — an `@` after the first `/` (path)
        must not swallow the host."""
        assert CS.redact_url_userinfo("https://h/p@q") == "https://h/p@q"

    def test_scrub_secret_and_urls_runs_both(self):
        out = CS.scrub_secret_and_urls("https://stale:old@h/x mine=NEW", "NEW")
        assert "old" not in out and "NEW" not in out
