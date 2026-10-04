"""Card (fiat) plans must be payable: the plan's x402 scheme reaches the
facilitator (abilityai/trinity#3215).

Before this, `_build_payment_required` passed no `scheme=` to the SDK helper, so
every 402 — and every verify and settle built from the same helper — advertised
the SDK's default `nvm:erc4337`. The facilitator is handed that requirements
document verbatim (`x402/facilitator_api.py` POSTs `model_dump(by_alias=True)`),
so `accepts[0].scheme` is the ONLY channel the scheme travels through: a card
plan's token was always checked against crypto requirements and rejected.

What these tests pin:

* **A1** a card plan's 402 advertises `nvm:card-delegation` on its fiat network;
* **A2** a crypto plan's bytes are unchanged, per environment, as frozen literals
  (not "whatever the SDK derives" — a payments-py bump must not move a live
  agent's network silently, #3216);
* **A3** the 402, the verify and the settle agree — the property AC2 names;
* **A4** Trinity's own plan parser agrees with the SDK's resolver on the same
  payload (we parse ourselves so a failure is a WARN instead of the SDK's DEBUG);
* **A5** a flood of cold 402s costs ONE `get_plan` and ZERO facilitator slots;
* **A6** `scheme_from_token` is an allow-list over a caller-supplied field;
* **A7** a settle re-driven after the cache expired, with the plan lookup now
  failing, still settles as card — because it reads the token, not the plan.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from payments_py.x402.resolve_scheme import (  # noqa: E402
    clear_scheme_cache,
    resolve_network as sdk_resolve_network,
    resolve_scheme as sdk_resolve_scheme,
)
from services import nevermined_payment_service as nps  # noqa: E402

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _config(environment="sandbox", plan_id="plan-1"):
    return SimpleNamespace(
        agent_name="agent-a", nvm_plan_id=plan_id, nvm_agent_id="did:nv:1",
        nvm_environment=environment, credits_per_request=1, enabled=True,
    )


CRYPTO_PLAN = {"registry": {"price": {"isCrypto": True}}}
CARD_PLAN = {
    "registry": {"price": {"isCrypto": False}},
    "metadata": {"plan": {"fiatPaymentProvider": "stripe"}},
}
CARD_PLAN_BRAINTREE = {
    "registry": {"price": {"isCrypto": False}},
    "metadata": {"plan": {"fiatPaymentProvider": "braintree"}},
}
CARD_PLAN_NO_PROVIDER = {"registry": {"price": {"isCrypto": False}}}


class _Plans:
    """Stands in for `payments.plans` — counts the lookups."""

    def __init__(self, plan=None, raises=None, delay=0.0):
        self.plan = plan
        self.raises = raises
        self.delay = delay
        self.calls: list[str] = []

    def get_plan(self, plan_id):
        self.calls.append(plan_id)
        if self.delay:
            import time as _t
            _t.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.plan


class _Facilitator:
    def __init__(self, *, verify=None, settle=None):
        self._verify = verify
        self._settle = settle
        self.verify_args: list = []
        self.settle_args: list = []

    def verify_permissions(self, payment_required, access_token, *rest):
        self.verify_args.append(payment_required)
        return self._verify

    def settle_permissions(self, payment_required, access_token, max_amount=None,
                           agent_request_id=None):
        self.settle_args.append(payment_required)
        return self._settle


def _service(*, plans=None, facilitator=None, slot_spy=None):
    svc = nps.NeverminedPaymentService()
    client = SimpleNamespace(
        facilitator=facilitator or _Facilitator(),
        plans=plans if plans is not None else _Plans(CRYPTO_PLAN),
    )
    svc._get_payments_client = lambda *a, **k: client
    return svc


def _verify_reply(valid=True):
    return SimpleNamespace(is_valid=valid, payer="0xpayer", agent_request_id="areq-1",
                           invalid_reason=None if valid else "nope")


def _settle_reply(success=True):
    return SimpleNamespace(success=success, payer="0xpayer", credits_redeemed="1",
                           remaining_balance="9", transaction="0xtx", error_reason=None)


def _token(*, scheme="nvm:card-delegation", network="stripe", plan_id="plan-1",
           accepted=True):
    """A real x402 access token envelope, encoded exactly as the SDK mints it."""
    from payments_py.x402.token import encode_access_token
    body: dict = {"authorization": {"from": "0xpayer"}}
    if accepted:
        body["accepted"] = {"scheme": scheme, "network": network, "planId": plan_id}
    return encode_access_token(body)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Clean per-loop gates and an empty scheme cache for every test."""
    monkeypatch.setattr(nps, "_FACILITATOR_GATES", type(nps._FACILITATOR_GATES)())
    monkeypatch.setattr(nps, "_PLAN_LOOKUP_GATES", type(nps._PLAN_LOOKUP_GATES)())
    monkeypatch.setattr(nps, "_PLAN_LOOKUP_INFLIGHT", type(nps._PLAN_LOOKUP_INFLIGHT)())
    nps.clear_plan_scheme_cache()
    yield
    nps.clear_plan_scheme_cache()


# ---------------------------------------------------------------------------
# A1 — a card plan's 402 is payable by card
# ---------------------------------------------------------------------------

class TestCardPlan402:

    async def test_a_card_plan_402_advertises_the_card_scheme(self):
        svc = _service(plans=_Plans(CARD_PLAN))
        scheme = await svc.resolve_plan_scheme("k", "sandbox", _config())
        body = svc.build_402_response(_config(), "https://x.test", plan_scheme=scheme)
        assert body["accepts"][0]["scheme"] == "nvm:card-delegation"
        assert body["accepts"][0]["network"] == "stripe"

    async def test_the_fiat_provider_picks_the_network(self):
        svc = _service(plans=_Plans(CARD_PLAN_BRAINTREE))
        assert await svc.resolve_plan_scheme("k", "sandbox", _config()) == \
            nps.PlanScheme("nvm:card-delegation", "braintree")

    async def test_a_card_plan_without_a_provider_falls_back_to_stripe(self):
        svc = _service(plans=_Plans(CARD_PLAN_NO_PROVIDER))
        assert await svc.resolve_plan_scheme("k", "sandbox", _config()) == \
            nps.PlanScheme("nvm:card-delegation", "stripe")

    async def test_the_stripe_fallback_is_the_sdks_own_default(self):
        from payments_py.x402.schemes import get_default_network
        assert nps.CARD_NETWORK_DEFAULT == get_default_network("nvm:card-delegation")

    async def test_a_lookup_failure_warns_and_keeps_todays_bytes(self, caplog):
        svc = _service(plans=_Plans(raises=RuntimeError("nvm down")))
        with caplog.at_level("WARNING"):
            scheme = await svc.resolve_plan_scheme("k", "live", _config())
        assert scheme == nps.PlanScheme("nvm:erc4337", "eip155:8453")
        # Honest status: the SDK swallows this at DEBUG, which is how a card
        # plan silently stayed crypto with nothing in Trinity's logs.
        assert any("plan lookup failed" in r.getMessage() for r in caplog.records)
        # The key is never logged.
        assert not any("k" == r.getMessage() for r in caplog.records)

    async def test_a_failure_is_cached_so_an_outage_is_not_re_dialled(self):
        plans = _Plans(raises=RuntimeError("nvm down"))
        svc = _service(plans=plans)
        for _ in range(5):
            await svc.resolve_plan_scheme("k", "sandbox", _config())
        assert len(plans.calls) == 1

    async def test_last_known_good_is_served_through_an_outage(self, monkeypatch):
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans)
        assert (await svc.resolve_plan_scheme("k", "sandbox", _config())).scheme == \
            "nvm:card-delegation"
        # TTL expires; the plan lookup now fails.
        nps._PLAN_SCHEME_CACHE[("sandbox", "plan-1")].fetched_at -= 10_000
        plans.raises = RuntimeError("nvm down")
        assert (await svc.resolve_plan_scheme("k", "sandbox", _config())).scheme == \
            "nvm:card-delegation"

    async def test_a_custom_environment_without_a_reachable_backend_keeps_the_default(self):
        svc = _service(plans=_Plans(raises=ConnectionError("no route")))
        assert await svc.resolve_plan_scheme("k", "custom", _config()) == \
            nps.PlanScheme("nvm:erc4337", "eip155:84532")

    async def test_an_agent_with_no_plan_id_never_dials_out(self):
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans)
        assert await svc.resolve_plan_scheme("k", "sandbox", _config(plan_id="")) == \
            nps.PlanScheme("nvm:erc4337", "eip155:84532")
        assert plans.calls == []

    async def test_the_cache_is_keyed_on_environment_too(self):
        """The SDK's cache is plan_id-only, which can serve a sandbox answer live."""
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans)
        await svc.resolve_plan_scheme("k", "sandbox", _config())
        await svc.resolve_plan_scheme("k", "live", _config())
        assert len(plans.calls) == 2


# ---------------------------------------------------------------------------
# A2 — crypto bytes are frozen
# ---------------------------------------------------------------------------

class TestCryptoBytesUnchanged:

    GOLDEN = {
        "sandbox": "eip155:84532",
        "staging_sandbox": "eip155:84532",
        "live": "eip155:8453",
        "staging_live": "eip155:8453",
        "custom": "eip155:84532",
        "something-nobody-has-shipped": "eip155:84532",
    }

    @pytest.mark.parametrize("env", sorted(GOLDEN))
    async def test_no_plan_scheme_is_todays_document(self, env):
        body = nps.NeverminedPaymentService().build_402_response(
            _config(env), "http://localhost"
        )
        accepts = body["accepts"][0]
        assert accepts["scheme"] == "nvm:erc4337"
        assert accepts["network"] == self.GOLDEN[env]

    @pytest.mark.parametrize("env", sorted(GOLDEN))
    async def test_a_resolved_crypto_plan_is_byte_identical_to_no_plan_scheme(self, env):
        svc = _service(plans=_Plans(CRYPTO_PLAN))
        scheme = await svc.resolve_plan_scheme("k", env, _config(env))
        with_scheme = svc.build_402_response(
            _config(env), "http://localhost", plan_scheme=scheme
        )
        without = svc.build_402_response(_config(env), "http://localhost")
        assert with_scheme == without

    async def test_the_environment_network_map_is_the_frozen_literal(self):
        assert nps._ERC4337_NETWORK_BY_ENV == {
            "sandbox": "eip155:84532",
            "staging_sandbox": "eip155:84532",
            "live": "eip155:8453",
            "staging_live": "eip155:8453",
            "custom": "eip155:84532",
        }
        assert nps._ERC4337_NETWORK_FALLBACK == "eip155:84532"


# ---------------------------------------------------------------------------
# A3 — 402 == verify == settle
# ---------------------------------------------------------------------------

class TestOneDocumentThreeCallSites:

    async def test_the_402_the_verify_and_the_settle_agree_for_a_card_token(self):
        facilitator = _Facilitator(verify=_verify_reply(), settle=_settle_reply())
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans, facilitator=facilitator)
        token = _token()

        scheme = await svc.resolve_plan_scheme("k", "sandbox", _config())
        body = svc.build_402_response(_config(), "https://x.test", plan_scheme=scheme)

        await svc.verify_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token=token,
                                 base_url="https://x.test")
        await svc.settle_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token=token,
                                 base_url="https://x.test")

        verified = facilitator.verify_args[0].accepts[0]
        settled = facilitator.settle_args[0].accepts[0]
        assert body["accepts"][0]["scheme"] == verified.scheme == settled.scheme \
            == "nvm:card-delegation"
        assert body["accepts"][0]["network"] == verified.network == settled.network \
            == "stripe"

    async def test_the_money_path_needs_no_plan_lookup_at_all(self):
        """Every network call removed from verify/settle (the AC2 insurance)."""
        plans = _Plans(CARD_PLAN)
        facilitator = _Facilitator(verify=_verify_reply(), settle=_settle_reply())
        svc = _service(plans=plans, facilitator=facilitator)
        token = _token()
        await svc.verify_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token=token)
        await svc.settle_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token=token)
        assert plans.calls == []

    async def test_a_settle_retries_against_byte_identical_requirements(self):
        facilitator = _Facilitator(verify=_verify_reply(), settle=_settle_reply(False))
        svc = _service(plans=_Plans(CARD_PLAN), facilitator=facilitator)
        result = await svc.settle_payment(
            nvm_api_key="k", nvm_environment="sandbox", config=_config(),
            access_token=_token(),
        )
        assert result.success is False
        assert len(facilitator.settle_args) == 1
        assert facilitator.settle_args[0].accepts[0].scheme == "nvm:card-delegation"


# ---------------------------------------------------------------------------
# A4 — parser parity with the SDK's own resolver
# ---------------------------------------------------------------------------

class TestParserParityWithSdk:

    @pytest.mark.parametrize("plan", [
        CRYPTO_PLAN,
        CARD_PLAN,
        CARD_PLAN_BRAINTREE,
        CARD_PLAN_NO_PROVIDER,
        {},
        {"registry": "not-a-dict"},
        {"registry": {"price": "not-a-dict"}},
        {"registry": {"price": {}}},
        {"registry": {"price": {"isCrypto": None}}},
        {"registry": {"price": {"isCrypto": False}}, "metadata": "not-a-dict"},
        {"registry": {"price": {"isCrypto": False}}, "metadata": {"plan": "nope"}},
        None,
        "not-a-dict",
    ])
    async def test_scheme_matches_the_sdk_on_the_same_payload(self, plan):
        clear_scheme_cache()
        payments = SimpleNamespace(plans=_Plans(plan))
        assert nps._parse_plan(plan, "sandbox").scheme == \
            sdk_resolve_scheme(payments, "plan-1")

    @pytest.mark.parametrize("plan", [CARD_PLAN, CARD_PLAN_BRAINTREE])
    async def test_card_network_matches_the_sdks_resolve_network(self, plan):
        clear_scheme_cache()
        payments = SimpleNamespace(plans=_Plans(plan))
        assert nps._parse_plan(plan, "sandbox").network == \
            sdk_resolve_network(payments, "plan-1")

    async def test_the_sdk_returns_no_network_for_a_crypto_plan_so_we_keep_our_map(self):
        clear_scheme_cache()
        payments = SimpleNamespace(plans=_Plans(CRYPTO_PLAN))
        assert sdk_resolve_network(payments, "plan-1") is None
        assert nps._parse_plan(CRYPTO_PLAN, "live").network == "eip155:8453"

    async def test_the_supported_network_literal_matches_the_sdk(self):
        from typing import get_args

        from payments_py.x402.networks import SupportedNetworks
        assert nps._SUPPORTED_NETWORKS == frozenset(get_args(SupportedNetworks))


# ---------------------------------------------------------------------------
# A5 — an anonymous 402 flood costs one lookup and no facilitator slot
# ---------------------------------------------------------------------------

class TestLookupIsBounded:

    async def test_fifty_cold_402s_cost_one_get_plan_and_no_facilitator_slot(self):
        plans = _Plans(CARD_PLAN, delay=0.05)
        svc = _service(plans=plans)
        facilitator_gate = nps._FACILITATOR_GATES

        results = await asyncio.gather(*[
            svc.resolve_plan_scheme("k", "sandbox", _config()) for _ in range(50)
        ])
        assert len(plans.calls) == 1
        assert all(r.scheme == "nvm:card-delegation" for r in results)
        # The facilitator gate was never even instantiated — a 402 flood cannot
        # starve the paying verify/settle path.
        assert len(facilitator_gate) == 0

    async def test_the_plan_lookup_gate_is_its_own_small_bound(self):
        assert nps.NEVERMINED_PLAN_LOOKUP_MAX_INFLIGHT == 2
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans)
        await svc.resolve_plan_scheme("k", "sandbox", _config())
        gate = list(nps._PLAN_LOOKUP_GATES.values())[0]
        assert gate._value == 2

    async def test_the_in_flight_registry_is_emptied_after_the_lookup(self):
        svc = _service(plans=_Plans(CARD_PLAN))
        await svc.resolve_plan_scheme("k", "sandbox", _config())
        assert all(not d for d in nps._PLAN_LOOKUP_INFLIGHT.values())


# ---------------------------------------------------------------------------
# A6 — the token is an allow-list, not a trusted input
# ---------------------------------------------------------------------------

class TestSchemeFromToken:

    async def test_a_card_token_names_its_own_scheme(self):
        svc = nps.NeverminedPaymentService()
        assert svc.scheme_from_token(_token(), _config()) == \
            nps.PlanScheme("nvm:card-delegation", "stripe")

    @pytest.mark.parametrize("token,why", [
        (_token(accepted=False), "no accepted block"),
        (_token(scheme="nvm:bearer-of-good-news"), "scheme not in the SDK's set"),
        (_token(network="eip155:1"), "network not in SupportedNetworks"),
        (_token(network="solana"), "network not in SupportedNetworks"),
        (_token(plan_id="someone-elses-plan"), "planId is not this agent's"),
        ("not-base64-at-all!!!", "undecodable"),
        ("", "empty"),
        (None, "not a string"),
        (b"bytes", "not a string"),
        (12345, "not a string"),
    ])
    async def test_anything_else_is_not_a_source_of_truth(self, token, why):
        svc = nps.NeverminedPaymentService()
        assert svc.scheme_from_token(token, _config()) is None, why

    async def test_a_token_without_an_accepted_block_behaves_exactly_as_today(self):
        """The fallback chain: token -> plan -> today's default."""
        facilitator = _Facilitator(verify=_verify_reply())
        svc = _service(plans=_Plans(CRYPTO_PLAN), facilitator=facilitator)
        await svc.verify_payment(nvm_api_key="k", nvm_environment="live",
                                 config=_config("live"),
                                 access_token=_token(accepted=False))
        accepts = facilitator.verify_args[0].accepts[0]
        assert (accepts.scheme, accepts.network) == ("nvm:erc4337", "eip155:8453")

    async def test_a_tokenless_verify_against_a_card_plan_falls_back_to_the_plan(self):
        facilitator = _Facilitator(verify=_verify_reply())
        svc = _service(plans=_Plans(CARD_PLAN), facilitator=facilitator)
        await svc.verify_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token="opaque-token")
        assert facilitator.verify_args[0].accepts[0].scheme == "nvm:card-delegation"

    async def test_a_verify_survives_a_payments_client_without_plans(self):
        """The pre-#3215 fakes hand back a client with a facilitator and nothing else."""
        facilitator = _Facilitator(verify=_verify_reply())
        svc = nps.NeverminedPaymentService()
        svc._get_payments_client = lambda *a, **k: SimpleNamespace(
            facilitator=facilitator
        )
        result = await svc.verify_payment(nvm_api_key="k", nvm_environment="sandbox",
                                          config=_config(), access_token="tok")
        assert result.success is True
        assert facilitator.verify_args[0].accepts[0].scheme == "nvm:erc4337"


# ---------------------------------------------------------------------------
# A7 — a settle long after its verify
# ---------------------------------------------------------------------------

class TestSettleAfterTheCacheIsGone:

    async def test_a_resettle_hours_later_is_still_a_card_settle(self):
        facilitator = _Facilitator(settle=_settle_reply())
        plans = _Plans(CARD_PLAN)
        svc = _service(plans=plans, facilitator=facilitator)
        token = _token()

        # Verify at T0 while the plan was reachable.
        await svc.settle_payment(nvm_api_key="k", nvm_environment="sandbox",
                                 config=_config(), access_token=token)
        # Hours later: cache gone, Nevermined unreachable.
        nps.clear_plan_scheme_cache()
        plans.raises = RuntimeError("nvm down")

        facilitator.settle_args.clear()
        result = await svc.settle_payment(nvm_api_key="k", nvm_environment="sandbox",
                                          config=_config(), access_token=token)
        assert result.success is True
        assert facilitator.settle_args[0].accepts[0].scheme == "nvm:card-delegation"
        assert plans.calls == []
