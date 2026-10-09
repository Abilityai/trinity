"""Unit tests for provider_pricing.turn_cost.

Claude Code prices every turn from its Anthropic price table, so a turn routed
through OpenRouter to a non-Anthropic model reports ``total_cost_usd`` at
Anthropic prices (``"costBasis": "unknown"`` in ``modelUsage``). turn_cost
re-prices from ``modelUsage`` and the provider's price list, and must keep the
native figure whenever it cannot.
"""

from __future__ import annotations

import pytest

# tests/unit/conftest.py preloads the real agent_server package; just import.
from agent_server.services import provider_pricing  # noqa: E402
from agent_server.services.provider_pricing import turn_cost  # noqa: E402
from agent_server.services.stream_parser import parse_stream_json_output  # noqa: E402

FLASH = "deepseek/deepseek-v4.1-flash"
PRICES = {
    FLASH: {"prompt": 3e-7, "completion": 1.2e-6, "cache_read": 6e-9, "cache_write": 3e-7},
}


def _result(model_usage=None, native=0.311228):
    return {
        "type": "result",
        "total_cost_usd": native,
        "duration_ms": 22202,
        "num_turns": 10,
        "result": "done",
        "modelUsage": model_usage if model_usage is not None else {
            FLASH: {
                "inputTokens": 26721,
                "outputTokens": 4063,
                "cacheReadInputTokens": 132096,
                "cacheCreationInputTokens": 0,
                "costUSD": native,
                "costBasis": "unknown",
            },
        },
    }


@pytest.fixture
def proxy(monkeypatch):
    """Route through openrouter.ai with a fixed price list; returns the fetch-call counter."""
    calls = []

    def fetch():
        calls.append(1)
        return PRICES

    monkeypatch.setattr(provider_pricing, "_cache", {})
    monkeypatch.setattr(provider_pricing, "_PRICE_SOURCES", {"openrouter.ai": fetch})
    monkeypatch.setattr(provider_pricing, "_proxy_host", lambda: "openrouter.ai")
    return calls


def test_reprices_from_model_usage(proxy):
    expected = round(26721 * 3e-7 + 4063 * 1.2e-6 + 132096 * 6e-9, 6)
    assert turn_cost(_result()) == pytest.approx(expected)
    assert turn_cost(_result()) < 0.02  # native was 0.31


def test_sums_every_model_of_the_turn(proxy, monkeypatch):
    side = "qwen/qwen-small"
    monkeypatch.setitem(PRICES, side, {"prompt": 1e-7, "completion": 2e-7, "cache_read": 1e-7, "cache_write": 1e-7})
    usage = {
        FLASH: {"inputTokens": 1000, "outputTokens": 100},
        side: {"inputTokens": 500, "outputTokens": 50},
    }
    assert turn_cost(_result(usage)) == pytest.approx(round(1000 * 3e-7 + 100 * 1.2e-6 + 500 * 1e-7 + 50 * 2e-7, 6))


def test_unpriced_model_keeps_native(proxy):
    usage = {"vendor/unknown-model": {"inputTokens": 1000, "outputTokens": 100}}
    assert turn_cost(_result(usage)) == 0.311228


def test_no_proxy_keeps_native(proxy, monkeypatch):
    monkeypatch.setattr(provider_pricing, "_proxy_host", lambda: None)
    assert turn_cost(_result()) == 0.311228
    assert proxy == []


def test_unknown_proxy_host_keeps_native(proxy, monkeypatch):
    monkeypatch.setattr(provider_pricing, "_proxy_host", lambda: "litellm.internal")
    assert turn_cost(_result()) == 0.311228


def test_missing_model_usage_keeps_native(proxy):
    msg = _result()
    del msg["modelUsage"]
    assert turn_cost(msg) == 0.311228
    assert turn_cost(_result({})) == 0.311228


def test_fetch_failure_keeps_native_and_is_cached(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise OSError("network down")

    monkeypatch.setattr(provider_pricing, "_cache", {})
    monkeypatch.setattr(provider_pricing, "_PRICE_SOURCES", {"openrouter.ai": boom})
    monkeypatch.setattr(provider_pricing, "_proxy_host", lambda: "openrouter.ai")
    assert turn_cost(_result()) == 0.311228
    assert turn_cost(_result()) == 0.311228
    assert len(calls) == 1  # no refetch per turn while the failure is fresh


def test_price_list_fetched_once(proxy):
    turn_cost(_result())
    turn_cost(_result())
    assert len(proxy) == 1


def test_proxy_host_from_execution_env(monkeypatch):
    monkeypatch.setattr(provider_pricing, "build_execution_env",
                        lambda: {"ANTHROPIC_BASE_URL": "https://OpenRouter.ai/api"})
    assert provider_pricing._proxy_host() == "openrouter.ai"
    monkeypatch.setattr(provider_pricing, "build_execution_env", lambda: {})
    assert provider_pricing._proxy_host() is None


def test_stream_parser_uses_turn_cost(proxy):
    import json
    _, _, metadata = parse_stream_json_output(json.dumps(_result()))
    assert metadata.cost_usd < 0.02
