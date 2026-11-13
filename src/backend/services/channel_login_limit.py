"""Rate limit for channel sign-in code requests (abilityai/trinity-enterprise#1253).

Telegram ``/login <email>``, WhatsApp ``/login <email>`` and the Slack
``require_email`` email step each email a code. Before minting one, an adapter
asks ``login_code_request_allowed``. Three caps, each over 10 minutes:

* per sender (5): ``{channel}:{sender_id}``, not the adapter's message rate key,
  which in Telegram is per group and per bot.
* per address (3): shared by all channels. Telegram/WhatsApp codes are rows
  in ``email_login_codes``, so they also count against the web login's cap.
  The reverse is deliberately NOT read here: the web login writes rows only
  for whitelisted addresses, so a channel reply that depended on that count
  would tell an attacker which addresses are whitelisted.
* per bot / WhatsApp binding / Slack workspace (20): new Telegram/WhatsApp
  accounts cost nothing, so the sender cap alone leaves volume unbounded.

Requests are counted with ``rate_limiter.check`` keyed on the input, never by
counting code rows: rows exist only for some callers (whitelisted addresses on
the web; outsiders while the ent#849 policy refuses members), and a cap derived
from them leaks which callers those are. Callers run this BEFORE the ent#849
membership branch for the same reason.

Order: sender, then address, then bot. A request refused by an earlier cap is
not charged to a later one, so a sender over its cap cannot burn a victim's
address budget, and requests for a flooded address do not use up the bot's.
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


def login_code_request_allowed(channel: str, bot_key: str, sender_id: str, email: str) -> bool:
    """Record one code request and return False if any cap is exceeded."""
    email = (email or "").strip().lower()

    if not rate_limiter.check(f"channel_login_sender:{channel}:{sender_id}",
                              PER_SENDER, WINDOW_SECONDS).allowed:
        logger.warning("[ent#1253] %s login code suppressed: sender rate limit (sender=%s)",
                       channel, _sender_tag(sender_id))
        return False

    if not rate_limiter.check(f"channel_login_email:{email}", PER_ADDRESS, WINDOW_SECONDS).allowed:
        logger.warning("[ent#1253] %s login code suppressed: rate limit for %r", channel, email)
        return False

    if not rate_limiter.check(f"channel_login_bot:{channel}:{bot_key}", PER_BOT, WINDOW_SECONDS).allowed:
        logger.warning("[ent#1253] %s login code suppressed: bot rate limit (%r)", channel, bot_key)
        return False

    return True
