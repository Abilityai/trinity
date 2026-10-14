"""AAuth agent identity for the A2A seam — prototype (trinity-enterprise#623).

AAuth's smallest access mode (agent-identity-only, self-hosted bootstrap):
this instance is its own Agent Provider. It publishes
``/.well-known/aauth-agent.json`` plus a JWKS, self-issues ``aa-agent+jwt``
agent tokens, and signs outbound A2A requests per RFC 9421 with the key
conveyed by ``Signature-Key: sig=jwt;jwt="…"``. Inbound, the resource verifies
a caller against the caller's own published keys and gates on its
``aauth:<local>@<host>`` identity.

Protocol sources (editor's copies, 2026-09-14): draft-hardt-oauth-aauth-protocol,
draft-hardt-aauth-bootstrap, draft-hardt-httpbis-signature-key -09.

Off by default (``config.is_enabled``). FastAPI-free by construction — the
routers map ``AAuthError`` to HTTP.

Stated prototype limits: one software-held instance key (no rotation, no HSM);
the backend signs on the agent's behalf; identity is keyed by agent name.
"""
