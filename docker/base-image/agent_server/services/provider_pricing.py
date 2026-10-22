"""Turn cost for Claude Code runs routed through an Anthropic-compatible proxy.

Claude Code prices every turn from its own Anthropic price table and reports
the sum as ``total_cost_usd``. With ``ANTHROPIC_BASE_URL`` pointing at
OpenRouter the turn is served by whatever model the ``ANTHROPIC_DEFAULT_*``
aliases map to (``deepseek/...``, ``qwen/...``), so that number is a guess at
Anthropic prices for a model Claude Code does not know — measured 27× too high
for DeepSeek Flash. Claude Code says as much: such a ``modelUsage`` entry
carries ``"costBasis": "unknown"``.

``turn_cost`` re-prices the turn from ``modelUsage`` (token counts per model,
cumulative over every API call of the turn, side calls included) against the
provider's public price list, and falls back to the native figure whenever
that is not possible: not OpenRouter, a model missing from the list, a price
list that is not loaded yet or cannot be fetched. It never raises and never
touches the network — it runs on the stdout reader thread, where a stalled
fetch would outlive ``_drain_bounded``'s grace and lose the turn's result. The
list is fetched in a background thread: at agent-server boot
(``prefetch_prices``) and again whenever ``turn_cost`` finds it stale.

Only OpenRouter is supported; another provider is one entry in
``_PRICE_SOURCES`` (a host and a function that returns per-token prices).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

from .execution_env import ENV_FILE, build_execution_env, runtime_overrides

logger = logging.getLogger(__name__)

# Per-token USD prices for one model: prompt, completion, cache_read, cache_write.
Prices = Dict[str, float]

_PRICE_TTL_S = 6 * 3600
_FAILURE_RETRY_S = 300
_FETCH_TIMEOUT_S = 5


def _fetch_openrouter_prices() -> Dict[str, Prices]:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "trinity-agent-server"},
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
        data = json.load(resp).get("data") or []

    def price(p: Mapping, key: str) -> float:
        try:
            return max(float(p.get(key) or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    prices: Dict[str, Prices] = {}
    for model in data:
        p = model.get("pricing") or {}
        if not model.get("id") or "prompt" not in p or "completion" not in p:
            continue
        prices[model["id"]] = {
            "prompt": price(p, "prompt"),
            "completion": price(p, "completion"),
            # Providers without a cache price bill cached tokens as prompt.
            "cache_read": price(p, "input_cache_read") if p.get("input_cache_read") is not None else price(p, "prompt"),
            "cache_write": price(p, "input_cache_write") if p.get("input_cache_write") is not None else price(p, "prompt"),
        }
    return prices


_PRICE_SOURCES: Dict[str, Callable[[], Dict[str, Prices]]] = {
    "openrouter.ai": _fetch_openrouter_prices,
}

_cache: Dict[str, tuple] = {}  # host -> (refresh_at, prices or None)
_refreshing: set = set()  # hosts with a fetch in flight
_lock = threading.Lock()  # guards _cache and _refreshing; never held across a fetch


def _refresh(host: str, fetch: Callable[[], Dict[str, Prices]]) -> None:
    try:
        prices = fetch()
        logger.info("provider pricing: loaded %d model prices from %s", len(prices), host)
        entry = (time.monotonic() + _PRICE_TTL_S, prices)
    except Exception as e:  # network, JSON, anything — keep what we had
        logger.warning("provider pricing: could not load prices from %s: %s", host, e)
        entry = None
    with _lock:
        if entry is None:
            entry = (time.monotonic() + _FAILURE_RETRY_S, _cache.get(host, (0.0, None))[1])
        _cache[host] = entry
        _refreshing.discard(host)


def _refresh_in_background(host: str) -> Optional[threading.Thread]:
    """Start a price fetch for ``host`` unless its list is fresh or one is running."""
    fetch = _PRICE_SOURCES.get(host)
    if fetch is None:
        return None
    with _lock:
        if time.monotonic() < _cache.get(host, (0.0, None))[0] or host in _refreshing:
            return None
        _refreshing.add(host)
    thread = threading.Thread(target=_refresh, args=(host, fetch), name=f"price-refresh-{host}", daemon=True)
    try:
        thread.start()
    except Exception:
        with _lock:
            _refreshing.discard(host)
        raise
    return thread


def _prices_for(host: str) -> Optional[Dict[str, Prices]]:
    """The cached price list for ``host``; a stale one is refreshed in the background."""
    _refresh_in_background(host)
    with _lock:
        return _cache.get(host, (0.0, None))[1]


_host_memo: tuple = (None, None)  # (env signature, host)


def _env_signature() -> tuple:
    try:
        st = ENV_FILE.stat()
        file_sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        file_sig = None
    # Only this override can change the URL; the others carry credentials.
    return file_sig, runtime_overrides().get("ANTHROPIC_BASE_URL", "<unset>")


def _proxy_host() -> Optional[str]:
    """Host of the spawn env's ``ANTHROPIC_BASE_URL``, re-read only when ``.env``
    or a runtime override changed (``build_execution_env`` logs per call)."""
    global _host_memo
    sig = _env_signature()
    if _host_memo[0] == sig:
        return _host_memo[1]
    base_url = build_execution_env().get("ANTHROPIC_BASE_URL") or ""
    host = (urlparse(base_url).hostname or "").lower() or None
    _host_memo = (sig, host)
    return host


def prefetch_prices() -> Optional[threading.Thread]:
    """Start loading the proxy's price list so the first turn is already priced."""
    try:
        host = _proxy_host()
        return _refresh_in_background(host) if host else None
    except Exception as e:
        logger.warning("provider pricing: prefetch failed: %s", e)
        return None


def turn_cost(result: Mapping) -> Optional[float]:
    """Cost of the turn described by a Claude Code ``result`` event.

    The provider's price when the turn ran through a known proxy and every
    model in ``modelUsage`` is priced, else Claude Code's ``total_cost_usd``.
    """
    native = result.get("total_cost_usd")
    try:
        usage = result.get("modelUsage")
        if not isinstance(usage, Mapping) or not usage:
            return native
        host = _proxy_host()
        if host is None:
            return native
        prices = _prices_for(host)
        if not prices:
            return native
        total = 0.0
        for model, u in usage.items():
            p = prices.get(model)
            if p is None:
                logger.info("provider pricing: no %s price for %r, keeping native cost", host, model)
                return native
            total += (
                (u.get("inputTokens") or 0) * p["prompt"]
                + (u.get("outputTokens") or 0) * p["completion"]
                + (u.get("cacheReadInputTokens") or 0) * p["cache_read"]
                + (u.get("cacheCreationInputTokens") or 0) * p["cache_write"]
            )
        total = round(total, 6)
        logger.info("provider pricing: %s cost $%s (native estimate $%s)", host, total, native)
        return total
    except Exception as e:
        logger.warning("provider pricing: re-pricing failed, keeping native cost: %s", e)
        return native
