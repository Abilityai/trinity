"""Terms of use per instance (trinity-enterprise#1107).

The OSS owner of the terms: the text (an instance setting with a shipped
default), the decision the gate asks for, the acceptance record and the hooks
a consumer registers. ``services/terms_gate.py`` is the seam that calls
``decision``; ``routers/terms.py`` is the HTTP surface.

Settings (``system_settings`` rows, resolved ``system_settings → default``):

* ``terms_required`` — the switch. Absent ⇒ ON: every first-time person on
  every door is shown the terms (the operator's direction on ent#1107). An
  admin may set it off through ``PUT /api/terms``.
* ``terms_title`` / ``terms_text`` — the text; absent ⇒ the shipped default
  (``terms_defaults``).
* ``terms_version`` — the version a person accepts. Absent ⇒
  ``DEFAULT_TERMS_VERSION``; saving a text stamps a new ISO-Z timestamp, and
  restoring the default deletes the three rows so the constant is back.

The four keys are written only through ``set_terms`` / ``restore_default``
(the generic ``PUT`` and ``DELETE /api/settings/{key}`` refuse them), so a value with a
range is always validated: empty text ``terms_text_empty``, text over
``TERMS_TEXT_MAX_BYTES`` ``terms_text_too_large``, a title over
``TERMS_TITLE_MAX_CHARS`` ``terms_title_too_long``.

``decision(user)`` — required ⇔ the switch is on and the person has no
acceptance row for the current version. Every person, every role: an admin's
own first visit is not exempt (setup resolves no principal, so the claim always
comes first). A ``PortalPerson`` — a Workspace portal session, the emailed-code
sign-in with no ``users`` row (ruling A(1)) — is decided by its EMAIL: any row
for that address and version, a session's or an account's, is the same person
having accepted.

``accept(user, version, ...)`` — idempotent on the table's unique keys: one row
per (person, version), the second call a no-op. A ``PortalPerson``'s row
carries ``user_id`` NULL, the email, and no door. ``version_stale`` (409) when the
body's version is not the current one. Audits ``AUTHORIZATION/terms_accepted``
(version and door — never the text).

**Hooks.** ``register_acceptance_hook(fn)`` registers a consumer called as
``fn(user_id, email, terms_version, accepted_at)`` after a NEW acceptance row
commits. The route schedules ``run_acceptance_hooks`` through FastAPI
``BackgroundTasks``, after the response, so the acceptance never waits on a
consumer; a consumer that raises is logged and audited
(``AUTHORIZATION/terms_hook_failed``). ``run_acceptance_hooks`` is a coroutine
so every hook runs ON the event-loop thread.

``register_acceptance_hook(fn, inline=True)`` (trinity-enterprise#1111) opts a
consumer into being awaited BEFORE the response instead:
``run_inline_acceptance_hooks`` runs the inline hooks under one ceiling
(``TERMS_INLINE_HOOK_BUDGET_S``) for the whole acceptance. For work the person
is about to look for — a share that has to be on their roster when the
Workspace re-reads it at the redirect — "after the response" always loses that
race. The ceiling is a WAIT ceiling, not a cancellation: past it the hook runs
on as a shielded task and the acceptance answers anyway, audited
``AUTHORIZATION/terms_hook_slow``.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from database import db
from services import terms_defaults
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

SETTING_REQUIRED = "terms_required"
SETTING_VERSION = "terms_version"
SETTING_TEXT = "terms_text"
SETTING_TITLE = "terms_title"
SETTING_KEYS = frozenset({SETTING_REQUIRED, SETTING_VERSION, SETTING_TEXT, SETTING_TITLE})

TERMS_TEXT_MAX_BYTES = 200_000
TERMS_TITLE_MAX_CHARS = 200

AcceptanceHook = Callable[[Optional[int], Optional[str], Optional[str], Optional[str]], Any]


@dataclass(frozen=True)
class PortalPerson:
    """A Workspace portal session as the gate and the terms routes see it
    (ruling A(1) on trinity-enterprise#1107): a verified email with no platform
    account. Carries every identity field the principal predicates read, each
    with the PERSON value — a JWT-shaped interactive visit (``mcp_scope`` None,
    ``interactive_session`` True, no agent identity) — so ``is_person_principal``
    / ``is_interactive_principal`` admit it and ``terms_gate._is_person`` /
    ``_is_visit`` ask it (``interactive_session`` is the token's own claim on
    the gate path; the terms routes do not read it). ``id`` is None: that is
    what keys its record by email.
    Role ``user``: the gate's fault policy treats it as the Workspace-only
    population it is (a provider fault is fail-closed)."""

    email: str
    id: Optional[int] = None
    username: Optional[str] = None
    role: str = "user"
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    is_event_loopback: bool = False
    vouched_source_agent: Optional[str] = None
    interactive_session: bool = True
    suspended_at: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "email", (self.email or "").strip().lower())


def _email_of(user: Any) -> Optional[str]:
    return (getattr(user, "email", None) or "").strip().lower() or None


def has_accepted(user: Any, version: str) -> bool:
    """One acceptance lookup: by account for a platform person, by address
    for a ``PortalPerson`` (``id`` None)."""
    user_id = getattr(user, "id", None)
    if user_id is not None:
        return bool(db.get_terms_acceptance(int(user_id), version))
    email = _email_of(user)
    return bool(email and db.get_terms_acceptance_by_email(email, version))


class TermsError(Exception):
    """A refusal the route turns into ``{status, code, message, version}``."""

    def __init__(self, status_code: int, code: str, message: str, *, version: Optional[str] = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.version = version
        super().__init__(message)


# --- The settings -------------------------------------------------------

def _setting(key: str) -> Optional[str]:
    """A ``system_settings`` value, or ``None`` when unset or unreadable."""
    try:
        return db.get_setting_value(key, None)
    except Exception:  # noqa: BLE001 — a settings read must not break the door
        logger.warning("[terms] setting %s unreadable; using the default", key, exc_info=True)
        return None


def switch_on() -> bool:
    """The gate's switch. Absent ⇒ on."""
    raw = _setting(SETTING_REQUIRED)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def current_version() -> str:
    raw = (_setting(SETTING_VERSION) or "").strip()
    return raw or terms_defaults.DEFAULT_TERMS_VERSION


def current_terms() -> dict:
    """``{"required", "version", "title", "text", "is_default"}``.

    ``required`` is the switch; ``is_default`` says the shipped text is in
    force (no custom text saved)."""
    text = _setting(SETTING_TEXT)
    custom = text is not None and bool(str(text).strip())
    title = _setting(SETTING_TITLE)
    return {
        "required": switch_on(),
        "version": current_version(),
        "title": (str(title).strip() if title and str(title).strip() else terms_defaults.DEFAULT_TERMS_TITLE),
        "text": str(text) if custom else terms_defaults.DEFAULT_TERMS_TEXT,
        "is_default": not custom,
    }


def validate_text(text: Any) -> str:
    """The body as it will be stored, or ``TermsError`` (422) by name."""
    body = "" if text is None else str(text)
    if not body.strip():
        raise TermsError(422, "terms_text_empty",
                         "The terms text is empty. Paste the terms (Markdown), or restore the default.")
    if len(body.encode("utf-8")) > TERMS_TEXT_MAX_BYTES:
        raise TermsError(422, "terms_text_too_large",
                         f"The terms text is over {TERMS_TEXT_MAX_BYTES // 1000} KB. "
                         "Shorten it, or link to the full document.")
    return body


def validate_title(title: Any) -> str:
    value = ("" if title is None else str(title)).strip()
    if len(value) > TERMS_TITLE_MAX_CHARS:
        raise TermsError(422, "terms_title_too_long",
                         f"The title is over {TERMS_TITLE_MAX_CHARS} characters. "
                         "Use a short title, e.g. \"Terms of use\".")
    return value or terms_defaults.DEFAULT_TERMS_TITLE


def set_terms(*, title: Any = None, text: Any = None, required: Optional[bool] = None,
              author: Any = None, actor_ip: Optional[str] = None,
              now: Optional[str] = None) -> dict:
    """Save what was given. A text (or title) save that CHANGES what a person
    reads stamps a NEW version — the change is what asks everyone again — and
    the switch alone does not. A save of the text and title already in force
    writes nothing (W7): the same words are the same version, so nobody is
    asked twice for a save that changed nothing.

    Both fields are validated BEFORE either is written (C3): a refused title
    must not have published the text under the old version — that would be
    a change nobody is asked to accept.

    ``author`` (the admin saving) is recorded as having accepted the version
    they just wrote (``door="author"``): the person who set the text is not
    bounced out of Settings by their own save."""
    current = current_terms()
    new_text = validate_text(text) if text is not None else None
    new_title = validate_title(title) if title is not None else None
    text_changes = new_text is not None and new_text != current["text"]
    title_changes = new_title is not None and new_title != current["title"]
    stamped = None
    if text_changes or title_changes:
        # Both rows are written so a title over the shipped body pins that
        # body: what a person reads is (title, text), and the version names it.
        db.set_setting(SETTING_TEXT, new_text if new_text is not None else current["text"])
        db.set_setting(SETTING_TITLE, new_title if new_title is not None else current["title"])
        stamped = now or utc_now_iso()
        db.set_setting(SETTING_VERSION, stamped)
    if required is not None:
        db.set_setting(SETTING_REQUIRED, "true" if required else "false")
    if stamped and author is not None and getattr(author, "id", None) is not None:
        db.insert_terms_acceptance(
            int(author.id), (getattr(author, "email", None) or "").strip().lower() or None,
            stamped, stamped, door="author", actor_ip=actor_ip,
        )
    return current_terms()


def restore_default() -> dict:
    """The one action that brings the shipped text back. The switch is left
    as it is — restoring the text is not a decision about the gate."""
    for key in (SETTING_TEXT, SETTING_TITLE, SETTING_VERSION):
        db.delete_setting(key)
    return current_terms()


# --- The decision -------------------------------------------------------

def decision(user: Any) -> dict:
    """``{"required", "version", "reason"}`` for the gate. The switch first
    (one read, and the whole cost while the gate is off), then the version
    and one acceptance lookup."""
    if not switch_on():
        return {"required": False, "version": None, "reason": "terms_required"}
    version = current_version()
    if has_accepted(user, version):
        return {"required": False, "version": version, "reason": "terms_required"}
    return {"required": True, "version": version, "reason": "terms_required"}


def accepted_current(user: Any) -> bool:
    """Has this principal (an account, or a ``PortalPerson`` by address)
    accepted the current version? An int is read as a user id for the
    callers that still pass one."""
    if isinstance(user, int):
        return bool(db.get_terms_acceptance(user, current_version()))
    return has_accepted(user, current_version())


# --- The acceptance -----------------------------------------------------

async def accept(user: Any, version: str, *, actor_ip: Optional[str],
                 request_id: Optional[str] = None, endpoint: Optional[str] = None,
                 door: Optional[str] = None, now: Optional[str] = None) -> dict:
    """Record ``user``'s acceptance of ``version``. See the module doc."""
    now = now or utc_now_iso()
    current = current_version()
    if (version or "").strip() != current:
        raise TermsError(409, "version_stale", "The terms have changed; read the current version.",
                         version=current)
    raw_id = getattr(user, "id", None)
    user_id = int(raw_id) if raw_id is not None else None
    email = _email_of(user)
    if user_id is None and not email:
        raise TermsError(403, "person_required", "Only a person can accept the terms.")

    row_id = db.insert_terms_acceptance(user_id, email, current, now, door=door, actor_ip=actor_ip)
    created = row_id is not None
    if created:
        await _audit("terms_accepted", user, actor_ip=actor_ip, request_id=request_id,
                     endpoint=endpoint, details={"version": current, "door": door})
        logger.info("[terms] %s accepted terms version %s",
                    f"user {user_id}" if user_id is not None else "a Workspace session", current)
    row = (db.get_terms_acceptance(user_id, current) if user_id is not None
           else db.get_terms_acceptance_by_email(email, current)) or {}
    return {
        "accepted": True,
        "created": created,
        "version": current,
        "accepted_at": row.get("accepted_at", now),
        "email": email,
        "user_id": user_id,
    }


def list_acceptances(limit: int = 200, email: Optional[str] = None) -> List[Dict]:
    if email:
        return db.terms_acceptances_for_email(email)[:limit]
    return db.list_terms_acceptances(limit)


async def audit_change(action: str, actor: Any, *, actor_ip: Optional[str], request_id: Optional[str],
                       endpoint: Optional[str], version: str) -> None:
    """``CONFIGURATION/terms_updated`` or ``terms_restored`` — the version only,
    never the text."""
    try:
        from services.platform_audit_service import AuditEventType, platform_audit_service

        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action=action,
            source="api",
            actor_user=actor,
            actor_ip=actor_ip,
            target_type="settings",
            target_id="terms",
            endpoint=endpoint,
            request_id=request_id,
            details={"version": version},
        )
    except Exception:  # noqa: BLE001 — the audit must not undo the change
        logger.exception("[terms] audit %s failed", action)


async def _audit(action: str, actor: Any, *, actor_ip: Optional[str], request_id: Optional[str],
                 endpoint: Optional[str], details: dict) -> None:
    try:
        from services.platform_audit_service import AuditEventType, platform_audit_service

        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action=action,
            source="api",
            actor_user=actor,
            actor_ip=actor_ip,
            target_type="user",
            target_id=str(getattr(actor, "username", None) or getattr(actor, "id", None)
                          or getattr(actor, "email", None) or ""),
            endpoint=endpoint,
            request_id=request_id,
            details=details,
        )
    except Exception:  # noqa: BLE001 — the audit must not undo the acceptance
        logger.exception("[terms] audit %s failed", action)


# --- The hooks ----------------------------------------------------------

_HOOKS: List[AcceptanceHook] = []

# trinity-enterprise#1111 (T1): the consumers awaited BEFORE the response.
_INLINE_HOOKS: List[AcceptanceHook] = []

# The ceiling on the whole inline phase of ONE acceptance — not per hook, so
# three slow consumers cannot add up to three ceilings on one request. It is a
# WAIT ceiling, never a cancellation: past it the hook runs on as a shielded
# task and reaches its own compensation (see `run_inline_acceptance_hooks`).
TERMS_INLINE_HOOK_BUDGET_S = 5

# Strong references to the inline tasks still running. Without this the loop is
# the only owner of a task it is no longer awaiting, and a mid-mutation consumer
# can be garbage-collected.
_PENDING_INLINE: set = set()


def register_acceptance_hook(fn: AcceptanceHook, *, inline: bool = False) -> None:
    """Register a consumer of new acceptances. Idempotent per function.

    ``inline=True`` opts the consumer into the phase awaited BEFORE the
    response (trinity-enterprise#1111): for work the person is about to look
    for — a share that has to be on their roster when the Workspace re-reads it
    at the redirect — "after the response" always loses that race. An inline
    hook should be a coroutine function and should keep its inline half short;
    anything slower belongs in a background hook, or in a task the inline half
    schedules.
    """
    hooks = _INLINE_HOOKS if inline else _HOOKS
    if fn not in hooks:
        hooks.append(fn)
        logger.info("[terms] %s acceptance hook registered: %s",
                    "inline" if inline else "background", getattr(fn, "__qualname__", fn))


def clear_acceptance_hooks() -> None:
    """Drop every hook, both kinds — used by tests."""
    _HOOKS.clear()
    _INLINE_HOOKS.clear()


def acceptance_hooks() -> tuple:
    return tuple(_HOOKS)


def inline_acceptance_hooks() -> tuple:
    return tuple(_INLINE_HOOKS)


def _hook_name(fn: AcceptanceHook) -> str:
    return getattr(fn, "__module__", "") + "." + getattr(fn, "__qualname__", repr(fn))


async def _run_one_inline(fn: AcceptanceHook, name: str, user_id: Optional[int],
                          email: Optional[str], terms_version: Optional[str],
                          accepted_at: Optional[str]) -> None:
    """One inline hook, owning its own failure.

    The failure is audited HERE rather than by the waiter, because past the
    ceiling there is no waiter left to catch it — and an exception nobody
    retrieves is a warning on stderr instead of an audit row.
    """
    try:
        result = fn(user_id, email, terms_version, accepted_at)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a consumer never fails an acceptance
        logger.exception("[terms] inline acceptance hook %s failed for user %s", name, user_id)
        await _audit("terms_hook_failed", None, actor_ip=None, request_id=None, endpoint=None,
                     details={"hook": name, "user_id": user_id, "version": terms_version,
                              "error": type(e).__name__, "inline": True})


async def run_inline_acceptance_hooks(user_id: Optional[int], email: Optional[str],
                                      terms_version: Optional[str],
                                      accepted_at: Optional[str]) -> None:
    """Await the inline hooks before the response, under ONE ceiling.

    Each hook runs as its own task, and the wait is `wait_for(shield(task))`:
    on timeout the WAIT ends, the task does not. That is the whole point —
    `wait_for` over the work itself would cancel a consumer part-way through a
    multi-step mutation, which is worse than answering late. Past the ceiling
    the remaining hooks are still STARTED (so their work happens) but not
    waited on, and `terms_hook_slow` records that this acceptance's consumer
    did not finish in time.

    Never raises: an acceptance is already committed by the time this runs.
    """
    hooks = tuple(_INLINE_HOOKS)
    if not hooks:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(TERMS_INLINE_HOOK_BUDGET_S), 0.0)
    for fn in hooks:
        name = _hook_name(fn)
        try:
            task = asyncio.create_task(
                _run_one_inline(fn, name, user_id, email, terms_version, accepted_at))
        except Exception:  # noqa: BLE001 — a hook that cannot even start
            logger.exception("[terms] could not start inline acceptance hook %s", name)
            continue
        _PENDING_INLINE.add(task)
        task.add_done_callback(_PENDING_INLINE.discard)
        remaining = deadline - loop.time()
        if remaining <= 0:
            await _audit_hook_slow(name, user_id, terms_version, "budget_spent")
            continue
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.TimeoutError:
            await _audit_hook_slow(name, user_id, terms_version, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — `_run_one_inline` already audited it
            logger.exception("[terms] inline acceptance hook %s raised past its own guard",
                             name)


async def _audit_hook_slow(name: str, user_id: Optional[int], terms_version: Optional[str],
                           reason: str) -> None:
    logger.warning("[terms] inline acceptance hook %s did not finish within %ss (%s); "
                   "it runs on in the background", name, TERMS_INLINE_HOOK_BUDGET_S, reason)
    await _audit("terms_hook_slow", None, actor_ip=None, request_id=None, endpoint=None,
                 details={"hook": name, "user_id": user_id, "version": terms_version,
                          "budget_s": TERMS_INLINE_HOOK_BUDGET_S, "reason": reason})


async def run_acceptance_hooks(user_id: Optional[int], email: Optional[str], terms_version: Optional[str],
                               accepted_at: Optional[str]) -> None:
    """Call every hook once, on the event loop, swallowing and auditing failures."""
    for fn in tuple(_HOOKS):
        name = getattr(fn, "__module__", "") + "." + getattr(fn, "__qualname__", repr(fn))
        try:
            fn(user_id, email, terms_version, accepted_at)
        except Exception as e:  # noqa: BLE001 — a consumer never fails an acceptance
            logger.exception("[terms] acceptance hook %s failed for user %s", name, user_id)
            await _audit("terms_hook_failed", None, actor_ip=None, request_id=None, endpoint=None,
                         details={"hook": name, "user_id": user_id, "version": terms_version,
                                  "error": type(e).__name__})
