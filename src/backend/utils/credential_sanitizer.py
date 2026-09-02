"""
Backend credential sanitization utility.

Defense-in-depth layer that sanitizes execution logs before database persistence.
This catches any credentials that may have bypassed agent-side sanitization.

Note: The primary sanitization should happen on the agent side. This backend
layer is a safety net for cases where the agent may not have sanitized properly.
"""

import base64
import re
import json
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# Secret value patterns - values that look like secrets regardless of context
SECRET_VALUE_PATTERNS = [
    # One rule for the whole `sk-` family (#2208). Three prefix-specific patterns
    # meant every NEW variant shipped unredacted until someone noticed: the
    # generic `sk-[a-zA-Z0-9]{20,}` stops at the first hyphen, so an OpenAI
    # SERVICE-ACCOUNT key (`sk-svcacct-...`) matched none of them and passed
    # through logs and error bodies in clear text. Allowing `-`/`_` after the
    # prefix covers sk-proj-, sk-ant-, sk-svcacct- and whatever comes next.
    r'sk-[A-Za-z0-9][A-Za-z0-9_-]{19,}',
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

# Sensitive key patterns (for key=value pairs)
SENSITIVE_KEY_PATTERNS = [
    r'.*API_KEY.*',
    r'.*API_SECRET.*',
    r'.*TOKEN.*',
    r'.*SECRET.*',
    r'.*PASSWORD.*',
    r'.*CREDENTIAL.*',
    r'.*PRIVATE_KEY.*',
    r'.*AUTH.*',
    r'ANTHROPIC_.*',
    r'OPENAI_.*',
    r'GITHUB_.*',
    r'AWS_.*',
    r'TRINITY_MCP.*',
]

# Compiled patterns
_secret_value_re = [re.compile(p) for p in SECRET_VALUE_PATTERNS]
# (#2398) The compiled `_sensitive_key_re` list lived here and is gone with
# the quadratic test it fed — nothing referenced it any more. Leaving it
# would have kept the removed regexes compiled and reachable by name, which
# is exactly the artifact that let this bug survive #1670: something that
# looks like the live check and is not.

# --- #1661: linear KEY=value redaction -------------------------------------
# The patterns above describe KEY NAMES (`.*TOKEN.*` means "a name containing
# TOKEN"). The stage below used to compose each one into a LINE-scanning regex
# — `(.*TOKEN.*)=(["\']?)([^\s"\']+)\2` — i.e. two unbounded `.*` around a
# literal, re-scanned from every offset of the line. Cost was superlinear in
# line length: ~6.8s for `.*TOKEN.*` alone on an 8 KB line, ~10 CPU-minutes on a
# 44 KB line. Agent-side this pegged a core (#1661); here it burns backend CPU
# on the same input, since this layer sanitizes execution logs before DB write.
#
# One linear pass instead: find `key=value` pairs, then test the (short) key.
# The lookbehind stops the engine retrying every offset inside a long unbroken
# token (a 44 KB base64 blob would otherwise reintroduce quadratic cost).
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
# `re.search(r'(?:.*TOKEN.*)\Z', key)` retries the leading `.*` from every
# offset, so cost is O(len(key)^2). Measured on 3.13: 4 KB key = 0.32s,
# 44 KB key = 40s — for ONE key, per match, per line, on a thread holding the
# GIL. A py-spy watchdog caught it 14 times across three agents, every dump on
# this stack:
#
#     <genexpr> (credential_sanitizer.py)      <- the `any(...)` below
#     _is_sensitive_kv_key
#     _redact_kv_match  ->  sanitize_text  ->  sanitize_dict (recursive)
#     sanitize_subprocess_line  ->  read_stdout  (headless_executor)
#
# The old docstring called `key` "a short KEY= name". It is not: the key is
# whatever preceded an `=` in `_KV_LINE_RE`'s `([^\s"\'=]+)`, which is
# UNBOUNDED — and the stack above reaches here from stream-json tool RESULTS
# via recursive `sanitize_dict`, so multi-KB "keys" are the normal case rather
# than an adversarial one. That wrong assumption is what made a quadratic test
# look harmless.
#
# Every pattern is `.*LITERAL.*` or `LITERAL.*`, and under `.search()` with a
# trailing `\Z` both mean exactly "the key CONTAINS this literal". So the test
# is substring containment: linear, C-level, no backtracking, and byte-for-byte
# the same verdicts (pinned against the old implementation as an oracle over
# thousands of keys in tests/unit/test_2398_sanitizer_key_redos.py).
#
# The literals are DERIVED from the patterns rather than restated, and the
# derivation REFUSES anything that is not a plain literal. A future pattern
# needing real regex semantics therefore fails loudly at import instead of
# being silently reduced to a check that redacts less — the guard #1670 should
# have carried.
# Characters that carry no regex meaning, so a body made only of these is a
# literal and containment is exactly equivalent to the old anchored search.
_PLAIN_LITERAL_RE = re.compile(r'[A-Za-z0-9_-]+')


def _literal_from_pattern(pattern: str) -> str:
    body = pattern
    if body.startswith(".*"):
        body = body[2:]
    # The TRAILING `.*` is what makes containment equivalent, so its absence is
    # refused rather than tolerated (self-review, second pass). Under
    # `.search()` the old form was `(?:BODY)\Z` — anchored at the END — so a
    # pattern with no trailing `.*` meant "the key ENDS WITH this", and
    # containment is wider: a bare `TOKEN` would stop matching only `MYTOKEN`
    # and start matching `TOKEN_SUFFIX` too. That direction redacts MORE, so it
    # is not a leak — but this function exists to refuse SILENT changes of
    # meaning, and accepting one would be the same internal inconsistency that
    # produced #2398: a stated contract that no longer described the code
    # beneath it.
    if not body.endswith(".*"):
        raise ValueError(
            f"sensitive-key pattern {pattern!r} has no trailing `.*`, so it "
            f"meant 'the key ENDS WITH {body!r}'. #2398's containment test is "
            f"WIDER than that — it would also match keys carrying {body!r} in "
            f"the middle. Write it as `{body}.*` if containment is what you "
            f"want, or handle the suffix case explicitly."
        )
    body = body[:-2]
    # An explicit safe set, NOT `re.escape(body) != body`. That was the first
    # form and it is wrong in the dangerous direction for a module imported
    # everywhere: `re.escape` also escapes `-`, which is not a metacharacter
    # outside a character class — so adding a perfectly literal `.*API-KEY.*`
    # would have raised at import and taken the whole process down at boot.
    # `.` IS special, so `A.B` is still (correctly) refused.
    if not body or not _PLAIN_LITERAL_RE.fullmatch(body):
        raise ValueError(
            f"sensitive-key pattern {pattern!r} is not a plain `.*LITERAL.*` / "
            f"`LITERAL.*` form. #2398 replaced the quadratic regex test with "
            f"substring containment; a pattern needing real regex semantics "
            f"must be handled explicitly rather than silently reduced to "
            f"{body!r}."
        )
    return body.upper()


_SENSITIVE_KEY_LITERALS = tuple(
    _literal_from_pattern(p) for p in SENSITIVE_KEY_PATTERNS
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



REDACTION_PLACEHOLDER = "***REDACTED***"

# URL userinfo (e.g. a PAT baked into a git remote: https://x-access-token:ghp_…@github.com/…).
# Git prints the full remote URL in common error lines, so any surface that persists
# raw git output needs this redaction at the exit point (learnings 2026-07-14, #1595).
# Mirrors the agent-side `_redact_url_userinfo` in docker/base-image/agent_server/routers/git.py.
_URL_USERINFO_RE = re.compile(r'://[^/@\s]+@')


def redact_url_userinfo(text: str) -> str:
    """Redact the userinfo component of any URL in `text` (`://user:pat@host` → `://***@host`)."""
    if not text:
        return text
    return _URL_USERINFO_RE.sub('://***@', text)


def scrub_secret(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` (and its b64 form) in ``text``.

    Home for the primitive fork-to-own introduced (trinity-enterprise#93); it
    lives here rather than in a service so the post-creation repo binding
    (ent#109) and any future caller share ONE implementation. ``fork_to_own``
    re-exports it for its existing importers.
    """
    if not text:
        return text or ""
    if secret:
        text = text.replace(secret, "***")
        b64 = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        text = text.replace(b64, "***")
    return text


def scrub_secret_and_urls(text: str, secret: str) -> str:
    """Both passes, because they cover DIFFERENT secrets (ent#109).

    ``scrub_secret`` removes the token the caller is holding right now.
    ``redact_url_userinfo`` removes whatever userinfo is embedded in a remote
    URL the text happens to echo — which on a rebind can be a *stale baked*
    token that is not the caller's at all (learnings 2026-07-14). Dropping
    either pass leaks a real credential into an HTTP error body, the audit
    trail, or the Vector-captured platform log.

    Every path that builds a message out of FOREIGN text — git output, a docker
    exception, GitHub's own error string, an httpx header-validation error that
    echoes the ``Authorization`` value verbatim — goes through this, and there
    is exactly one implementation so a new call site cannot get half of it.
    """
    return redact_url_userinfo(scrub_secret(text or "", secret))


def sanitize_text(text: str) -> str:
    """
    Sanitize sensitive values from text.

    Args:
        text: Text that may contain sensitive values

    Returns:
        Sanitized text with credentials replaced
    """
    if not text:
        return text

    result = text

    # Replace values matching secret patterns
    for pattern in _secret_value_re:
        result = pattern.sub(REDACTION_PLACEHOLDER, result)

    # Redact key=value pairs where key is sensitive.
    # #1661: ONE linear pass (see _KV_LINE_RE) — this used to compile a
    # line-scanning regex per key pattern, costing CPU-minutes on a large line.
    result = _KV_LINE_RE.sub(_redact_kv_match, result)

    return result


def sanitize_dict(data: Dict[str, Any], depth: int = 0, max_depth: int = 10) -> Dict[str, Any]:
    """Recursively sanitize sensitive values in a dictionary."""
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
    """Recursively sanitize sensitive values in a list."""
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


def sanitize_execution_log(execution_log_json: Optional[str]) -> Optional[str]:
    """
    Sanitize an execution log JSON string before database persistence.

    This is the main entry point for sanitizing execution logs in the backend.

    Args:
        execution_log_json: JSON string containing execution log

    Returns:
        Sanitized JSON string
    """
    if not execution_log_json:
        return execution_log_json

    return sanitize_json_string(execution_log_json)


def sanitize_response(response: Optional[str]) -> Optional[str]:
    """
    Sanitize an agent response before database persistence.

    Args:
        response: Agent response text

    Returns:
        Sanitized response
    """
    if not response:
        return response

    return sanitize_text(response)
