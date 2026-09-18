"""The `WORKSPACE_ENABLED` / `workspace_available` flag is retired.

ent#438 merged the per-agent workspace page into the Workspace and left this
flag behind. It was still computed by `GET /api/settings/feature-flags`, still
shipped `false` in `.env.example` and all three compose files, and was still
described as *the* Workspace opt-in by `settings.py`'s own docstring and five
`docs/memory/` files — while **nothing in `src/` gated on it**.

That combination is worse than a dead key: an operator following the docs sets
`WORKSPACE_ENABLED=true` and changes nothing, or reads the default `false` and
concludes the Workspace ships dark when it does not. The `/release-plan` 0.9.5
coherence review flagged it as the one *forgotten* item in the payload — not a
forgotten flip, a forgotten removal.

These are the pins that keep it removed. They replace the two assertions in
`tests/test_platform_default_model.py` that required the key to be PRESENT
(#860), which is the contract ent#438 invalidated.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# Every file that shipped the flag to an operator. A re-add in any of them puts
# the misleading knob back in front of the person most likely to believe it.
_CONFIG_FILES = (
    ".env.example",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.hosted.yml",
)


def test_feature_flags_does_not_carry_workspace_available(monkeypatch):
    """The handler must not re-introduce the key.

    Built on the `test_2217_canary_status.py` stub shape so this stays a pure
    handler test (no DB). The settings stub deliberately does NOT define
    `is_workspace_enabled`, but do not rely on that alone to catch a revert:
    the old expression was `voice_available and is_workspace_enabled()`, and
    `voice_available` is False without a Gemini key, so `and` short-circuits
    and the method is never reached. The key check below is what actually
    bites; `test_settings_service_has_no_workspace_resolver` covers the
    resolver itself.
    """
    import asyncio
    from types import SimpleNamespace

    # #1028: `get_public_feature_flags` lives in the package's `flags` module.
    from routers.settings import flags as settings_module

    stub_settings = SimpleNamespace(
        is_brain_orb_enabled=lambda: False,
        is_session_tab_enabled=lambda: False,
        is_brain_orb_voice_enabled=lambda: False,
        is_brain_orb_write_enabled=lambda: False,
        get_elevenlabs_api_key=lambda: None,
        get_platform_default_model=lambda: "model",
        get_anthropic_api_key=lambda: None,
        get_install_source=lambda: "unknown",
        is_marketplace_install=lambda: False,
        is_hardening_guide_eligible=lambda: False,
        get_install_tls_posture=lambda: "unconfigured",
        is_public_url_reached=lambda: False,
    )
    monkeypatch.setattr(settings_module, "settings_service", stub_settings)
    monkeypatch.setattr(
        settings_module,
        "telemetry_sharing_service",
        SimpleNamespace(
            is_consent_enabled=lambda: False,
            public_flags=lambda: {"telemetry_sharing_enabled": False},
        ),
    )
    monkeypatch.setattr(settings_module.db, "has_any_subscription", lambda: False)

    import services.a2a_outbound_service as a2a_module
    from services.entitlement_service import entitlement_service

    monkeypatch.setattr(a2a_module, "is_outbound_enabled", lambda: False)
    monkeypatch.setattr(entitlement_service, "list_entitled_features", lambda: [])

    flags = asyncio.run(settings_module.get_public_feature_flags(current_user=None))

    assert "workspace_available" not in flags, (
        "`workspace_available` is back in the feature-flags payload. ent#438 "
        "retired the surface it named; nothing in src/ gates on it, so a "
        "present key is a claim the product does not honour."
    )
    # The sibling keys must survive — this pin is about one retired flag, not
    # about thinning the flags document.
    for still_expected in ("session_tab_enabled", "voice_available", "voip_available"):
        assert still_expected in flags, f"{still_expected} was dropped by mistake"


def test_settings_service_has_no_workspace_resolver():
    """`is_workspace_enabled()` is gone, so no caller can resurrect the gate."""
    from services.settings_service import settings_service

    assert not hasattr(settings_service, "is_workspace_enabled"), (
        "settings_service.is_workspace_enabled() is back. It resolved a "
        "system_settings row / WORKSPACE_ENABLED env var for a surface that no "
        "longer exists (ent#438)."
    )


@pytest.mark.parametrize("relpath", _CONFIG_FILES)
def test_shipped_config_does_not_declare_workspace_enabled(relpath):
    """No operator-facing config file re-declares the variable."""
    path = _ROOT / relpath
    assert path.exists(), f"{relpath} is missing — update this guard's file list"

    offenders = [
        f"{relpath}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        # Match the variable, not prose mentioning it, and never
        # WORKSPACE_VOICE_MAX_DURATION — a live, unrelated setting.
        if re.search(r"(?<![A-Z_])WORKSPACE_ENABLED(?![A-Z_])", line)
    ]
    assert not offenders, (
        "WORKSPACE_ENABLED is declared again in shipped config:\n  "
        + "\n  ".join(offenders)
        + "\nIt gates nothing (ent#438). Setting it changes no behaviour."
    )


def _strip_comments(text: str, suffix: str) -> str:
    """Blank out comment regions, preserving line count and numbering.

    Line-based detection is not enough: the reference that motivated this
    helper sits on a CONTINUATION line of a multi-line `<!-- -->` block in
    `AgentHeader.vue`, which starts with a backtick, not a comment marker.
    Comment bodies are replaced by spaces (newlines kept) so reported line
    numbers still point at the real file.
    """
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    if suffix == ".py":
        return re.sub(r"#[^\n]*", blank, text)
    # .js / .ts / .vue — block, line and HTML comments.
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", blank, text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", blank, text)


def test_no_backend_or_frontend_source_reads_the_flag():
    """Nothing in the shipped source reads the env var or the flag key.

    Comments are allowed to mention the retirement — that is how a future
    reader learns why the knob went away — so comment regions are stripped
    before matching.
    """
    roots = (_ROOT / "src" / "backend", _ROOT / "src" / "frontend" / "src")
    pattern = re.compile(
        r"(?<![A-Z_])WORKSPACE_ENABLED(?![A-Z_])|workspace_available|workspaceAvailable"
    )

    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".js", ".vue", ".ts"} or not path.is_file():
                continue
            # The enterprise submodule is a separate repo with its own guards.
            if "enterprise" in path.parts:
                continue
            body = _strip_comments(path.read_text(errors="replace"), path.suffix)
            for n, line in enumerate(body.splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(_ROOT)}:{n}: {line.strip()[:100]}")

    assert not offenders, (
        "Live code references the retired workspace flag:\n  " + "\n  ".join(offenders)
    )
