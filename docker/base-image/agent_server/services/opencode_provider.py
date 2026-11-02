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
    root URL is what an operator has at hand. **Unset and the turn is not on
    ``ollama/…`` ⇒ no block is emitted** and OpenCode's own providers are
    untouched (graceful degradation: an agent that never asks for
    ``ollama/…`` is unaffected).

    **Unset and the turn IS on ``ollama/…`` ⇒ the base URL is DERIVED from
    the container's default gateway** (Abilityai/lilu#272). Ollama on the
    host answers on the gateway address and refuses ``localhost`` from inside
    the container, so the one value that works appeared in no config and no
    env — unknowable without inspecting the host, and OpenCode's failure for
    the undeclared provider is the opaque ``err_411e3b55`` in 1.6s. The
    gateway is read per turn from :data:`PROC_NET_ROUTE` rather than baked
    into a config key, because it is a property of the network the container
    is attached to, not a setting: hard-coding it would be wrong the first
    time the agent network is renumbered. The env var still wins when set.

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

**Any provider is configuration** (Abilityai/lilu#653). The Ollama block above
is one instance of a general grammar, read from the SAME spawn env the
``opencode`` child gets (the agent's ``.env`` over the container baseline)::

    OPENCODE_PROVIDER_<ID>_BASE_URL   absolute http(s) URL of the endpoint
    OPENCODE_PROVIDER_<ID>_API_KEY    -> provider.<id>.options.apiKey
    OPENCODE_PROVIDER_<ID>_MODELS     comma-separated, '<id>/' prefix optional
    OPENCODE_PROVIDER_<ID>_NPM        driver; only the pre-seeded
                                      ``@ai-sdk/openai-compatible`` is accepted

``<ID>`` is lower-cased into the provider id (``..._LMSTUDIO_...`` ->
``lmstudio/<model>``). ``OPENCODE_OLLAMA_BASE_URL`` / ``OPENCODE_OLLAMA_MODELS``
remain as aliases of ``<ID>=OLLAMA`` (the canonical name wins on a base URL;
model lists are unioned). For ``ollama`` only, the base URL falls back to
``OPENCODE_DEFAULT_PROVIDER_BASE_URL`` — the platform setting the backend
injects into the container env at create/recreate — and then to the derived
gateway, so the order is ``.env`` > platform > gateway.

``openrouter/…`` needs no block: it is a built-in OpenCode provider and reads
``OPENROUTER_API_KEY`` from the child env. A turn on it with no key anywhere is
refused by :func:`require_openrouter_key` with a named error — never left to
fall through to some other provider's credentials. An optional
``OPENCODE_PROVIDER_OPENROUTER_*`` block pins a proxy base URL or declares
models.

A driver other than the pre-seeded one is refused by name at config time: the
image ships exactly one, and OpenCode would otherwise fetch the package from
the npm registry in the middle of a turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from ._runtime_config import opencode_images_override

logger = logging.getLogger(__name__)

#: Abilityai/lilu#660 — the per-model field the 1.18.32 binary gates image
#: parts on. A config-declared model's `capabilities.input.image` is
#: `modalities.input.includes("image")`, else models.dev, else false; and
#: `input.text` is `modalities.input.includes("text")` once `modalities` is
#: set at all, so "text" MUST be listed too or text input is switched off.
IMAGE_MODALITIES = {"input": ["text", "image"], "output": ["text"]}

#: Provider id as it appears in ``-m ollama/<model>``.
OLLAMA_PROVIDER_ID = "ollama"

#: Driver package for any OpenAI-compatible endpoint. Pre-seeded into the image
#: at ``/opt/trinity/opencode/node_modules`` — OpenCode does NOT fetch it on
#: demand (measured), and a cold container must not depend on the npm registry.
OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"

#: Drivers present in the image's seeded ``node_modules``. Anything else would
#: be installed from the npm registry at turn time, which a turn must never do.
SEEDED_NPM_DRIVERS = frozenset({OPENAI_COMPATIBLE_NPM})

#: ``OPENCODE_PROVIDER_<ID>_<FIELD>`` — the general per-provider grammar.
ENV_PROVIDER_PREFIX = "OPENCODE_PROVIDER_"
PROVIDER_ENV_FIELDS = ("BASE_URL", "API_KEY", "MODELS", "NPM")

#: Platform-tier Ollama base URL, injected by the backend from the
#: ``opencode_default_provider_base_url`` setting. A distinct name (not the
#: alias) so any agent-level declaration outranks it whatever layer it is in.
ENV_PLATFORM_BASE_URL = "OPENCODE_DEFAULT_PROVIDER_BASE_URL"

OPENROUTER_PROVIDER_ID = "openrouter"
ENV_OPENROUTER_API_KEY = "OPENROUTER_API_KEY"

#: Providers OpenCode resolves on its own (models.dev): a block for one of
#: these may omit the base URL. Every other declared provider needs one.
BUILTIN_PROVIDER_IDS = frozenset({OPENROUTER_PROVIDER_ID})

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

ENV_BASE_URL = "OPENCODE_OLLAMA_BASE_URL"
ENV_EXTRA_MODELS = "OPENCODE_OLLAMA_MODELS"
ENV_RUNTIME_MODEL = "AGENT_RUNTIME_MODEL"

#: The kernel's routing table as seen from inside the container. Read to learn
#: the default gateway, which is the host as the container can address it.
PROC_NET_ROUTE = "/proc/net/route"

#: Ollama's fixed listen port. Only the HOST half is derived — an operator who
#: moved the port sets :data:`ENV_BASE_URL` in full, which still wins.
OLLAMA_DEFAULT_PORT = 11434


class OpenCodeProviderConfigError(ValueError):
    """A provider declaration that cannot be built. Carries the reason text.

    Named rather than swallowed: a typo'd base URL must fail with the env var
    name (quality bar 6), not degrade into OpenCode's own "unknown provider".
    """


class OpenCodeProviderCredentialError(OpenCodeProviderConfigError):
    """A turn on a provider whose credential is configured nowhere.

    A subclass so every existing ``except OpenCodeProviderConfigError`` site
    already turns it into the same named 400.
    """


def provider_env_name(provider_id: str, field: str) -> str:
    """``("ollama", "BASE_URL")`` -> ``OPENCODE_PROVIDER_OLLAMA_BASE_URL``."""
    return f"{ENV_PROVIDER_PREFIX}{provider_id.upper()}_{field}"


def provider_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, str]]:
    """Every ``OPENCODE_PROVIDER_<ID>_<FIELD>`` with a non-blank value.

    Returns ``{provider_id: {FIELD: value}}``. Unknown suffixes are ignored
    (a typo there is not a declaration), as are blank values (an unset
    ``${VAR}`` substitution must not declare an empty endpoint).
    """
    source = os.environ if env is None else env
    declared: Dict[str, Dict[str, str]] = {}
    for key, value in source.items():
        if not key.startswith(ENV_PROVIDER_PREFIX) or not (value or "").strip():
            continue
        rest = key[len(ENV_PROVIDER_PREFIX):]
        for field in PROVIDER_ENV_FIELDS:
            suffix = f"_{field}"
            if rest.endswith(suffix) and len(rest) > len(suffix):
                provider_id = rest[: -len(suffix)].lower()
                if _PROVIDER_ID_RE.match(provider_id):
                    declared.setdefault(provider_id, {})[field] = value.strip()
                break
    return declared


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


def validate_endpoint_url(raw: str, *, env_var: str) -> str:
    """An OpenAI-compatible endpoint URL, used as given (no ``/v1`` appended:
    unlike an Ollama root, an endpoint's path is part of its address)."""
    candidate = raw.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OpenCodeProviderConfigError(
            f"{env_var}={raw!r} is not an absolute http(s) URL. Use the "
            "endpoint's OpenAI-compatible base, e.g. 'https://host/api/v1'."
        )
    return candidate


def _strip_provider(model: str, provider_id: str = OLLAMA_PROVIDER_ID) -> Optional[str]:
    """``ollama/gemma4:12b`` → ``gemma4:12b``; a bare name passes through.

    Returns ``None`` for a model that belongs to a different provider — its
    name must not leak into this provider's model map.
    """
    candidate = (model or "").strip()
    if not candidate:
        return None
    if "/" not in candidate:
        return candidate
    provider, _, rest = candidate.partition("/")
    if provider != provider_id:
        return None
    return rest.strip() or None


def model_provider(
    *, model: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """The provider half of the model this turn will actually run.

    The turn's own ``-m`` wins over the agent-wide pin, mirroring
    :func:`resolve_models`' ordering: a turn that overrides the model must be
    judged on the model it overrode to.
    """
    source = os.environ if env is None else env
    for candidate in (model, source.get(ENV_RUNTIME_MODEL)):
        name = (candidate or "").strip()
        if not name:
            continue
        provider, _, rest = name.partition("/")
        if not rest.strip():
            return None
        return provider.strip() or None
    return None


def read_default_gateway(route_path: str = PROC_NET_ROUTE) -> Optional[str]:
    """The container's default-gateway IPv4 address, or ``None``.

    ``/proc/net/route`` is whitespace-separated with a header row; the default
    route is the one whose ``Destination`` is all-zero. ``Gateway`` is a
    little-endian hex word — ``01001CAC`` is ``172.28.0.1``, NOT ``1.0.28.172``
    — so the bytes are unpacked with an explicit ``<L`` rather than formatted
    in string order.

    Returns ``None`` (never raises) when the file is missing, unreadable, has
    no default route, or has an on-link default with no gateway: the caller
    decides whether an absent gateway is fatal, and for a non-Ollama turn it
    is not.
    """
    try:
        with open(route_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        logger.warning("[OpenCode] cannot read %s (%s)", route_path, exc)
        return None

    for line in lines[1:]:  # row 0 is the header
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        gateway_hex = fields[2]
        if gateway_hex == "00000000":
            continue  # on-link default route: no next hop to talk to
        try:
            packed = struct.pack("<L", int(gateway_hex, 16))
        except (ValueError, struct.error):
            continue
        return socket.inet_ntoa(packed)
    return None


def derive_gateway_base_url(route_path: str = PROC_NET_ROUTE) -> Optional[str]:
    """``http://<default-gateway>:11434/v1``, or ``None`` if no gateway."""
    gateway = read_default_gateway(route_path)
    if not gateway:
        return None
    return normalize_base_url(f"http://{gateway}:{OLLAMA_DEFAULT_PORT}")


def configured_ollama_base_url(env: Mapping[str, str]) -> Optional[str]:
    """The Ollama base URL from configuration, before the gateway fallback.

    Canonical ``OPENCODE_PROVIDER_OLLAMA_BASE_URL``, then the
    ``OPENCODE_OLLAMA_BASE_URL`` alias, then the platform tier. Each is
    normalized under its own name, so a malformed value fails naming the
    variable that holds it rather than falling through to the next tier.
    """
    for env_var in (provider_env_name(OLLAMA_PROVIDER_ID, "BASE_URL"),
                    ENV_BASE_URL, ENV_PLATFORM_BASE_URL):
        configured = normalize_base_url(env.get(env_var), env_var=env_var)
        if configured:
            return configured
    return None


def resolve_base_url(
    *,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    route_path: str = PROC_NET_ROUTE,
) -> Optional[str]:
    """The base URL for this turn: env first, then the derived gateway.

    ``None`` means "nothing to declare" — either the env is unset and the turn
    is not on ``ollama/…`` (the Anthropic/OpenRouter case, which must stay a
    no-op), or it is and no gateway could be read, which the caller turns into
    a NAMED failure rather than an opaque one.
    """
    source = os.environ if env is None else env
    configured = configured_ollama_base_url(source)
    if configured:
        return configured
    if model_provider(model=model, env=source) != OLLAMA_PROVIDER_ID:
        return None
    derived = derive_gateway_base_url(route_path)
    if derived:
        # INFO, with the value: when a turn fails against a local Ollama the
        # first question is which host it dialled, and "derived" vs "set" is
        # the difference between fixing the network and fixing the .env.
        logger.info(
            "[OpenCode] %s is unset; DERIVED ollama base URL %s from the "
            "container default gateway (%s)",
            ENV_BASE_URL, derived, route_path,
        )
    return derived


def resolve_models(
    *, model: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> List[str]:
    """The Ollama model ids to declare, de-duplicated, first-seen order.

    Order is deliberate: the turn's own model first, so the id a failure would
    be attributed to is the first thing in the emitted config.
    """
    source = os.environ if env is None else env
    return _provider_models(
        OLLAMA_PROVIDER_ID,
        model=model,
        env=source,
        extra=(source.get(provider_env_name(OLLAMA_PROVIDER_ID, "MODELS")),
               source.get(ENV_EXTRA_MODELS)),
    )


def _provider_models(
    provider_id: str,
    *,
    model: Optional[str],
    env: Mapping[str, str],
    extra: Iterable[Optional[str]] = (),
) -> List[str]:
    """Turn model, agent pin, then each comma-separated ``extra`` list,
    de-duplicated, first-seen order.

    The turn model and the pin are full ``provider/model`` ids, kept only when
    they belong to ``provider_id``. A declared list entry is already scoped to
    this provider, so its ``<id>/`` prefix is optional and anything else is
    the model id verbatim — OpenRouter ids carry their own slash
    (``qwen/qwen3-coder``) and must not be mistaken for a foreign provider.
    """
    resolved: List[str] = []

    def _add(name: Optional[str]) -> None:
        if name and name not in resolved:
            resolved.append(name)

    for candidate in (model, env.get(ENV_RUNTIME_MODEL)):
        _add(_strip_provider(candidate or "", provider_id))
    prefix = f"{provider_id}/"
    for listing in extra:
        for entry in (listing or "").split(","):
            name = entry.strip()
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
            _add(name or None)
    return resolved


def build_ollama_provider(base_url: str, models: Iterable[str]) -> Dict[str, object]:
    """The literal ``provider.ollama`` value OpenCode expects."""
    return {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {"baseURL": base_url},
        "models": {name: {} for name in models},
    }


def _declared_base_url(config: Optional[Mapping[str, object]]) -> Optional[str]:
    """``provider.ollama.options.baseURL`` from an operator-written config."""
    if not isinstance(config, Mapping):
        return None
    providers = config.get("provider")
    if not isinstance(providers, Mapping):
        return None
    entry = providers.get(OLLAMA_PROVIDER_ID)
    if not isinstance(entry, Mapping):
        return None
    options = entry.get("options")
    if not isinstance(options, Mapping):
        return None
    declared = options.get("baseURL")
    return declared.strip() if isinstance(declared, str) and declared.strip() else None


def require_ollama_base_url(
    model: Optional[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    config: Optional[Mapping[str, object]] = None,
    route_path: str = PROC_NET_ROUTE,
) -> None:
    """Raise if this turn pins ``ollama/…`` and no base URL exists anywhere.

    Called at the ``-m`` boundary so the turn dies BEFORE the subprocess: past
    that point the only diagnosis an operator gets is OpenCode's own
    `err_411e3b55` for a provider it cannot resolve (Abilityai/lilu#272).
    A no-op for every other provider, and silent when a URL is available from
    any of the three sources (env, operator-declared config, derived gateway)
    — it decides nothing, it only refuses the unrunnable.
    """
    source = os.environ if env is None else env
    if model_provider(model=model, env=source) != OLLAMA_PROVIDER_ID:
        return
    # normalize_base_url deliberately raises on a malformed value: a typo'd
    # URL must fail named here too, not resolve into the gateway default.
    if configured_ollama_base_url(source):
        return
    if _declared_base_url(config):
        return
    # derive_* rather than resolve_*: resolve_base_url logs the DERIVED line,
    # and _prepare_turn_config is about to call it for real one line later.
    if derive_gateway_base_url(route_path):
        return
    raise OpenCodeProviderConfigError(_unresolvable_base_url_reason(route_path))


def _unresolvable_base_url_reason(route_path: str) -> str:
    """One reason text for every "no base URL" exit, naming the env var.

    Named rather than opaque: OpenCode's own answer for a provider it cannot
    resolve is `err_411e3b55` in 1.6s (Abilityai/lilu#272), which tells an
    operator nothing about what to set.
    """
    return (
        f"OpenCode provider 'ollama' has no base URL: "
        f"{provider_env_name(OLLAMA_PROVIDER_ID, 'BASE_URL')}, {ENV_BASE_URL} "
        f"and {ENV_PLATFORM_BASE_URL} are unset and no default gateway could "
        f"be read from {route_path}. Set {ENV_BASE_URL} (e.g. "
        "'http://10.0.0.5:11434') in the agent's "
        ".env, or declare provider.ollama.options.baseURL in opencode.json."
    )


def _checked_npm(value: str, *, source: str) -> str:
    """Refuse a driver the image does not ship, naming where it came from."""
    if value not in SEEDED_NPM_DRIVERS:
        raise OpenCodeProviderConfigError(
            f"{source}={value!r} is not a pre-seeded OpenCode driver. Only "
            f"{', '.join(sorted(SEEDED_NPM_DRIVERS))} is installed in the image; "
            "any other package would be fetched from the npm registry during a "
            "turn, which Trinity refuses. Drop the setting to use the default."
        )
    return value


def _merge_entry(existing: Optional[Mapping[str, object]], generated: Dict[str, object]) -> Dict[str, object]:
    """Operator declaration wins on ``npm`` and on each option key; models are
    unioned so a pinned model is never absent from a customised config."""
    if not isinstance(existing, Mapping):
        return generated
    entry = dict(existing)
    if "npm" in generated:
        entry.setdefault("npm", generated["npm"])
    options = entry.get("options")
    options = dict(options) if isinstance(options, Mapping) else {}
    for key, value in (generated.get("options") or {}).items():
        if not options.get(key):
            options[key] = value
    if options:
        entry["options"] = options
    declared = entry.get("models")
    declared = dict(declared) if isinstance(declared, Mapping) else {}
    for name, spec in (generated.get("models") or {}).items():
        declared.setdefault(name, spec)
    entry["models"] = declared
    return entry


def _build_env_provider(
    provider_id: str,
    fields: Mapping[str, str],
    *,
    model: Optional[str],
    env: Mapping[str, str],
) -> Dict[str, object]:
    """``provider.<id>`` from its ``OPENCODE_PROVIDER_<ID>_*`` declaration."""
    entry: Dict[str, object] = {}
    options: Dict[str, object] = {}
    base_url = fields.get("BASE_URL")
    if base_url:
        options["baseURL"] = validate_endpoint_url(
            base_url, env_var=provider_env_name(provider_id, "BASE_URL")
        )
        entry["npm"] = _checked_npm(
            fields.get("NPM") or OPENAI_COMPATIBLE_NPM,
            source=provider_env_name(provider_id, "NPM"),
        )
    elif provider_id not in BUILTIN_PROVIDER_IDS:
        raise OpenCodeProviderConfigError(
            f"OpenCode provider {provider_id!r} is declared "
            f"({', '.join(provider_env_name(provider_id, f) for f in sorted(fields))}) "
            f"but {provider_env_name(provider_id, 'BASE_URL')} is unset. A "
            "custom provider needs the URL of its OpenAI-compatible endpoint."
        )
    elif fields.get("NPM"):
        _checked_npm(fields["NPM"], source=provider_env_name(provider_id, "NPM"))
        entry["npm"] = fields["NPM"]
    if fields.get("API_KEY"):
        options["apiKey"] = fields["API_KEY"]
    if options:
        entry["options"] = options
    entry["models"] = {
        name: {}
        for name in _provider_models(
            provider_id, model=model, env=env, extra=(fields.get("MODELS"),)
        )
    }
    return entry


def _apply_image_modalities(
    provider_id: str, generated: Dict[str, object], env: Mapping[str, str]
) -> None:
    """Declare image input on every generated model when
    ``OPENCODE_PROVIDER_<ID>_IMAGES=true`` (Abilityai/lilu#660). An operator's
    own model entry in opencode.json still wins (``_merge_entry``)."""
    if opencode_images_override(provider_id, env) is not True:
        return
    models = generated.get("models")
    if isinstance(models, dict):
        for name in models:
            models[name] = {"modalities": {k: list(v) for k, v in IMAGE_MODALITIES.items()}}


def apply_provider_config(
    config: Dict[str, object],
    *,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    route_path: str = PROC_NET_ROUTE,
) -> Dict[str, object]:
    """Merge the env-declared provider blocks into ``config``; returns ``config``.

    Mutates and returns the same dict so a caller can chain it onto the loaded
    shared config. A no-op when the turn is not on ``ollama/…``, no provider is
    declared in the env **and** the operator declared nothing — the common
    Anthropic/OpenRouter case pays nothing and sees no ``provider`` key it did
    not ask for. An ``ollama/…`` turn always gets a base URL (env, platform,
    else the derived gateway) or a NAMED failure; it never silently emits
    nothing. Every provider in the result is checked for an unseeded driver.
    """
    source = os.environ if env is None else env
    declared_env = provider_env(source)
    had_provider_key = isinstance(config.get("provider"), Mapping)
    providers = config.get("provider")
    providers = dict(providers) if isinstance(providers, Mapping) else {}

    # -- ollama: the one provider with a derived fallback --------------------
    base_url = resolve_base_url(model=model, env=source, route_path=route_path)
    existing = providers.get(OLLAMA_PROVIDER_ID)
    existing = dict(existing) if isinstance(existing, Mapping) else None

    if base_url is None and existing is None:
        # Either this turn wants nothing from Ollama, or it wants Ollama and
        # nothing could supply a host — a named failure, not a silent skip
        # that degrades into OpenCode's own `err_*` ref (Abilityai/lilu#272).
        if model_provider(model=model, env=source) == OLLAMA_PROVIDER_ID:
            raise OpenCodeProviderConfigError(_unresolvable_base_url_reason(route_path))
    else:
        ollama_npm = declared_env.get(OLLAMA_PROVIDER_ID, {}).get("NPM")
        generated = build_ollama_provider(base_url or "", resolve_models(model=model, env=source))
        if ollama_npm:
            generated["npm"] = _checked_npm(
                ollama_npm, source=provider_env_name(OLLAMA_PROVIDER_ID, "NPM")
            )
        ollama_key = declared_env.get(OLLAMA_PROVIDER_ID, {}).get("API_KEY")
        if ollama_key:
            generated["options"]["apiKey"] = ollama_key
        if not base_url:
            generated["options"].pop("baseURL", None)
        _apply_image_modalities(OLLAMA_PROVIDER_ID, generated, source)
        entry = _merge_entry(existing, generated)
        if not (entry.get("options") or {}).get("baseURL"):
            raise OpenCodeProviderConfigError(_unresolvable_base_url_reason(route_path))
        providers[OLLAMA_PROVIDER_ID] = entry
        logger.info(
            "[OpenCode] provider 'ollama' -> %s models=%s",
            entry["options"]["baseURL"], sorted(entry.get("models", {})),
        )

    # -- every other env-declared provider -----------------------------------
    for provider_id, fields in sorted(declared_env.items()):
        if provider_id == OLLAMA_PROVIDER_ID:
            continue
        generated = _build_env_provider(provider_id, fields, model=model, env=source)
        _apply_image_modalities(provider_id, generated, source)
        entry = _merge_entry(providers.get(provider_id), generated)
        providers[provider_id] = entry
        logger.info(
            "[OpenCode] provider %r -> %s models=%s", provider_id,
            (entry.get("options") or {}).get("baseURL") or "(built-in endpoint)",
            sorted(entry.get("models", {})),
        )

    # -- no provider may pull a driver from the registry mid-turn ------------
    for provider_id, entry in providers.items():
        if isinstance(entry, Mapping) and entry.get("npm"):
            _checked_npm(str(entry["npm"]), source=f"opencode.json provider.{provider_id}.npm")

    if providers or had_provider_key:
        config["provider"] = providers
    return config


def require_openrouter_key(
    model: Optional[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    config: Optional[Mapping[str, object]] = None,
    auth_path: Optional[str] = None,
) -> None:
    """Raise if this turn is on ``openrouter/…`` and no key exists anywhere.

    Sources, any one of which suffices: ``OPENROUTER_API_KEY`` in the spawn env
    (agent ``.env`` / the platform ``openrouter_api_key``
    setting injected at create), ``OPENCODE_PROVIDER_OPENROUTER_API_KEY``, an
    operator-declared ``provider.openrouter.options.apiKey``, or an
    ``openrouter`` entry in OpenCode's ``auth.json``. Refused BEFORE the
    subprocess, named — a keyless OpenRouter turn must never be answered by
    some other provider's credentials or by OpenCode's opaque ``err_*`` ref.
    """
    source = os.environ if env is None else env
    if model_provider(model=model, env=source) != OPENROUTER_PROVIDER_ID:
        return
    for env_var in (ENV_OPENROUTER_API_KEY,
                    provider_env_name(OPENROUTER_PROVIDER_ID, "API_KEY")):
        if (source.get(env_var) or "").strip():
            return
    if isinstance(config, Mapping):
        entry = (config.get("provider") or {}) if isinstance(config.get("provider"), Mapping) else {}
        entry = entry.get(OPENROUTER_PROVIDER_ID)
        options = entry.get("options") if isinstance(entry, Mapping) else None
        if isinstance(options, Mapping) and str(options.get("apiKey") or "").strip():
            return
    if auth_path:
        try:
            with open(auth_path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, Mapping) and stored.get(OPENROUTER_PROVIDER_ID):
                return
        except (OSError, ValueError):
            pass
    raise OpenCodeProviderCredentialError(
        f"OpenCode model {model or source.get(ENV_RUNTIME_MODEL)!r} is on provider "
        f"'openrouter' but no key is configured: {ENV_OPENROUTER_API_KEY} is "
        "unset in the agent's .env, and the "
        "platform setting 'openrouter_api_key' is empty. Set one of them; the "
        "turn is refused rather than run on another provider's credentials."
    )


def configured_models(config: Optional[Mapping[str, object]]) -> List[str]:
    """``provider/model`` ids declared in a (generated) config, sorted."""
    providers = config.get("provider") if isinstance(config, Mapping) else None
    if not isinstance(providers, Mapping):
        return []
    found: List[str] = []
    for provider_id, entry in providers.items():
        models = entry.get("models") if isinstance(entry, Mapping) else None
        if isinstance(models, Mapping):
            found.extend(f"{provider_id}/{name}" for name in models)
    return sorted(set(found))
