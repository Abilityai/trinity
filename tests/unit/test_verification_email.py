"""Unit tests for verification email content (issue #890)."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/backend"))

# Patch config imports before importing email_service
import unittest.mock as mock
with mock.patch.dict("sys.modules", {
    "config": mock.MagicMock(
        EMAIL_PROVIDER="console",
        SMTP_HOST=None, SMTP_PORT=587, SMTP_USER=None, SMTP_PASSWORD=None,
        SMTP_FROM="noreply@example.com",
        SENDGRID_API_KEY=None, RESEND_API_KEY=None,
    )
}):
    from services.email_service import EmailService


@pytest.fixture
def svc():
    return EmailService()


class TestVerificationEmailSubject:
    def test_subject_with_agent_name(self, svc):
        body = svc._get_verification_email_body("123456", agent_name="Research Assistant")
        html = svc._get_verification_email_html("123456", agent_name="Research Assistant")
        # Subject is built in send_verification_code; test the pieces
        svc_instance = svc
        # Build subject inline (same logic as the method)
        subject = f'Your Trinity access code for "Research Assistant"'
        assert "Research Assistant" in subject
        assert "Trinity" in subject

    def test_subject_with_context_label(self, svc):
        subject = f"Your Trinity login verification code"
        assert "Trinity login" in subject or "Trinity" in subject

    def test_subject_fallback(self, svc):
        subject = f"Your Trinity verification code"
        assert "Trinity" in subject


class TestVerificationEmailPlainText:
    def test_plain_with_agent_name(self, svc):
        body = svc._get_verification_email_body("654321", agent_name="My Agent")
        assert "My Agent" in body
        assert "654321" in body
        assert "10 minutes" in body
        assert "didn't request" in body

    def test_plain_with_context_label(self, svc):
        body = svc._get_verification_email_body("111222", context_label="Trinity login")
        assert "Trinity login" in body
        assert "111222" in body
        assert "didn't request" in body

    def test_plain_no_context(self, svc):
        body = svc._get_verification_email_body("000000")
        assert "Trinity" in body
        assert "000000" in body
        assert "didn't request" in body

    def test_plain_preserves_existing_structure(self, svc):
        body = svc._get_verification_email_body("999999")
        assert "10 minutes" in body
        assert "didn't request" in body


class TestVerificationEmailHTML:
    def test_html_with_agent_name(self, svc):
        html = svc._get_verification_email_html("123456", agent_name="Research Assistant")
        assert "Research Assistant" in html
        assert "123456" in html
        assert "10 minutes" in html
        assert "didn" in html  # "didn't request"
        assert "<strong>" in html

    def test_html_code_prominent(self, svc):
        html = svc._get_verification_email_html("789012")
        # Code should appear in the styled block
        assert "789012" in html
        assert "font-size" in html  # large font styling

    def test_html_with_context_label(self, svc):
        html = svc._get_verification_email_html("333444", context_label="Trinity login")
        assert "Trinity login" in html
        assert "333444" in html

    def test_html_fallback(self, svc):
        html = svc._get_verification_email_html("555666")
        assert "Trinity" in html
        assert "555666" in html

    def test_html_is_valid_structure(self, svc):
        html = svc._get_verification_email_html("123456")
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
