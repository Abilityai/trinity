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
comes first).

``accept(user, version, ...)`` — idempotent on the table's UNIQUE: one row per
(person, version), the second call a no-op. ``version_stale`` (409) when the
body's version is not the current one. Audits ``AUTHORIZATION/terms_accepted``
(version and door — never the text).

**Hooks.** ``register_acceptance_hook(fn)`` registers a consumer called as
``fn(user_id, email, terms_version, accepted_at)`` after a NEW acceptance row
commits. The route schedules ``run_acceptance_hooks`` through FastAPI
``BackgroundTasks``, after the response, so the acceptance never waits on a
consumer; a consumer that raises is logged and audited
(``AUTHORIZATION/terms_hook_failed``). ``run_acceptance_hooks`` is a coroutine
so every hook runs ON the event-loop thread.
"""
from __future__ import annotations

import logging
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

AcceptanceHook = Callable[[int, Optional[str], Optional[str], Optional[str]], Any]


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
    user_id = getattr(user, "id", None)
    if user_id is None:
        return {"required": True, "version": version, "reason": "terms_required"}
    if db.get_terms_acceptance(int(user_id), version):
        return {"required": False, "version": version, "reason": "terms_required"}
    return {"required": True, "version": version, "reason": "terms_required"}


def accepted_current(user_id: int) -> bool:
    return bool(db.get_terms_acceptance(int(user_id), current_version()))


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
    user_id = int(getattr(user, "id"))
    email = (getattr(user, "email", None) or "").strip().lower() or None

    row_id = db.insert_terms_acceptance(user_id, email, current, now, door=door, actor_ip=actor_ip)
    created = row_id is not None
    if created:
        await _audit("terms_accepted", user, actor_ip=actor_ip, request_id=request_id,
                     endpoint=endpoint, details={"version": current, "door": door})
        logger.info("[terms] user %s accepted terms version %s", user_id, current)
    row = db.get_terms_acceptance(user_id, current) or {}
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
            target_id=str(getattr(actor, "username", None) or getattr(actor, "id", "")),
            endpoint=endpoint,
            request_id=request_id,
            details=details,
        )
    except Exception:  # noqa: BLE001 — the audit must not undo the acceptance
        logger.exception("[terms] audit %s failed", action)


# --- The hooks ----------------------------------------------------------

_HOOKS: List[AcceptanceHook] = []


def register_acceptance_hook(fn: AcceptanceHook) -> None:
    """Register a consumer of new acceptances. Idempotent per function."""
    if fn not in _HOOKS:
        _HOOKS.append(fn)
        logger.info("[terms] acceptance hook registered: %s", getattr(fn, "__qualname__", fn))


def clear_acceptance_hooks() -> None:
    """Drop every hook — used by tests."""
    _HOOKS.clear()


def acceptance_hooks() -> tuple:
    return tuple(_HOOKS)


async def run_acceptance_hooks(user_id: int, email: Optional[str], terms_version: Optional[str],
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
