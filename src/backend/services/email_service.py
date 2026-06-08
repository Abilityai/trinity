"""
Email service for sending verification codes (Phase 12.2: Public Agent Links).

Supports multiple providers:
- console: Print to console (development)
- smtp: Standard SMTP
- sendgrid: SendGrid API
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Optional

from config import (
    EMAIL_PROVIDER,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    SMTP_FROM,
    SENDGRID_API_KEY,
    RESEND_API_KEY
)

logger = logging.getLogger(__name__)


class EmailService:
    """
    Email service abstraction supporting multiple providers.

    Usage:
        email_service = EmailService()
        await email_service.send_verification_code("user@example.com", "123456")
    """

    def __init__(self):
        self.provider = EMAIL_PROVIDER.lower()
        logger.info(f"Email service initialized with provider: {self.provider}")

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
            if self.provider == "console":
                return self._send_console(to_email, subject, body)
            elif self.provider == "smtp":
                return self._send_smtp(to_email, subject, body, html_body)
            elif self.provider == "sendgrid":
                return await self._send_sendgrid(to_email, subject, body, html_body)
            elif self.provider == "resend":
                return await self._send_resend(to_email, subject, body, html_body)
            else:
                logger.error(f"Unknown email provider: {self.provider}")
                # Fall back to console
                return self._send_console(to_email, subject, body)
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

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; margin: 0; padding: 32px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width: 480px; margin: 0 auto;">
    <tr>
      <td style="background: #ffffff; border-radius: 8px; padding: 40px; box-shadow: 0 1px 4px rgba(0,0,0,0.08);">
        <p style="margin: 0 0 8px 0; font-size: 18px; font-weight: 600; color: #111;">Trinity</p>
        <p style="margin: 0 0 24px 0; font-size: 15px; color: #555;">{intro} Use the code below to verify your identity.</p>
        <div style="background: #f0f0f0; border-radius: 6px; padding: 20px; text-align: center; margin-bottom: 24px;">
          <span style="font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #111; font-family: 'Courier New', monospace;">{code}</span>
        </div>
        <p style="margin: 0 0 16px 0; font-size: 13px; color: #888;">This code expires in 10 minutes.</p>
        <p style="margin: 0; font-size: 13px; color: #aaa;">If you didn&rsquo;t request this code, you can safely ignore this email.</p>
      </td>
    </tr>
  </table>
</body>
</html>"""

    def _send_console(self, to_email: str, subject: str, body: str) -> bool:
        """Print email to console (development mode)."""
        logger.info(f"=" * 60)
        logger.info(f"EMAIL (console mode)")
        logger.info(f"To: {to_email}")
        logger.info(f"Subject: {subject}")
        logger.info(f"-" * 60)
        logger.info(body)
        logger.info(f"=" * 60)
        print(f"\n[EMAIL to {to_email}] Subject: {subject}")
        print(f"Body: {body}\n")
        return True

    def _send_smtp(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None
    ) -> bool:
        """Send email via SMTP."""
        if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
            logger.error("SMTP configuration incomplete. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD")
            # Fall back to console in development
            return self._send_console(to_email, subject, body)

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = SMTP_FROM
            msg["To"] = to_email

            # Attach plain text body
            msg.attach(MIMEText(body, "plain"))

            # Attach HTML body if provided
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            # Connect and send
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to_email, msg.as_string())

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
        html_body: Optional[str] = None
    ) -> bool:
        """Send email via SendGrid API."""
        if not SENDGRID_API_KEY:
            logger.error("SendGrid API key not configured")
            # Fall back to console
            return self._send_console(to_email, subject, body)

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                payload = {
                    "personalizations": [{"to": [{"email": to_email}]}],
                    "from": {"email": SMTP_FROM},
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
            logger.error("httpx not installed, falling back to console")
            return self._send_console(to_email, subject, body)
        except Exception as e:
            logger.error(f"SendGrid send failed: {e}")
            return False

    async def _send_resend(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None
    ) -> bool:
        """Send email via Resend API."""
        if not RESEND_API_KEY:
            logger.error("Resend API key not configured")
            # Fall back to console
            return self._send_console(to_email, subject, body)

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                payload = {
                    "from": SMTP_FROM,
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
                        "Authorization": f"Bearer {RESEND_API_KEY}",
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
            logger.error("httpx not installed, falling back to console")
            return self._send_console(to_email, subject, body)
        except Exception as e:
            logger.error(f"Resend send failed: {e}")
            return False


# Global email service instance
email_service = EmailService()
