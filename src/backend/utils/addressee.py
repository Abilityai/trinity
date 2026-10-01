"""The ONE spelling of an addressee email (trinity-enterprise#549, #2955).

Every writer of ``agent_shared_files.addressed_to_email``, the reader that
narrows a Files tab to it, and both ``audience_email`` request validators
(``models.ReportCreate``, ``models.ShareFileMcpRequest``) call this function.
It lives here — a pure leaf that imports only ``typing`` — because it
cross-cuts the layers: ``models.py`` is the API-contract module and must not
import a service (``services/turn_audience.py`` pulls in ``config``, which
raises without Redis credentials), while ``utils.*`` is import-safe from
anywhere and is never stubbed wholesale by a test isolating a sibling.
``services.turn_audience`` re-exports it under the same name — a re-export,
never a second copy (Invariant #1(a)).

This module holds the SHAPE rule only. No field name and no error text: the
boundary form that refuses by name (``audience_email must be an email
address``) is ``models._validate_audience_email``, because that message is
API-contract text naming a field.

Two email normalisers elsewhere are deliberately NOT this one — leave them:

- ``db_models.ScheduleCreate._normalize_delivery_email`` is STRICTER (exactly
  one ``@``, and it refuses control characters); ``ScheduleUpdate`` is pinned
  equal to it by ent#498's parity test. Unifying it onto this rule would
  silently drop the control-character refusal.
- ``services.mcp_auth_service.normalize_email`` is an auth LOOKUP KEY
  (strip + lower only), not an addressee.

Why not Pydantic ``EmailStr``: the resolver runs this over stored DB values
and must answer ``None``, never raise; and ``email-validator`` would change
decisions the resolver has already made (``a@b@c.com``, IDN domains).
"""
from typing import Any, Optional


def normalize_addressee_email(value: Any) -> Optional[str]:
    """The ONE spelling of an addressee, for every writer and the reader.

    The sources that feed this column do not agree: portal identities are
    lower-cased at login, rooms stamp a raw ``current_user.email`` and
    ``users.email`` is never normalised. Anything that is not email-shaped is
    None — "unaddressed" has exactly one spelling, and a channel-native id
    (``telegram:<bot>:<id>``) can never land in a column a Files tab is matched
    against, whatever column it arrived in.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or "@" not in v or any(c.isspace() for c in v):
        return None
    local, _, domain = v.rpartition("@")
    if not local or not domain:
        return None
    return v
