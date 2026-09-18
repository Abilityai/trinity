"""Operator diagnostic: sign one request to AAuth's public whoami service (ent#623).

    docker exec trinity-backend python -m services.aauth.whoami <agent-name>

Proof against a verifier that is not ours: whoami.aauth.dev fetches this
instance's ``/.well-known/aauth-agent.json`` + JWKS over the public internet,
verifies the agent token and the RFC 9421 signature, and echoes the identity.
Requires the prototype flag, a public ``aauth_issuer``, and the issuer's
well-known documents reachable from the internet.

Not an endpoint on purpose: signing as an agent is only placed on the A2A call
path (the agent's own key); this is a CLI an operator runs inside the backend.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

from services.aauth import signer

WHOAMI_URL = "https://whoami.aauth.dev/"


async def whoami(agent_name: str, url: str = WHOAMI_URL) -> dict:
    headers = signer.sign_request(agent_name=agent_name, method="GET", url=url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, trust_env=False) as client:
        resp = await client.get(url, headers={**headers, "Accept": "application/json"})
    interesting = {
        k: v for k, v in resp.headers.items()
        if k.lower() in ("signature-error", "aauth-requirement", "accept-signature",
                         "accept-signature-scheme", "accept-signature-alg", "content-type")
    }
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    return {
        "request_identity": signer.identity_for(agent_name),
        "status": resp.status_code,
        "headers": interesting,
        "body": body,
    }


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: python -m services.aauth.whoami <agent-name> [url]", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(whoami(argv[1], *(argv[2:3])))
    except signer.SigningUnavailable as exc:
        print(f"AAuth signing unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
