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
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

def _unquote_env_value(value: str) -> str:
    """The `.env` decoder, borrowed rather than re-implemented (#2023).

    This module builds the REDACTION set, so a decoder that disagrees with the
    one executions use means a credential is delivered in one form and searched
    for in another — i.e. silently not redacted from logs. It was a fourth copy
    of `.strip('"').strip("'")`; before the encoding became reversible the two
    agreed (both corrupted identically) and redaction happened to work.

    Resolved by PATH rather than by import name because this file is loaded in
    three shapes — as `agent_server.utils.credential_sanitizer` (package),
    flat, and standalone by path in the tests — and only the path is the same
    in all three. `services.execution_env` in particular resolves to the
    BACKEND package in a test process, which is a different module entirely.
    Falls back to the old positional strip only if the sibling cannot be
    loaded at all, so log redaction degrades rather than disappearing.
    """
    global _unquote_impl
    if _unquote_impl is None:
        try:
            import importlib.util as _ilu

            _target = Path(__file__).resolve().parents[1] / "services" / "execution_env.py"
            _spec = _ilu.spec_from_file_location("_agent_execution_env", _target)
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _unquote_impl = _mod.unquote_env_value
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not load the shared .env decoder (%s); falling back to "
                "positional stripping — quote-bearing credentials may not be "
                "redacted from logs", exc,
            )
            _unquote_impl = lambda v: v.strip().strip('"').strip("'")  # noqa: E731
    return _unquote_impl(value)


_unquote_impl = None

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
    # One rule for the whole `sk-` family (#2208). Three prefix-specific patterns
    # meant every NEW variant shipped unredacted until someone noticed: the
    # generic `sk-[a-zA-Z0-9]{20,}` stops at the first hyphen, so an OpenAI
    # SERVICE-ACCOUNT key (`sk-svcacct-...`) matched none of them and passed
    # through logs and error bodies in clear text. Allowing `-`/`_` after the
    # prefix covers sk-proj-, sk-ant-, sk-svcacct- and whatever comes next.
    r'sk-[A-Za-z0-9][A-Za-z0-9_-]{19,}',
    r'ghp_[a-zA-Z0-9]{36,}',          # GitHub PAT (classic, ~40 chars)
    r'github_pat_[a-zA-Z0-9_]{22,}',  # GitHub PAT (fine-grained, ~93 chars)
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
# The lookbehind is load-bearing, not decoration: it is what keeps this LINEAR.
# Without it the engine retries the key at every offset inside a long non-matching
# run, which is exactly the quadratic shape this issue is about. CodeQL reports
# `py/polynomial-redos` here because its model ignores lookbehinds; the adversarial
# cases it names (`!`*n, `!=`+`!=!`*n) are pinned as tests in
# tests/unit/test_1661_sanitizer_linear.py and run in ~1ms at 64 KB. Do not
# "simplify" the lookbehind away.
_KV_LINE_RE = re.compile(r'(?<![^\s"\'=])([^\s"\'=]+)=(["\']?)([^\s"\']+)\2')

# A name pattern must match a SUFFIX of the key, not the whole key: the old
# composed regex could start matching mid-token, so `DB_.*` redacted
# `MY_DB_PASS=x`. Anchoring with \Z reproduces exactly that (a `fullmatch`
# rewrite would silently redact LESS — a leak, not a speedup).
#
# --- #2398: the same quadratic shape, one call deeper -----------------------
# #1670 made the LINE scan linear (#1661) and left THIS test quadratic:
# `re.search(r'(?:.*TOKEN.*)\Z', key)` retries `.*` from every offset, so cost
# is O(len(key)^2). Measured on 3.13: 4 KB key = 0.32s, 44 KB key = 40s — for
# ONE key, per match, per line. py-spy caught it 14 times across three agents,
# every dump on this stack:
#
#     <genexpr> (credential_sanitizer.py)      <- the `any(...)` below
#     _is_sensitive_kv_key
#     _redact_kv_match  ->  sanitize_text  ->  sanitize_dict (recursive)
#     sanitize_subprocess_line  ->  read_stdout  (headless_executor)
#
# The old docstring called `key` "a short KEY= name". It is not. The key is
# whatever preceded an `=` in `_KV_LINE_RE`'s `([^\s"\'=]+)`, which is
# UNBOUNDED — and the stack above reaches here from stream-json tool RESULTS,
# so multi-KB "keys" are the normal case, not an adversarial one. That wrong
# assumption is what made a quadratic test look harmless.
#
# Every pattern is `.*LITERAL.*` or `LITERAL.*`, and under `.search()` with a
# trailing `\Z` both mean exactly "the key CONTAINS this literal". So the test
# is substring containment: linear, C-level, no backtracking, and byte-for-byte
# the same verdicts (pinned against the old implementation as an oracle over
# 5,420 keys in tests/unit/test_2398_sanitizer_key_redos.py).
#
# The literals are DERIVED from the patterns rather than restated, and the
# derivation REFUSES anything that is not a plain literal. A future pattern
# needing real regex semantics therefore fails loudly at import instead of
# being silently reduced to a check that redacts less.
def _literal_from_pattern(pattern: str) -> str:
    body = pattern
    if body.startswith(".*"):
        body = body[2:]
    if body.endswith(".*"):
        body = body[:-2]
    if not body or re.escape(body) != body:
        raise ValueError(
            f"sensitive-key pattern {pattern!r} is not a plain `.*LITERAL.*` / "
            f"`LITERAL.*` form. #2398 replaced the quadratic regex test with "
            f"substring containment; a pattern needing real regex semantics "
            f"must be handled explicitly rather than silently reduced to "
            f"{body!r}."
        )
    return body.upper()


_SENSITIVE_KEY_LITERALS = tuple(
    _literal_from_pattern(p) for p in SENSITIVE_VAR_PATTERNS
)


def _is_sensitive_kv_key(key: str) -> bool:
    """True if `key` names a credential.

    `key` is whatever preceded an `=` in the scanned text — NOT necessarily a
    short env-var name (#2398). Containment, not regex: see the note above.
    """
    upper = key.upper()
    return any(lit in upper for lit in _SENSITIVE_KEY_LITERALS)


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
                        # #2023: the SAME decoder the execution env uses. This
                        # was a fourth copy of `.strip('"').strip("'")`, and it
                        # is the one that builds the REDACTION set — so once the
                        # writer/reader pair became reversible, a credential
                        # containing a quote was held here in its escaped form
                        # while executions received the unescaped one, and
                        # `sanitize_text()` stopped redacting it from logs.
                        # Before that fix the two agreed (both corrupted
                        # identically), so redaction happened to work.
                        var_value = _unquote_env_value(var_value.strip())
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


# URL userinfo: `scheme://user:secret@host/...` → `scheme://***@host/...`
#
# Shape-INDEPENDENT, and that is the point. The value patterns above only catch
# tokens whose prefix we already know; this catches any credential embedded in a
# URL, including formats that do not exist yet. Trinity writes git remotes as
# `https://oauth2:<PAT>@github.com/...` (and `x-access-token:` in the field), so
# every git subprocess carries a live PAT in its argv.
#
# Anchored on `://` and stopping at the first `@` before any `/`, so a path or
# query containing `@` is untouched.
_URL_USERINFO_RE = re.compile(r"(://)[^/@\s]+@")


def redact_url_userinfo(text: str) -> str:
    """Strip credentials from URLs. Promoted from `routers/git.py` (#1595) so
    every log exit can use it, not just the two git-stderr sinks it was written
    for."""
    if not text:
        return text
    return _URL_USERINFO_RE.sub(r"\1***@", text)


def sanitize_cmdline(cmd: str) -> str:
    """Sanitize a process command line before it reaches a log sink.

    THE entry point for anything read out of `/proc/<pid>/cmdline` or otherwise
    derived from argv. A reaped `git remote-https` carries the PAT in argv, and
    logging it verbatim wrote a live credential into the container log, into
    Vector's persisted archives, and into any snapshot of the log volume.

    Log level is not a mitigation: agent logs are routed by container class with
    no level filter, so INFO and WARNING persist identically.
    """
    return sanitize_text(redact_url_userinfo(cmd))


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

    # 0. Redact URL userinfo FIRST. It is shape-independent, so it covers
    #    credentials the value patterns below would miss entirely — including a
    #    PAT format that does not exist yet.
    result = redact_url_userinfo(result)

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
