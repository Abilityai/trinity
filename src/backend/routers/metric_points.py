# mcp: metrics.ts (record_metrics → POST /agents/{name}/metrics/points)
"""Recorded metric points — the write path (trinity-enterprise#478).

`record_metrics` is the ONLY way a business metric enters the platform. The
tool POSTs here; the route owns transport concerns only (auth, limits,
idempotency, status codes) and delegates every judgement about a point to
`services/metric_points_service.py` and every row to `db/metric_points.py`
(Invariant #1).

## Gate order, and why it is that order

    self-gate → rate limit → size → execution provenance → batch idempotency
    → definitions → validate → daily cap → insert → complete → audit

Auth first and access-first (Invariant #8): an agent-scoped key may only
record as itself, decided before any lookup so the answer cannot vary by
whether a metric exists. The two cheap floods (per-minute rate, body size) are
refused before anything is READ — not before anything is parsed: FastAPI has
already parsed and validated the body by the time this handler runs, so the
byte cap bounds what gets STORED, not peak parse memory. `value` strings are
bounded per point (`METRIC_VALUE_TEXT_MAX_LEN`) for the same reason.

Execution provenance is resolved BEFORE the batch claim because the derived
key needs it — that is the one step the gate order above may not reorder.

The batch idempotency claim is taken AFTER the cheap refusals but BEFORE the
work, and **every** non-2xx exit past that point goes through `_reject`, which
releases the claim first. A 422 that wedged the caller's key for 24 hours
would turn one malformed batch into a day of silently dropped metrics
(learning 2026-09-09).

## Two layers of idempotency, on purpose

The ROW key is the invariant: `sha256(metric \0 ts \0 dims)` is the primary
key, so the same observation is one row with no client key, no Redis and no
execution id. The BATCH key is the convenience Invariant #18 asks for: a
re-delivered turn replays the first result rather than re-doing the work. When
no client key is given but `execution_id` resolves to this agent, the batch key
is derived from the execution — which is what dedups a batch of `ts`-less
points on a re-delivered turn, since those would otherwise take a fresh
server-now timestamp and hash to something new. With neither, a retry is a new
observation, and the tool description says so.

## Failure classification

Only CONNECTIVITY failures are retryable. A `DataError` / `IntegrityError` is
about the content of this batch and will fail identically forever — telling an
agent to retry it is how a permanent error becomes an infinite loop. Both
subclass `DBAPIError`, so ONE clause catches them and reads
`connection_invalidated` to tell the two apart; a separate `except
IntegrityError` after it would be dead code, never a second answer.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import DBAPIError, OperationalError

from database import db
from dependencies import AuthorizedAgent, get_current_user
from models import (
    METRIC_BATCH_MAX_BYTES,
    MetricPointsBatch,
    MetricPointsResult,
    User,
)
from services import idempotency_service, metric_points_service, rate_limiter
from services.settings_service import settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["metrics"])

# Per-agent write rate. Generous relative to the ent#482 norm (a playbook
# records tens of points per cadence) and far below what a runaway loop does.
METRICS_RATE_LIMIT = int(os.getenv("METRICS_RATE_LIMIT", "60"))
METRICS_RATE_WINDOW = 60  # seconds

STORE_RETRY_AFTER_SECONDS = 30


def _reject(
    *,
    idem,
    status_code: int,
    detail,
    headers: Optional[dict] = None,
) -> HTTPException:
    """The ONE non-2xx exit after a claim is taken.

    Keyword-only with a required `idem` so a new early return cannot forget it:
    releasing the claim is what stops a rejected batch from wedging the
    caller's key until the 24-hour TTL expires.
    """
    idempotency_service.fail(idem)
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _canonical_points(data) -> str:
    """The ONE serialisation both idempotency branches bind their key to."""
    return json.dumps(
        [p.model_dump(exclude_none=True) for p in data.points],
        sort_keys=True, default=str,
    )


def _body_fingerprint(data) -> str:
    """`sha256` of the canonical points payload — what makes a client key
    identify a BATCH rather than a caller."""
    return hashlib.sha256(_canonical_points(data).encode("utf-8")).hexdigest()


def _next_utc_midnight_seconds(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()), 1)


def _ops_int(key: str, fallback: int) -> int:
    """An ops integer, coerced toward the SAFE direction on garbage.

    "Safe" is per key, and the caller names it by choosing `fallback`:

    * the CAP falls back to the built-in default, never `0` — `0` means
      unlimited here, so an unparseable row must not silently remove the cap;
    * the RETENTION WINDOW falls back to `0`, which is what
      `cleanup_service._read_retention_setting` does with the same garbage
      (ent#478 I6). A route that refused `ts_before_retention` on a 365-day
      basis while the sweep was disabled would enforce a window nothing is
      actually pruning to.

    (`PUT /api/settings/{key}` can no longer write garbage to either key, but a
    hand-edited row still can.)
    """
    raw, _source = settings_service.resolve_ops_setting(key)
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        logger.warning(
            "[Metrics] Unparseable %s=%r — using the built-in default %s",
            key, raw, fallback,
        )
        return fallback


@router.post(
    "/{name}/metrics/points",
    response_model=MetricPointsResult,
    status_code=201,
)
async def record_metric_points(
    data: MetricPointsBatch,
    name: AuthorizedAgent,
    response: Response,
    # Bare `Request`, deliberately not `Optional[Request]`: FastAPI special-
    # cases the bare annotation as an ASGI injection and would otherwise try to
    # build a Pydantic field for it (the reports.py note).
    request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """Record a batch of metric points for an agent.

    All-or-nothing: if any point is invalid the whole batch is refused with a
    reason code per point, so a caller never has to reconcile a partial write
    against what it meant to send.
    """
    # --- self-gate (before any read: the answer must not vary by existence) --
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only record metrics as itself",
        )

    rate_limiter.enforce(
        f"agent_metrics:{name}",
        METRICS_RATE_LIMIT,
        METRICS_RATE_WINDOW,
        detail="Metric recording rate limit exceeded for this agent.",
    )

    # Two-stage size guard: the declared Content-Length is a cheap HINT, the
    # exact encoded size is the enforcement. Honest limit — Starlette has
    # already buffered AND Pydantic has already validated the body by now, so
    # this bounds STORAGE, not peak memory. The per-point `value` bound in
    # `metric_points_service` is what keeps a single field from being the
    # whole 2 MiB.
    declared = request.headers.get("content-length") if request else None
    if declared:
        try:
            if int(declared) > METRIC_BATCH_MAX_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"payload exceeds {METRIC_BATCH_MAX_BYTES} bytes",
                )
        except ValueError:
            pass  # a lying header falls through to the exact check
    encoded = json.dumps(
        [p.model_dump() for p in data.points], default=str).encode("utf-8")
    if len(encoded) > METRIC_BATCH_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"payload exceeds {METRIC_BATCH_MAX_BYTES} bytes",
        )

    # --- execution provenance (MEM-001: agent supplies, backend confirms) ----
    # The helper returns the execution row iff it belongs to this agent, and
    # fail-opens to None on anything else — so the stored column is provenance
    # this backend confirmed, never a claim the caller made. An unknown or
    # foreign id is stored as NULL, never a 4xx (MEM-001).
    execution_id = (
        data.execution_id
        if idempotency_service.resolve_and_validate_execution(
            data.execution_id, name) is not None
        else None
    )

    # --- batch idempotency ---------------------------------------------------
    header_key = request.headers.get("idempotency-key") if request else None
    client_key = header_key or data.idempotency_key
    if client_key:
        # The scope carries the identity the key omits. `AuthorizedAgent`
        # admits shared viewers, so a scope of `agent:{name}` alone would let
        # one principal's batch be swallowed as another's replay (learning
        # 2026-07-20).
        scope = idempotency_service.make_agent_scope(name)
        if not current_user.agent_name:
            scope = f"{scope}:user:{current_user.username}"
        # The body is folded into the key, exactly as the execution-derived
        # branch below already does and as the webhook boundary binds
        # `(token, body_hash)`. `idempotency_keys` stores no request
        # fingerprint, so a claim on the client key ALONE would answer every
        # later batch with the first batch's snapshot for 24 hours — and an
        # agent stamping a constant `idempotency_key: "metrics"` on every turn
        # is the realistic case. That is silent metric loss: `replayed: true`,
        # no 4xx, nothing written. Same batch → still a replay; different
        # batch under a reused key → a fresh claim, recorded.
        key = f"record_metrics:{client_key}:{_body_fingerprint(data)}"
    elif execution_id:
        # No client key, but a real turn: derive one, so a re-delivered turn
        # whose points carry no `ts` replays instead of writing a second set
        # under fresh server-now timestamps.
        scope = idempotency_service.make_effect_scope(execution_id)
        key = idempotency_service.derive_effect_key(
            execution_id,
            "record_metrics",
            _canonical_points(data),
        )
    else:
        scope, key = idempotency_service.make_agent_scope(name), None

    idem = idempotency_service.begin(scope, key)
    if idem.replay and idem.in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A batch with this idempotency key is already in progress",
        )
    if idem.replay and idem.snapshot:
        response.headers["X-Idempotent-Replay"] = "true"
        return MetricPointsResult(**{**idem.snapshot, "replayed": True})

    now = datetime.now(timezone.utc)
    # 0 on garbage — the sweep's direction, see `_ops_int`.
    retention_days = _ops_int("metrics_retention_days", 0)

    # --- validate ------------------------------------------------------------
    try:
        definitions = db.list_metric_definitions(name, include_retired=True)
    except Exception as exc:  # noqa: BLE001 — a read failure is retryable
        logger.error("[Metrics] Could not read definitions for %s: %s", name, exc)
        raise _reject(
            idem=idem,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="metric_store_unavailable",
            headers={"Retry-After": str(STORE_RETRY_AFTER_SECONDS)},
        )

    rows, errors = metric_points_service.validate_batch(
        definitions,
        [p.model_dump() for p in data.points],
        now=now,
        retention_days=retention_days,
        execution_id=execution_id,
    )
    if errors:
        raise _reject(
            idem=idem,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_points", "errors": errors},
        )

    # --- daily write cap -----------------------------------------------------
    cap = _ops_int("metrics_daily_point_cap", 100000)
    if cap > 0:
        day_start = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        # Bounded: the question is "does this batch cross the cap", not "how
        # many did today hold". Two concurrent batches can each pass and
        # overshoot by at most one batch — documented, and far cheaper than
        # serialising every write.
        headroom = max(cap - len(rows) + 1, 1)
        used = db.count_metric_points_today(name, day_start, headroom)
        if used + len(rows) > cap:
            retry_after = _next_utc_midnight_seconds(now)
            await _audit_cap_exceeded(name, current_user, cap, now)
            raise _reject(
                idem=idem,
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="daily_point_cap_exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    # --- write ---------------------------------------------------------------
    try:
        recorded, deduplicated = db.insert_metric_points(name, rows)
    except (OperationalError, DBAPIError) as exc:
        # Connectivity only. `DBAPIError` is checked for its
        # `connection_invalidated` flag so a content error wearing the same
        # base class is not mislabelled retryable.
        retryable = isinstance(exc, OperationalError) or getattr(
            exc, "connection_invalidated", False)
        if not retryable:
            logger.error("[Metrics] Unstorable batch for %s: %s", name, exc)
            raise _reject(
                idem=idem,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="metric_store_rejected_batch",
            )
        logger.error("[Metrics] Store unavailable for %s: %s", name, exc)
        raise _reject(
            idem=idem,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="metric_store_unavailable",
            headers={"Retry-After": str(STORE_RETRY_AFTER_SECONDS)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[Metrics] Unexpected store failure for %s: %s", name, exc)
        raise _reject(
            idem=idem,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="metric_store_unavailable",
            headers={"Retry-After": str(STORE_RETRY_AFTER_SECONDS)},
        )

    result = MetricPointsResult(
        agent_name=name,
        recorded=recorded,
        deduplicated=deduplicated,
        replayed=False,
        # The identity the store assigned, so a caller can tell which of its
        # points became which row — and learn the `ts` that was defaulted.
        points=[
            {"index": i, "ts": r["ts"], "idempotency_key": r["idempotency_key"]}
            for i, r in enumerate(rows)
        ],
    )
    idempotency_service.complete(idem, execution_id, result.model_dump())
    return result


async def _audit_cap_exceeded(name: str, current_user: User, cap: int,
                              now: datetime) -> None:
    """One audit row per (agent, UTC day) on the first cap refusal.

    Best-effort and in its own try: an audit failure must never turn a refusal
    into a 500. Quota events only — a row per accepted batch would be up to
    86k permanent, undeletable rows per agent per day at the cap.
    """
    claimed = None
    try:
        from services.platform_audit_service import (AuditEventType,
                                                     platform_audit_service)

        marker_scope = f"metrics-cap:{name}"
        marker_key = now.strftime("%Y-%m-%d")
        decision = idempotency_service.begin(marker_scope, marker_key)
        claimed = decision
        if decision.replay:
            return  # already recorded for this agent today
        await platform_audit_service.log(
            AuditEventType.METRICS,
            "daily_point_cap_exceeded",
            "api",
            actor_user=current_user,
            actor_agent_name=current_user.agent_name,
            target_type="agent",
            target_id=name,
            endpoint=f"/api/agents/{name}/metrics/points",
            # Counts and the day only — never a value, never a dimension: a
            # dimension value may be a customer name or an email, and this row
            # is undeletable for a year.
            details={"cap": cap, "day": marker_key},
        )
        idempotency_service.complete(decision, None, {"logged": True})
        claimed = None  # completed: there is nothing left to release
    except Exception as exc:  # noqa: BLE001
        # Release the marker, or a transient `log()` failure leaves it
        # `in_flight` and every later refusal today reads `replay` and returns
        # — no audit row for that agent-day, and no way to tell that from
        # "already recorded". `_reject`'s rule, one layer down.
        if claimed is not None:
            idempotency_service.fail(claimed)
        logger.warning("[Metrics] Could not audit cap refusal for %s: %s",
                       name, exc)
