"""The credential-variable charset a *detector* must accept (ent#128).

WHAT THIS IS
------------
`CREDENTIAL_DETECTOR_CHARSET` is the widest variable-name charset a detector has
to accept **so that it is never narrower than the substitution engine it
audits**. It is deliberately NOT "the variable-name charset every Trinity
surface agrees on" — no such charset exists, and believing it does is how a
mechanical alignment pass widens a security gate.

WHY IT EXISTS
-------------
Trinity's actual substitution engines impose no charset at all. The agent-side
writer does a plain `str.replace` and the `.env` writer slices `env_val[2:-1]`,
so `${my_var}` IS substituted at runtime. Two compatibility checks then audited
that runtime behaviour through an uppercase-only reader, which made them read
NARROWER than what they audit:

  * K-001 (HARD) compared `.mcp.json.template`'s `${VAR}`s against an
    uppercase-only view of `.env.example`, so a documented lowercase variable was
    invisible and the check HARD-failed a correct template;
  * the deploy-time advisory warning (`collect_mcp_credential_warnings`) had the
    same blind spot in both directions.

A detector narrower than the mechanism is a false positive when it under-reads
the documentation and a false negative when it under-reads the reference. This
constant is the one place that decision lives.

MEMBERS (detectors and their validators — all four adopt this)
--------------------------------------------------------------
  * `compatibility.static_checks._VAR_RE`          — `${VAR}` finder
  * `compatibility.static_checks._env_example_vars` — `.env.example` name validator
  * `template_service.extract_env_vars_from_mcp_json` — `${VAR}` finder
  * `template_service.extract_credentials_from_env_example` — name validator

NON-MEMBERS — these must NOT adopt it. Read this list before "aligning" anything
------------------------------------------------------------------------------
  * `mcp_validator._ENV_VAR_REF_RE` (`^[A-Z][A-Z0-9_]*$`) — a **fail-closed
    security gate**, not a detector. It is paired with a deliberately WIDEST
    finder (`_ENV_VAR_SUBSTRING_RE = \\$\\{([^}]*)\\}`) so nothing escapes
    detection and everything detected must pass a narrow allowlist. Reached from
    `.mcp.json` injection (→ 400), `.credentials.enc` import and deploy-local.
    Widening it does not fix a false positive — it ADMITS input that is
    currently rejected. That is the opposite of this module's purpose.
  * `skill_packaging.ENV_KEY_RE` (`^[A-Z][A-Z0-9_]{0,63}$`) — an adjacent domain
    (the ent#183 skill-package frontmatter contract) with its own length cap.
    It gates interpolation before a probe runs; it is not auditing credential
    substitution.
  * `static_checks._ASSIGN_RE` — a secret-*scanner*, not a name validator. Its
    `[A-Za-z0-9_]*` immediately before the `(?:KEY|SECRET|...)` alternation is
    the ambiguous-quantifier shape behind an already-FIXED `py/polynomial-redos`
    alert, on a path fed by agent-supplied text. Do not touch it.
  * `compatibility.static_checks.c_d006` — metric names, a different vocabulary
    that happens to look similar.
  * `utils.helpers` `.env` key parsing — a value reader that silently `continue`s
    on a non-matching key; changing it changes which credentials get read, not
    which get detected.

NAMED RESIDUALS — the gap is the brace grammar, not the charset
---------------------------------------------------------------
Adopting this charset does NOT make the finders equivalent to the runtime. Two
shapes are substituted (or mis-substituted) at runtime and remain invisible to
`${...}` finders that require a bare name followed by `}`:

  * `${my-key}` — a hyphen is not in any name charset, so the finder drops it
    SILENTLY while the agent-side `str.replace` engine would still substitute it.
  * `${VAR:-default}` — `generate_credential_files` slices `env_val[2:-1]`, so the
    key becomes `VAR:-default`, misses, and the value is replaced with `""`. This
    is a mis-substitution, not merely an unhandled form, and it is tracked
    separately.

Claim only what is true: the four members agree on which *valid names* they
accept. They do not claim finder/validator equivalence beyond that.
"""

from __future__ import annotations

import re

# The charset fragment itself, for callers composing their own pattern.
# ASCII letters + underscore to start, then letters/digits/underscore.
CREDENTIAL_DETECTOR_CHARSET = r"[A-Za-z_][A-Za-z0-9_]*"

# Anchored validator: "is this whole string a variable name?"
#
# `\Z` (not `$`) because `$` also matches before a trailing newline, so `"FOO\n"`
# would validate. Every call site `.strip()`s first, which is why the narrowing is
# unreachable today — keep the strip, or this becomes load-bearing.
CREDENTIAL_DETECTOR_NAME_RE = re.compile(rf"^{CREDENTIAL_DETECTOR_CHARSET}\Z")

# Finder: every `${VAR}` reference in a blob of text/JSON.
CREDENTIAL_DETECTOR_REF_RE = re.compile(rf"\$\{{({CREDENTIAL_DETECTOR_CHARSET})\}}")


def is_credential_var_name(name: str) -> bool:
    """True if `name` is a variable name a detector must accept.

    Non-strings are rejected rather than coerced: a caller handing this a parsed
    YAML fragment has a shape bug that must surface as `False`, not as a
    `TypeError` deep inside a compatibility check.
    """
    return isinstance(name, str) and bool(CREDENTIAL_DETECTOR_NAME_RE.match(name))
