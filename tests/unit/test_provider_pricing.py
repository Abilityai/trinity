"""Unit tests for provider_pricing.turn_cost.

Claude Code prices every turn from its Anthropic price table, so a turn routed
through OpenRouter to a non-Anthropic model reports ``total_cost_usd`` at
Anthropic prices (``"costBasis": "unknown"`` in ``modelUsage``). turn_cost
re-prices from ``modelUsage`` and the provider's price list, and must keep the
native figure whenever it cannot. It runs on the stdout reader thread, so it
must never fetch: the list loads in a background thread (#3489 review).
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid

import pytest

# tests/unit/conftest.py preloads the real agent_server package; just import.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services import jsonl_recovery, provider_pricing  # noqa: E402
from agent_server.services.error_classifier import _recover_metadata_from_raw_messages  # noqa: E402
from agent_server.services.provider_pricing import turn_cost  # noqa: E402
from agent_server.services.stream_parser import parse_stream_json_output, process_stream_line  # noqa: E402

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


REPRICED = round(26721 * 3e-7 + 4063 * 1.2e-6 + 132096 * 6e-9, 6)


def _route_through(monkeypatch, fetch, host="openrouter.ai"):
    monkeypatch.setattr(provider_pricing, "_cache", {})
    monkeypatch.setattr(provider_pricing, "_refreshing", set())
    monkeypatch.setattr(provider_pricing, "_PRICE_SOURCES", {"openrouter.ai": fetch})
    monkeypatch.setattr(provider_pricing, "_proxy_host", lambda: host)


@pytest.fixture
def proxy(monkeypatch):
    """Route through openrouter.ai with the price list already loaded (as after
    the boot prefetch); returns the fetch-call counter."""
    calls = []

    def fetch():
        calls.append(1)
        return PRICES

    _route_through(monkeypatch, fetch)
    provider_pricing._refresh("openrouter.ai", fetch)
    return calls


def test_reprices_from_model_usage(proxy):
    assert turn_cost(_result()) == pytest.approx(REPRICED)
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
    assert len(proxy) == 1  # only the warm-up fetch


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

    _route_through(monkeypatch, boom)
    provider_pricing._refresh("openrouter.ai", boom)
    assert turn_cost(_result()) == 0.311228
    assert turn_cost(_result()) == 0.311228
    assert len(calls) == 1  # no refetch per turn while the failure is fresh


def test_price_list_fetched_once(proxy):
    turn_cost(_result())
    turn_cost(_result())
    assert len(proxy) == 1


def test_proxy_host_from_execution_env(monkeypatch, tmp_path):
    monkeypatch.setattr(provider_pricing, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(provider_pricing, "_host_memo", (None, None))
    monkeypatch.setattr(provider_pricing, "build_execution_env",
                        lambda: {"ANTHROPIC_BASE_URL": "https://OpenRouter.ai/api"})
    assert provider_pricing._proxy_host() == "openrouter.ai"
    monkeypatch.setattr(provider_pricing, "_host_memo", (None, None))
    monkeypatch.setattr(provider_pricing, "build_execution_env", lambda: {})
    assert provider_pricing._proxy_host() is None


def test_proxy_host_reread_only_when_env_changes(monkeypatch, tmp_path):
    """build_execution_env logs per call, so it must not run per result event."""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_BASE_URL=https://openrouter.ai/api\n")
    calls = []

    def build():
        calls.append(1)
        line = env_file.read_text().strip()
        return {"ANTHROPIC_BASE_URL": line.split("=", 1)[1]} if line else {}

    monkeypatch.setattr(provider_pricing, "ENV_FILE", env_file)
    monkeypatch.setattr(provider_pricing, "_host_memo", (None, None))
    monkeypatch.setattr(provider_pricing, "build_execution_env", build)
    for _ in range(3):
        assert provider_pricing._proxy_host() == "openrouter.ai"
    assert len(calls) == 1

    env_file.write_text("")  # credentials reload drops the proxy
    assert provider_pricing._proxy_host() is None
    assert len(calls) == 2


# --- no network I/O on the result path -------------------------------------


def _wait_idle(timeout=2.0):
    deadline = time.monotonic() + timeout
    while provider_pricing._refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not provider_pricing._refreshing


def test_turn_cost_never_waits_for_the_fetch(monkeypatch):
    release = threading.Event()
    calls = []

    def slow_fetch():
        calls.append(1)
        assert release.wait(5)
        return PRICES

    _route_through(monkeypatch, slow_fetch)
    try:
        started = time.monotonic()
        assert turn_cost(_result()) == 0.311228  # cold cache: native, at once
        assert turn_cost(_result()) == 0.311228
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    _wait_idle()
    assert len(calls) == 1  # one fetch in flight, not one per turn
    assert turn_cost(_result()) == pytest.approx(REPRICED)


def test_prefetch_loads_prices_before_the_first_turn(monkeypatch):
    _route_through(monkeypatch, lambda: PRICES)
    thread = provider_pricing.prefetch_prices()
    assert thread is not None
    thread.join(2)
    assert turn_cost(_result()) == pytest.approx(REPRICED)


def test_prefetch_without_proxy_does_nothing(monkeypatch):
    _route_through(monkeypatch, lambda: PRICES, host=None)
    assert provider_pricing.prefetch_prices() is None


def test_failed_refresh_keeps_the_previous_list(monkeypatch):
    responses = [PRICES]

    def fetch():
        if responses:
            return responses.pop()
        raise OSError("network down")

    _route_through(monkeypatch, fetch)
    provider_pricing._refresh("openrouter.ai", fetch)
    # Expire the list; the next turn still prices from it while a refresh fails.
    monkeypatch.setitem(provider_pricing._cache, "openrouter.ai", (0.0, PRICES))
    assert turn_cost(_result()) == pytest.approx(REPRICED)
    _wait_idle()
    refresh_at, prices = provider_pricing._cache["openrouter.ai"]
    assert prices is PRICES
    assert refresh_at > time.monotonic()  # retried after _FAILURE_RETRY_S, not per turn


# --- _fetch_openrouter_prices: contract over a canned /api/v1/models ---------


def test_fetch_openrouter_prices_parses_models_payload(monkeypatch):
    payload = {"data": [
        {"id": FLASH, "pricing": {"prompt": "0.0000003", "completion": "0.0000012",
                                  "input_cache_read": "0.000000006", "input_cache_write": "0.00000035"}},
        # no cache prices: cached tokens are billed as prompt
        {"id": "vendor/no-cache", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        # an explicit "0" is a price (free cache reads), not a missing one
        {"id": "vendor/free-cache", "pricing": {"prompt": "0.000001", "completion": "0.000002",
                                                "input_cache_read": "0", "input_cache_write": 0}},
        {"id": "vendor/garbage", "pricing": {"prompt": "n/a", "completion": "-1"}},
        {"id": "vendor/no-completion", "pricing": {"prompt": "0.000001"}},
        {"pricing": {"prompt": "0.000001", "completion": "0.000002"}},
    ]}
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(provider_pricing.urllib.request, "urlopen", fake_urlopen)
    prices = provider_pricing._fetch_openrouter_prices()

    assert seen == {"url": "https://openrouter.ai/api/v1/models", "timeout": provider_pricing._FETCH_TIMEOUT_S}
    assert prices[FLASH] == pytest.approx({"prompt": 3e-7, "completion": 1.2e-6, "cache_read": 6e-9, "cache_write": 3.5e-7})
    assert prices["vendor/no-cache"] == pytest.approx({"prompt": 1e-6, "completion": 2e-6, "cache_read": 1e-6, "cache_write": 1e-6})
    assert prices["vendor/free-cache"]["cache_read"] == prices["vendor/free-cache"]["cache_write"] == 0.0
    assert prices["vendor/garbage"] == {"prompt": 0.0, "completion": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    assert set(prices) == {FLASH, "vendor/no-cache", "vendor/free-cache", "vendor/garbage"}


# --- every production reader of a result's cost goes through turn_cost ------


def test_batch_stream_parser_uses_turn_cost(proxy):
    _, _, metadata = parse_stream_json_output(json.dumps(_result()))
    assert metadata.cost_usd == pytest.approx(REPRICED)


def test_live_stream_parser_uses_turn_cost(proxy):
    """process_stream_line is what both spawn paths run on every stdout line."""
    metadata = ExecutionMetadata()
    process_stream_line(json.dumps(_result()), [], metadata, {}, [])
    assert metadata.cost_usd == pytest.approx(REPRICED)


def test_raw_messages_recovery_uses_turn_cost(proxy):
    metadata = ExecutionMetadata()
    assert _recover_metadata_from_raw_messages(metadata, [_result()])
    assert metadata.cost_usd == pytest.approx(REPRICED)


def test_jsonl_recovery_uses_turn_cost(proxy, monkeypatch, tmp_path):
    session_id = str(uuid.uuid4())
    (tmp_path / f"{session_id}.jsonl").write_text(json.dumps(_result()) + "\n")
    monkeypatch.setattr(jsonl_recovery, "_JSONL_PROJECTS_DIR", str(tmp_path))
    metadata = ExecutionMetadata()
    assert jsonl_recovery._recover_metadata_from_jsonl(session_id, since_iso=None, metadata=metadata)
    assert metadata.cost_usd == pytest.approx(REPRICED)
