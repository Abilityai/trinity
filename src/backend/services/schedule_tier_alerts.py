"""A schedule whose runs are a shared person's says so on the run (#4281).

A schedule's runs are rated by its creator (`turn_tier_service.schedule_run_tier`).
When the creator is neither an admin nor the agent's owner, every run is a shared
person's turn: no shell, no tools the owner added. The terminal write records
`restricted_turn` in the run's `turn_integrity` JSON (`run_restriction` →
`execution_integrity`). The run's status does not change.
"""
import logging
from typing import Dict, Optional, Tuple

from services import execution_integrity, turn_tier_service as tts

logger = logging.getLogger(__name__)


# ---- the run ------------------------------------------------------------------------------

def run_restriction(db_, agent_name: str, execution) -> Tuple[Optional[tts.RunTier], Optional[Dict]]:
    """`(rating, restricted_turn record)` for a run.

    The rating is None for a run that is not schedule-backed (a person started
    it, or it names no schedule). The record is None unless the rating is not
    owner. Never raises: a rating that cannot be computed gives `(None, None)`,
    so it never changes the run's own outcome (the #2467 belt)."""
    try:
        rated = tts.schedule_backed_run_tier(db_, agent_name, execution)
    except Exception as exc:  # noqa: BLE001 - a stamp must never break the terminal write
        logger.warning("[#4281] could not rate run %s on %s: %s",
                       getattr(execution, "id", None), agent_name, exc)
        return None, None
    if rated is None or rated.tier == tts.OWNER:
        return rated, None
    schedule_id = (execution.get("schedule_id") if isinstance(execution, dict)
                   else getattr(execution, "schedule_id", None))
    record = execution_integrity.restricted_turn_record(
        tier=rated.tier, kind=rated.kind, cause=rated.cause,
        schedule_id=schedule_id, creator_id=rated.creator_id)
    return rated, record


def run_restriction_by_id(db_, agent_name: str, execution_id: Optional[str]):
    """`run_restriction` for the run `execution_id`. Never raises."""
    if not execution_id:
        return None, None
    try:
        execution = db_.get_execution(execution_id)
    except Exception as exc:  # noqa: BLE001 - see run_restriction
        logger.warning("[#4281] could not read run %s on %s: %s", execution_id, agent_name, exc)
        return None, None
    return run_restriction(db_, agent_name, execution)
