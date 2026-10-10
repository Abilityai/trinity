"""
Password strength validation following OWASP ASVS 2.1.

Deliberately uses composition rules (uppercase, lowercase, digit, special character)
per OWASP ASVS 2.1 rather than NIST 800-63B (length-only + dictionary), because the
pentest report specifically requested complexity requirements.

The common passwords list is a supplemental check — not a comprehensive breach database.
It catches the most trivially guessable passwords as a first-pass defense.
"""

import re

# Top ~100 most common passwords from SecLists / OWASP curated lists.
# Case-insensitive: all entries stored lowercase, input lowercased before lookup.
# This is a supplemental check, not a comprehensive breach database.
COMMON_PASSWORDS: frozenset[str] = frozenset({
    "123456", "password", "12345678", "qwerty", "123456789",
    "12345", "1234", "111111", "1234567", "dragon",
    "123123", "baseball", "abc123", "football", "monkey",
    "letmein", "shadow", "master", "666666", "qwertyuiop",
    "123321", "mustang", "1234567890", "michael", "654321",
    "superman", "1qaz2wsx", "7777777", "121212", "000000",
    "qazwsx", "123qwe", "killer", "trustno1", "jordan",
    "jennifer", "zxcvbnm", "asdfgh", "hunter", "buster",
    "soccer", "harley", "batman", "andrew", "tigger",
    "sunshine", "iloveyou", "2000", "charlie", "robert",
    "thomas", "hockey", "ranger", "daniel", "starwars",
    "klaster", "112233", "george", "computer", "michelle",
    "jessica", "pepper", "1111", "zxcvbn", "555555",
    "11111111", "131313", "freedom", "777777", "pass",
    "maggie", "159753", "aaaaaa", "ginger", "princess",
    "joshua", "cheese", "amanda", "summer", "love",
    "ashley", "nicole", "chelsea", "biteme", "matthew",
    "access", "yankees", "987654321", "dallas", "austin",
    "thunder", "taylor", "matrix", "welcome", "welcome1",
    "admin", "admin123", "adminadmin", "administrator",
    "passw0rd", "p@ssw0rd", "password1", "password123",
    "changeme", "letmein123", "qwerty123", "abc1234",
    "trinity", "trinity123",
})

MIN_LENGTH = 12
MAX_LENGTH = 128

# Single source of truth for the generic error message returned to unauthenticated callers.
PASSWORD_REQUIREMENTS_MESSAGE = (
    "Password does not meet requirements: minimum 12 characters, "
    "must include uppercase, lowercase, digit, and special character, "
    "must not be a common password"
)


def validate_password_strength(password: str) -> list[str]:
    """Validate password against OWASP ASVS 2.1 complexity requirements.

    Returns a list of failure reasons. Empty list means the password is valid.
    """
    errors: list[str] = []

    if len(password) < MIN_LENGTH:
        errors.append(f"Must be at least {MIN_LENGTH} characters (currently {len(password)})")

    if len(password) > MAX_LENGTH:
        errors.append(f"Must be at most {MAX_LENGTH} characters")

    if not re.search(r"[A-Z]", password):
        errors.append("Must contain at least one uppercase letter (A-Z)")

    if not re.search(r"[a-z]", password):
        errors.append("Must contain at least one lowercase letter (a-z)")

    if not re.search(r"[0-9]", password):
        errors.append("Must contain at least one digit (0-9)")

    if not re.search(r"[^A-Za-z0-9]", password):
        errors.append("Must contain at least one special character (!@#$%^&*...)")

    if password.lower() in COMMON_PASSWORDS:
        errors.append("Password is too common — choose something less predictable")

    return errors
