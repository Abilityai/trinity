"""The community-catalog env vars actually reach the container (ent#237).

`config.py` reads `TRINITY_DEFAULT_SKILL_SOURCE(_REF)` and `.env.example`
documents them, but neither compose file passed them through — so on every
deployed instance the operator's override was inert and only the code default
ever applied. Prod compose launches standalone (no base merge, no `env_file:`
on the backend service), so the explicit `environment:` list is the ONLY route
in. It worked on a laptop, where the shell environment reaches the process
directly, which is exactly why it survived review.

This is the **seventh** recurrence of that class in `docs/memory/learnings.md`
(#1056 `VOIP_*`, ent#31 `LOG_*`, #1039, #1871 `AGENT_LOG_*`, #411 `CANARY_*`,
ent#14 `TEMPLATE_REGISTRY_*`). A new `os.getenv` in the backend is THREE edits,
not one: `docker-compose.yml`, `docker-compose.prod.yml`, `.env.example`.

WHY THE FORM MATTERS MORE THAN THE PRESENCE

The obvious fix — copy the neighbouring
`TRINITY_DEFAULT_SYSTEM_MANIFEST=${VAR:-}` line — is WRONG for this variable,
and would have been worse than the bug it fixes. Empty is the *documented
disable* here ("Set the URL to \"\" to disable the seed entirely"), while the
manifest treats empty as "use the bundled default" (its disable sentinels are
`disabled/none/off/0/false`). Rendered with `docker compose config`:

    form                      unset            set to URL   set to ""
    ------------------------  ---------------  -----------  -----------------
    - VAR            (bare)   null -> default  the URL      "" -> disabled
    - VAR=${VAR:-}            "" -> DISABLED   the URL      ""
    - VAR=${VAR:-default}     default          the URL      default (!)

`${VAR:-}` would silently turn the community catalog off on every install; the
explicit-default form would make the documented disable impossible. Only the
bare pass-through is correct, so these tests assert the FORM, not the presence.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
VARS = ("TRINITY_DEFAULT_SKILL_SOURCE", "TRINITY_DEFAULT_SKILL_SOURCE_REF")


def _backend_env_lines(compose: str) -> list[str]:
    """The backend service's `environment:` entries."""
    text = (_REPO / compose).read_text()
    m = re.search(r"^  backend:$(.*?)(?=^  \w)", text, re.M | re.S)
    assert m, f"no backend service found in {compose}"
    env = re.search(r"^    environment:$(.*?)(?=^    \w)", m.group(1), re.M | re.S)
    assert env, f"backend has no environment: block in {compose}"
    return [ln.strip() for ln in env.group(1).splitlines() if ln.strip().startswith("- ")]


@pytest.mark.parametrize("compose", COMPOSE_FILES)
@pytest.mark.parametrize("var", VARS)
def test_the_var_is_passed_to_the_backend(compose, var):
    """Both files. The prod one is load-bearing and the easiest to forget,
    because dev works without it."""
    lines = _backend_env_lines(compose)
    assert any(ln == f"- {var}" or ln.startswith(f"- {var}=") for ln in lines), (
        f"{var} is not in {compose}'s backend environment — the operator's "
        "override is inert on every deployed instance"
    )


@pytest.mark.parametrize("compose", COMPOSE_FILES)
@pytest.mark.parametrize("var", VARS)
def test_the_form_is_bare_pass_through(compose, var):
    """`- VAR`, never `- VAR=${VAR:-...}`.

    This is the assertion that matters. A presence-only check passes for
    `- VAR=${VAR:-}`, which sends "" on every install and disables the very
    catalog this variable configures.
    """
    lines = _backend_env_lines(compose)
    entry = next(ln for ln in lines if ln == f"- {var}" or ln.startswith(f"- {var}="))
    assert entry == f"- {var}", (
        f"{compose} uses `{entry}`. Empty is the documented DISABLE for this "
        f"setting, so an interpolated default sends a value on every install: "
        f'`${{{var}:-}}` disables the catalog everywhere, and '
        f"`${{{var}:-<default>}}` swallows an operator's explicit disable. Use "
        f"the bare pass-through `- {var}`."
    )


def test_the_code_default_is_not_duplicated_in_compose():
    """One source of truth for the default.

    `config.py` owns it. Restating the URL in two compose files is how the two
    drift, and the bare pass-through means compose never needs to know it.
    """
    from_config = (_REPO / "src" / "backend" / "config.py").read_text()
    m = re.search(r'"TRINITY_DEFAULT_SKILL_SOURCE",\s*"([^"]+)"', from_config)
    assert m, "config.py no longer declares the default — update this guard"
    default = m.group(1)

    for compose in COMPOSE_FILES:
        text = (_REPO / compose).read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        assert default not in code, (
            f"{compose} hardcodes the catalog default ({default}); config.py owns "
            "it and the bare pass-through form makes the copy unnecessary"
        )


@pytest.mark.parametrize("var", VARS)
def test_the_var_is_documented_for_operators(var):
    """Third edit of the three. A var that is wired but undocumented is a
    capability nobody knows exists."""
    assert var in (_REPO / ".env.example").read_text(), (
        f"{var} is not in .env.example"
    )


def test_config_still_reads_both_vars():
    """Guards the other direction: a rename in config.py that leaves compose
    passing the old names would be just as inert."""
    src = (_REPO / "src" / "backend" / "config.py").read_text()
    for var in VARS:
        assert f'"{var}"' in src, f"config.py no longer reads {var}"
