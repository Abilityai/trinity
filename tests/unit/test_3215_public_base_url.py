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
# B1 (cont.) — one request, one origin across card, paid 402 and a2a 402
# ---------------------------------------------------------------------------

class TestOneOriginPerRequest:

    def test_the_card_and_both_doors_share_one_origin(self, monkeypatch):
        import routers.a2a as a2a
        import routers.paid as paid

        class _Settings:
            def get_public_chat_url(self):
                return ""
        monkeypatch.setitem(sys.modules, "x", None)
        monkeypatch.setattr("services.settings_service.settings_service",
                            _Settings(), raising=False)
        monkeypatch.setattr("config.FRONTEND_URL", "", raising=False)

        req = _request("http://trinity.test/a2a/agent-a", x_forwarded_proto="https")
        assert a2a._base_url_from_request(req) == "https://trinity.test"
        assert paid._paid_base_url(req) == "https://trinity.test"

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
