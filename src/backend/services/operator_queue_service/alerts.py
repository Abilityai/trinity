"""The bounded-alert delegates (#3785, S6): `ALERT_*`, `create_bounded_alert(_outcome)`.

Since #3254 these are a thin delegate of `platform_alerts.observe`: the seam
composes the #1677 per-(agent, registered-type) budget inside its locked
create, and every in-repo budgeted emitter reports through it directly. The
helper keeps its signature and its outcome contract for any caller still on
it. The package re-exports these names for the callers outside. Only
`create_bounded_alert` is read solely from outside, so it alone is patched on
the package; the others are read inside this module too (the bool view reads
the outcome, the outcome reads the `ALERT_*` constants), so no single patch
target reaches every reader and the #3785 guard refuses a patch of them.

The registered types and the budget (`_BUDGETED_ALERT_TYPES`,
`OPERATOR_ALERT_MAX_PENDING_PER_TYPE`) are package-owned config (O1) and are
read as `_oq.X` at call time, so a patch on `oqs.X` reaches this reader too.
The #1677 budget EPISODE and its cooldown stay package-owned in `__init__`:
`platform_alerts.observe` reaches the episode through the package, and a test
patches it there.
"""

import logging

from services import operator_queue_service as _oq

logger = logging.getLogger("services.operator_queue_service")


# `create_bounded_alert_outcome` results. Only `ALERT_CREATED` means a row was
# written; the rest separate a deliberate REFUSAL (unregistered type, budget
# full) from a FAILURE (count read or create raised) for a caller that must
# treat them differently (#3130 review I1: the flood emitter retries a failure
# but not a refusal).
ALERT_CREATED = "created"
ALERT_REFUSED_UNREGISTERED = "refused_unregistered"
ALERT_REFUSED_AT_BUDGET = "refused_at_budget"
ALERT_FAILED_COUNT = "failed_count"
ALERT_FAILED_CREATE = "failed_create"


async def create_bounded_alert(agent_name: str, item: dict) -> bool:
    """#1677: the bool view of `create_bounded_alert_outcome` (see there)."""
    return await create_bounded_alert_outcome(agent_name, item) == ALERT_CREATED


async def create_bounded_alert_outcome(agent_name: str, item: dict) -> str:
    """#1677: the create seam for agent-INFLUENCEABLE platform alert emitters,
    since #3254 a thin delegate of ``platform_alerts.observe``.

    Every in-repo budgeted emitter now reports through ``observe`` itself, which
    composes the same per-(agent, registered-type) pending budget inside its
    locked create (``OPERATOR_ALERT_MAX_PENDING_PER_TYPE``) and spawns the budget
    episode on a refusal. This helper keeps its signature and its outcome
    contract for any caller still on it; the #3246 ratchet counts each such
    caller as off the seam, which steers new code to ``observe``.

    The budget type is derived from ``item["type"]`` (never a separate
    parameter: two sources of one fact silently void the bound) and MUST be a
    static literal registered in ``_BUDGETED_ALERT_TYPES``. That type names its
    platform-alert kind (the kind whose ``budgeted_type`` it is), and the key is
    read back from ``item["id"]`` + ``item["context"]`` by
    ``platform_alerts.derive_legacy_subject``, which must resolve to that same
    kind. The two kinds whose ids carry no derivable subject (the gitignore
    sweep's alert and the Workspace problem report) are keyed by the request id
    minus the kind's prefix: for the problem report that is the per-person /
    target / day digest its own emitter keys by; for gitignore it is the old
    per-call id, so a call still files one row.

    Outcomes (never raises; every refused or failed arm logs ``[#1677 budget]``):
      * type not registered          → ``ALERT_REFUSED_UNREGISTERED`` (fail-closed,
        nothing written: register the type, a one-line reviewed act);
      * the id does not read back as the type's kind, or carries no key
                                     → ``ALERT_FAILED_CREATE`` (fail-closed);
      * else ``observe``'s outcome: ``ALERT_CREATED``; ``"updated"`` (the
        subject's pending row was rewritten in place); ``"snoozed"`` (a person
        ended the subject inside the snooze window, nothing written);
        ``ALERT_REFUSED_AT_BUDGET`` (the budget was full; the episode alert is
        spawned by ``observe``); ``ALERT_FAILED_CREATE`` for its ``"failed"``.

    ``ALERT_FAILED_COUNT`` stays importable and is no longer returned: the
    per-type count now runs inside the seam's locked create, where a failed
    count and a failed create are one exception path. ``create_bounded_alert``
    is the bool view and is true for ``ALERT_CREATED`` only, so a paired side
    effect (a notification) never fires twice for one pending row.
    """
    from services import platform_alerts

    item_type = item.get("type") if isinstance(item, dict) else None
    if item_type not in _oq._BUDGETED_ALERT_TYPES:
        logger.error(
            "[#1677 budget] refusing operator alert for '%s': type %r is not a "
            "registered budgeted type (%s) — add it to _BUDGETED_ALERT_TYPES "
            "before routing an emitter through create_bounded_alert "
            "(fail-closed; no item created)",
            agent_name, item_type, sorted(_oq._BUDGETED_ALERT_TYPES),
        )
        return ALERT_REFUSED_UNREGISTERED

    kind = next(
        (k for k in platform_alerts.KINDS.values() if k.budgeted_type == item_type), None
    )
    match = platform_alerts.derive_legacy_subject(item.get("id"), item.get("context"))
    key = None
    if kind is not None and match is not None and match.kind == kind.name:
        if match.subject is not None:
            key = match.subject[len(kind.name) + 1:]
        elif kind.name in ("gitignore_untracked", "workspace_problem_report"):
            key = item["id"][len(kind.prefix):] or None
    if key is None:
        logger.error(
            "[#1677 budget] refusing operator alert for '%s': id %r does not read "
            "back as a keyed %r alert, the kind its type %r registers "
            "(fail-closed; no item created)",
            agent_name, item.get("id"), kind.name if kind else None, item_type,
        )
        return ALERT_FAILED_CREATE

    outcome = platform_alerts.observe(
        agent_name, kind.name, key,
        title=item.get("title") or "",
        question=item.get("question") or "",
        priority=item.get("priority") or "high",
        context=item.get("context"),
    )
    if outcome == platform_alerts.OBSERVED_FAILED:
        logger.error(
            "[#1677 budget] operator alert create failed for %s/%s — suppressed "
            "(fail-closed; the seam logged the cause)",
            agent_name, item_type,
        )
        return ALERT_FAILED_CREATE
    if outcome == ALERT_REFUSED_AT_BUDGET:
        logger.warning(
            "[#1677 budget] alert budget full for %s/%s (cap=%d) — refused; the "
            "budget episode alert reports it",
            agent_name, item_type, _oq.OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
        )
    return outcome
