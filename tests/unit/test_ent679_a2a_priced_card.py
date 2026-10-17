"""ent#679 checkpoint C — the A2A card states the price (AC2).

A stranger could meet the 402 from `POST /a2a/{name}` only by calling first and
being refused. The card is A2A's discovery document, so a priced agent now
declares its price there: an x402-speaking client mints a token from `agentId` +
`planId` off the card alone and meets the paywall on its first request.

What this file proves, each at the layer it lives in:

1. **The unpriced card is byte-identical** (the hard line). No price config, a
   disabled one, or a config missing its ids ⇒ the same card object, by
   identity. Every A2A install that does not sell anything is untouched.
2. **The priced card carries the Nevermined vocabulary** and nothing else — the
   official Google/Coinbase x402 extension URI is deliberately absent, because
   declaring it advertises an activation handshake Trinity does not run.
3. **Both card surfaces carry it** (ent#180 FR-3): the public well-known card
   and the authenticated per-agent card go through the one producer, driven here
   over a real `TestClient` rather than asserted from source text.
4. **`generate_a2a_card` stays pure** — no payment knowledge leaked into the
   builder, no I/O.
5. **T9: 0 credits is legal.** A duration plan charges by time; the model
   accepts 0, a negative is still a named 422, and the card declares such a
   plan as `dynamic` rather than contradicting itself with `fixed`/0.
6. **Fail-open.** An unreadable payment config serves the card anyway — a card
   route must never 5xx (the pre-existing contract for an unreachable agent).
"""
from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
from services import a2a_card_service, a2a_gate  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "bot"

#: The official A2A x402 extension URI the provider SDK's own card helper
#: appends and this implementation must NOT (plan §3.5): declaring an extension
#: means offering its `X-A2A-Extensions` activation handshake, and Trinity runs
#: no handshake — it reads in-band metadata and answers 402. A client that
#: activated it would wait for a negotiation that never comes.
OFFICIAL_X402_URI = (
    "https://github.com/google-agentic-commerce/a2a-x402/blob/main/spec/v0.2"
)

TEMPLATE = {
    "display_name": "Bot",
    "description": "a bot",
    "capabilities": ["chat", "research"],
    "use_cases": ["ask it things"],
}


def _config(*, enabled=True, credits=2, plan="plan-1", agent_id="agent-1",
            environment="sandbox"):
    return types.SimpleNamespace(
        agent_name=AGENT,
        enabled=enabled,
        credits_per_request=credits,
        nvm_plan_id=plan,
        nvm_agent_id=agent_id,
        nvm_environment=environment,
    )


def _base_card():
    return a2a_card_service.generate_a2a_card(
        agent_name=AGENT, template_data=TEMPLATE, base_url="http://t.example"
    )


def _extensions(card):
    return (card.get("capabilities") or {}).get("extensions") or []


def _payment_ext(card):
    exts = [
        e for e in _extensions(card)
        if e.get("uri") == a2a_card_service.NEVERMINED_PAYMENT_EXTENSION_URI
    ]
    assert len(exts) == 1, f"expected exactly one payment extension, got {exts}"
    return exts[0]


# --------------------------------------------------------------------------- #
# 1. The unpriced card is byte-identical
# --------------------------------------------------------------------------- #
class TestUnpricedCardIsUnchanged:
    """The hard line. An install that sells nothing must not see a byte move."""

    def test_no_config_returns_the_same_object(self):
        card = _base_card()
        out = a2a_card_service.with_payment_extension(
            card, None, agent_name=AGENT, base_url="http://t.example"
        )
        # Identity, not equality: don't even rebuild the dict.
        assert out is card
        assert "extensions" not in (card.get("capabilities") or {})

    def test_disabled_config_returns_the_same_object(self):
        card = _base_card()
        out = a2a_card_service.with_payment_extension(
            card, _config(enabled=False), agent_name=AGENT, base_url="http://t.example"
        )
        assert out is card

    @pytest.mark.parametrize("missing", ["plan", "agent_id"])
    def test_a_config_that_cannot_say_what_to_buy_is_silent(self, missing):
        """An enabled config with no plan (or no agent) id would activate a
        payment flow a client cannot complete. Silence beats a dead end."""
        card = _base_card()
        cfg = _config(**{missing: ""})
        out = a2a_card_service.with_payment_extension(
            card, cfg, agent_name=AGENT, base_url="http://t.example"
        )
        assert out is card

    def test_the_input_card_is_never_mutated_when_priced(self):
        card = _base_card()
        before = copy.deepcopy(card)
        a2a_card_service.with_payment_extension(
            card, _config(), agent_name=AGENT, base_url="http://t.example"
        )
        assert card == before


# --------------------------------------------------------------------------- #
# 2. The priced card's vocabulary
# --------------------------------------------------------------------------- #
class TestPricedCardShape:
    def test_params_carry_what_a_client_needs_to_pay(self):
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(credits=3), agent_name=AGENT,
            base_url="http://t.example",
        )
        ext = _payment_ext(card)
        assert ext["required"] is False
        assert ext["params"] == {
            "agentId": "agent-1",
            "planId": "plan-1",
            "credits": 3,
            "paymentType": "fixed",
            "costDescription": "3 credits per call via Nevermined plan plan-1",
            "environment": "sandbox",
            "paymentInfoUrl": "http://t.example/api/paid/bot/info",
        }
        assert ext["description"] == ext["params"]["costDescription"]

    def test_one_credit_is_singular(self):
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(credits=1), agent_name=AGENT, base_url="http://x"
        )
        assert _payment_ext(card)["params"]["costDescription"] == (
            "1 credit per call via Nevermined plan plan-1"
        )

    def test_the_official_x402_extension_uri_is_not_declared(self):
        """We speak the payment vocabulary; we do not offer the handshake."""
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(), agent_name=AGENT, base_url="http://x"
        )
        assert all(e.get("uri") != OFFICIAL_X402_URI for e in _extensions(card))

    def test_payment_info_url_is_omitted_without_a_base_url(self):
        """`generate_a2a_card` omits `url` when it has no base; a relative
        "where to buy" pointer would be worse than none."""
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(), agent_name=AGENT, base_url=""
        )
        assert "paymentInfoUrl" not in _payment_ext(card)["params"]

    def test_an_existing_extension_is_preserved(self):
        card = _base_card()
        card["capabilities"]["extensions"] = [{"uri": "urn:other"}]
        out = a2a_card_service.with_payment_extension(
            card, _config(), agent_name=AGENT, base_url="http://x"
        )
        assert [e["uri"] for e in _extensions(out)] == [
            "urn:other", a2a_card_service.NEVERMINED_PAYMENT_EXTENSION_URI,
        ]

    def test_the_rest_of_the_card_is_untouched(self):
        base = _base_card()
        out = a2a_card_service.with_payment_extension(
            base, _config(), agent_name=AGENT, base_url="http://t.example"
        )
        for key, value in base.items():
            if key == "capabilities":
                continue
            assert out[key] == value
        # The pre-existing capability flags survive alongside `extensions`.
        assert out["capabilities"]["streaming"] is True
        assert out["capabilities"]["pushNotifications"] is False


# --------------------------------------------------------------------------- #
# 3. T9 — 0 credits is a duration plan, not free
# --------------------------------------------------------------------------- #
class TestZeroCredits:
    def test_the_model_accepts_zero(self):
        """A Nevermined duration plan charges by time, so the per-call credit
        amount is honestly 0. The old `>= 1` floor forced the operator to
        claim a per-call price nothing would ever charge."""
        from db_models import NeverminedConfigCreate

        cfg = NeverminedConfigCreate(
            nvm_api_key="sandbox:jwt", nvm_agent_id="a", nvm_plan_id="p",
            credits_per_request=0,
        )
        assert cfg.credits_per_request == 0

    def test_the_model_still_rejects_a_negative(self):
        import pydantic
        from db_models import NeverminedConfigCreate

        with pytest.raises(pydantic.ValidationError) as exc:
            NeverminedConfigCreate(
                nvm_api_key="sandbox:jwt", nvm_agent_id="a", nvm_plan_id="p",
                credits_per_request=-1,
            )
        assert "credits_per_request must be >= 0" in str(exc.value)

    def test_the_model_default_is_still_one(self):
        from db_models import NeverminedConfigCreate

        cfg = NeverminedConfigCreate(
            nvm_api_key="sandbox:jwt", nvm_agent_id="a", nvm_plan_id="p",
        )
        assert cfg.credits_per_request == 1

    def test_zero_declares_a_dynamic_cost_not_a_fixed_zero(self):
        """`{paymentType: "fixed", credits: 0}` is a contradiction — it reads as
        free. The provider SDK's own card validator rejects exactly that shape
        for a paid plan."""
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(credits=0), agent_name=AGENT, base_url="http://x"
        )
        params = _payment_ext(card)["params"]
        assert params["paymentType"] == "dynamic"
        assert params["credits"] == 0
        assert params["costDescription"] == (
            "Cost per call is set by Nevermined plan plan-1"
        )

    def test_an_unreadable_credit_amount_does_not_break_the_card(self):
        card = a2a_card_service.with_payment_extension(
            _base_card(), _config(credits=None), agent_name=AGENT, base_url="http://x"
        )
        assert _payment_ext(card)["params"]["credits"] == 0


# --------------------------------------------------------------------------- #
# 4. `generate_a2a_card` stays pure
# --------------------------------------------------------------------------- #
class TestBuilderStaysPure:
    def test_the_pure_builder_declares_no_extensions(self):
        card = _base_card()
        assert "extensions" not in card["capabilities"]
        assert "urn:nevermined:payment" not in str(card)


# --------------------------------------------------------------------------- #
# 5. Both card surfaces, over the real routes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cards(monkeypatch):
    """Both card routes on a TestClient, with the payment config switchable."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = {"config": None, "raises": False}

    def _get_config(name):
        if state["raises"]:
            raise RuntimeError("encryption service down")
        return state["config"] if name == AGENT else None

    monkeypatch.setattr(a2a, "db", types.SimpleNamespace(
        get_a2a_exposed=lambda name: name == AGENT,
        get_nevermined_config=_get_config,
    ))
    monkeypatch.setattr(
        a2a, "get_agent_container",
        lambda name: types.SimpleNamespace(status="running", labels={})
        if name == AGENT else None)

    async def _tmpl(name, container):
        return dict(TEMPLATE)
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a_, **k_: None)
    a2a_gate.clear_provider()

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)          # public well-known card
    # The authenticated per-agent card resolves its agent via a path
    # dependency; override it to the same agent so both surfaces are
    # comparable.
    app.include_router(a2a.router)              # authenticated per-agent card
    app.dependency_overrides[deps.get_authorized_agent_by_name] = lambda: AGENT

    return types.SimpleNamespace(http=TestClient(app), state=state)


def _well_known(cards):
    r = cards.http.get(f"/a2a/{AGENT}/.well-known/agent-card.json")
    assert r.status_code == 200, r.text
    return r.json()


def _authenticated(cards):
    r = cards.http.get(f"/api/agents/{AGENT}/a2a/agent-card")
    assert r.status_code == 200, r.text
    return r.json()


class TestBothSurfaces:
    def test_neither_surface_prices_an_unconfigured_agent(self, cards):
        for card in (_well_known(cards), _authenticated(cards)):
            assert "extensions" not in (card.get("capabilities") or {})

    def test_both_surfaces_carry_the_price_block(self, cards):
        cards.state["config"] = _config(credits=5)
        wk, auth = _well_known(cards), _authenticated(cards)
        for card in (wk, auth):
            params = _payment_ext(card)["params"]
            assert params["planId"] == "plan-1"
            assert params["credits"] == 5
        # ent#180 FR-3: the two surfaces must not disagree about the agent.
        assert _payment_ext(wk) == _payment_ext(auth)

    def test_a_disabled_config_prices_neither_surface(self, cards):
        cards.state["config"] = _config(enabled=False)
        for card in (_well_known(cards), _authenticated(cards)):
            assert "extensions" not in (card.get("capabilities") or {})

    def test_the_price_block_points_at_this_instance(self, cards):
        cards.state["config"] = _config()
        card = _well_known(cards)
        info_url = _payment_ext(card)["params"]["paymentInfoUrl"]
        assert info_url.endswith(f"/api/paid/{AGENT}/info")
        # Same origin the card's own `url` is built from — a client following
        # either one reaches this instance, not a hardcoded host.
        assert info_url.startswith(card["url"].rsplit("/a2a/", 1)[0])


# --------------------------------------------------------------------------- #
# 6. Fail-open
# --------------------------------------------------------------------------- #
class TestFailOpen:
    def test_an_unreadable_payment_config_still_serves_the_card(self, cards, caplog):
        """A card route has never 5xx'd — an unreachable agent falls back to
        Docker labels. An unreadable payment config gets the same treatment:
        the gate reads the config itself and still answers 402, so the only
        cost of failing open here is a priced agent briefly looking free."""
        cards.state["raises"] = True
        with caplog.at_level("WARNING"):
            card = _well_known(cards)
        assert "extensions" not in (card.get("capabilities") or {})
        assert any("payment config unreadable" in r.message for r in caplog.records)
