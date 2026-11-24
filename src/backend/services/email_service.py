"""
Email service for sending verification codes (Phase 12.2: Public Agent Links).

Supports multiple providers:
- console: Print to console (development)
- smtp: Standard SMTP
- sendgrid: SendGrid API
"""
import asyncio
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Awaitable, Callable, Optional, Sequence, Tuple

from config import (
    EMAIL_PROVIDER,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    SENDGRID_API_KEY,
)

logger = logging.getLogger(__name__)

def _console_body_allowed() -> bool:
    """Read per send, so a test or an operator's restart-free change applies."""
    return os.getenv("EMAIL_CONSOLE_LOG_BODY", "").strip().lower() in ("1", "true", "yes", "on")


# Socket timeout for each SMTP connect/read (#3637). Without it a slow or
# unreachable server holds the send thread until the OS gives up on the socket.
SMTP_TIMEOUT_SECONDS = 30


# -- relay -------------------------------------------------------------------
# An install with no mail provider configured cannot deliver anything itself:
# the `console` provider prints the message to the log. A module may register a
# relay, something able to deliver a small set of STRUCTURED mails for this
# install (a kind and a few short fields, never a subject or a body, so the
# relay words the mail itself). It is consulted only when no provider of this
# install's own can deliver; a configured provider always wins.
_relay: Optional[dict] = None


def register_relay(available: Callable[[], bool], send: Callable[[str, str, dict], Awaitable[bool]]) -> None:
    """Register the relay. ``available()`` is cheap and synchronous (it is read
    on the unauthenticated auth-mode path); ``send(kind, to, fields)`` delivers
    one mail and answers whether it did. Neither may raise."""
    global _relay
    _relay = {"available": available, "send": send}


def clear_relay() -> None:
    global _relay
    _relay = None


class EmailService:
    """
    Email service abstraction supporting multiple providers.

    Usage:
        email_service = EmailService()
        await email_service.send_verification_code("user@example.com", "123456")
    """

    def __init__(self):
        # The env default only. The provider actually used is resolved per send
        # (`_runtime_config`, ent#582) so a Resend key saved in Settings applies
        # without a restart and in every worker.
        self.provider = EMAIL_PROVIDER.lower()
        logger.info(f"Email service initialized with provider: {self.provider}")

    def _runtime_config(self):
        """(provider, resend_key, from_address), Settings → env, read per send.

        Lazy import: `services.settings_service` pulls in `database`, and this
        module must stay importable under the stubbed-`config` unit tests.
        """
        from services.settings_service import settings_service
        return (
            settings_service.get_email_provider(),
            settings_service.get_resend_api_key(),
            settings_service.get_email_from_address(),
        )

    def code_is_logged(self) -> bool:
        """Is a sign-in code readable in the backend log on this install?

        Only when an operator chose the `console` provider AND asked for mail
        bodies to be printed (`EMAIL_CONSOLE_LOG_BODY`, a development setting).
        Everywhere else a code that was not delivered is not written anywhere.
        The Login page reads this (via `GET /api/auth/mode`) so it never sends
        someone to the log for a code that is not there.
        """
        try:
            provider, _, _ = self._runtime_config()
        except Exception:  # noqa: BLE001 — copy only; never fail the mode read
            return False
        return provider == "console" and _console_body_allowed()

    def can_deliver(self) -> bool:
        """Can this install deliver an email right now, by a provider of its own
        or through a registered relay?"""
        return self._provider_can_deliver() or self._relay_available()

    @staticmethod
    def _relay_available() -> bool:
        if not _relay:
            return False
        try:
            return bool(_relay["available"]())
        except Exception:  # noqa: BLE001 - a relay that cannot answer is not there
            logger.warning("[email] the relay could not say whether it is available", exc_info=True)
            return False

    async def _relayed(self, kind: str, to_email: str, fields: dict) -> Optional[bool]:
        """Deliver through the relay when this install has no provider of its
        own. None when the relay does not apply (the caller sends as before);
        otherwise whether it delivered. A relay that fails is NOT reported as
        sent: the `console` provider answering True for a mail nobody received
        is the thing `can_deliver` exists to stop (#3655)."""
        if self._provider_can_deliver() or not self._relay_available():
            return None
        try:
            return bool(await _relay["send"](kind, to_email, fields))
        except Exception as e:  # noqa: BLE001
            logger.warning("[email] relay failed for a %s mail: %s", kind, type(e).__name__)
            return False

    def _provider_can_deliver(self) -> bool:
        """Can a provider of this install's own deliver an email right now? (#3655)

        False when the provider resolved per send is `console`, unknown, or
        missing what it needs. None of those sends anything: `console` logs
        who and what (the body only when `EMAIL_CONSOLE_LOG_BODY` is set), and
        a provider missing its configuration reports the mail as not sent. The
        Login page reads it (install-level, via `GET /api/auth/mode`) to stop
        saying "we sent a code" when nothing was sent. An unreadable config answers
        True, so the page behaves exactly as it did before.
        """
        try:
            provider, resend_key, _ = self._runtime_config()
        except Exception:  # noqa: BLE001 — copy only; never fail the mode read
            logger.warning("[email] could not read the email provider config", exc_info=True)
            return True
        if provider == "resend":
            return bool(resend_key)
        if provider == "smtp":
            return all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD])
        if provider == "sendgrid":
            return bool(SENDGRID_API_KEY)
        return False   # console, or a provider this service does not know

    async def send_verification_code(
        self,
        to_email: str,
        code: str,
        agent_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> bool:
        """
        Send a verification code to an email address.

        Args:
            to_email: Recipient email address
            code: 6-digit verification code
            agent_name: Optional agent name to include in subject/body
            context_label: Optional context label (e.g. "Trinity login") when no agent_name

        Returns:
            True if email was sent successfully, False otherwise
        """
        if agent_name:
            subject = f'Your Trinity access code for "{agent_name}"'
        else:
            subject = f"Your {context_label or 'Trinity'} verification code"

        relayed = await self._relayed("code", to_email, {
            "code": code, "label": f"Trinity ({agent_name})" if agent_name else (context_label or "Trinity")})
        if relayed is not None:
            if not relayed:
                # Say it was not sent, and nothing more: a sign-in code written to the log is one
                # hop from whoever reads the logs. (The `console` provider prints one only when a
                # developer set EMAIL_CONSOLE_LOG_BODY; a relay that refused is not that choice.)
                logger.warning("Verification code not sent: the mail relay refused or did not answer.")
            return relayed

        body = self._get_verification_email_body(code, agent_name=agent_name, context_label=context_label)
        html_body = self._get_verification_email_html(code, agent_name=agent_name, context_label=context_label)

        return await self.send_email(to_email, subject, body, html_body=html_body)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None
    ) -> bool:
        """
        Send an email using the configured provider.

        Args:
            to_email: Recipient email address
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body

        Returns:
            True if email was sent successfully, False otherwise
        """
        try:
            provider, resend_key, from_address = self._runtime_config()
            if provider == "console":
                return self._send_console(to_email, subject, body)
            elif provider == "smtp":
                # smtplib is synchronous: run it off the event loop (#3637).
                return await asyncio.to_thread(
                    self._send_smtp, to_email, subject, body, html_body, from_address
                )
            elif provider == "sendgrid":
                return await self._send_sendgrid(to_email, subject, body, html_body, from_address)
            elif provider == "resend":
                return await self._send_resend(to_email, subject, body, html_body, resend_key, from_address)
            else:
                return self._not_sent(to_email, subject, f"unknown email provider {provider!r}")
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

    def _get_verification_email_body(
        self,
        code: str,
        agent_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> str:
        """Get the plain-text verification email body."""
        if agent_name:
            intro = f'You requested access to {agent_name} on Trinity. Use the code below to verify your identity.'
        elif context_label:
            intro = f'You requested to sign in to {context_label}. Use the code below to verify your identity.'
        else:
            intro = 'You requested to sign in to Trinity. Use the code below to verify your identity.'

        return f"""{intro}

Your verification code is: {code}

This code expires in 10 minutes.

If you didn't request this code, you can safely ignore this email.
"""

    # ------------------------------------------------------------------ #
    # The one HTML shell every platform mail renders on (trinity-enterprise#1054)
    # ------------------------------------------------------------------ #

    _HTML_SHELL_HEAD = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; margin: 0; padding: 32px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width: 480px; margin: 0 auto;">
    <tr>
      <td style="background: #ffffff; border-radius: 8px; padding: 40px; box-shadow: 0 1px 4px rgba(0,0,0,0.08);">
        <p style="margin: 0 0 8px 0; font-size: 18px; font-weight: 600; color: #111;">Trinity</p>
"""
    _HTML_SHELL_TAIL = """
      </td>
    </tr>
  </table>
</body>
</html>"""

    @classmethod
    def _html_shell(cls, *blocks: str) -> str:
        """The card every platform mail sits in: the brand line, then ``blocks``
        (already-rendered HTML lines, 8-space indented), then the closing tags.
        The verification mail is rendered on it byte-for-byte as before — pinned
        by ``tests/unit/test_ent1054_email_html_shell.py``."""
        return cls._HTML_SHELL_HEAD + "\n".join(blocks) + cls._HTML_SHELL_TAIL

    def _get_verification_email_html(
        self,
        code: str,
        agent_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> str:
        """Get the HTML verification email body."""
        if agent_name:
            intro = f'You requested access to <strong>{agent_name}</strong> on Trinity.'
        elif context_label:
            intro = f'You requested to sign in to <strong>{context_label}</strong>.'
        else:
            intro = 'You requested to sign in to <strong>Trinity</strong>.'

        return self._html_shell(
            f'        <p style="margin: 0 0 24px 0; font-size: 15px; color: #555;">{intro} Use the code below to verify your identity.</p>',
            '        <div style="background: #f0f0f0; border-radius: 6px; padding: 20px; text-align: center; margin-bottom: 24px;">',
            f'          <span style="font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #111; font-family: \'Courier New\', monospace;">{code}</span>',
            '        </div>',
            '        <p style="margin: 0 0 16px 0; font-size: 13px; color: #888;">This code expires in 10 minutes.</p>',
            '        <p style="margin: 0; font-size: 13px; color: #aaa;">If you didn&rsquo;t request this code, you can safely ignore this email.</p>',
        )

    async def send_html(
        self,
        to_email: str,
        subject: str,
        *,
        intro: str,
        paragraphs: Optional[Sequence[Optional[str]]] = None,
        cta: Optional[Tuple[str, str]] = None,
        relay: Optional[Tuple[str, dict]] = None,
    ) -> bool:
        """Send a mail rendered on the shared shell, with a plain-text twin.

        ``intro`` is the lead paragraph; ``paragraphs`` follow it (blank or
        ``None`` entries are omitted — an empty operator-written setting leaves
        no placeholder behind); ``cta`` is an optional ``(label, url)`` rendered
        as a button-like link. Every string is TEXT and is HTML-escaped; the
        plain-text body is the same content unescaped, with ``label: url`` for
        the call to action. Delivery goes through ``send_email`` (provider
        resolution, best-effort semantics) unchanged.

        ``relay`` is the same mail as ``(kind, fields)`` for a registered relay,
        which words it itself. Used only when this install has no provider of
        its own; a mail given no ``relay`` is never relayed.
        """
        if relay:
            relayed = await self._relayed(relay[0], to_email, relay[1])
            if relayed is not None:
                return relayed
        from html import escape

        lines = [p.strip() for p in (paragraphs or []) if p and p.strip()]
        blocks = [
            f'        <p style="margin: 0 0 24px 0; font-size: 15px; color: #555;">{escape(intro)}</p>',
        ]
        for line in lines:
            blocks.append(
                f'        <p style="margin: 0 0 16px 0; font-size: 14px; color: #555;">{escape(line)}</p>'
            )
        if cta:
            label, url = cta
            blocks.append(
                '        <p style="margin: 8px 0 0 0;">'
                f'<a href="{escape(url, quote=True)}" style="display: inline-block; background: #111; '
                'color: #ffffff; text-decoration: none; font-size: 14px; font-weight: 600; '
                f'padding: 12px 20px; border-radius: 6px;">{escape(label)}</a></p>'
            )
        html_body = self._html_shell(*blocks)

        text_parts = [intro.strip()] + lines
        if cta:
            text_parts.append(f"{cta[0]}: {cta[1]}")
        body = "\n\n".join(text_parts) + "\n"
        return await self.send_email(to_email, subject, body, html_body=html_body)

    def _send_console(self, to_email: str, subject: str, body: str) -> bool:
        """The `console` provider: an operator chose to send no mail.

        Who and what is logged; the BODY is not. A body carries sign-in codes,
        invite links and whatever an agent wrote, and the backend's log is read
        by more than the person the mail was for (the container log, the log
        files, anything they are shipped to). On a development machine, set
        `EMAIL_CONSOLE_LOG_BODY=true` to print it.
        """
        logger.info("=" * 60)
        logger.info("EMAIL (console mode)")
        logger.info(f"To: {to_email}")
        logger.info(f"Subject: {subject}")
        logger.info("-" * 60)
        if _console_body_allowed():
            logger.info(body)
        else:
            logger.info("(body not logged: set EMAIL_CONSOLE_LOG_BODY=true on a development machine to print it)")
        logger.info("=" * 60)
        return True

    @staticmethod
    def _not_sent(to_email: str, subject: str, why: str) -> bool:
        """A provider that is configured but cannot send. The mail is NOT
        delivered and the send says so: nothing is printed in its place, least
        of all the body. (`can_deliver` already reports this state to the UI.)"""
        logger.error("Email not sent to %s (subject: %s): %s. The message body is not logged.",
                     to_email, subject, why)
        return False

    def _send_smtp(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        from_address: str = "",
    ) -> bool:
        """Send email via SMTP."""
        if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
            return self._not_sent(to_email, subject,
                                  "SMTP configuration incomplete (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD)")

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_address
            msg["To"] = to_email

            # Attach plain text body
            msg.attach(MIMEText(body, "plain"))

            # Attach HTML body if provided
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            # Connect and send
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(from_address, to_email, msg.as_string())

            logger.info(f"Email sent via SMTP to {to_email}")
            return True

        except Exception as e:
            logger.error(f"SMTP send failed: {e}")
            return False

    async def _send_sendgrid(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        from_address: str = "",
    ) -> bool:
        """Send email via SendGrid API."""
        if not SENDGRID_API_KEY:
            return self._not_sent(to_email, subject, "SendGrid API key not configured")

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                payload = {
                    "personalizations": [{"to": [{"email": to_email}]}],
                    "from": {"email": from_address},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": body}]
                }

                if html_body:
                    payload["content"].append({"type": "text/html", "value": html_body})

                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {SENDGRID_API_KEY}",
                        "Content-Type": "application/json"
                    }
                )

                if response.status_code in (200, 202):
                    logger.info(f"Email sent via SendGrid to {to_email}")
                    return True
                else:
                    logger.error(f"SendGrid API error: {response.status_code} - {response.text}")
                    return False

        except ImportError:
            return self._not_sent(to_email, subject, "httpx is not installed")
        except Exception as e:
            logger.error(f"SendGrid send failed: {e}")
            return False

    async def _send_resend(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        api_key: str = "",
        from_address: str = "",
    ) -> bool:
        """Send email via Resend API."""
        if not api_key:
            return self._not_sent(to_email, subject, "Resend API key not configured")

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                payload = {
                    "from": from_address,
                    "to": [to_email],
                    "subject": subject,
                    "text": body
                }

                if html_body:
                    payload["html"] = html_body

                response = await client.post(
                    "https://api.resend.com/emails",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                )

                if response.status_code == 200:
                    logger.info(f"Email sent via Resend to {to_email}")
                    return True
                else:
                    logger.error(f"Resend API error: {response.status_code} - {response.text}")
                    return False

        except ImportError:
            return self._not_sent(to_email, subject, "httpx is not installed")
        except Exception as e:
            logger.error(f"Resend send failed: {e}")
            return False


# Global email service instance
email_service = EmailService()
