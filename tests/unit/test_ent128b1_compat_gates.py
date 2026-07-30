"""ent#128 PR-B — K-001 / K-002 read narrower than they audit.

Two HARD compatibility gates were two expressions of one root cause: a reader
narrower than the mechanism it audits.

  * **K-001** compared `.mcp.json.template`'s `${VAR}` references against an
    UPPERCASE-ONLY view of `.env.example`. `${my_var}` is substituted at runtime
    (the engines are a `str.replace` and an `env_val[2:-1]` slice — no charset at
    all), so a correctly documented lowercase variable was invisible and the gate
    HARD-failed a correct template.
  * **K-002 / T-015** compared those same references against
    `set(credentials.keys())` — the *section* names (`mcp_servers`, `env_file`),
    not the variables. So the structured, documented form
    (`credentials.mcp_servers.<s>.env_vars`) satisfied nothing and HARD-failed,
    while `${env_file}` and `${mcp_servers}` PASSED.

And the fix must not introduce a third expression: a reader that can *stop
reading*. `run_static` catches `Exception` → `skipped`, and `_counts` counts only
`status == "fail"`, so a raise inside a HARD check drops `hard_count` and can flip
`overall_status` from `issues` to `compatible`. That is a HARD gate silently
ceasing to protect, and it is indistinguishable from a clean pass in the counts.

Transition set (the complete one — the blanket "strictly monotone" claim is
false):

  | transition                            | cause                              |
  |---------------------------------------|------------------------------------|
  | `fail → pass` K-001/K-002/T-015       | the intended fix                   |
  | `pass → fail` K-002 on `${env_file}`  | deliberate — a closed false negative |
  | `pass → fail` K-003 (SOFT)            | collateral, correct, release-noted |
  | `pass → pass` S-010                   | verified: blocklist is uppercase-exact |
  | `fail/pass → skipped`                 | MUST NOT HAPPEN — that is the bug  |

Target: src/backend/services/compatibility/static_checks.py,
        src/backend/services/compatibility/__init__.py,
        src/backend/services/credential_charset.py
Issue:  Abilityai/trinity-enterprise#128
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent128b1.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Snapshot builder — the collector's shape, minimal
# ---------------------------------------------------------------------------


def _snap(**files) -> dict:
    """Build a collector-shaped snapshot from `path=content` kwargs.

    `None` means "file absent". Keys use `_` for `.` and `/` so they can be
    kwargs: `mcp_template`, `env_example`, `template_yaml`.
    """
    name_map = {
        "mcp_template": ".mcp.json.template",
        "env_example": ".env.example",
        "template_yaml": "template.yaml",
    }
    out: dict = {"files": {}, "dirs": {}, "skills": {}}
    for key, content in files.items():
        path = name_map[key]
        if content is None:
            out["files"][path] = {"exists": False}
        else:
            out["files"][path] = {
                "exists": True,
                "content": content,
                "size": len(content),
                "binary": False,
                "truncated": False,
            }
    return out


def _mcp_template(**servers) -> str:
    """`.mcp.json.template` referencing `${VAR}` per server."""
    return json.dumps(
        {
            "mcpServers": {
                name: {"command": "npx", "env": {"TOKEN": f"${{{var}}}"}}
                for name, var in servers.items()
            }
        }
    )


# ===========================================================================
# Test 10 — charset agreement AND non-membership
# ===========================================================================
#
# The four-way agreement test cannot notice a fifth pattern being absorbed into
# the constant — by construction it only asserts the derived four agree. The
# non-membership assertions are the ones that catch the dangerous refactor.

_CHARSET_CORPUS = [
    ("FOO", True),
    ("FOO_BAR_1", True),
    ("_foo", True),
    ("my_var", True),
    ("A1", True),
    ("1BAD", False),
    ("my-key", False),
    ("", False),
    ("FOO BAR", False),
    ("FOO\n", False),
]


@pytest.mark.parametrize("name,valid", _CHARSET_CORPUS)
def test_all_four_detectors_agree_on_valid_names(name, valid):
    """The four members of the detector charset accept exactly the same names."""
    from services import template_service as ts
    from services.compatibility import static_checks as sc
    from services.credential_charset import is_credential_var_name

    assert is_credential_var_name(name) is valid

    # Member 1+3: the two `${VAR}` finders.
    blob = f"${{{name}}}"
    assert bool(sc._VAR_RE.findall(blob)) is valid
    assert bool(ts.CREDENTIAL_DETECTOR_REF_RE.findall(blob)) is valid

    # Member 2: `.env.example` name validator inside the compatibility check.
    assert bool(sc._env_example_vars(_snap(env_example=f"{name}=x\n"))) is valid

    # Member 4: the extractor feeding the live deploy-time warning.
    if valid:
        assert ts.CREDENTIAL_DETECTOR_NAME_RE.match(name)
    else:
        assert not ts.CREDENTIAL_DETECTOR_NAME_RE.match(name)


def test_mcp_validator_gate_stays_narrow():
    """NON-MEMBER: the fail-closed security gate must NOT adopt the charset.

    `mcp_validator._ENV_VAR_REF_RE` is paired with a deliberately widest finder
    (`[^}]*`) so nothing escapes detection and everything detected must pass a
    narrow allowlist. Widening it does not fix a false positive — it ADMITS input
    that is currently rejected. This is the assertion that catches the
    "align all the regexes" refactor.
    """
    from services import mcp_validator

    assert mcp_validator._ENV_VAR_REF_RE.match("MY_VAR")
    assert not mcp_validator._ENV_VAR_REF_RE.match(
        "my_var"
    ), "the .mcp.json injection gate was widened — it must stay uppercase-only"
    assert not mcp_validator._ENV_VAR_REF_RE.match("_foo")
    # And its finder must stay the widest one, or something escapes detection.
    assert mcp_validator._ENV_VAR_SUBSTRING_RE.findall("${my-key}") == ["my-key"]


def test_assign_re_and_skill_packaging_untouched():
    """NON-MEMBERS: a secret scanner and an adjacent domain's contract."""
    from services.compatibility import static_checks as sc
    from services import skill_packaging

    # `_ASSIGN_RE`'s quantifier shape is behind an already-FIXED
    # py/polynomial-redos alert. The value must stay greedy-to-end-of-line.
    assert r"(.*)$" in sc._ASSIGN_RE.pattern
    assert sc._ASSIGN_RE.pattern.count("[ \\t]*(.+?)") == 0

    # The ent#183 skill contract keeps its own length cap.
    assert skill_packaging.ENV_KEY_RE.pattern == r"^[A-Z][A-Z0-9_]{0,63}$"
    assert not skill_packaging.ENV_KEY_RE.match("my_var")


# ===========================================================================
# Test 5 — K-001 (HARD): a documented lowercase variable is no longer invisible
# ===========================================================================

def _run(check_id: str, snap: dict):
    from services.compatibility.static_checks import run_static

    return run_static(snap, [check_id])[check_id]


def test_k001_accepts_a_documented_lowercase_variable():
    """`${my_var}` + `my_var=` is a COMPLETE template. Must fail on baseline."""
    status, _msg, detail = _run(
        "K-001",
        _snap(
            mcp_template=_mcp_template(vault="my_var"),
            env_example="# the vault path\nmy_var=/path/to/vault\n",
        ),
    )
    assert status == "pass", f"K-001 still HARD-fails a documented var: {detail}"


def test_k001_still_fails_a_genuinely_undocumented_variable():
    """The gate must keep working — widening `provided` is not disarming it."""
    status, _msg, detail = _run(
        "K-001",
        _snap(
            mcp_template=_mcp_template(stripe="STRIPE_SECRET_KEY"),
            env_example="# nothing useful\nOTHER_KEY=x\n",
        ),
    )
    assert status == "fail"
    assert detail["missing"] == ["STRIPE_SECRET_KEY"]


def test_k001_still_exempts_platform_injected_variables():
    status, _msg, _detail = _run(
        "K-001",
        _snap(
            mcp_template=_mcp_template(google="GEMINI_API_KEY"),
            env_example="# platform-injected, operator supplies nothing\nOTHER=x\n",
        ),
    )
    assert status == "pass"


# ===========================================================================
# Test 6 — K-003 (SOFT) collateral: a `pass → fail` we accept and release-note
# ===========================================================================

def test_k003_now_fails_a_lowercase_only_comment_free_env_example():
    """Intended collateral of widening `_env_example_vars`.

    `_env_example_vars` is K-003's precondition for *demanding* comments, so
    growing it makes the verdict worse for a file that genuinely has no comments.
    The verdict is CORRECT — an undocumented `.env.example` is undocumented
    whatever case its variables use — but it is a `pass → fail` and must not be
    invisible. SOFT, so `hard_count` is unaffected.
    """
    status, _msg, _detail = _run("K-003", _snap(env_example="my_var=\nother_var=\n"))
    assert status == "fail"

    from services.compatibility import spec

    assert spec.effective_severity(next(c for c in spec.CHECKS if c.id == "K-003")) == "soft"


def test_k003_passes_once_the_lowercase_vars_are_commented():
    status, _msg, _detail = _run(
        "K-003", _snap(env_example="# what these are for\nmy_var=\nother_var=\n")
    )
    assert status == "pass"


# ===========================================================================
# Test 7 — S-010 (SOFT) does NOT flip, and why
# ===========================================================================

def test_s010_does_not_flip_for_lowercase_generic_names():
    """Safe by COINCIDENCE OF CASING, so it is asserted rather than assumed.

    `c_s010`'s `generic` blocklist is uppercase-exact (`API_KEY`, `TOKEN`, ...),
    and every member already passed the old narrow filter, so no newly-visible
    lowercase name can join it. Add a lowercase member to that blocklist and this
    test starts failing — which is the point.
    """
    status, _msg, _detail = _run(
        "S-010",
        _snap(mcp_template=_mcp_template(x="api_key"), env_example="api_key=\n"),
    )
    assert status == "pass"

    from services.compatibility.static_checks import c_s010
    import inspect

    blocklist_src = inspect.getsource(c_s010)
    assert "generic = {" in blocklist_src
    # Every blocklist member is uppercase — the property this test rests on.
    generic = {"API_KEY", "SECRET", "TOKEN", "PASSWORD", "KEY", "KEY1", "KEY2", "APIKEY"}
    for member in generic:
        assert f'"{member}"' in blocklist_src
        assert member == member.upper()


def test_s010_still_flags_an_uppercase_generic_name():
    status, _msg, detail = _run(
        "S-010",
        _snap(mcp_template=_mcp_template(x="API_KEY"), env_example="API_KEY=\n"),
    )
    assert status == "fail"
    assert detail["names"] == ["API_KEY"]


# ===========================================================================
# Tests 1-4 — K-002 / T-015 (HARD): read the DECLARATION, not the section names
# ===========================================================================
#
# STRIPE_API_KEY, not GEMINI_API_KEY: the latter is in
# `template_service._PLATFORM_INJECTED_EXACT`, so a fixture using it passes
# VACUOUSLY and proves nothing about the declaration being read.

_TEMPLATE_HEAD = "name: fixture\nresources:\n  cpu: '2'\n  memory: '4g'\n"


def test_k002_accepts_the_structured_mcp_servers_declaration():
    """`credentials.mcp_servers.<s>.env_vars` is THE documented form.

    It declares variables one level deeper than `set(creds.keys())` ever looked,
    so the structured form satisfied nothing and this HARD gate failed a correctly
    declared template. Must fail on baseline.
    """
    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_API_KEY"),
        template_yaml=_TEMPLATE_HEAD
        + "credentials:\n  mcp_servers:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n",
    )
    for check_id in ("T-015", "K-002"):
        status, _msg, detail = _run(check_id, snap)
        assert status == "pass", f"{check_id} still fails a declared var: {detail}"


def test_k002_accepts_the_env_file_declaration():
    """The other declaration site. Must fail on baseline."""
    snap = _snap(
        mcp_template=_mcp_template(vault="VAULT_BASE_PATH"),
        template_yaml=_TEMPLATE_HEAD + "credentials:\n  env_file: [VAULT_BASE_PATH]\n",
    )
    for check_id in ("T-015", "K-002"):
        status, _msg, detail = _run(check_id, snap)
        assert status == "pass", f"{check_id} still fails a declared var: {detail}"


def test_k002_still_fails_a_genuinely_undeclared_variable():
    """Widening `listed` must not disarm the gate."""
    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_API_KEY", other="HEYGEN_API_KEY"),
        template_yaml=_TEMPLATE_HEAD
        + "credentials:\n  mcp_servers:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n",
    )
    status, _msg, detail = _run("T-015", snap)
    assert status == "fail"
    assert detail["missing"] == ["HEYGEN_API_KEY"]


def test_k002_keeps_tolerating_the_flat_legacy_mapping():
    """`credentials: {STRIPE_API_KEY: '...'}` is legitimate and must keep passing.

    This is what the section-name subtraction must NOT break: only the three known
    STRUCTURE keys are removed, so a flat variable-name mapping survives.
    """
    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_API_KEY"),
        template_yaml=_TEMPLATE_HEAD + "credentials:\n  STRIPE_API_KEY: 'from the vault'\n",
    )
    status, _msg, detail = _run("T-015", snap)
    assert status == "pass", detail


@pytest.mark.parametrize("section", ["env_file", "mcp_servers", "config_files"])
def test_k002_no_longer_passes_a_reference_to_a_section_name(section):
    """A deliberate `pass → fail`: a closed false negative, release-noted.

    `listed` was `set(creds.keys())` — "whichever section names this template
    happens to use" — so `${env_file}` PASSED. A blind spot that depends on the
    template's own contents cannot be found by reading the check. This test
    PASSES on baseline (that is what makes it the `pass → fail` evidence).
    """
    snap = _snap(
        mcp_template=_mcp_template(broken=section),
        template_yaml=_TEMPLATE_HEAD
        + f"credentials:\n  {section}:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n",
    )
    status, _msg, detail = _run("T-015", snap)
    assert status == "fail", f"${{{section}}} is a section name, not a credential"
    assert detail["missing"] == [section]


# ===========================================================================
# Tests 8-9 — the gate must FAIL CLOSED, never go dark
# ===========================================================================
#
# W1: `run_static` caught `Exception` → `skipped`, and `_counts` counted only
# `status == "fail"`, so a raise inside a HARD check DROPPED `hard_count` and
# could flip `overall_status` from `issues` to `compatible`. `c_k002` delegates to
# `c_t015`, so ONE raise took both HARD gates dark together — indistinguishable
# from a clean pass. Four lines of untrusted YAML were the whole trigger.

_HOSTILE_DECLARATIONS = [
    pytest.param("  mcp_servers:\n    s:\n      env_vars:\n        - {K: v}\n", id="element-mapping"),
    pytest.param("  mcp_servers:\n    s:\n      env_vars:\n        - [a, b]\n", id="element-sequence"),
    pytest.param("  mcp_servers:\n    s:\n      env_vars:\n        - null\n", id="element-null"),
    pytest.param("  mcp_servers:\n    s:\n      env_vars:\n        - 7\n", id="element-int"),
    pytest.param("  mcp_servers:\n    s:\n      env_vars: nope\n", id="env_vars-string"),
    pytest.param("  mcp_servers:\n    s: nope\n", id="server-string"),
    pytest.param("  mcp_servers: nope\n", id="mcp_servers-string"),
    pytest.param("  env_file: {a: b}\n", id="env_file-mapping"),
    pytest.param("  env_file:\n    - {K: v}\n", id="env_file-element-mapping"),
]


@pytest.mark.parametrize("declaration", _HOSTILE_DECLARATIONS)
def test_hostile_declaration_still_hard_fails_and_never_skips(declaration):
    """The undeclared credential must still be reported. Must fail on baseline."""
    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_SECRET_KEY"),
        template_yaml=_TEMPLATE_HEAD + "credentials:\n" + declaration,
    )
    for check_id in ("T-015", "K-002"):
        status, _msg, detail = _run(check_id, snap)
        assert status == "fail", f"{check_id} went dark on a hostile declaration"
        assert detail.get("skip_reason") != "check_error"
        assert detail["missing"] == ["STRIPE_SECRET_KEY"]


def test_a_raising_check_is_a_failure_not_a_skip():
    """`run_static`'s own handler: a check that cannot evaluate did not pass."""
    from services.compatibility import static_checks as sc

    def boom(_snap):
        raise TypeError("unhashable type: 'dict'")

    original = sc.STATIC_CHECKS["T-015"]
    try:
        sc.STATIC_CHECKS["T-015"] = boom
        status, msg, detail = sc.run_static({}, ["T-015"])["T-015"]
    finally:
        sc.STATIC_CHECKS["T-015"] = original

    assert status == "fail", "a raising check still downgrades to skipped"
    assert "could not be evaluated" in msg
    assert detail["check_error"]


def test_check_error_cannot_erase_a_hard_finding():
    """The `_counts` belt: a `skipped`+`check_error` row still counts as hard.

    Second layer at the sink (#1525). `run_static` no longer produces this shape,
    but if any future path reintroduces it the count must still hold — that is the
    property whose absence let 4 lines of YAML take `hard_count` 1 → 0.
    """
    from services.compatibility import _counts

    crashed = [
        {"status": "skipped", "severity": "hard", "skip_reason": "check_error"},
        {"status": "skipped", "severity": "soft", "skip_reason": "check_error"},
    ]
    assert _counts(crashed) == {"hard_count": 1, "soft_count": 1, "info_count": 0}

    # A legitimate precondition skip is NOT a finding — that distinction is the
    # reason `run_static` has a skip path at all.
    benign = [
        {"status": "skipped", "severity": "hard", "skip_reason": "no_template"},
        {"status": "skipped", "severity": "soft", "skip_reason": "ai_not_run"},
    ]
    assert _counts(benign) == {"hard_count": 0, "soft_count": 0, "info_count": 0}


def test_overall_status_cannot_flip_to_compatible_on_a_hostile_declaration():
    """The end-to-end property, asserted on counts rather than a single verdict.

    `hard_count` alone cannot distinguish `fail → pass` from `fail → skipped`,
    which is exactly how this hid. Assert both the verdict AND the count.
    """
    from services.compatibility import _counts, _check_dict, spec

    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_SECRET_KEY"),
        template_yaml=_TEMPLATE_HEAD
        + "credentials:\n  mcp_servers:\n    s:\n      env_vars:\n        - {K: v}\n",
    )
    from services.compatibility.static_checks import run_static

    ids = ["T-015", "K-002"]
    results = run_static(snap, ids)
    checks = [
        _check_dict(next(c for c in spec.CHECKS if c.id == cid), *results[cid])
        for cid in ids
    ]
    counts = _counts(checks)

    assert counts["hard_count"] >= 1, "a HARD gate went dark"
    overall = "issues" if (counts["hard_count"] + counts["soft_count"]) > 0 else "compatible"
    assert overall == "issues"


def test_the_new_union_term_fails_closed_when_the_reader_raises(monkeypatch):
    """The `try/except` around the union degrades to the NARROW verdict.

    `test_hostile_declaration_still_hard_fails_and_never_skips` above cannot prove
    this on its own: `declared_credential_names` filters those shapes structurally,
    so nothing raises and the assertion holds for the wrong reason. This forces the
    raise directly.

    Degrading must make `missing` LARGER, never smaller — shrinking `listed` errs
    toward failing, which is the definition of fail-closed here.
    """
    from services.compatibility import static_checks as sc

    def boom(_block):
        raise TypeError("unhashable type: 'dict'")

    monkeypatch.setattr(sc, "declared_credential_names", boom)

    snap = _snap(
        mcp_template=_mcp_template(stripe="STRIPE_API_KEY"),
        template_yaml=_TEMPLATE_HEAD
        + "credentials:\n  mcp_servers:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n",
    )
    status, _msg, detail = _run("T-015", snap)

    # Not `skipped`, not `pass` — the pre-fix verdict, loudly.
    assert status == "fail"
    assert detail["missing"] == ["STRIPE_API_KEY"]
    assert "check_error" not in detail
