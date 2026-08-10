"""Regression guard: the template-registry env knobs must reach the backend in BOTH composes.

Bug (caught by /validate-pr on the ent#14 branch, before merge): `config.py` read
`TEMPLATE_REGISTRY_ENABLED` / `TEMPLATE_REGISTRY_URL` from the environment, and
neither compose file injected them. Prod compose launches **standalone** — no
base-compose merge and no `env_file:` on any service — so the explicit
`environment:` list is the only path into the container, and every deploy path
uses it (`.github/workflows/deploy-dev.yml`, `scripts/deploy/gcp-deploy.sh`).

What made this one worth pinning rather than just fixing: the var is the feature's
**hard kill switch**. `TEMPLATE_REGISTRY_ENABLED=false` is documented as the
air-gap / policy answer that no `system_settings` row can override — so an inert
one is not a missing convenience, it is a feature that ships default-ON with
outbound egress and no way to turn it off on a deployed install. The irony is
load-bearing and belongs in the guard: the config.py comment block *directly
above* the `os.getenv` call argues at length against routing the flag through
`settings_service._resolve_bool_flag` precisely because that helper's opt-in-only
env leg would "silently swallow `TEMPLATE_REGISTRY_ENABLED=false` and ship an
inert kill switch (#1039 class)". The flag then shipped inert anyway, by the other
route. Avoiding a known failure mode in the layer you are looking at does not
avoid it in the layer you are not.

This is the #1039/#1056 packaging-gap class, and it has now recurred **six**
times (#1056 `VOIP_*`, trinity-enterprise#31 `LOG_*`, #1039, #1871 `AGENT_LOG_*`,
#411 `CANARY_*`, and this). Shape mirrors `test_canary_env_prod_parity.py`, which
is the same guard for the fifth.

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`) collects
it — a guard must run where the bug would regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml"]

_REGISTRY_VARS = ["TEMPLATE_REGISTRY_ENABLED", "TEMPLATE_REGISTRY_URL"]


def _injection_lines(compose_file: str, var: str) -> list[str]:
    """Uncommented `- VAR=${VAR:-…}` lines for `var` in `compose_file`."""
    text = (_REPO_ROOT / compose_file).read_text(encoding="utf-8")
    return [
        ln
        for ln in text.splitlines()
        if f"{var}=${{{var}:-" in ln and not ln.strip().startswith("#")
    ]


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
@pytest.mark.parametrize("var", _REGISTRY_VARS)
def test_registry_var_is_injected(compose_file, var):
    """Both registry knobs must be injected in BOTH compose files."""
    assert _injection_lines(compose_file, var), (
        f"{compose_file}: no `{var}=${{{var}:-…}}` injection under "
        f"backend.environment. Without it the .env lever is INERT on any deploy "
        f"using this file (the #1039/#1056 packaging-gap class)."
    )


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
def test_kill_switch_default_is_on_but_overridable(compose_file):
    """The default is `true`, and the point is that `false` must survive.

    Two halves, and the second is the one that matters. Defaulting to `true`
    matches config.py, so wiring the var in changes nothing about default
    behaviour. What wiring it in buys is that an operator's
    `TEMPLATE_REGISTRY_ENABLED=false` is interpolated instead of dropped — the
    `${VAR:-true}` form passes any set value straight through, which is exactly
    why a hardcoded `- TEMPLATE_REGISTRY_ENABLED=true` would pass the presence
    test above while re-breaking the kill switch.
    """
    lines = _injection_lines(compose_file, "TEMPLATE_REGISTRY_ENABLED")
    for ln in lines:
        assert "${TEMPLATE_REGISTRY_ENABLED:-true}" in ln, (
            f"{compose_file}: `TEMPLATE_REGISTRY_ENABLED` must be injected as "
            f"${{TEMPLATE_REGISTRY_ENABLED:-true}} (found: {ln.strip()!r}). A "
            f"hardcoded value would make the documented hard kill switch inert."
        )


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
def test_registry_url_default_is_non_empty(compose_file):
    """A bare `:-` would arrive set-but-empty and shadow the code default.

    The #1076 class: `- TEMPLATE_REGISTRY_URL=${TEMPLATE_REGISTRY_URL:-}` puts an
    empty string in the container env, `os.getenv` returns `""` rather than
    falling back, and the registry is pointed at nothing — a failure mode
    strictly worse than not wiring the var at all, because it looks wired.
    """
    for ln in _injection_lines(compose_file, "TEMPLATE_REGISTRY_URL"):
        value = ln.split(":-", 1)[1].rstrip().rstrip("}")
        assert value.startswith("https://"), (
            f"{compose_file}: `TEMPLATE_REGISTRY_URL` must carry its FULL "
            f"non-empty https default (found default: {value!r}). A bare `:-` "
            f"shadows the config.py default with an empty string (#1076 class)."
        )


def test_compose_default_matches_config_default():
    """The compose default and the code default must be the same URL.

    Two spellings of one default silently disagree the moment either moves, and
    the disagreement is invisible: the container always wins, so the code default
    becomes decorative while still reading as authoritative.
    """
    config_text = (_REPO_ROOT / "src" / "backend" / "config.py").read_text(
        encoding="utf-8"
    )
    # The literal on the line after `TEMPLATE_REGISTRY_URL = os.getenv(`
    assert "TEMPLATE_REGISTRY_URL = os.getenv(" in config_text
    tail = config_text.split("TEMPLATE_REGISTRY_URL = os.getenv(", 1)[1]
    config_default = tail.split('"')[3]

    for compose_file in _COMPOSE_FILES:
        for ln in _injection_lines(compose_file, "TEMPLATE_REGISTRY_URL"):
            compose_default = ln.split(":-", 1)[1].rstrip().rstrip("}")
            assert compose_default == config_default, (
                f"{compose_file} defaults TEMPLATE_REGISTRY_URL to "
                f"{compose_default!r} but config.py defaults to "
                f"{config_default!r} — they must not drift."
            )


def test_both_composes_agree():
    """dev and prod must not drift — the drift IS the bug this file guards."""
    for var in _REGISTRY_VARS:
        per_file = {f: len(_injection_lines(f, var)) for f in _COMPOSE_FILES}
        assert len(set(per_file.values())) == 1 and 0 not in per_file.values(), (
            f"{var} is wired inconsistently across compose files: {per_file}. "
            f"A var present in dev but not prod is inert on every deploy."
        )


@pytest.mark.parametrize("var", _REGISTRY_VARS)
def test_documented_in_env_example(var):
    """A lever nobody knows about is only marginally better than an inert one."""
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert any(
        ln.strip().startswith(f"{var}=") for ln in text.splitlines()
    ), f".env.example must document `{var}` — it is a supported operator lever."


def test_guard_would_catch_a_revert():
    """Meta-test: prove the matcher actually fails on the pre-fix content.

    Without this, a matcher typo would make every assertion above vacuously
    true and the guard would rot into a no-op — which is the failure mode of
    a guard written to close a gap nobody is watching any more.
    """
    var = "TEMPLATE_REGISTRY_ENABLED"
    needle = f"{var}=${{{var}:-"

    def _match(content: str) -> list[str]:
        return [
            ln
            for ln in content.splitlines()
            if needle in ln and not ln.strip().startswith("#")
        ]

    # The literal pre-fix state of both compose files: the var simply absent.
    assert not _match("    environment:\n      - SECRET_KEY=${SECRET_KEY}\n")
    assert _match(f"      - {var}=${{{var}:-true}}\n")
    # A commented-out injection must NOT count as wired.
    assert not _match(f"      # - {var}=${{{var}:-true}}\n")
    # A hardcoded value must NOT count as wired — it is the kill-switch revert.
    assert not _match(f"      - {var}=true\n")
