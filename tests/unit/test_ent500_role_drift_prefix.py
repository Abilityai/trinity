"""trinity-enterprise#500 (C5) — `role-drift-` is a platform-reserved id prefix.

Role-drift alerts say "this agent's recorded role file no longer matches what is
on disk". The role file lives in the AGENT'S OWN workspace and is fully
agent-writable, so an unreserved prefix would let the agent pre-create the id of
the alert about its own configuration and have `create_item`'s
`on_conflict_do_nothing` swallow the real one — the #1631 suppression attack,
aimed at the alarm most likely to notice tampering.

Reserved as a NAMED public constant rather than a bare literal, and that is the
deliberate deviation from the house convention. The convention puts the constant
in the emitter's own module (`BASE_IMAGE_STALE_ALERT_PREFIX` lives in
`system_agent_service.py`); here the emitter is in a DIFFERENT REPOSITORY, so a
literal on each side would be two strings with no compiler, no test and no
review connecting them. The registered module imports this name.
"""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")


def test_the_prefix_is_reserved():
    from services.operator_queue_service import (
        ROLE_DRIFT_ALERT_PREFIX,
        _RESERVED_ID_PREFIXES,
    )

    assert ROLE_DRIFT_ALERT_PREFIX == "role-drift-"
    assert ROLE_DRIFT_ALERT_PREFIX in _RESERVED_ID_PREFIXES


def test_an_agent_authored_role_drift_id_is_recognised_as_platform_minted():
    """`is_platform_minted` is the single predicate both agent-facing sinks
    consult, so membership in the tuple IS the refusal."""
    from services.operator_queue_service import (
        ROLE_DRIFT_ALERT_PREFIX,
        is_platform_minted,
    )

    ident = f"{ROLE_DRIFT_ALERT_PREFIX}ops-companion-a1-changed-489000"
    assert is_platform_minted(ident)
    assert is_platform_minted({"request_id": ident})
    # Case/whitespace-folded, so a lookalike cannot slip past.
    assert is_platform_minted(f"  ROLE-Drift-{ident}")
    # An ordinary agent-authored id is unaffected.
    assert not is_platform_minted("please-approve-the-refund")


def test_a_reserved_role_drift_id_is_still_id_shaped():
    """A reserved prefix is worthless if the platform's own id is refused at the
    db sink — the id must satisfy the same validator agent ids do."""
    from services.operator_queue_service import _ID_RE, ROLE_DRIFT_ALERT_PREFIX

    ident = f"{ROLE_DRIFT_ALERT_PREFIX}ops-companion-a1b2c3-changed-489000"
    assert _ID_RE.match(ident)


def test_the_constant_is_exported_for_a_cross_repo_importer():
    """The reason it is a named constant at all: another repository imports it
    by name. A rename here must be a visible break there, not a silent one."""
    import services.operator_queue_service as oqs

    assert hasattr(oqs, "ROLE_DRIFT_ALERT_PREFIX")
    assert not oqs.ROLE_DRIFT_ALERT_PREFIX.startswith("_")
