"""Mail each run of a schedule to the person who asked for it (abilityai/trinity-enterprise#1391).

The first session's channel offer. One column, ``agent_schedules.deliver_to_email``,
names who gets each run's result. Only ``enable`` writes it, and only with the
caller's own address. It is never an argument the caller supplies, so a schedule
cannot be pointed at a third person to use Trinity as a mailer.

Delivery joins parts that already exist:

* the terminal chokepoint ``channel_completion_report.report_completion``, which
  every CAS-won terminal already calls. This module is one leg of it, so no new
  terminal writer exists;
* ``email_service.send_html``, which sends through this install's own provider
  and otherwise through the registered relay as the structured ``run_result``
  kind. The gateway delivers that kind only to an address it already holds for
  the instance;
* ``idempotency_service.effect_guard``, which makes each run mailed at most once.

A failure here never disturbs the run, which has already finished.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 30.0
# What the mail carries. The relay caps the same at 20 000.
MAX_RESULT_CHARS = 20_000
_SUCCEEDED = {"success", "succeeded", "completed"}


class EmailResultsRefused(Exception):
    """``enable`` could not set it up. ``reason`` is a code, ``detail`` a sentence."""

    def __init__(self, reason: str, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.reason, self.detail, self.status_code = reason, detail, status_code


def _run_url(agent_name: str, execution_id: str) -> str:
    base = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    return f"{base}/agents/{agent_name}/executions/{execution_id}" if base else ""


def _outcome(status: str) -> str:
    return "succeeded" if str(status or "").lower() in _SUCCEEDED else "failed"


async def send_run(*, to: str, agent_name: str, title: str, execution_id: str,
                   status: str, result: Optional[str]) -> bool:
    """Mail one run's result to ``to``. True only when it was sent."""
    from services.email_service import email_service

    outcome = _outcome(status)
    text = (result or "").strip()[:MAX_RESULT_CHARS]
    url = _run_url(agent_name, execution_id)
    head = (f"Your agent {agent_name} finished its run \"{title}\"." if outcome == "succeeded"
            else f"Your agent {agent_name} did not finish its run \"{title}\".")
    send = email_service.send_html(
        to,
        f"{agent_name}: {title}",
        intro=head,
        paragraphs=[text or "It wrote nothing this time.",
                    "You get this because you asked for each run's result by email. "
                    "Turn it off on the agent's Schedules tab."],
        cta=("Open it on your instance", url) if url else None,
        relay=("run_result", {"agent": agent_name, "title": title, "status": outcome,
                              "result": text, "url": url}),
    )
    try:
        return bool(await asyncio.wait_for(send, timeout=SEND_TIMEOUT_SECONDS))
    except Exception as e:  # noqa: BLE001 - the caller says it was not sent
        logger.warning("[ent#1391] result of %s not mailed: %s", execution_id, type(e).__name__)
        return False


async def deliver(*, execution_id: str, status: str) -> bool:
    """The terminal leg: mail this run if its schedule asks for it. Never raises."""
    try:
        from database import db
        from services.idempotency_service import EffectInProgressError, effect_guard

        row = db.get_execution(execution_id) if execution_id else None
        schedule_id = getattr(row, "schedule_id", None)
        schedule = db.get_schedule(schedule_id) if schedule_id else None
        to = getattr(schedule, "deliver_to_email", None) if schedule else None
        if not to or getattr(schedule, "agent_name", None) != getattr(row, "agent_name", None):
            return False
        try:
            async with effect_guard("schedule_result_email", {"to": to.lower()},
                                    execution_id=execution_id, agent_name=row.agent_name) as guard:
                if guard.replay:
                    return False
                if not await send_run(to=to, agent_name=row.agent_name, title=schedule.name,
                                      execution_id=execution_id, status=status,
                                      result=getattr(row, "response", None) or getattr(row, "error", None)):
                    return False
                guard.snapshot = {"mailed": True}
        except EffectInProgressError:
            return False
        return True
    except Exception as e:  # noqa: BLE001 - never disturb a finished run
        logger.warning("[ent#1391] result mail raised for %s: %s", execution_id, e)
        return False


def _latest_finished(db, agent_name: str):
    for row in db.get_agent_executions(agent_name, limit=20) or []:
        if getattr(row, "completed_at", None) and str(getattr(row, "status", "")).lower() not in ("running", "pending", "queued"):
            return row
    return None


async def enable(*, agent_name: str, schedule, email: Optional[str]) -> dict:
    """Send each run of ``schedule`` to ``email``, the caller's own address, and
    mail the latest finished run now, so the person sees it arrive.

    The address is kept only once a mail went: a setting that has never
    delivered is not a working channel. Raises EmailResultsRefused.
    """
    from database import db

    address = (email or "").strip()
    if "@" not in address:
        raise EmailResultsRefused("no_email_address", "Your account has no email address, so there is nowhere to send it.")
    latest = _latest_finished(db, agent_name)
    if latest is not None:
        sent = await send_run(to=address, agent_name=agent_name, title=schedule.name,
                              execution_id=latest.id, status=latest.status,
                              result=getattr(latest, "response", None) or getattr(latest, "error", None))
    else:
        sent = await send_run(to=address, agent_name=agent_name, title=schedule.name, execution_id="",
                              status="success", result=f"Each run of \"{schedule.name}\" will come to this address.")
    if not sent:
        raise EmailResultsRefused("email_not_sent", "The email could not be sent, so nothing was set up. "
                                                    "Try again in a moment.")
    db.set_schedule_result_email(schedule.id, address)
    return {"deliver_to_email": address, "sent": True,
            "sent_execution_id": getattr(latest, "id", None)}


def disable(*, schedule) -> dict:
    from database import db

    db.set_schedule_result_email(schedule.id, None)
    return {"deliver_to_email": None}
