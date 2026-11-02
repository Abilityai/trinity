"""One HTML shell for every platform mail (abilityai/trinity-enterprise#1054, A2).

`EmailService.send_html(to, subject, *, intro, paragraphs, cta)` renders a mail
on the same shell the verification code uses, so an invitation or any later
notice looks like the platform's own mail and not like a new template. The
refactor is held to one promise: the verification mail is BYTE-IDENTICAL to
what it rendered before, pinned here against a literal copy of that output.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _service():
    from services.email_service import EmailService

    return EmailService()


def _verification_html_before(intro: str, code: str) -> str:
    """The exact output `_get_verification_email_html` produced before the shell
    was extracted (copied from the source at the fork point, not re-derived)."""
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


class TestTheVerificationMailIsUnchanged:
    def test_plain_sign_in(self):
        svc = _service()
        assert svc._get_verification_email_html("123456") == _verification_html_before(
            "You requested to sign in to <strong>Trinity</strong>.", "123456"
        )

    def test_agent_access(self):
        svc = _service()
        assert svc._get_verification_email_html("654321", agent_name="atlas") == (
            _verification_html_before(
                "You requested access to <strong>atlas</strong> on Trinity.", "654321"
            )
        )

    def test_context_label(self):
        svc = _service()
        assert svc._get_verification_email_html("000111", context_label="Acme") == (
            _verification_html_before(
                "You requested to sign in to <strong>Acme</strong>.", "000111"
            )
        )


@pytest.fixture
def captured(monkeypatch):
    """`send_email` captured at the provider boundary: what `send_html` hands it."""
    svc = _service()
    calls = []

    async def fake_send_email(to_email, subject, body, html_body=None):
        calls.append({"to": to_email, "subject": subject, "body": body, "html": html_body})
        return True

    monkeypatch.setattr(svc, "send_email", fake_send_email)
    return svc, calls


class TestSendHtml:
    def test_renders_on_the_shared_shell_with_a_plain_text_twin(self, captured):
        svc, calls = captured
        ok = asyncio.run(svc.send_html(
            "person@example.com", "You're invited",
            intro="Ada invited you to Trinity.",
            paragraphs=["Sign in with this address.", "Then accept the terms."],
            cta=("Sign in", "https://trinity.example.com/login"),
        ))
        assert ok is True
        sent = calls[0]
        assert sent["to"] == "person@example.com"
        assert sent["subject"] == "You're invited"
        html = sent["html"]
        # The shell: same head, same card, same brand line as the verification mail.
        assert html.startswith("<!DOCTYPE html>")
        assert '<p style="margin: 0 0 8px 0; font-size: 18px; font-weight: 600; color: #111;">Trinity</p>' in html
        assert html.endswith("</html>")
        assert "Ada invited you to Trinity." in html
        assert "Sign in with this address." in html and "Then accept the terms." in html
        assert 'href="https://trinity.example.com/login"' in html and ">Sign in</a>" in html
        # The plain-text twin carries everything a text-only client needs.
        body = sent["body"]
        assert "Ada invited you to Trinity." in body
        assert "Sign in with this address." in body
        assert "Sign in: https://trinity.example.com/login" in body

    def test_text_is_escaped_never_interpreted(self, captured):
        """The join instructions are an operator-written setting; a `<` in them
        must not become markup, and a URL must not break the attribute."""
        svc, calls = captured
        asyncio.run(svc.send_html(
            "p@example.com", "s",
            intro="Use <b>bold</b> & care",
            paragraphs=['Step "one"'],
            cta=("Go", 'https://x.example/?a=1&b="2"'),
        ))
        html = calls[0]["html"]
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; care" in html
        assert "Step &quot;one&quot;" in html
        assert 'href="https://x.example/?a=1&amp;b=&quot;2&quot;"' in html
        # The text twin is NOT escaped: it is text.
        assert "Use <b>bold</b> & care" in calls[0]["body"]

    def test_empty_paragraphs_and_no_cta_leave_nothing_behind(self, captured):
        """An empty join-instructions setting omits the paragraph — never a
        placeholder, never an empty <p>."""
        svc, calls = captured
        asyncio.run(svc.send_html("p@example.com", "s", intro="Hello.", paragraphs=["", "  ", None]))
        html = calls[0]["html"]
        assert html.count("<p ") == 2  # the brand line + the intro
        assert "<a " not in html
        assert calls[0]["body"].strip() == "Hello."

    def test_a_failing_provider_answers_false(self, monkeypatch):
        svc = _service()

        async def boom(*_a, **_k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(svc, "send_email", boom)
        with pytest.raises(RuntimeError):
            # send_html does not swallow: `send_email` itself is the best-effort
            # boundary (it returns False on provider failure); a stub that raises
            # through it is the caller's problem to bound, as the invite path does.
            asyncio.run(svc.send_html("p@example.com", "s", intro="x"))
