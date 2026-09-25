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
"""

from __future__ import annotations

import logging
import os
import socket
import struct
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
    configured = normalize_base_url(source.get(ENV_BASE_URL))
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
    if normalize_base_url(source.get(ENV_BASE_URL)):
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
        f"OpenCode provider 'ollama' has no base URL: {ENV_BASE_URL} is unset "
        f"and no default gateway could be read from {route_path}. Set "
        f"{ENV_BASE_URL} (e.g. 'http://10.0.0.5:11434') or declare "
        "provider.ollama.options.baseURL in opencode.json."
    )


def apply_provider_config(
    config: Dict[str, object],
    *,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    route_path: str = PROC_NET_ROUTE,
) -> Dict[str, object]:
    """Merge the Ollama provider block into ``config``; returns ``config``.

    Mutates and returns the same dict so a caller can chain it onto the loaded
    shared config. A no-op when the turn is not on ``ollama/…`` **and** the
    operator declared nothing — the common Anthropic/OpenRouter case pays
    nothing and sees no ``provider`` key it did not ask for. An ``ollama/…``
    turn always gets a base URL (env, else the derived gateway) or a NAMED
    failure; it never silently emits nothing.
    """
    source = os.environ if env is None else env
    base_url = resolve_base_url(model=model, env=source, route_path=route_path)
    models = resolve_models(model=model, env=source)

    providers = config.get("provider")
    providers = dict(providers) if isinstance(providers, Mapping) else {}
    existing = providers.get(OLLAMA_PROVIDER_ID)
    existing = dict(existing) if isinstance(existing, Mapping) else None

    if base_url is None and existing is None:
        # Either this turn wants nothing from Ollama — emit no `provider` key
        # at all — or it wants Ollama and neither the env nor the gateway
        # could supply a host, which is a named failure, not a silent skip
        # that degrades into OpenCode's own `err_*` ref (Abilityai/lilu#272).
        if model_provider(model=model, env=source) == OLLAMA_PROVIDER_ID:
            raise OpenCodeProviderConfigError(_unresolvable_base_url_reason(route_path))
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
        raise OpenCodeProviderConfigError(_unresolvable_base_url_reason(route_path))

    providers[OLLAMA_PROVIDER_ID] = entry
    config["provider"] = providers
    logger.info(
        "[OpenCode] provider 'ollama' -> %s models=%s",
        entry["options"]["baseURL"], sorted(entry.get("models", {})),
    )
    return config
