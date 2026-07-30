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
