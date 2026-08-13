"""Claude subscription and metered API usage must never share semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_models import SubscriptionUsageWindow, SubscriptionUtilizationAvailable
from services import subscription_service
from services.subscription_service import build_agent_usage_presentation


def test_subscription_usage_names_the_subscription_and_has_no_cost_metric():
    usage = build_agent_usage_presentation(
        subscription=SimpleNamespace(
            id="sub-1",
            name="studio-max",
            subscription_type="max",
        ),
        has_api_key=True,
    )

    payload = usage.model_dump(mode="json")
    assert payload["billing_mode"] == "subscription"
    assert payload["provider"] == "anthropic"
    assert payload["subscription"] == {
        "id": "sub-1",
        "name": "studio-max",
        "plan": "max",
    }
    assert payload["utilization"]["status"] == "unavailable"
    assert payload["utilization"]["percent"] is None
    assert payload["utilization"]["last_updated_at"] is None
    assert payload["utilization"]["reason"] == "provider_signal_unavailable"
    assert "cost" not in payload


def test_api_usage_is_explicitly_metered_and_cost_oriented():
    usage = build_agent_usage_presentation(subscription=None, has_api_key=True)

    assert usage.model_dump(mode="json") == {
        "billing_mode": "api",
        "provider": "anthropic",
        "metering": "metered",
        "cost_currency": "USD",
    }


def test_unconfigured_usage_does_not_claim_subscription_or_api_billing():
    usage = build_agent_usage_presentation(subscription=None, has_api_key=False)

    assert usage.model_dump(mode="json") == {
        "billing_mode": "unconfigured",
        "provider": "anthropic",
    }


def test_available_subscription_utilization_carries_percent_window_and_freshness():
    utilization = SubscriptionUtilizationAvailable(
        percent=42.5,
        window="5-hour rolling window",
        resets_at="2026-08-13T20:00:00Z",
        last_updated_at="2026-08-13T18:00:00Z",
    )

    payload = utilization.model_dump(mode="json")
    assert payload == {
        "status": "available",
        "percent": 42.5,
        "window": "5-hour rolling window",
        "resets_at": "2026-08-13T20:00:00Z",
        "last_updated_at": "2026-08-13T18:00:00Z",
    }


def test_subscription_activity_window_has_no_dollar_cost_field():
    payload = SubscriptionUsageWindow(
        input_tokens=1200,
        output_tokens=300,
        message_count=4,
    ).model_dump()

    assert payload == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "message_count": 4,
    }


@pytest.mark.asyncio
async def test_non_claude_runtime_does_not_receive_anthropic_usage(monkeypatch):
    monkeypatch.setattr(subscription_service.db, "get_agent_subscription", lambda _name: None)
    monkeypatch.setattr(subscription_service.db, "get_use_platform_api_key", lambda _name: True)
    monkeypatch.setattr(subscription_service, "get_agent_runtime", lambda _name: "codex")

    status = await subscription_service.get_agent_auth_mode("codex-agent")

    assert status.auth_mode == "api_key"
    assert status.usage is None
