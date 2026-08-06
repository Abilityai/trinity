"""
Instance identity — the short human label naming WHICH Trinity instance an
outbound alert came from (#1987).

An outbound webhook sink is anonymous by construction: a Slack incoming
webhook carries no sender identity beyond what the payload itself says, so
two instances pointed at one channel emit indistinguishable alerts. The
canary green→red alert (`services/canary_alerts.py`) is the first sink to
hit this — `dev` and `eu2` both post to that channel as of 2026-08-04, the
latter for the #1766 pull/work-stealing soak — but the anonymity is a
property of *webhook sinks*, not of the canary. So the resolver lives here,
where the operator-queue and retention-guard alarms can reuse it verbatim if
either ever grows a webhook.

Resolution order, first usable wins:

  1. ``TRINITY_INSTANCE_NAME`` — explicit operator override, unset by default.
  2. The first DNS label of ``FRONTEND_URL``'s host
     (``https://eu2.abilityai.dev`` → ``eu2``). This tier is why the feature
     needs no fleet-wide ``.env`` rollout to start working: managed instances
     already set ``FRONTEND_URL`` and both compose files already forward it,
     so attribution improves everywhere the moment this ships.
  3. ``installation_id[:8]`` — the durable per-install UUID
     (``operator_intake_service``). Opaque to a human, but an OSS install
     carrying neither of the above still gets *something* that distinguishes
     it from its neighbour, which is the whole point. Note this tier can
     *mint* that id if none exists yet — see ``_label_from_installation_id``.

Returns ``None`` when all three are absent, and never raises. An unlabelled
alert is exactly today's behaviour, whereas an alert that fails to send is
strictly worse than one that cannot name its origin — so every tier degrades
to the next rather than propagating.

Env is read at call time rather than mirrored into ``config.py`` on purpose:
a module-level constant snapshots the environment at import, so the resolved
label would depend on import order relative to whoever set the variable, and
a test could only steer it by reaching in and rebinding the constant. Reading
``os.environ`` per call costs nothing on a path that fires once per green→red
transition, and keeps this module a stdlib-only leaf any sink can import.
"""

import ipaddress
import logging
import os
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_OVERRIDE_ENV = "TRINITY_INSTANCE_NAME"

# Bounds the label so it cannot be the reason a payload is rejected. Slack caps
# a `header` plain_text block at 150 chars and the canary composer's longest
# unlabelled header today is 57 (emoji + G-03's name + " — critical"), so a
# 32-char label lands at 92 — headroom for a longer invariant name later, and
# wide enough for any realistic instance name. The #1880 rendering guard is why
# this matters: an over-length block is a 400 that drops the whole message
# while the transition still counts as alerted — a *silently lost* alert.
_MAX_LABEL_CHARS = 32

# Hostname-shaped punctuation only. Everything else — `<`, `>`, `!`, `|`, `/` —
# becomes a space (whitespace runs collapse below), which is what stops a label
# from forging Slack markup: `<url|text>` renders as a live link and
# `<!channel>` mass-pings everyone in the alert channel, and neither survives
# this filter. ASCII-only alphanumerics are deliberate on top of that:
# `str.isalnum()` is true for Cyrillic too, so allowing it would let a homoglyph
# label impersonate another instance in a message whose entire job is saying
# which instance sent it.
_EXTRA_ALLOWED_CHARS = "._-: "


def sanitize_instance_label(raw: Optional[str]) -> Optional[str]:
    """Coerce an arbitrary string into a safe, bounded instance label.

    Returns ``None`` for anything that sanitizes away to nothing, which the
    resolver reads as "this tier produced nothing" and falls through on — a
    garbage ``TRINITY_INSTANCE_NAME`` therefore degrades to the derived
    ``FRONTEND_URL`` label instead of silently costing the alert its
    attribution.

    Exported because the render boundary calls it again on whatever it is
    handed. That is the same argument ``canary_alerts._mrkdwn_safe`` makes for
    itself: a guarantee that holds only because every producer is
    well-behaved is a guarantee re-verified by hand at every audit, whereas
    one applied where the value crosses into the payload is structural.
    """
    if not raw:
        return None
    text = "".join(
        ch if (ch.isascii() and ch.isalnum()) or ch in _EXTRA_ALLOWED_CHARS else " "
        for ch in str(raw)
    )
    # Collapse whitespace runs so dropped characters don't leave gaps.
    text = " ".join(text.split())
    return text[:_MAX_LABEL_CHARS].strip() or None


def _label_from_frontend_url(url: str) -> Optional[str]:
    """First DNS label of ``FRONTEND_URL``'s host, or the full host for an IP.

    ``https://eu2.abilityai.dev`` → ``eu2``: on managed instances the first
    label *is* the ops slug, which is what an on-call would type to reach it.

    Two shapes are handled rather than assumed away. A bare
    ``eu2.abilityai.dev`` with no scheme parses as a *path* and yields no
    hostname at all, so it is re-parsed as a netloc. And an IP literal keeps
    its full host — the first label of ``10.0.0.5`` is ``10``, which is worse
    than no label because it looks like a name.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url if "//" in url else f"//{url}")
        host = parsed.hostname
    except ValueError:
        # urlparse raises on a malformed IPv6 bracket; a bad URL must not be
        # the reason an alert is unlabelled *or* the reason it doesn't send.
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return sanitize_instance_label(host.split(".")[0])
    return sanitize_instance_label(host)


def _label_from_installation_id() -> Optional[str]:
    """First 8 chars of the durable per-install UUID, or ``None``.

    Imported lazily and wrapped broadly: this reaches `system_settings`
    through `database`, and the alert path must not acquire a DB dependency
    that can fail it. A DB error here means an unlabelled alert, not a lost
    one.

    **This tier can WRITE.** `get_or_create_installation_id` mints and
    persists the UUID when `system_settings` has none, so on an install that
    never completed operator intake the first canary alert is what creates
    it. That is deliberate rather than incidental — a read-only variant would
    return ``None`` on exactly the un-configured OSS install this tier exists
    to label — but it has two consequences worth stating. The id is local
    until the operator opts into intake/telemetry, so minting it transmits
    nothing. And `canary_service` has no leader lock (unlike monitoring
    #1464 / operator-queue #1632), so under `--workers 2` two workers can
    race the read-then-write and land different UUIDs, last-write-wins; the
    cost is bounded to a differing 8-char label across one cycle's alerts on
    a fresh install, and the race is pre-existing in the accessor.
    """
    try:
        from services.operator_intake_service import get_or_create_installation_id

        installation_id = get_or_create_installation_id() or ""
    except Exception:  # noqa: BLE001 — any failure degrades to "no label"
        logger.debug("instance label: installation_id unavailable", exc_info=True)
        return None
    return sanitize_instance_label(installation_id[:8])


def get_instance_label() -> Optional[str]:
    """Resolve this instance's alert label, or ``None`` if nothing identifies it.

    Order: ``TRINITY_INSTANCE_NAME`` → ``FRONTEND_URL`` host → installation-id
    prefix. Never raises.
    """
    override = sanitize_instance_label(os.getenv(_OVERRIDE_ENV, ""))
    if override:
        return override

    derived = _label_from_frontend_url(os.getenv("FRONTEND_URL", "").strip())
    if derived:
        return derived

    return _label_from_installation_id()
