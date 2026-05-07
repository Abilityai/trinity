"""
Utility helper functions for the Trinity backend.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import List, Tuple


# =============================================================================
# Timezone-Aware Timestamp Utilities
# =============================================================================
#
# IMPORTANT: All timestamps in Trinity MUST be stored and transmitted as UTC
# with the 'Z' suffix to ensure consistent behavior across timezones.
#
# Use these functions instead of datetime.utcnow().isoformat() directly.
# =============================================================================

def utc_now() -> datetime:
    """
    Get current UTC time as timezone-aware datetime.

    Returns:
        datetime with UTC timezone info
    """
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """
    Get current UTC time as ISO 8601 string with 'Z' suffix.

    ALWAYS use this instead of datetime.utcnow().isoformat() to ensure
    JavaScript correctly interprets timestamps as UTC.

    Returns:
        ISO timestamp like "2026-01-15T10:30:00.123456Z"

    Example:
        >>> utc_now_iso()
        '2026-01-15T10:30:00.123456Z'
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def iso_cutoff(hours: int = 0, *, minutes: int = 0) -> str:
    """
    Compute a past-cutoff ISO timestamp in the same format as utc_now_iso().

    Use this when building SQL that filters an `utc_now_iso()`-formatted
    TEXT column by a rolling time window. DO NOT use SQLite's
    `datetime('now', '-N hours')` for this — its output uses a space
    separator and no `Z` suffix, which breaks lexicographic comparison
    against ISO-Z strings at the separator character (issue #476).

    Example:
        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE occurred_at > ?",
            (iso_cutoff(2),)
        )
        cursor.execute(
            "SELECT COUNT(*) FROM events WHERE occurred_at > ?",
            (iso_cutoff(minutes=30),)
        )

    Args:
        hours: Hours in the past (positive). `iso_cutoff(0)` ≈ `utc_now_iso()`.
        minutes: Additional minutes in the past (positive, keyword-only).
            Combine with `hours` for arbitrary windows, or use alone for
            sub-hour windows.

    Returns:
        ISO timestamp matching the format of utc_now_iso().
    """
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)
    ).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def to_utc_iso(dt: datetime) -> str:
    """
    Convert a datetime to UTC ISO 8601 string with 'Z' suffix.

    If the datetime is naive (no timezone), it's assumed to be UTC.
    If it has a timezone, it's converted to UTC first.

    Args:
        dt: datetime object (naive or timezone-aware)

    Returns:
        ISO timestamp with 'Z' suffix
    """
    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    else:
        # Convert to UTC
        utc_dt = dt.astimezone(timezone.utc)
        return utc_dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def parse_iso_timestamp(timestamp: str) -> datetime:
    """
    Parse an ISO timestamp string to datetime.

    Handles timestamps with or without timezone info:
    - "2026-01-15T10:30:00Z" -> UTC
    - "2026-01-15T10:30:00+00:00" -> UTC
    - "2026-01-15T10:30:00" -> assumes UTC (adds UTC timezone)

    Args:
        timestamp: ISO 8601 timestamp string

    Returns:
        timezone-aware datetime in UTC
    """
    # Handle 'Z' suffix
    if timestamp.endswith('Z'):
        timestamp = timestamp[:-1] + '+00:00'

    dt = datetime.fromisoformat(timestamp)

    # If naive, assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def parse_env_content(content: str) -> List[Tuple[str, str]]:
    """
    Parse .env-style content into list of (key, value) tuples.

    Handles:
    - KEY=VALUE
    - KEY="VALUE WITH SPACES"
    - KEY='VALUE WITH SPACES'
    - # comments
    - Empty lines
    """
    results = []

    for line in content.split('\n'):
        line = line.strip()

        if not line or line.startswith('#'):
            continue

        if '=' not in line:
            continue

        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()

        # Remove surrounding quotes if present
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        # Validate key format (must be uppercase with underscores)
        if not key or not re.match(r'^[A-Z][A-Z0-9_]*$', key):
            continue

        if key and value:
            results.append((key, value))

    return results


def infer_service_from_key(key: str) -> str:
    """
    Infer service name from environment variable name.

    Examples:
    - HEYGEN_API_KEY -> heygen
    - TWITTER_API_SECRET -> twitter
    - OPENAI_API_KEY -> openai
    - CLOUDINARY_CLOUD_NAME -> cloudinary
    """
    service_patterns = [
        ('HEYGEN_', 'heygen'),
        ('TWITTER_', 'twitter'),
        ('OPENAI_', 'openai'),
        ('ANTHROPIC_', 'anthropic'),
        ('CLOUDINARY_', 'cloudinary'),
        ('GEMINI_', 'gemini'),
        ('GOOGLE_', 'google'),
        ('GITHUB_', 'github'),
        ('SLACK_', 'slack'),
        ('NOTION_', 'notion'),
        ('GIPHY_', 'giphy'),
        ('METRICOOL_', 'metricool'),
        ('BLOTATO_', 'blotato'),
        ('GOFILE_', 'gofile'),
        ('KLAP_', 'klap'),
        ('AWS_', 'aws'),
        ('AZURE_', 'azure'),
        ('STRIPE_', 'stripe'),
        ('SENDGRID_', 'sendgrid'),
        ('TWILIO_', 'twilio'),
        ('DISCORD_', 'discord'),
        ('LINKEDIN_', 'linkedin'),
        ('INSTAGRAM_', 'instagram'),
        ('TIKTOK_', 'tiktok'),
        ('YOUTUBE_', 'youtube'),
    ]

    key_upper = key.upper()
    for prefix, service in service_patterns:
        if key_upper.startswith(prefix):
            return service

    parts = key.split('_')
    if len(parts) > 1:
        return parts[0].lower()

    return 'custom'


def infer_type_from_key(key: str) -> str:
    """Infer credential type from environment variable name."""
    key_upper = key.upper()

    if '_API_KEY' in key_upper or key_upper.endswith('_KEY'):
        return 'api_key'
    elif '_TOKEN' in key_upper:
        return 'token'
    elif '_SECRET' in key_upper:
        return 'api_key'
    elif '_PASSWORD' in key_upper:
        return 'basic_auth'
    else:
        return 'api_key'


def sanitize_agent_name(name: str) -> str:
    """
    Sanitize agent name for Docker container naming rules.

    Docker container names must match: [a-zA-Z0-9][a-zA-Z0-9_.-]
    """
    # Replace spaces and invalid chars with hyphens
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', name)
    # Remove leading non-alphanumeric chars
    sanitized = re.sub(r'^[^a-zA-Z0-9]+', '', sanitized)
    # Collapse multiple hyphens
    sanitized = re.sub(r'-+', '-', sanitized)
    # Remove trailing hyphens
    sanitized = sanitized.rstrip('-')
    return sanitized.lower()
