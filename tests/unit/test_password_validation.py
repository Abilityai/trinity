"""
Password Validation Unit Tests (test_password_validation.py)

Tests for the password strength validation module.
Covers OWASP ASVS 2.1 complexity requirements (#189).

These are pure unit tests — no backend or Docker required.
"""

import importlib
import sys
from pathlib import Path

import pytest

# tests/utils/ shadows src/backend/utils/, so we import via importlib with an explicit path
_mod_path = Path(__file__).resolve().parent.parent.parent / "src" / "backend" / "utils" / "password_validation.py"
_spec = importlib.util.spec_from_file_location("password_validation", _mod_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

validate_password_strength = _mod.validate_password_strength
is_password_weak = _mod.is_password_weak
COMMON_PASSWORDS = _mod.COMMON_PASSWORDS
PASSWORD_REQUIREMENTS_MESSAGE = _mod.PASSWORD_REQUIREMENTS_MESSAGE
MIN_LENGTH = _mod.MIN_LENGTH

pytestmark = pytest.mark.unit


class TestValidatePasswordStrength:
    """Tests for validate_password_strength()."""

    def test_valid_password_passes_all_rules(self):
        """A strong password meeting all rules should return no errors."""
        assert validate_password_strength("MyStr0ng!Pass99") == []

    def test_exactly_12_chars_valid(self):
        """A 12-character password meeting all rules should pass."""
        assert validate_password_strength("Abcdefgh1!23") == []

    def test_too_short_rejected(self):
        """An 8-character password should be rejected (old min was 8, new is 12)."""
        errors = validate_password_strength("Abc!1234")
        assert any("12 characters" in e for e in errors)

    def test_regression_8_char_compliant_rejected(self):
        """An 8-char password that meets all OTHER rules must still be rejected.

        This catches accidental revert of min-length from 12 back to 8.
        """
        errors = validate_password_strength("V@lidP4s")
        assert any(str(MIN_LENGTH) in e for e in errors)

    def test_exactly_11_chars_rejected(self):
        """11 characters is one short of the minimum."""
        errors = validate_password_strength("V@lidP4ss!1")
        assert len(errors) > 0
        assert any("12" in e for e in errors)

    def test_no_uppercase_rejected(self):
        """Password without uppercase letters should be rejected."""
        errors = validate_password_strength("mystrongpass1!")
        assert any("uppercase" in e for e in errors)

    def test_no_lowercase_rejected(self):
        """Password without lowercase letters should be rejected."""
        errors = validate_password_strength("MYSTRONGPASS1!")
        assert any("lowercase" in e for e in errors)

    def test_no_digit_rejected(self):
        """Password without digits should be rejected."""
        errors = validate_password_strength("MyStrongPass!!")
        assert any("digit" in e for e in errors)

    def test_no_special_char_rejected(self):
        """Password without special characters should be rejected."""
        errors = validate_password_strength("MyStrongPass123")
        assert any("special" in e for e in errors)

    def test_common_password_rejected(self):
        """A known common password should be rejected."""
        errors = validate_password_strength("password")
        assert any("common" in e for e in errors)

    def test_common_password_case_insensitive(self):
        """Common password check should be case-insensitive."""
        errors = validate_password_strength("PASSWORD")
        assert any("common" in e for e in errors)

    def test_common_password_mixed_case(self):
        """Mixed-case version of common password should be rejected."""
        errors = validate_password_strength("PaSsWoRd")
        assert any("common" in e for e in errors)

    def test_all_rules_can_fail_simultaneously(self):
        """A very short, simple password should fail multiple rules."""
        errors = validate_password_strength("abc")
        # Should fail: length, uppercase, digit, special
        assert len(errors) >= 4

    def test_empty_password_fails(self):
        """Empty password should fail multiple rules."""
        errors = validate_password_strength("")
        assert len(errors) >= 4

    def test_non_common_password_not_flagged(self):
        """A unique password should not be flagged as common."""
        errors = validate_password_strength("Xq9!mZp2rT#wY4")
        assert not any("common" in e for e in errors)


class TestIsPasswordWeak:
    """Tests for is_password_weak() convenience wrapper."""

    def test_strong_password_not_weak(self):
        assert is_password_weak("MyStr0ng!Pass99") is False

    def test_weak_password_is_weak(self):
        assert is_password_weak("weak") is True

    def test_common_password_is_weak(self):
        assert is_password_weak("password") is True


class TestCommonPasswordsList:
    """Tests for the COMMON_PASSWORDS frozenset."""

    def test_is_frozenset(self):
        """Common passwords should be a frozenset for immutability and O(1) lookup."""
        assert isinstance(COMMON_PASSWORDS, frozenset)

    def test_all_lowercase(self):
        """All entries should be lowercase for case-insensitive matching."""
        for pw in COMMON_PASSWORDS:
            assert pw == pw.lower(), f"Common password '{pw}' is not lowercase"

    def test_contains_well_known_passwords(self):
        """Should contain the most obvious weak passwords."""
        for pw in ["password", "123456", "admin", "qwerty"]:
            assert pw in COMMON_PASSWORDS, f"'{pw}' missing from common passwords list"

    def test_reasonable_size(self):
        """List should have a reasonable number of entries (50-200)."""
        assert 50 <= len(COMMON_PASSWORDS) <= 200


class TestConstants:
    """Tests for module constants."""

    def test_requirements_message_exists(self):
        assert isinstance(PASSWORD_REQUIREMENTS_MESSAGE, str)
        assert len(PASSWORD_REQUIREMENTS_MESSAGE) > 0

    def test_requirements_message_mentions_key_rules(self):
        msg = PASSWORD_REQUIREMENTS_MESSAGE.lower()
        assert "12" in msg
        assert "uppercase" in msg
        assert "lowercase" in msg
        assert "digit" in msg
        assert "special" in msg

    def test_min_length_is_12(self):
        assert MIN_LENGTH == 12
