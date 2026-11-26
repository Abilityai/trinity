"""Mail each run of a schedule to the person who asked for it (abilityai/trinity-enterprise#1391).

The first session's channel offer. The schedule keeps WHO asked
(``agent_schedules.email_results_user_id``), never an address. Only ``enable``
writes it, and only with the caller's own id, so a schedule cannot be pointed
at a third person to use Trinity as a mailer.

At every send the recipient is resolved again from that person's account:

* the address is their account's email now, so it follows an account change;
* a person who no longer manages the agent (unshared, demoted, suspended,
  deleted) gets nothing: mail stops, the setting is cleared and the schedule
  says why;
* a take-over keeps it only when the taker is that person
  (``reassign_schedule_creator``).

Delivery joins parts that already exist:

* the terminal chokepoint ``channel_completion_report.report_completion``, which
  every CAS-won terminal already calls. This module is one leg of it, so no new
  terminal writer exists;
* that module's redaction, the one every channel uses: a result passes
  ``_sanitized_detail`` (credentials), a failure is only ever
  ``person_safe_failure`` (never the raw error or a traceback);
* ``email_service.send_html``, which sends through this install's own provider
  and otherwise through the registered relay as the structured ``run_result``
  kind. A relay refusal comes back with its reason (``EmailRefused``);
* ``idempotency_service.effect_guard``, keyed on the run a retry chain started
  from, so a run is mailed once however many attempts it took. A failed
  attempt the scheduler will retry is not mailed at all: its retry's ending is.

A mail that did not go is recorded on the schedule (``email_results_error``),
so the person sees the last delivery failed and why. A failure here never
disturbs the run, which has already finished.

All database work runs in a thread (#4674): this is reached from the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 30.0
# What the mail carries. The relay caps the same at 20 000.
MAX_RESULT_CHARS = 20_000
_SUCCEEDED = {"success", "succeeded", "completed"}
_UNFINISHED = {"running", "pending", "queued", "pending_retry"}
# The control that turns it off: the Schedules tab's button carries this label
# (SchedulesPanel.vue), and the mail and the assistant card name it.
STOP_CONTROL = "Stop emailing"
STOP_LINE = (f"To stop these emails, open the agent's Schedules tab and choose "
             f"\"{STOP_CONTROL}\" on this schedule.")

# Why mail stopped by itself. Shown on the schedule to whoever reads it, so it
# names no one.
LOST_ACCESS = ("Result emails stopped: the person who turned them on no longer "
               "manages this agent.")
NO_ADDRESS = ("Result emails stopped: the account that turned them on has no "
              "email address.")
TIMED_OUT = ("The email service did not answer within 30 seconds, so the "
             "result was not emailed.")
NOT_SENT = ("This instance's email provider did not send the result. Check the "
            "email settings, then run the schedule again.")


class EmailResultsRefused(Exception):
    """``enable`` could not set it up. ``reason`` is a code, ``detail`` a sentence."""

    def __init__(self, reason: str, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.reason, self.detail, self.status_code = reason, detail, status_code


class _NotSent(Exception):
    """A send that did not go, with the sentence the schedule shows."""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _run_url(agent_name: str, execution_id: str) -> str:
    base = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    if not base or not execution_id:
        return ""
    return f"{base}/agents/{agent_name}/executions/{execution_id}"


def _succeeded(status: str) -> bool:
    return str(status or "").lower() in _SUCCEEDED


def person_text(status: str, response: Optional[str], error: Optional[str]) -> str:
    """What the mail may say about a run: the same redaction as every other
    delivery path. A success is its credential-sanitised result; a failure is
    only the person-safe sentence, never the raw error or a traceback."""
    from services.channel_completion_report import _sanitized_detail, person_safe_failure

    if _succeeded(status):
        return _sanitized_detail(response, limit=MAX_RESULT_CHARS)
    return person_safe_failure(str(status or "failed").lower(), error)


async def send_run(*, to: str, agent_name: str, title: str, execution_id: str,
                   succeeded: bool, text: str, head: Optional[str] = None) -> None:
    """Mail one run's (already redacted) text to ``to``. Raises ``_NotSent``
    with the reason when it did not go."""
    from services.email_service import EmailRefused, email_service

    url = _run_url(agent_name, execution_id)
    head = head or (f"Your agent {agent_name} finished its run \"{title}\"." if succeeded
                    else f"Your agent {agent_name} did not finish its run \"{title}\".")
    send = email_service.send_html(
        to,
        f"{agent_name}: {title}",
        intro=head,
        paragraphs=[text or "It wrote nothing this time.",
                    "You get this because you asked for each run's result by email. " + STOP_LINE],
        cta=("Open it on your instance", url) if url else None,
        relay=("run_result", {"agent": agent_name, "title": title,
                              "status": "succeeded" if succeeded else "failed",
                              "result": text, "url": url}),
        raise_refusal=True,
    )
    try:
        sent = await asyncio.wait_for(send, timeout=SEND_TIMEOUT_SECONDS)
    except EmailRefused as refused:
        raise _NotSent(refused.code, refused.message,
                       refused.status_code if 400 <= refused.status_code < 500 else 502)
    except asyncio.TimeoutError:
        raise _NotSent("email_timed_out", TIMED_OUT, 504)
    except Exception as e:  # noqa: BLE001 - the caller records it was not sent
        logger.warning("[ent#1391] result of %s not mailed: %s", execution_id or "-", type(e).__name__)
        raise _NotSent("email_not_sent", NOT_SENT)
    if not sent:
        raise _NotSent("email_not_sent", NOT_SENT)


# --- who it goes to, decided at send time ----------------------------------


def _manages(db, user: dict, agent_name: str) -> bool:
    """The route's own gate (`OwnedAgent`): an admin, or a person who may share
    the agent. A suspended account manages nothing.

    The recipient is a stored account row, never an agent key, so the reach
    helpers admit it on identity alone; they are used rather than a bare role
    compare so this gate cannot drift from the route's (ent#629, ent#872).

    So an admin who neither owns the agent nor is shared on it is mailed, as
    long as they turned it on themselves. A change of owner ends that: the
    transfer clears every recipient but the new owner (ent#1391)."""
    from dependencies import agent_may_reach, is_fleet_admin

    if user.get("suspended_at"):
        return False
    principal = SimpleNamespace(role=user.get("role"), agent_name=None)
    if not agent_may_reach(principal, agent_name, manage=True):
        return False
    if is_fleet_admin(principal):
        return True
    return bool(db.can_user_share_agent(user.get("username") or "", agent_name))


def _recipient(db, user_id, agent_name: str):
    """(address, why-not). The address of the person who turned it on, read
    now from their account, or the sentence saying why there is none."""
    try:
        user = db.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        user = None
    if not user or not _manages(db, user, agent_name):
        return None, LOST_ACCESS
    address = (user.get("email") or "").strip()
    if "@" not in address:
        return None, NO_ADDRESS
    return address, None


# --- the terminal leg -------------------------------------------------------


@dataclass
class _Plan:
    schedule_id: str
    agent_name: str
    title: str
    root_id: str
    execution_id: str
    to: Optional[str]
    stopped: Optional[str]
    succeeded: bool
    text: str


def _will_retry(row, schedule, status: str) -> bool:
    """The scheduler's own retry rule (`scheduler/service.py::_handle_retry`):
    a failed attempt with retries left is followed by another attempt, whose
    ending is the one mailed."""
    from services.failure_classifier import is_turn_limit

    status = str(status or "").lower()
    if _succeeded(status) or status in ("skipped", "cancelled"):
        return False
    error = (getattr(row, "error", None) or "").lstrip()
    if error.startswith("poison_lease") or error.lower().startswith("[max_turns]") or is_turn_limit(error):
        return False
    max_retries = int(getattr(schedule, "max_retries", 0) or 0)
    return max_retries > 0 and int(getattr(row, "attempt_number", None) or 1) <= max_retries


def _root_id(db, row) -> str:
    """The first attempt of a retry chain (the scheduler's
    `get_original_execution_id`, same 5-hop bound)."""
    current = row
    for _ in range(5):
        parent = getattr(current, "retry_of_execution_id", None)
        if not parent:
            break
        found = db.get_execution(parent)
        if found is None:
            return parent
        current = found
    return current.id


def _plan_delivery(execution_id: str, status: Optional[str], retry_dropped: bool = False) -> Optional[_Plan]:
    """Everything the send needs, read in a thread. None: nothing to mail.

    ``retry_dropped``: the scheduler says this failed attempt's retry will never
    run (the schedule was turned off or removed, autonomy went off, a sync
    freeze), so the attempt skipped as "a retry is coming" is mailed now."""
    from database import db

    row = db.get_execution(execution_id) if execution_id else None
    if retry_dropped and row is not None:
        status = getattr(row, "status", None)
        if _succeeded(str(status or "").lower()) or str(status or "").lower() in _UNFINISHED:
            return None
    schedule_id = getattr(row, "schedule_id", None)
    # A deleted schedule mails nothing, on purpose, including a failure held
    # back for a retry that now never runs: get_schedule() skips soft-deleted
    # rows, and the person who removed the schedule has stopped its mail.
    schedule = db.get_schedule(schedule_id) if schedule_id else None
    if not schedule or getattr(schedule, "email_results_user_id", None) is None:
        return None
    if schedule.agent_name != row.agent_name:
        return None
    if not retry_dropped and _will_retry(row, schedule, status):
        return None
    to, stopped = _recipient(db, schedule.email_results_user_id, schedule.agent_name)
    if stopped:
        # Mail stops with the person's access; the schedule says why.
        db.set_schedule_email_results(schedule.id, None, error=stopped)
        return None
    return _Plan(schedule_id=schedule.id, agent_name=schedule.agent_name, title=schedule.name,
                 root_id=_root_id(db, row), execution_id=execution_id, to=to, stopped=None,
                 succeeded=_succeeded(status),
                 text=person_text(status, getattr(row, "response", None), getattr(row, "error", None)))


def _record(schedule_id: str, error: Optional[str]) -> None:
    from database import db

    db.record_schedule_email_results_outcome(schedule_id, error)


async def deliver(*, execution_id: str, status: Optional[str] = None, retry_dropped: bool = False) -> bool:
    """The terminal leg: mail this run if its schedule asks for it. Never raises.

    Also the scheduler's dropped-retry notice (``retry_dropped``). The claim is
    keyed on the chain's first attempt either way, so a chain is mailed once."""
    try:
        from services.idempotency_service import EffectInProgressError, effect_guard

        plan = await asyncio.to_thread(_plan_delivery, execution_id, status, retry_dropped)
        if plan is None:
            return False
        try:
            async with effect_guard("schedule_result_email", {"schedule_id": plan.schedule_id},
                                    execution_id=plan.root_id, agent_name=plan.agent_name) as guard:
                if guard.replay:
                    return False
                # A send that did not go raises, which releases the claim.
                await send_run(to=plan.to, agent_name=plan.agent_name, title=plan.title,
                               execution_id=plan.execution_id, succeeded=plan.succeeded, text=plan.text)
                guard.snapshot = {"mailed": True}
        except EffectInProgressError:
            return False
        except _NotSent as not_sent:
            await asyncio.to_thread(_record, plan.schedule_id, not_sent.message)
            return False
        await asyncio.to_thread(_record, plan.schedule_id, None)
        return True
    except Exception as e:  # noqa: BLE001 - never disturb a finished run
        logger.warning("[ent#1391] result mail raised for %s: %s", execution_id, type(e).__name__)
        return False


# --- turning it on and off --------------------------------------------------


def _latest_finished(schedule_id: str):
    """This schedule's own latest finished run: never another schedule's, a
    chat, or anyone's turn on the agent. None when it has not finished one."""
    from database import db

    for row in db.get_schedule_executions(schedule_id, limit=20) or []:
        if getattr(row, "completed_at", None) and str(getattr(row, "status", "")).lower() not in _UNFINISHED:
            return row
    return None


def _store(schedule_id: str, user_id: Optional[int]) -> None:
    from database import db

    db.set_schedule_email_results(schedule_id, user_id)


async def enable(*, agent_name: str, schedule, user) -> dict:
    """Mail each run of ``schedule`` to ``user``, the caller, at their own
    address, and mail this schedule's latest finished run now so the person sees
    it arrive. With no finished run yet, a plain confirmation goes instead, and
    the answer says so.

    The setting is kept only once a mail went: a setting that has never
    delivered is not a working channel. Raises EmailResultsRefused with the
    real reason (a relay refusal's own sentence) when it did not.
    """
    address = (getattr(user, "email", None) or "").strip()
    if "@" not in address:
        raise EmailResultsRefused("no_email_address", "Your account has no email address, so there is nowhere to send it.")
    latest = await asyncio.to_thread(_latest_finished, schedule.id)
    try:
        if latest is not None:
            await send_run(to=address, agent_name=agent_name, title=schedule.name,
                           execution_id=latest.id, succeeded=_succeeded(latest.status),
                           text=person_text(latest.status, getattr(latest, "response", None),
                                            getattr(latest, "error", None)))
        else:
            await send_run(to=address, agent_name=agent_name, title=schedule.name, execution_id="",
                           succeeded=True,
                           head=f"Each run of \"{schedule.name}\" on {agent_name} will be emailed to you.",
                           text=("This schedule has not finished a run yet, so there is no result to "
                                 "send now. You will get the first one when it finishes."))
    except _NotSent as not_sent:
        raise EmailResultsRefused(not_sent.code, "Nothing was set up: " + not_sent.message,
                                  not_sent.status_code)
    await asyncio.to_thread(_store, schedule.id, user.id)
    return {
        "email_results": True,
        "email_results_to_me": True,
        "sent_execution_id": getattr(latest, "id", None),
        "no_finished_run_yet": latest is None,
        "message": ("The latest result is on its way to your email address." if latest is not None
                    else "This schedule has not finished a run yet, so a confirmation went to your "
                         "email address instead. The first result follows when it finishes."),
    }


async def disable(*, schedule) -> dict:
    await asyncio.to_thread(_store, schedule.id, None)
    return {"email_results": False, "email_results_to_me": False}
