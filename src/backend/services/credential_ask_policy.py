"""Which environment-variable NAME a credential ask may carry
(trinity-enterprise#1066).

An agent that needs a secret asks for it by name — one `credential` ask whose
`context.credential_name` says which `.env` variable the person is filling
through the Workspace secret field. The value itself never travels through an
ask; the name does, so the name is what this leaf validates.

Three rules, each a named reason the sink turns into a 422
`invalid_credential_name`:

* `not_a_name` — not the variable-name charset the agent's own `.env` reader
  sets (`credential_charset.is_credential_var_name`: a detector must never be
  narrower than the substitution engine it audits, ent#128);
* `too_long` — over `CREDENTIAL_NAME_MAX` characters;
* `reserved` — a name the agent runtime refuses to take from `.env` at all
  (`execution_env.PROTECTED_KEYS`, its prefix families, the routing and
  per-runtime endpoint names — a line the agent would silently drop) or one
  the PLATFORM writes (its MCP key, backend URL, agent name, the model
  credential and the subscription token — a line a person must never be asked
  to overwrite from a chat).

The agent-side set is MIRRORED here, not imported: the agent server ships as
its own image and structurally cannot be imported from the backend (Invariant
#5). `tests/unit/test_ent1066_credential_ask_kind.py` holds the two in parity
by loading the agent module by file path, so a name added to the agent's
protected set without being added here fails CI rather than a person.
"""

from __future__ import annotations

from typing import Optional

from services.credential_charset import is_credential_var_name

#: The longest name a credential ask may carry. `skill_packaging.ENV_KEY_RE`
#: caps its own keys at 64 too; a longer name is a mistake, not a credential.
CREDENTIAL_NAME_MAX = 64

#: Names the PLATFORM writes into an agent's environment. A credential ask may
#: never name one: the person would be overwriting the platform's own wiring.
PLATFORM_WRITTEN_ENV_NAMES = frozenset({
    "TRINITY_MCP_API_KEY", "TRINITY_BACKEND_URL", "TRINITY_AGENT_NAME",
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
})

#: Mirror of the agent image's `execution_env.PROTECTED_KEYS`.
_AGENT_PROTECTED_KEYS = frozenset({
    "PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "BASH_ENV", "ENV", "IFS",
    "CLAUDE_CODE_SHELL_PREFIX",
    "AGENT_RUNTIME",
    "NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
    "GIT_EDITOR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_NOSYSTEM", "GIT_ASKPASS", "GIT_PROXY_COMMAND",
    "GEMINI_CLI_HOME", "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
})

#: Mirror of the agent image's `execution_env.ROUTING_KEYS`.
_AGENT_ROUTING_KEYS = frozenset({
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE",
})

#: Mirror of the union of the agent image's `execution_env.RUNTIME_ENDPOINT_KEYS`
#: values — every runtime's, since a credential ask does not know which
#: runtime the agent runs and an endpoint name is never a credential.
_AGENT_RUNTIME_ENDPOINT_KEYS = frozenset({
    # claude
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_API_BASE_URL",
    # codex
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
    "CODEX_AUTHAPI_BASE_URL", "CODEX_EXEC_SERVER_URL",
    "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL", "CODEX_CA_CERTIFICATE",
    # gemini
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL", "CODE_ASSIST_ENDPOINT",
})

#: Every name a credential ask may not carry.
RESERVED_ENV_NAMES = frozenset(
    PLATFORM_WRITTEN_ENV_NAMES | _AGENT_PROTECTED_KEYS | _AGENT_ROUTING_KEYS
    | _AGENT_RUNTIME_ENDPOINT_KEYS
)

#: Mirror of the agent image's `execution_env.PROTECTED_KEY_PREFIXES` — an
#: unbounded family, matched by prefix.
RESERVED_ENV_NAME_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def credential_name_problem(name: object) -> Optional[str]:
    """Why ``name`` may not be the variable a credential ask fills — one of
    ``not_a_name`` / ``too_long`` / ``reserved`` — or None when it may."""
    if not is_credential_var_name(name):
        return "not_a_name"
    assert isinstance(name, str)
    if len(name) > CREDENTIAL_NAME_MAX:
        return "too_long"
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_NAME_PREFIXES):
        return "reserved"
    return None


def credential_name_of(context: object) -> Optional[str]:
    """The name a stored credential row carries, or None when the row carries
    none that passes this policy. Read at answer time as well as at raise time:
    a row can arrive through the file channel, which validates nothing."""
    if not isinstance(context, dict):
        return None
    name = context.get("credential_name")
    return name if credential_name_problem(name) is None else None
