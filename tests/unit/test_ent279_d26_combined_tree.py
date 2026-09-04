"""ent#279 D26 — combined-tree composition guard.

The credential vault is split across two repos: the enterprise module owns
delivery (``_stage_or_fail_closed``) and lazy-imports ``stage_secret`` from the
OSS ``services.runtime_secret_scrub`` seam at call time; the OSS persistence
chokepoints later read ``get_staged_values()`` from that same module. The whole
scrub mitigation therefore rests on one unstated assumption: **both sides bind
the SAME module object**. A vendored copy, a second import path, or a packaging
change that gives the enterprise tree its own ``services`` package would keep
every per-repo test green while the staged set the chokepoints read stays
forever empty — plaintext persists, nothing red.

This test runs only in the combined tree and proves the composition by
interception: patching the OSS seam module must change what the ENTERPRISE
function calls. It skips (never fails) when the submodule is absent — an
OSS-only clone has no vault to compose with, and the enterprise repo's own
suite covers ``_stage_or_fail_closed`` in isolation.
"""

import pytest

svc = pytest.importorskip(
    "enterprise.backend.credential_vault.service",
    reason="enterprise submodule not mounted (OSS-only clone)",
)

import services.runtime_secret_scrub as seam


def test_d26_enterprise_lazy_import_binds_the_oss_seam(monkeypatch):
    """Patching the OSS module intercepts the enterprise call — same object."""
    staged = []
    monkeypatch.setattr(
        seam, "stage_secret", lambda agent, value: staged.append((agent, value))
    )
    svc._stage_or_fail_closed("agent-x", "v-3ry-s3cret")
    assert staged == [("agent-x", "v-3ry-s3cret")]


def test_d26_stage_failure_fails_closed_through_the_seam(monkeypatch):
    """A seam-side failure surfaces as the 503 refusal, not a delivery (D16)."""

    def boom(agent, value):
        raise RuntimeError("redis down")

    monkeypatch.setattr(seam, "stage_secret", boom)
    with pytest.raises(svc.VaultError) as exc_info:
        svc._stage_or_fail_closed("agent-x", "v")
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "vault_staging_unavailable"
