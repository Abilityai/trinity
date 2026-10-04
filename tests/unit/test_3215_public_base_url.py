"""The 402's `resource.url` must carry the origin the buyer actually calls
(abilityai/trinity#3215, AC5).

An x402 token is minted and verified against `resource.url`, and the facilitator
compares origin+path — so one wrong scheme character mints a token for a URL the
client never calls. Both payment doors derived that origin from
`request.base_url`, which is http on a standard Trinity deployment for two
independent reasons:

* `docker-compose.prod.yml` / `docker-compose.hosted.yml` override the image
  `command:` and drop the Dockerfile CMD's `--proxy-headers
  --forwarded-allow-ips=*`, so uvicorn does not trust `X-Forwarded-*` at all and
  `request.url.scheme` stays http behind ANY proxy — which is why
  `public_base_url` reads the RAW header and never `request.url.scheme`
  (restoring those flags is a trust change, deliberately deferred);
* the frontend nginx forwarded `X-Forwarded-Proto $scheme`, and `$scheme` is http
  on that hop, clobbering the upstream terminator's https. Fixed by the
  `$fwd_proto` map, guarded here.

And `/info` — the card's `paymentInfoUrl`, from which a payment-aware client
pays on its first request — advertised a relative `resource.url` and always the
crypto scheme.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

# Imported at COLLECTION time, deliberately: `tests/unit/conftest.py` pops
# `dependencies` from `sys.modules` between tests (`_POP_PREFIXES`), so a
# fixture-local import yields a fresh module whose
# `get_authorized_agent_by_name` is a DIFFERENT object from the one
# `routers.a2a` captured — and `dependency_overrides` keyed on it silently
# stops matching from the second test onwards (401 instead of the card).
import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
from utils.public_url import public_base_url  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _request(url="http://trinity.test/api/paid/a/chat", **headers):
    """A stand-in for a Starlette Request: `.url` and `.headers` only."""
    scheme, rest = url.split("://", 1)
    netloc = rest.split("/", 1)[0]
    return SimpleNamespace(
        url=SimpleNamespace(scheme=scheme, netloc=netloc),
        headers={k.replace("_", "-").lower(): v for k, v in headers.items()},
    )


# ---------------------------------------------------------------------------
# B1 — the helper
# ---------------------------------------------------------------------------

class TestPublicBaseUrl:

    def test_a_configured_origin_on_the_same_host_wins(self):
        assert public_base_url(
            _request("http://trinity.test/x"), configured="https://trinity.test",
        ) == "https://trinity.test"

    def test_a_configured_origin_on_another_host_does_not_redirect_the_buyer(self):
        """A caller that reached a private host must not be sent to the public
        one, whose narrow tunnel may not route `/a2a/*` at all."""
        assert public_base_url(
            _request("http://10.0.0.5:8000/x"), configured="https://public.test",
        ) == "http://10.0.0.5:8000"

    def test_the_configured_origin_is_used_when_there_is_no_request_host(self):
        assert public_base_url(None, configured="https://trinity.test/") == \
            "https://trinity.test"

    def test_the_raw_header_upgrades_http_to_https_with_no_configured_url(self):
        """The addendum case: uvicorn never applied the header, so
        `request.url.scheme` is still http, and nothing is configured."""
        req = _request("http://trinity.test/x", x_forwarded_proto="https")
        assert req.url.scheme == "http"
        assert public_base_url(req) == "https://trinity.test"

    def test_the_header_never_downgrades_an_https_request(self):
        assert public_base_url(
            _request("https://trinity.test/x", x_forwarded_proto="http"),
        ) == "https://trinity.test"

    def test_a_proxy_chain_uses_the_client_facing_hop(self):
        assert public_base_url(
            _request("http://trinity.test/x", x_forwarded_proto="https, http"),
        ) == "https://trinity.test"

    @pytest.mark.parametrize("value", ["HTTPS", " https ", "https"])
    def test_the_header_is_matched_case_and_space_insensitively(self, value):
        assert public_base_url(
            _request("http://trinity.test/x", x_forwarded_proto=value),
        ) == "https://trinity.test"

    @pytest.mark.parametrize("value", ["ftp", "httpsx", "", "wss"])
    def test_only_https_upgrades(self, value):
        assert public_base_url(
            _request("http://trinity.test/x", x_forwarded_proto=value),
        ) == "http://trinity.test"

    def test_no_header_and_no_config_is_todays_bytes(self):
        assert public_base_url(_request("http://trinity.test/x")) == \
            "http://trinity.test"

    def test_the_frontend_url_is_the_second_best_configured_origin(self):
        assert public_base_url(
            _request("http://trinity.test/x"), frontend_url="https://trinity.test/",
        ) == "https://trinity.test"

    def test_public_chat_url_outranks_frontend_url(self):
        assert public_base_url(
            _request("http://a.test/x"), configured="https://a.test",
            frontend_url="https://b.test",
        ) == "https://a.test"

    def test_a_port_is_part_of_the_host_comparison(self):
        assert public_base_url(
            _request("http://trinity.test:8000/x"), configured="https://trinity.test",
        ) == "http://trinity.test:8000"

    def test_nothing_resolvable_returns_empty_so_the_caller_omits_the_url(self):
        assert public_base_url(SimpleNamespace(url=None, headers={})) == ""
        assert public_base_url(None) == ""

    def test_a_header_mapping_that_raises_is_not_fatal(self):
        class _Hostile:
            def get(self, _key):
                raise RuntimeError("no headers for you")
        req = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="trinity.test"),
            headers=_Hostile(),
        )
        assert public_base_url(req) == "http://trinity.test"

    def test_the_helper_never_reads_request_url_scheme_for_the_upgrade(self):
        """uvicorn's proxy-header trust is OFF on prod compose, so a helper that
        consulted `request.url.scheme` instead of the raw header would be inert
        exactly where AC5 was reported."""
        req = _request("http://trinity.test/x", x_forwarded_proto="https")
        assert public_base_url(req).startswith("https://")


# ---------------------------------------------------------------------------
# Review I3 — `configured_wins` restores the card's pre-#3215 precedence
# ---------------------------------------------------------------------------

class TestConfiguredWins:
    """The card is a DISCOVERY document, not a minted token.

    A 402's `resource.url` must name the origin the caller actually used, which
    is why the default is same-host. A card is the opposite: whoever fetched it
    (an operator in the UI, the `get_agent_a2a_card` MCP tool proxying from
    `backend:8000`) publishes it to a buyer somewhere else, so it must advertise
    the operator's declared origin regardless of the host it was read on. That
    was dev's behaviour and #3215 must not regress it.
    """

    def test_the_configured_origin_wins_on_an_internal_host(self):
        assert public_base_url(
            _request("http://backend:8000/api/agents/bot/a2a/agent-card"),
            configured="https://pub.example", configured_wins=True,
        ) == "https://pub.example"

    def test_it_still_strips_a_trailing_slash(self):
        assert public_base_url(
            _request("http://backend:8000/x"),
            configured="https://pub.example/", configured_wins=True,
        ) == "https://pub.example"

    def test_the_frontend_url_is_still_the_second_best_origin(self):
        assert public_base_url(
            _request("http://backend:8000/x"),
            frontend_url="https://pub.example", configured_wins=True,
        ) == "https://pub.example"

    def test_with_nothing_configured_it_falls_back_to_the_request_host(self):
        """One upgrade rule, not two: the same raw-header logic as the doors."""
        assert public_base_url(
            _request("http://x/x", x_forwarded_proto="https"), configured_wins=True,
        ) == "https://x"

    def test_the_fallback_upgrade_is_still_upgrade_only(self):
        assert public_base_url(
            _request("https://x/x", x_forwarded_proto="http"), configured_wins=True,
        ) == "https://x"

    def test_nothing_resolvable_is_still_empty(self):
        assert public_base_url(SimpleNamespace(url=None, headers={}),
                               configured_wins=True) == ""

    def test_the_default_is_the_same_host_rule(self):
        """Omitting the keyword must not change a single door's bytes."""
        req = _request("http://backend:8000/x")
        assert public_base_url(req, configured="https://pub.example") == \
            "http://backend:8000"


# ---------------------------------------------------------------------------
# B1 (cont.) — one origin per request for the DOORS; the card is declared
# ---------------------------------------------------------------------------

class TestOneOriginPerRequest:
    """Review I3 split the two surfaces that #3215 had fused.

    The doors still share one origin per request — a paid 402 and an A2A 402
    minted for different hosts on the same hop is the defect #3215 fixed. The
    card is now excluded from that rule by design: it declares the operator's
    public origin so that a buyer following it arrives on the host whose 402 it
    will then be shown, which is how card and 402 still agree for every caller
    that follows the card.
    """

    def test_both_doors_share_one_origin(self, monkeypatch):
        import routers.paid as paid

        class _Settings:
            def get_public_chat_url(self):
                return ""
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Settings(), raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)

        req = _request("http://trinity.test/a2a/agent-a", x_forwarded_proto="https")
        assert a2a._base_url_from_request(req) == "https://trinity.test"
        assert paid._paid_base_url(req) == "https://trinity.test"

    def test_with_nothing_configured_the_card_agrees_with_the_doors(self, monkeypatch):
        """The split is invisible on an install that declared no public URL."""
        import routers.paid as paid

        class _Settings:
            def get_public_chat_url(self):
                return ""
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Settings(), raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)

        req = _request("http://trinity.test/a2a/agent-a", x_forwarded_proto="https")
        assert a2a._card_base_url(req) == a2a._base_url_from_request(req) == \
            paid._paid_base_url(req) == "https://trinity.test"

    def test_a_configured_origin_moves_the_card_but_not_the_doors(self, monkeypatch):
        """The whole of I3 in one assertion."""
        import routers.paid as paid

        class _Settings:
            def get_public_chat_url(self):
                return "https://pub.example"
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Settings(), raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)

        req = _request("http://backend:8000/a2a/agent-a")
        assert a2a._card_base_url(req) == "https://pub.example"
        assert a2a._base_url_from_request(req) == "http://backend:8000"
        assert paid._paid_base_url(req) == "http://backend:8000"

    def test_an_unreadable_settings_row_falls_back_to_the_env(self, monkeypatch):
        import routers.paid as paid

        class _Broken:
            def get_public_chat_url(self):
                raise RuntimeError("db gone")
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Broken(), raising=False)
        monkeypatch.setattr("config.PUBLIC_CHAT_URL", "https://trinity.test",
                            raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)
        assert paid._paid_base_url(_request("http://trinity.test/x")) == \
            "https://trinity.test"



# ---------------------------------------------------------------------------
# B2 / A8 — `/info`
# ---------------------------------------------------------------------------

class TestPaidInfo:

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.paid as paid

        config = SimpleNamespace(
            agent_name="agent-a", nvm_plan_id="plan-1", nvm_agent_id="did:nv:1",
            nvm_environment="sandbox", credits_per_request=3, enabled=True,
        )
        state = {"resolved": [], "config": config}

        class _Db:
            def get_nevermined_config_with_key(self, name):
                if state.get("missing"):
                    return None
                return {"config": state["config"], "nvm_api_key": "sandbox:SECRET-KEY"}
        monkeypatch.setattr(paid, "db", _Db())

        class _PaymentService:
            async def resolve_plan_scheme(self, **kw):
                state["resolved"].append(kw)
                return state.get("plan_scheme")

            def build_402_response(self, config_, base_url="", endpoint=None,
                                   plan_scheme=None):
                from services.nevermined_payment_service import (
                    NeverminedPaymentService,
                )
                return NeverminedPaymentService().build_402_response(
                    config_, base_url, endpoint, plan_scheme
                )
        monkeypatch.setattr(paid, "get_nevermined_payment_service",
                            lambda: _PaymentService())
        monkeypatch.setattr(paid, "NEVERMINED_AVAILABLE", True)

        class _Settings:
            def get_public_chat_url(self):
                return ""
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Settings(), raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)

        app = FastAPI()
        app.include_router(paid.router)
        c = TestClient(app, base_url="http://trinity.test")
        c.state = state
        return c

    def test_info_advertises_the_plans_real_scheme(self, client):
        from services.nevermined_payment_service import PlanScheme

        client.state["plan_scheme"] = PlanScheme("nvm:card-delegation", "stripe")
        body = client.get("/api/paid/agent-a/info").json()
        accepts = body["payment_required"]["accepts"][0]
        assert accepts["scheme"] == "nvm:card-delegation"
        assert accepts["network"] == "stripe"

    def test_info_emits_an_absolute_resource_url(self, client):
        r = client.get("/api/paid/agent-a/info")
        url = r.json()["payment_required"]["resource"]["url"]
        assert url == "http://trinity.test/api/paid/agent-a/chat"

    def test_info_honours_the_forwarded_proto(self, client):
        r = client.get("/api/paid/agent-a/info",
                       headers={"X-Forwarded-Proto": "https"})
        assert r.json()["payment_required"]["resource"]["url"] == \
            "https://trinity.test/api/paid/agent-a/chat"

    def test_info_never_serialises_the_api_key(self, client):
        r = client.get("/api/paid/agent-a/info")
        assert "SECRET-KEY" not in r.text
        assert "nvm_api_key" not in r.text
        # It WAS used, server-side, to resolve the scheme.
        assert client.state["resolved"][0]["nvm_api_key"] == "sandbox:SECRET-KEY"

    def test_info_still_404s_for_an_unknown_agent(self, client):
        client.state["missing"] = True
        assert client.get("/api/paid/agent-a/info").status_code == 404

    def test_the_402s_resource_url_honours_the_forwarded_proto(self, client):
        """The door that mints the token, not just the one that describes it."""
        r = client.post("/api/paid/agent-a/chat", json={"message": "hi"},
                        headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 402
        body = r.json()["payment_required"]
        assert body["resource"]["url"] == \
            "https://trinity.test/api/paid/agent-a/chat"

    def test_the_402_advertises_the_plans_real_scheme(self, client):
        from services.nevermined_payment_service import PlanScheme

        client.state["plan_scheme"] = PlanScheme("nvm:card-delegation", "braintree")
        r = client.post("/api/paid/agent-a/chat", json={"message": "hi"})
        assert r.status_code == 402
        accepts = r.json()["payment_required"]["accepts"][0]
        assert (accepts["scheme"], accepts["network"]) == \
            ("nvm:card-delegation", "braintree")

    def test_info_still_404s_when_payments_are_disabled(self, client):
        client.state["config"] = SimpleNamespace(
            **{**client.state["config"].__dict__, "enabled": False}
        )
        assert client.get("/api/paid/agent-a/info").status_code == 404


# ---------------------------------------------------------------------------
# B3 — the nginx hop must stop clobbering the scheme
# ---------------------------------------------------------------------------

class TestNginxForwardsTheClientScheme:
    """A text guard, and the consumer IS text: nginx reads this file. There is
    no runtime seam to mount — the regression it prevents is a redeployed image
    that silently sends http again."""

    CONF = (REPO / "src/frontend/nginx.conf").read_text()

    def test_the_map_preserves_an_upstream_https(self):
        assert "map $http_x_forwarded_proto $fwd_proto {" in self.CONF
        block = self.CONF.split("map $http_x_forwarded_proto $fwd_proto {", 1)[1]
        block = block.split("}", 1)[0]
        assert "default $scheme;" in block
        assert "https   https;" in block or "https https;" in block

    def test_no_proxied_location_still_sends_this_hops_scheme(self):
        assert "X-Forwarded-Proto $scheme;" not in self.CONF

    def test_every_proxied_location_forwards_the_mapped_value(self):
        # /api/, /a2a/ and /mcp all proxy to the backend; a door that lied about
        # the scheme on one of them would mint tokens for the wrong origin.
        assert self.CONF.count("proxy_set_header X-Forwarded-Proto $fwd_proto;") == 3

    def test_hsts_still_reads_the_incoming_header_directly(self):
        assert "map $http_x_forwarded_proto $hsts_header {" in self.CONF


# ---------------------------------------------------------------------------
# Review I3 — over the real routes: the card declares, the door answers
# ---------------------------------------------------------------------------

AGENT = "bot"


@pytest.fixture()
def surfaces(monkeypatch):
    """Both card routes AND the anonymous A2A door, on one internal host.

    Driven over real routes rather than through `_base_url_from_request`,
    because the defect I3 reports is a CALL SITE passing the wrong precedence —
    a helper-level test cannot see that. `configured` is switchable so the same
    client proves both arms.
    """
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from services import a2a_gate

    state = {"configured": "", "built_endpoints": []}

    def _config():
        return types.SimpleNamespace(
            agent_name=AGENT, enabled=True, credits_per_request=2,
            nvm_plan_id="plan-1", nvm_agent_id="agent-1",
            nvm_environment="sandbox",
        )

    monkeypatch.setattr(a2a, "db", types.SimpleNamespace(
        get_a2a_exposed=lambda name: name == AGENT,
        get_nevermined_config=lambda name: None,   # unpriced card: no extension
        get_nevermined_config_with_key=lambda name: (
            {"config": _config(), "nvm_api_key": "sandbox:jwt"}
            if name == AGENT else None),
    ))
    monkeypatch.setattr(
        a2a, "get_agent_container",
        lambda name: types.SimpleNamespace(status="running", labels={})
        if name == AGENT else None)

    async def _tmpl(name, container):
        return {"display_name": "Bot", "capabilities": ["chat"]}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a_, **k_: None)

    class _PaymentService:
        def build_402_response(self, config, base_url="", endpoint=None,
                               plan_scheme=None):
            state["built_endpoints"].append(endpoint)
            return {"resource": {"url": endpoint}, "x402Version": 1}

        async def resolve_plan_scheme(self, **kw):
            return None
    monkeypatch.setattr(a2a, "get_nevermined_payment_service",
                        lambda: _PaymentService())
    monkeypatch.setattr(a2a, "NEVERMINED_AVAILABLE", True)

    class _Settings:
        def get_public_chat_url(self_):
            return state["configured"]
    monkeypatch.setattr("services.settings_service.settings_service",
                        _Settings(), raising=False)
    monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)
    a2a_gate.clear_provider()

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    app.include_router(a2a.router)
    app.dependency_overrides[deps.get_authorized_agent_by_name] = lambda: AGENT
    app.dependency_overrides[deps.get_user_or_anonymous] = lambda: None

    return types.SimpleNamespace(
        http=TestClient(app, base_url="http://backend:8000"), state=state)


def _card_urls(surfaces):
    """The `url` both card surfaces advertise, which must agree (ent#180 FR-3)."""
    wk = surfaces.http.get(f"/a2a/{AGENT}/.well-known/agent-card.json")
    auth = surfaces.http.get(f"/api/agents/{AGENT}/a2a/agent-card")
    assert (wk.status_code, auth.status_code) == (200, 200), (wk.text, auth.text)
    return wk.json()["url"], auth.json()["url"]


def _door_402(surfaces, host_header=None):
    headers = {"Host": host_header} if host_header else {}
    r = surfaces.http.post(
        f"/a2a/{AGENT}",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
              "params": {"message": {"role": "user",
                                     "parts": [{"kind": "text", "text": "hi"}]}}},
        headers=headers,
    )
    assert r.status_code == 402, r.text
    return r.json()["payment_required"]["resource"]["url"]


class TestCardAndDoorOverTheRealRoutes:

    def test_the_card_advertises_the_configured_origin_on_an_internal_host(
            self, surfaces):
        """I3's exact report: `get_agent_a2a_card` proxies from `backend:8000`,
        and a card advertising that host is unusable to every external buyer."""
        surfaces.state["configured"] = "https://pub.example"
        wk, auth = _card_urls(surfaces)
        assert wk == auth == f"https://pub.example/a2a/{AGENT}"

    def test_with_nothing_configured_the_card_uses_the_request_host(self, surfaces):
        wk, auth = _card_urls(surfaces)
        assert wk == auth == f"http://backend:8000/a2a/{AGENT}"

    def test_the_cards_fallback_still_honours_the_forwarded_proto(self, surfaces):
        r = surfaces.http.get(f"/a2a/{AGENT}/.well-known/agent-card.json",
                              headers={"Host": "x", "X-Forwarded-Proto": "https"})
        assert r.status_code == 200, r.text
        assert r.json()["url"] == f"https://x/a2a/{AGENT}"

    def test_the_door_mints_for_the_host_the_caller_used(self, surfaces):
        """T5 is unchanged for the doors: a configured origin must NOT move the
        402, because the token is verified against the URL the caller called."""
        surfaces.state["configured"] = "https://pub.example"
        assert _door_402(surfaces) == f"http://backend:8000/a2a/{AGENT}"

    def test_a_buyer_that_followed_the_card_gets_a_402_on_the_card_host(
            self, surfaces):
        """Why the split is safe: arrive on the configured host and the door's
        own same-host rule returns that very declared origin, so the 402 the
        buyer meets is byte-identical to the URL the card sent it to."""
        surfaces.state["configured"] = "https://pub.example"
        wk, _ = _card_urls(surfaces)
        assert wk == f"https://pub.example/a2a/{AGENT}"
        assert _door_402(surfaces, host_header="pub.example") == wk
