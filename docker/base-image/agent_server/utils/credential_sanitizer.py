"""
Credential sanitization utility.

Redacts sensitive values from text output to prevent credential leakage
via execution logs, subprocess output, and agent responses.

Security: This module is critical for preventing credential exposure.
All subprocess output and agent responses should be filtered through this.
"""

import os
import re
import json
import logging
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger(__name__)

# Known sensitive environment variable patterns (case-insensitive)
SENSITIVE_VAR_PATTERNS = [
    r'.*API_KEY.*',
    r'.*API_SECRET.*',
    r'.*TOKEN.*',
    r'.*SECRET.*',
    r'.*PASSWORD.*',
    r'.*CREDENTIAL.*',
    r'.*PRIVATE_KEY.*',
    r'.*AUTH.*',
    r'.*BEARER.*',
    r'ANTHROPIC_.*',
    r'OPENAI_.*',
    r'GITHUB_.*',
    r'GH_.*',
    r'AWS_.*',
    r'AZURE_.*',
    r'GCP_.*',
    r'GOOGLE_.*',
    r'STRIPE_.*',
    r'TWILIO_.*',
    r'SENDGRID_.*',
    r'SLACK_.*',
    r'DISCORD_.*',
    r'TRINITY_MCP.*',
    r'FIBERY_.*',
    r'DATABASE_.*',
    r'DB_.*',
    r'REDIS_.*',
    r'MONGO_.*',
    r'POSTGRES_.*',
    r'MYSQL_.*',
]

# Credential value patterns (values that look like secrets regardless of variable name)
SECRET_VALUE_PATTERNS = [
    r'sk-[a-zA-Z0-9]{20,}',           # OpenAI API keys
    r'sk-proj-[a-zA-Z0-9\-_]{20,}',   # OpenAI project keys
    r'sk-ant-[a-zA-Z0-9\-_]{20,}',    # Anthropic API keys
    r'ghp_[a-zA-Z0-9]{36,}',          # GitHub PAT (fine-grained)
    r'github_pat_[a-zA-Z0-9_]{22,}',  # GitHub PAT (classic)
    r'gho_[a-zA-Z0-9]{36,}',          # GitHub OAuth token
    r'ghs_[a-zA-Z0-9]{36,}',          # GitHub App token
    r'ghr_[a-zA-Z0-9]{36,}',          # GitHub refresh token
    r'xoxb-[a-zA-Z0-9\-]+',           # Slack bot token
    r'xoxp-[a-zA-Z0-9\-]+',           # Slack user token
    r'xoxa-[a-zA-Z0-9\-]+',           # Slack app token
    r'AKIA[A-Z0-9]{16}',              # AWS access key
    r'trinity_mcp_[a-zA-Z0-9]{16,}',  # Trinity MCP keys
    r'Bearer\s+[a-zA-Z0-9\-_.]+',     # Bearer tokens
    r'Basic\s+[a-zA-Z0-9+/=]+',       # Basic auth
]

# Compiled patterns for performance
_sensitive_var_re = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_VAR_PATTERNS]

# --- #1661: linear KEY=value redaction -------------------------------------
# The patterns above describe VARIABLE NAMES (`.*TOKEN.*` means "a name
# containing TOKEN"). Stage 3 below used to compose each one into a
# LINE-scanning regex — `(.*TOKEN.*)=(["\']?)([^\s"\']+)\2` — i.e. two
# unbounded `.*` around a literal, re-scanned from every offset of the line.
# Cost was superlinear in line length: ~6.8s for `.*TOKEN.*` alone on an 8 KB
# line, ~10 CPU-minutes on the 44 KB tool-result lines that triggered #1661.
# That is what pegged a core: the agent-server reader thread was not blocked on
# a pipe, it was *computing* — which is why the #728/#1502 pipe fixes never
# helped, and why it "self-cleared" (the regex eventually finished).
#
# One linear pass instead: find `key=value` pairs, then test the (short) key.
# The key is bounded by delimiters, and the lookbehind stops the engine from
# retrying every offset inside a long unbroken token (a 44 KB base64 blob would
# otherwise reintroduce quadratic cost).
_KV_LINE_RE = re.compile(r'(?<![^\s"\'=])([^\s"\'=]+)=(["\']?)([^\s"\']+)\2')

# A name pattern must match a SUFFIX of the key, not the whole key: the old
# composed regex could start matching mid-token, so `DB_.*` redacted
# `MY_DB_PASS=x`. Anchoring with \Z reproduces exactly that (a `fullmatch`
# rewrite would silently redact LESS — a leak, not a speedup).
_SENSITIVE_KEY_SUFFIX_RE = [
    re.compile(r'(?:' + p + r')\Z', re.IGNORECASE) for p in SENSITIVE_VAR_PATTERNS
]


def _is_sensitive_kv_key(key: str) -> bool:
    """True if `key` (a short `KEY=` name) names a credential."""
    return any(r.search(key) for r in _SENSITIVE_KEY_SUFFIX_RE)


def _redact_kv_match(match: "re.Match") -> str:
    """Redact the value of a sensitive `key=value` pair, keep everything else.

    Mirrors the old replacement byte-for-byte: the value (and its quotes, which
    the old `\1=` replacement also dropped) becomes the placeholder; text
    around the pair is untouched.
    """
    key = match.group(1)
    if _is_sensitive_kv_key(key):
        return f"{key}={REDACTION_PLACEHOLDER}"
    return match.group(0)


_secret_value_re = [re.compile(p) for p in SECRET_VALUE_PATTERNS]

# Cache for known credential values (loaded from environment)
_credential_values: Optional[Set[str]] = None

REDACTION_PLACEHOLDER = "***REDACTED***"


def _load_credential_values() -> Set[str]:
    """
    Load actual credential values from environment and .env file.
    These are the exact values we need to redact.
    """
    values = set()

    # Get values from environment variables matching sensitive patterns
    for var_name, var_value in os.environ.items():
        if var_value and len(var_value) >= 8:  # Skip short values
            for pattern in _sensitive_var_re:
                if pattern.match(var_name):
                    values.add(var_value)
                    break

    # Also read from .env file if it exists
    env_file = os.path.expanduser("~/.env")
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        var_name, _, var_value = line.partition('=')
                        var_name = var_name.strip()
                        var_value = var_value.strip().strip('"').strip("'")
                        if var_value and len(var_value) >= 8:
                            for pattern in _sensitive_var_re:
                                if pattern.match(var_name):
                                    values.add(var_value)
                                    break
        except Exception as e:
            logger.warning(f"Failed to read .env file for credential values: {e}")

    return values


def get_credential_values() -> Set[str]:
    """Get cached set of known credential values."""
    global _credential_values
    if _credential_values is None:
        _credential_values = _load_credential_values()
        logger.debug(f"Loaded {len(_credential_values)} credential values for sanitization")
    return _credential_values


def refresh_credential_values():
    """Refresh the credential values cache (call after credential injection)."""
    global _credential_values
    _credential_values = _load_credential_values()
    logger.info(f"Refreshed credential cache with {len(_credential_values)} values")


def sanitize_text(text: str) -> str:
    """
    Sanitize sensitive values from text.

    This function:
    1. Replaces known credential values with REDACTED
    2. Replaces values matching secret patterns (API keys, tokens)
    3. Redacts values in key=value format where key matches sensitive patterns

    Args:
        text: Text that may contain sensitive values

    Returns:
        Sanitized text with credentials replaced by ***REDACTED***
    """
    if not text:
        return text

    result = text

    # 1. Replace known credential values (exact match)
    for value in get_credential_values():
        if value in result:
            result = result.replace(value, REDACTION_PLACEHOLDER)

    # 2. Replace values matching secret patterns
    for pattern in _secret_value_re:
        result = pattern.sub(REDACTION_PLACEHOLDER, result)

    # 3. Redact key=value pairs where key is sensitive
    # Handle: KEY=value, KEY="value", KEY='value'
    # #1661: ONE linear pass (see _KV_LINE_RE) — this used to compile a
    # line-scanning regex per name pattern, which cost CPU-minutes on a large
    # line and pegged a core.
    result = _KV_LINE_RE.sub(_redact_kv_match, result)

    return result


def sanitize_dict(data: Dict[str, Any], depth: int = 0, max_depth: int = 10) -> Dict[str, Any]:
    """
    Recursively sanitize sensitive values in a dictionary.

    Args:
        data: Dictionary that may contain sensitive values
        depth: Current recursion depth
        max_depth: Maximum recursion depth to prevent infinite loops

    Returns:
        Sanitized dictionary with credentials replaced
    """
    if depth > max_depth:
        return data

    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = sanitize_text(value)
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, depth + 1, max_depth)
        elif isinstance(value, list):
            result[key] = sanitize_list(value, depth + 1, max_depth)
        else:
            result[key] = value
    return result


def sanitize_list(data: List[Any], depth: int = 0, max_depth: int = 10) -> List[Any]:
    """
    Recursively sanitize sensitive values in a list.

    Args:
        data: List that may contain sensitive values
        depth: Current recursion depth
        max_depth: Maximum recursion depth

    Returns:
        Sanitized list with credentials replaced
    """
    if depth > max_depth:
        return data

    result = []
    for item in data:
        if isinstance(item, str):
            result.append(sanitize_text(item))
        elif isinstance(item, dict):
            result.append(sanitize_dict(item, depth + 1, max_depth))
        elif isinstance(item, list):
            result.append(sanitize_list(item, depth + 1, max_depth))
        else:
            result.append(item)
    return result


def sanitize_json_string(json_str: str) -> str:
    """
    Sanitize a JSON string by parsing, sanitizing, and re-serializing.

    Args:
        json_str: JSON string that may contain sensitive values

    Returns:
        Sanitized JSON string
    """
    if not json_str:
        return json_str

    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            sanitized = sanitize_dict(data)
        elif isinstance(data, list):
            sanitized = sanitize_list(data)
        else:
            return sanitize_text(json_str)
        return json.dumps(sanitized)
    except json.JSONDecodeError:
        # If not valid JSON, sanitize as plain text
        return sanitize_text(json_str)


def sanitize_execution_log(log_entries: List[Dict]) -> List[Dict]:
    """
    Sanitize an execution log (list of Claude Code JSON messages).

    This specifically handles the Claude Code stream-json format,
    sanitizing tool outputs, responses, and any embedded content.

    Args:
        log_entries: List of Claude Code JSON messages

    Returns:
        Sanitized log entries
    """
    if not log_entries:
        return log_entries

    return sanitize_list(log_entries)


def sanitize_subprocess_line(line: str) -> str:
    """
    Sanitize a single line of subprocess output.

    Optimized for line-by-line processing of Claude Code stream output.

    Args:
        line: Single line of subprocess output (may be JSON)

    Returns:
        Sanitized line
    """
    if not line:
        return line

    # Try to parse as JSON for structured sanitization
    line_stripped = line.strip()
    if line_stripped.startswith('{') or line_stripped.startswith('['):
        try:
            data = json.loads(line_stripped)
            if isinstance(data, dict):
                sanitized = sanitize_dict(data)
            elif isinstance(data, list):
                sanitized = sanitize_list(data)
            else:
                return sanitize_text(line)
            return json.dumps(sanitized)
        except json.JSONDecodeError:
            pass

    # Fall back to text sanitization
    return sanitize_text(line)
