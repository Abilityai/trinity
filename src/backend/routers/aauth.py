# mcp: none — AAuth protocol discovery documents, unauthenticated by spec (ent#623); not an agent capability
"""AAuth discovery documents — prototype (trinity-enterprise#623).

This instance acts as its own Agent Provider (AAuth self-hosted bootstrap) and
as a resource that accepts agent tokens on the A2A task endpoint:

* ``GET /.well-known/aauth-agent.json``    — Agent Provider metadata
* ``GET /.well-known/aauth-jwks.json``     — the key set agent tokens verify against
* ``GET /.well-known/aauth-resource.json`` — resource metadata (agent-token mode)

Public by protocol. Every route answers a uniform 404 unless AAuth is live
(flag ON and a valid ``aauth_issuer``), so a default install publishes nothing.
Per-IP rate limited like the public A2A card route.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from routers.public import _get_client_ip
from services import rate_limiter
from services.aauth import config as aauth_config
from services.aauth import httpsig, keys as aauth_keys
from services.aauth.verifier import SIGNATURE_WINDOW

logger = logging.getLogger(__name__)

router = APIRouter(tags=["aauth"])

AAUTH_DISCOVERY_RATE_LIMIT = 60
AAUTH_DISCOVERY_RATE_WINDOW = 60

JWKS_PATH = "/.well-known/aauth-jwks.json"


def _live_issuer(request: Request) -> str:
    rate_limiter.enforce(
        f"aauth_discovery_ip:{_get_client_ip(request)}",
        AAUTH_DISCOVERY_RATE_LIMIT,
        AAUTH_DISCOVERY_RATE_WINDOW,
        detail="Too many AAuth discovery requests from this address.",
    )
    issuer = aauth_config.active_issuer()
    if not issuer:
        raise HTTPException(status_code=404, detail="Not found")
    return issuer


@router.get("/.well-known/aauth-agent.json")
async def aauth_agent_metadata(request: Request):
    issuer = _live_issuer(request)
    return {
        "issuer": issuer,
        "jwks_uri": issuer + JWKS_PATH,
        "name": "Trinity",
        "accept_signature_algs": ["Ed25519"],
    }


@router.get(JWKS_PATH)
async def aauth_jwks(request: Request):
    _live_issuer(request)
    try:
        return aauth_keys.jwks()
    except aauth_keys.SigningKeyUnavailable:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/.well-known/aauth-resource.json")
async def aauth_resource_metadata(request: Request):
    issuer = _live_issuer(request)
    return {
        "issuer": issuer,
        "access_mode": "agent-token",
        "signature_window": SIGNATURE_WINDOW,
        "additional_signature_components": list(httpsig.BODY_COMPONENTS),
    }
