"""Rate limit for channel sign-in code requests (abilityai/trinity-enterprise#1253).

Telegram ``/login <email>``, WhatsApp ``/login <email>`` and the Slack
``require_email`` email step each email a code. Before minting one, an adapter
asks ``login_code_request_allowed``. Three caps, each over 10 minutes:

* per sender (5): ``{channel}:{sender_id}``, not the adapter's message rate key,
  which in Telegram is per group and per bot.
* per address (3): one budget shared by the channels, the web login and MCP
  inline auth (``address_allowed``). Web and MCP charge it BEFORE their
  whitelist / known-address check, so it counts every address alike and its
  refusal says nothing about membership; the web reply is uniform anyway and
  the MCP send runs after the response.
* per bot / WhatsApp binding / Slack workspace (20): new Telegram/WhatsApp
  accounts cost nothing, so the sender cap alone leaves volume unbounded.

The address key holds a sha256 of the normalised address, so its size is
fixed whatever the caller sends; the web route also refuses an address over
254 characters before charging (MCP and the channels already do).

Requests are counted with ``rate_limiter.check`` keyed on the input, never by
counting code rows: rows exist only for some callers (whitelisted addresses on
the web; outsiders while the ent#849 policy refuses members), and a cap derived
from them leaks which callers those are. Callers run this BEFORE the ent#849
membership branch for the same reason.

Order: sender, then address, then bot. A refused request spends nothing: the
caps it passed before the refusing one are refunded. So a sender over its cap
cannot burn a victim's address budget, and requests refused by a flooded bot
do not use up the sender's or the address's.

Accepted risks:
* Named-victim lockout: any per-address cap lets one sender (within its own
  cap) request codes for ``victim@corp.com`` 3 times per 10 minutes and keep
  that address from getting a code on every channel, the web login and MCP.
  Slack had no per-address cap before ent#1253, so this is new there.
* With Redis down the in-process fallback is per worker, so caps multiply by
  the worker count.
* Plus-addressing gets around the address cap; the sender and bot caps hold.
* 4 throwaway accounts can block ``/login`` on one bot for 10 minutes.
"""
import hashlib
import logging

from services import rate_limiter

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 10 * 60
PER_SENDER = 5
PER_ADDRESS = 3
PER_BOT = 20

OVER_LIMIT_REPLY = "Too many sign-in code requests. Try again in 10 minutes."


def _sender_tag(sender_id: str) -> str:
    """Log a hash: a WhatsApp sender id is a phone number."""
    return hashlib.sha256(sender_id.encode()).hexdigest()[:12]


def _address_key(email: str) -> str:
    """One key per normalised address for every surface. Hashed, so the key
    size is fixed whatever the caller sends."""
    normalised = (email or "").strip().lower()
    return f"login_code_email:{hashlib.sha256(normalised.encode()).hexdigest()}"


def address_allowed(email: str) -> bool:
    """Charge one request to the shared per-address budget (web and MCP login)."""
    return rate_limiter.check(_address_key(email), PER_ADDRESS, WINDOW_SECONDS).allowed


def login_code_request_allowed(channel: str, bot_key: str, sender_id: str, email: str) -> bool:
    """Record one code request and return False if any cap is exceeded.
    A refused request is charged to no cap."""
    email = (email or "").strip().lower()
    caps = [
        (f"channel_login_sender:{channel}:{sender_id}", PER_SENDER,
         f"sender rate limit (sender={_sender_tag(sender_id)})"),
        (_address_key(email), PER_ADDRESS, f"rate limit for {email!r}"),
        (f"channel_login_bot:{channel}:{bot_key}", PER_BOT, f"bot rate limit ({bot_key!r})"),
    ]
    charged = []
    for key, limit, reason in caps:
        result = rate_limiter.check(key, limit, WINDOW_SECONDS)
        if not result.allowed:
            for k, r in charged:
                rate_limiter.refund(k, r)
            logger.warning("[ent#1253] %s login code suppressed: %s", channel, reason)
            return False
        charged.append((key, result))
    return True
