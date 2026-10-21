"""Template-declared provider block for the generated OpenCode config (#2981).

OpenCode routes ``-m <provider>/<model>`` through a provider entry. Providers
that models.dev knows about (``openrouter``, ``anthropic``, ``openai``, …)
resolve themselves from a fixed env var; a self-hosted OpenAI-compatible
endpoint does not — it has to be declared, with the npm package that drives
it and the base URL to reach it. A template declares it once::

    runtime:
      type: opencode
      model: local/qwen3:27b
      provider:
        id: local
        npm: "@ai-sdk/openai-compatible"
        base_url: http://host.docker.internal:11434/v1    # docstring example only
        api_key_env: LOCAL_API_KEY
        models:
          qwen3:27b: {cost: {input: 0.0, output: 0.0}}   # cost optional (ruling T6)

The backend serializes that block to ``AGENT_RUNTIME_PROVIDER`` (JSON) at
create (ruling T5); THIS module reads it per turn and renders::

    "provider": {
      "local": {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "http://…/v1", "apiKey": "{env:LOCAL_API_KEY}"},
        "models": {"qwen3:27b": {"cost": {…}}}
      }
    }

Secret rule (decision 6)
------------------------
``apiKey`` is the ``{env:NAME}`` *reference*, never ``os.environ[NAME]``'s
value. OpenCode substitutes ``{env:VAR}`` from the subprocess environment
when it loads the config text (``packages/opencode/src/config/variable.ts``,
pinned v1.18.32), so the per-turn ``opencode.json`` carries the variable's
NAME and nothing a backup, a log line or a shared folder could leak.

Validation at the harness boundary
----------------------------------
The block is validated here, per turn, and a bad field fails NAMED — the
message carries the field, so an operator reads "api_key_env" and not
OpenCode's own opaque "unknown provider". ``npm`` is an ALLOWLIST (only the
OpenAI-compatible driver, pre-seeded into the image — a package name that is
not seeded would make OpenCode reach the npm registry from a cold container).
``base_url`` must be an absolute http(s) URL. There is deliberately NO gateway
probing and NO host discovery here (decision 5): the URL is what the template
says it is.

Key presence (ruling T4)
------------------------
Before the subprocess starts, the provider half of the turn's model is
resolved to the env var that must carry its key: the custom block's
``api_key_env`` for the block's own ``id``, and the fixed names for the three
models.dev providers Trinity pre-checks (:data:`NATIVE_KEY_ENV`). A required
var that is unset or empty is reported by NAME and the turn fails 503 at the
boundary — never a silent fallback to another provider. Any other models.dev
provider gets no pre-check: its auth failure arrives as a stream ``error``
event and is classified 503 by the parser. The OpenRouter native path renders
NO provider block — OpenCode knows it from models.dev — only the check.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: The template's ``runtime.provider`` block, JSON-serialized at create.
ENV_PROVIDER = "AGENT_RUNTIME_PROVIDER"

#: Driver packages a template may name. Pre-seeded into the image; OpenCode
#: does NOT fetch a provider package on demand from a cold container.
NPM_ALLOWLIST = frozenset({"@ai-sdk/openai-compatible"})

#: models.dev providers whose key OpenCode reads from one fixed env var. The
#: pre-check names that var when it is missing (ruling T4). A provider not
#: listed here is not pre-checked — its auth failure is classified from the
#: stream instead.
NATIVE_KEY_ENV: Dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][\w./:@+-]*$")


class OpenCodeProviderConfigError(ValueError):
    """A ``runtime.provider`` block that cannot be rendered. The message names
    the field (quality bar 6: a bad input fails with a specific reason)."""


def load_provider_block(environ: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """The raw block from :data:`ENV_PROVIDER`, or ``None`` when unset/blank.

    The ONE reader of that variable: the cost policy in ``opencode_runtime``
    reads through here too, so the serialized shape has a single parser.
    Raises :class:`OpenCodeProviderConfigError` when the value is not a JSON
    object — a malformed block is a configuration fault, named as such.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_PROVIDER) or "").strip()
    if not raw:
        return None
    try:
        block = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenCodeProviderConfigError(
            f"{ENV_PROVIDER} is not valid JSON ({exc}); it must be the "
            "template's runtime.provider object"
        ) from exc
    if not isinstance(block, dict):
        raise OpenCodeProviderConfigError(
            f"{ENV_PROVIDER} must be a JSON object (the template's "
            f"runtime.provider block), got {type(block).__name__}"
        )
    return block


def validate_provider_block(block: Mapping) -> dict:
    """Return the normalized block or raise a NAMED error.

    Every rule names its field in the message: ``id`` (``^[a-z0-9_-]+$``),
    ``npm`` (allowlist), ``base_url`` (absolute http/https), ``api_key_env``
    (``^[A-Z_][A-Z0-9_]*$``), ``models`` (a mapping of model ids to objects).
    """
    if not isinstance(block, Mapping):
        raise OpenCodeProviderConfigError("runtime.provider must be an object")

    provider_id = block.get("id")
    if not isinstance(provider_id, str) or not _ID_RE.match(provider_id):
        raise OpenCodeProviderConfigError(
            f"runtime.provider.id {provider_id!r} is invalid: it must match "
            "^[a-z0-9_-]+$ (it is the provider half of `-m <id>/<model>`)"
        )

    npm = block.get("npm")
    if not isinstance(npm, str) or npm not in NPM_ALLOWLIST:
        raise OpenCodeProviderConfigError(
            f"runtime.provider.npm {npm!r} is not allowed: the driver package "
            f"must be one of {sorted(NPM_ALLOWLIST)} (pre-seeded into the image)"
        )

    base_url = block.get("base_url")
    parsed = urlparse(base_url) if isinstance(base_url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OpenCodeProviderConfigError(
            f"runtime.provider.base_url {base_url!r} is invalid: it must be an "
            "absolute http:// or https:// URL"
        )

    api_key_env = block.get("api_key_env")
    if not isinstance(api_key_env, str) or not _ENV_NAME_RE.match(api_key_env):
        raise OpenCodeProviderConfigError(
            f"runtime.provider.api_key_env {api_key_env!r} is invalid: it must "
            "be an environment variable NAME matching ^[A-Z_][A-Z0-9_]*$ "
            "(the key itself is read from that variable, never from the template)"
        )

    models = block.get("models")
    if not isinstance(models, Mapping):
        raise OpenCodeProviderConfigError(
            f"runtime.provider.models must be a mapping of model ids, got "
            f"{type(models).__name__}"
        )
    normalized_models: Dict[str, dict] = {}
    for model_id, declared in models.items():
        if not isinstance(model_id, str) or not _MODEL_ID_RE.match(model_id):
            raise OpenCodeProviderConfigError(
                f"runtime.provider.models has an invalid model id {model_id!r}"
            )
        if declared is None:
            declared = {}
        if not isinstance(declared, Mapping):
            raise OpenCodeProviderConfigError(
                f"runtime.provider.models.{model_id} must be an object "
                f"(e.g. {{}} or {{cost: {{input, output}}}}), got {type(declared).__name__}"
            )
        normalized_models[model_id] = dict(declared)

    return {
        "id": provider_id,
        "npm": npm,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "models": normalized_models,
    }


def provider_block(environ: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """Read + validate: the block this turn renders, or ``None`` when the
    template declares none. Raises :class:`OpenCodeProviderConfigError`."""
    raw = load_provider_block(environ)
    if raw is None:
        return None
    return validate_provider_block(raw)


def render_provider_config(
    config: Dict[str, object], block: Optional[Mapping]
) -> Dict[str, object]:
    """Merge the validated ``block`` into ``config['provider']`` in place.

    ``None`` ⇒ untouched: an agent on a models.dev provider gets NO
    ``provider:`` key from Trinity. An operator-declared ``provider.<id>`` in
    the shared config wins on ``npm``/``options``; its model map is unioned
    with the template's so the pinned model is never missing from it.
    """
    if not block:
        return config
    provider_id = str(block["id"])
    rendered = {
        "npm": block["npm"],
        "options": {
            "baseURL": block["base_url"],
            # The REFERENCE, substituted by OpenCode from the subprocess env
            # at config load (variable.ts, v1.18.32). Never the value.
            "apiKey": "{env:%s}" % block["api_key_env"],
        },
        "models": {mid: dict(spec) for mid, spec in block["models"].items()},
    }

    existing = config.get("provider")
    providers: Dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    declared = providers.get(provider_id)
    if isinstance(declared, dict):
        logger.info(
            "[OpenCode] provider %r declared in the shared config — kept; "
            "template models unioned in", provider_id,
        )
        merged = dict(rendered)
        merged.update(declared)
        models = dict(rendered["models"])
        declared_models = declared.get("models")
        if isinstance(declared_models, dict):
            models.update(declared_models)
        merged["models"] = models
        providers[provider_id] = merged
    else:
        providers[provider_id] = rendered
    config["provider"] = providers
    return config


def required_key_env(model: Optional[str], block: Optional[Mapping]) -> Optional[str]:
    """The env var that must carry the key for ``model``'s provider, or
    ``None`` when that provider is not pre-checked (ruling T4)."""
    if not model or "/" not in model:
        return None
    provider_id = model.partition("/")[0]
    if block and str(block.get("id")) == provider_id:
        return str(block["api_key_env"])
    return NATIVE_KEY_ENV.get(provider_id)


def missing_key_env(
    model: Optional[str],
    block: Optional[Mapping],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The NAME of the required key variable when it is unset or empty in
    ``environ``; ``None`` when present or when the provider is not
    pre-checked. The caller fails the turn 503 naming it."""
    var = required_key_env(model, block)
    if var is None:
        return None
    env = os.environ if environ is None else environ
    if (env.get(var) or "").strip():
        return None
    return var
