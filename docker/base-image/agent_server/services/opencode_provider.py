"""Custom-provider block for the generated OpenCode config (Abilityai/lilu#257 step 6).

OpenCode routes ``-m <provider>/<model>`` through a provider entry in
``opencode.json``. Providers that models.dev knows about resolve themselves; a
**local Ollama** does not — it has to be declared, with an npm package to drive
it and a base URL to reach it::

    "provider": {
      "ollama": {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "http://<host>:11434/v1"},
        "models": {"gemma4:12b": {}}
      }
    }

**Where it is emitted.** Here, in the runtime, not in ``startup.sh``. The turn
config is already built per turn by
``opencode_runtime._prepare_turn_config`` — so the provider block is computed
from the environment the turn actually runs with. That matters because the
agent's ``.env`` is re-read at every spawn (#1999): pointing an agent at a
different Ollama host takes effect on the next turn, with no container restart
and no file edited by hand inside the container. A block baked once by
``startup.sh`` would be stale from the first change onward.

**Where the values come from.**

``OPENCODE_OLLAMA_BASE_URL``
    The Ollama root, e.g. ``http://100.93.125.91:11434``. ``/v1`` is appended
    when absent — Ollama's OpenAI-compatible surface lives there, and the
    root URL is what an operator has at hand. **Unset ⇒ no block is emitted**
    and OpenCode's own providers are untouched (graceful degradation: an
    agent that never asks for ``ollama/…`` is unaffected).

``AGENT_RUNTIME_MODEL`` / the turn's ``-m``
    The pinned model. ``runtime: {type: opencode, model: "ollama/gemma4:12b"}``
    therefore works on its own — the model being requested is always declared,
    so no operator has to maintain a parallel list to use one model.

``OPENCODE_OLLAMA_MODELS``
    Optional, comma-separated, for the *extra* models a turn may switch to
    (``gemma4:12b, ministral-3:14b``). Entries may carry the ``ollama/``
    prefix or not.

An operator-declared ``provider.ollama`` already present in the shared config
wins on ``options``/``npm`` — the env is a default, not an override — and its
model map is *unioned* with the models above rather than replaced, so a pinned
model can never be missing from a config an operator has customised.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Provider id as it appears in ``-m ollama/<model>``.
OLLAMA_PROVIDER_ID = "ollama"

#: Driver package for any OpenAI-compatible endpoint. Pre-seeded into the image
#: at ``/opt/trinity/opencode/node_modules`` — OpenCode does NOT fetch it on
#: demand (measured), and a cold container must not depend on the npm registry.
OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"

ENV_BASE_URL = "OPENCODE_OLLAMA_BASE_URL"
ENV_EXTRA_MODELS = "OPENCODE_OLLAMA_MODELS"
ENV_RUNTIME_MODEL = "AGENT_RUNTIME_MODEL"


class OpenCodeProviderConfigError(ValueError):
    """A provider declaration that cannot be built. Carries the reason text.

    Named rather than swallowed: a typo'd base URL must fail with the env var
    name (quality bar 6), not degrade into OpenCode's own "unknown provider".
    """


def normalize_base_url(raw: Optional[str], *, env_var: str = ENV_BASE_URL) -> Optional[str]:
    """``http://host:11434`` → ``http://host:11434/v1``. ``None``/blank → ``None``.

    Raises :class:`OpenCodeProviderConfigError` for anything that is not an
    absolute http(s) URL, naming ``env_var`` so the operator knows what to fix.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OpenCodeProviderConfigError(
            f"{env_var}={raw!r} is not an absolute http(s) URL. "
            "Use the Ollama root, e.g. 'http://10.0.0.5:11434'."
        )
    trimmed = candidate.rstrip("/")
    # Idempotent: re-normalizing an already-normalized value is a no-op, so an
    # operator who *did* write the /v1 suffix does not get '/v1/v1'.
    if trimmed.endswith("/v1"):
        return trimmed
    return f"{trimmed}/v1"


def _strip_provider(model: str) -> Optional[str]:
    """``ollama/gemma4:12b`` → ``gemma4:12b``; a bare name passes through.

    Returns ``None`` for a model that belongs to a different provider — its
    name must not leak into the Ollama model map.
    """
    candidate = (model or "").strip()
    if not candidate:
        return None
    if "/" not in candidate:
        return candidate
    provider, _, rest = candidate.partition("/")
    if provider != OLLAMA_PROVIDER_ID:
        return None
    return rest.strip() or None


def resolve_models(
    *, model: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> List[str]:
    """The Ollama model ids to declare, de-duplicated, first-seen order.

    Order is deliberate: the turn's own model first, so the id a failure would
    be attributed to is the first thing in the emitted config.
    """
    source = os.environ if env is None else env
    candidates: List[Optional[str]] = [model, source.get(ENV_RUNTIME_MODEL)]
    for entry in (source.get(ENV_EXTRA_MODELS) or "").split(","):
        candidates.append(entry)

    resolved: List[str] = []
    for candidate in candidates:
        name = _strip_provider(candidate or "")
        if name and name not in resolved:
            resolved.append(name)
    return resolved


def build_ollama_provider(base_url: str, models: Iterable[str]) -> Dict[str, object]:
    """The literal ``provider.ollama`` value OpenCode expects."""
    return {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {"baseURL": base_url},
        "models": {name: {} for name in models},
    }


def apply_provider_config(
    config: Dict[str, object],
    *,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    """Merge the Ollama provider block into ``config``; returns ``config``.

    Mutates and returns the same dict so a caller can chain it onto the loaded
    shared config. A no-op when :data:`ENV_BASE_URL` is unset **and** the
    operator declared nothing — the common Anthropic/OpenRouter case pays
    nothing and sees no ``provider`` key it did not ask for.
    """
    source = os.environ if env is None else env
    base_url = normalize_base_url(source.get(ENV_BASE_URL))
    models = resolve_models(model=model, env=source)

    providers = config.get("provider")
    providers = dict(providers) if isinstance(providers, Mapping) else {}
    existing = providers.get(OLLAMA_PROVIDER_ID)
    existing = dict(existing) if isinstance(existing, Mapping) else None

    if base_url is None and existing is None:
        return config

    if existing is None:
        entry: Dict[str, object] = build_ollama_provider(base_url or "", models)
    else:
        # Operator's declaration wins on npm/options; models are unioned so a
        # pinned model is never absent from a customised config.
        entry = dict(existing)
        entry.setdefault("npm", OPENAI_COMPATIBLE_NPM)
        options = entry.get("options")
        options = dict(options) if isinstance(options, Mapping) else {}
        if not options.get("baseURL") and base_url:
            options["baseURL"] = base_url
        entry["options"] = options
        declared = entry.get("models")
        declared = dict(declared) if isinstance(declared, Mapping) else {}
        for name in models:
            declared.setdefault(name, {})
        entry["models"] = declared

    if not entry.get("options", {}).get("baseURL"):
        raise OpenCodeProviderConfigError(
            f"OpenCode provider 'ollama' has no base URL. Set {ENV_BASE_URL} "
            "(e.g. 'http://10.0.0.5:11434') or declare "
            "provider.ollama.options.baseURL in opencode.json."
        )

    providers[OLLAMA_PROVIDER_ID] = entry
    config["provider"] = providers
    logger.info(
        "[OpenCode] provider 'ollama' -> %s models=%s",
        entry["options"]["baseURL"], sorted(entry.get("models", {})),
    )
    return config
