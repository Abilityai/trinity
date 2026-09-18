# AAuth Agent Identity on the A2A Seam (ent#623 — prototype, flag OFF)

An agent on one Trinity instance calls an agent on another and is authenticated
as **itself** — by a key only it holds — instead of by a shared bearer secret.
Nothing is pre-shared between the two instances: the caller signs, the callee
fetches the caller's *published* key set over HTTPS and verifies, and the
callee's own allow-list decides on the caller's agent identity.

This is AAuth's smallest access mode (**agent-identity-only**, self-hosted
bootstrap: no Person Server, no Access Server, no missions) applied to the A2A
seam Trinity already ships. It is an **authentication** change, not a new
transport: the wire is still A2A JSON-RPC 2.0 over `POST /a2a/{name}`.

> **Status:** prototype behind `aauth_prototype_enabled`, **OFF by default**.
> With the flag off, inbound, outbound and the served card behave exactly as
> they did before (§32.6 in `docs/memory/requirements/mcp.md`).

## Why it exists

The pre-existing federation story is "instance B mints a Trinity MCP API key and
instance A stores it". That credential resolves to **its owner, carrying the
owner's role** — the bug class behind six prior incidents — so the callee's
answer to *who is calling?* is "somebody holding a key owned by a user here".
The calling agent's name never crosses the wire, the secret must be issued,
stored, rotated and revoked, and anyone who captures it can replay it.

Under AAuth the answer becomes `aauth:<agent>@<instance-host>`: per agent, no
role attached, proved per request, and revocable by deleting one line on the
callee. The same signed request is also accepted by non-Trinity AAuth verifiers,
so the mechanism points outward at any AAuth resource, not only at Trinity.

## Roles and vocabulary

| AAuth role | Who plays it in Trinity |
|---|---|
| **Agent** | A Trinity agent. Its identity is `aauth:<agent-name>@<issuer-host>`; the calling agent never holds the key — its instance signs on its behalf (see Limits) |
| **Agent Provider (AP)** | The Trinity **instance** itself. Self-hosted bootstrap: it publishes agent metadata + a JWKS and self-issues agent tokens; there is no third-party service in the loop |
| **Resource** | The callee instance's `POST /a2a/{name}` endpoint |
| **Person Server / Access Server / missions** | Not used in this mode. Out of scope |

Two tokens/documents matter: the **agent token** (`aa-agent+jwt`, ≤1 h, signed by
the instance key, carrying the per-agent public key in `cnf.jwk`) and the
**RFC 9421 HTTP message signature** over the request, made with that per-agent
key. The token says *who this is*; the signature proves *this request is theirs*.

## Layers

| Layer | File | Notes |
|---|---|---|
| Flag + issuer | `services/aauth/config.py` | `aauth_prototype_enabled`, `aauth_issuer` — both `system_settings` → env → default. Issuer must be `https://<host>`: no port, no path, lowercase |
| JOSE | `services/aauth/jose.py` | Compact JWS with the fully-specified `Ed25519` alg (PyJWT only registers `EdDSA`, which the draft forbids verifiers to accept), JWK ⇄ key, RFC 7638 thumbprints |
| RFC 9421 profile | `services/aauth/httpsig.py` | Signature base, `@authority` normalisation, Content-Digest, and a **strict** RFC 8941 subset (anything unexpected is refused, not interpreted) |
| Instance key | `services/aauth/keys.py` | One Ed25519 key per instance, AES-256-GCM in `system_settings.aauth_signing_key_encrypted` (Invariant #12), created on first use via insert-if-absent |
| Signer (caller) | `services/aauth/signer.py` | Per-agent ephemeral key + token cache; `sign_request()` returns the headers for one request |
| Discovery (callee) | `services/aauth/discovery.py` | `{iss}/.well-known/aauth-agent.json` → `jwks_uri` → key by `kid`, through the existing pinned public-HTTPS egress |
| Verifier (callee) | `services/aauth/verifier.py` | FastAPI-free; returns `VerifiedAgent` or raises `AAuthError` |
| Attribution (callee) | `services/aauth/inbound.py` | Audit details, the inbound activity row, and the execution → identity map |
| Discovery routes | `routers/aauth.py` | The three public `/.well-known/aauth-*.json` documents |
| Inbound wiring | `routers/a2a.py` → `_A2AServerRoute`, `_a2a_aauth_jsonrpc`, `_dispatch_jsonrpc` | Routes a signed request around the bearer dependency; both entry points share one method table |
| Outbound wiring | `services/a2a_outbound.py`, `a2a_outbound_service.py`, `a2a_client.py` | `auth_scheme` on the endpoint registry; signer threaded to the RPC |
| Allow-list seam | `services/a2a_gate.py` → `explicit_inbound_identities` | **Fail-closed**, the inverse of the bearer gate next to it |
| Operator CLI | `services/aauth/whoami.py` | Signs one request to AAuth's public whoami service — proof against a verifier that is not ours |

## Public surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/.well-known/aauth-agent.json` | none | AP metadata: `{issuer, jwks_uri, name, accept_signature_algs}` |
| GET | `/.well-known/aauth-jwks.json` | none | The key set agent tokens verify against (one public Ed25519 key) |
| GET | `/.well-known/aauth-resource.json` | none | Resource metadata: `access_mode: agent-token`, `signature_window`, `additional_signature_components` |
| POST | `/a2a/{name}` | **AAuth** *or* bearer | The existing JSON-RPC task endpoint. A request carrying `Signature-Key` takes the AAuth path; everything else is unchanged |

All three documents answer a uniform **404** unless AAuth is live (flag on **and**
a valid issuer), so a default install publishes nothing. They are per-IP rate
limited like the public card route.

## Configuration state

| Key | Where | Meaning |
|---|---|---|
| `aauth_prototype_enabled` | `system_settings` → `AAUTH_PROTOTYPE_ENABLED` | Master switch, default **off** |
| `aauth_issuer` | `system_settings` → `AAUTH_ISSUER` | This instance's AAuth identity domain, `https://<host>` |
| `aauth_signing_key_encrypted` | `system_settings` | The instance key envelope. Blocked on the generic settings PUT/DELETE; included in the credential-rotation script |
| `agent_ownership.a2a_exposed` | OSS column | Pre-existing per-agent exposure, default off |
| inbound allow-list | private A2A module | One list per agent; `aauth:`-prefixed entries are the AAuth audience |
| endpoint registry `auth_scheme` | `system_settings` (AES-GCM envelope) | Per outbound endpoint: `bearer` (default) or `aauth` |

Flag on **without** a valid issuer means AAuth is *inert*, never half-on: the
well-knowns 404, signing is refused, and an inbound signed request falls back to
the ordinary bearer 401.

## Flow 1 — Enablement (per instance)

1. Operator sets `aauth_issuer` to the instance's public origin and turns the flag on.
2. First use of the key (a signature, or a JWKS fetch) creates one Ed25519 key,
   encrypts it, and writes it **insert-if-absent**, then re-reads the row — so two
   uvicorn workers racing on first use converge on one key rather than forking.
3. The three well-known documents start answering. The `kid` is the key's RFC 7638
   thumbprint, so the published key and the tokens that reference it cannot drift.
4. An unreadable key envelope (e.g. a credential-key rotation that skipped the row)
   makes AAuth **fail closed** with a named error; it is deliberately never
   regenerated, because a silent new key would change this instance's identity
   underneath every peer that already trusts it.

**The issuer host must equal the host in the served agent card's `url`.** The
caller signs `@authority` from the card; the callee verifies against its configured
issuer host. A mismatch fails every call with `invalid_signature`; the card
generator logs a warning naming both hosts.

## Flow 2 — Outbound: A's agent calls B (the caller side)

Trigger: the agent calls the existing MCP tool `call_a2a_agent(endpoint, message,
dedup_label)` → `POST /api/agents/{name}/a2a/call`. **The agent never supplies a
URL** — `endpoint` is a reference into the operator-registered endpoint list
(§32.5 FR-1), and that rule is unchanged here.

1. **Kill switch → rate bounds → resolve.** Unchanged from #736: outbound must be
   enabled, per-agent and fleet limits apply, and the endpoint reference resolves
   server-side to a stored record.
2. **Scheme check.** The resolved endpoint carries `auth_scheme`. `bearer` is the
   pre-existing path (stored credential → `Authorization`). `aauth` means *no
   credential exists for this endpoint at all* — a stored one is never sent, and
   switching an endpoint to `aauth` clears it.
3. **Two refusals before any egress**, both named, neither a 500:
   - **Only the agent itself may sign.** `AuthorizedAgentByName` also admits the
     owner, shared users and admins; for a bearer endpoint that only affects
     attribution, but under AAuth it would let a human make the backend sign *as
     the agent*. So the caller must be the agent's own key.
   - **AAuth must be live and the instance key loadable.** The token is minted
     here, at the pre-check, rather than deep inside the HTTP client.
4. **Agent token.** A per-agent Ed25519 key is generated and cached with its token
   (1 h, refreshed with 5 min to spare), keyed by `(issuer, kid, agent)` so an
   issuer change or a key change retires it immediately. The public half travels in
   `cnf.jwk`; agent keys are never published.
5. **Card fetch** (uncredentialed, unsigned, pinned, capped). For an `aauth`
   endpoint with a path, the card is read from `<endpoint>/.well-known/agent-card.json`
   first — a Trinity peer serves its card under the agent path, while the origin
   root either 404s or (behind nginx) returns the SPA's HTML. The origin is the
   fallback, and only when the first card is absent or unusable; a timeout
   propagates rather than doubling the call's budget.
6. **Target resolution is unchanged:** the card is a hint, never an authority —
   a declared `url` on another origin is refused, and a registered path must match
   the card's path exactly.
7. **Sign the request that is actually sent.** The JSON-RPC body is serialised
   once; the signature covers `@method`, `@authority` (the registered host, default
   port dropped — *not* the pinned IP the socket connects to), `@path`,
   `signature-key`, plus `content-digest` and `content-type` on a POST. Headers go
   out as `Signature-Key`, `Signature-Input`, `Signature`, `Content-Digest`, and
   **no `Authorization`**.
8. **Response handling is unchanged** — JSON-RPC errors ride HTTP 200 and become a
   502 with a named reason; the peer's 403 (not allow-listed) surfaces as
   `rpc_http_error`.
9. **Records:** the existing `agent_activities` row gains `auth: "aauth"`, and the
   audit row gains the same — never the token, never a credential.

## Flow 3 — Inbound: B verifies and decides (the callee side)

A signed request never reaches the bearer handler. `POST /a2a/{name}` is served by
a custom route class: if the request carries `Signature-Key` **and** AAuth is live,
it goes to the AAuth entry point; otherwise the untouched bearer handler runs
(which is why the bearer path, its 401s and its test overrides are unchanged).
While AAuth is live, a bearer-less 401 additionally advertises how to sign
(`Accept-Signature`, `Accept-Signature-Scheme: jwt`, `AAuth-Requirement`).

Order is deliberate — cheap and local first, network second, the body last:

1. **Per-IP rate limit**, before anything that costs a DB read.
2. **Exposure**: not exposed → uniform 404, same as an agent that does not exist.
3. **Header shape**: `Signature-Key` must use the `jwt` scheme; `Signature-Input`
   and `Signature` must parse under the strict subset; `Signature` must be
   standard base64 (base64url is refused).
4. **Coverage and window**: every required component must be covered (including
   `signature-key` itself, and the body components on a POST), no `alg` parameter,
   `created` within ±60 s, `expires` honoured if present, declared `Content-Length`
   within the 1 MiB cap.
5. **Token claims, still untrusted**: `alg` is `Ed25519` (never `EdDSA`, never
   `none`), `typ` is `aa-agent+jwt`, `dwk` names the AP metadata document, no
   key-injecting header parameters, `iss` is a valid server identifier, `sub`
   matches `aauth:<local>@<host>` **with the host equal to the issuer's**, lifetime
   sane, `cnf.jwk` is an Ed25519 public key (private material refused).
6. **Trusted-issuer pre-gate**: the issuer host must be the host of at least one
   `aauth:` entry on *this agent's* allow-list. Otherwise the request is refused
   **without any network fetch** — an unauthenticated caller can never make this
   backend go and fetch a domain nobody listed.
7. **Discovery**: fetch the caller's AP metadata (its `issuer` must match), then its
   JWKS from a `jwks_uri` **on the same origin**, under one 8-second deadline, with
   results *and* failures cached per issuer and at most one refetch per minute.
8. **Token signature** against the issuer's key for that `kid`. Until this passes,
   `sub` is a string the caller typed; only afterwards is a refusal attributable —
   otherwise anyone who knows a listed partner domain could fill the audit log with
   rows naming an identity they invented.
9. **HTTP signature** with the token's `cnf.jwk`, over a base rebuilt from the
   request: the method; the authority from **this instance's configured issuer
   host** (never the `Host` header, which a proxy or tunnel controls); the
   percent-encoded raw path; the raw `signature-key` header; and the covered
   fields. This step covers the `Content-Digest` **header**, which is what makes
   it safe to read the body afterwards rather than before.
10. **Body**: read under the cap, then its SHA-256 must equal the signed digest.
11. **Replay**: a Redis `SET NX` over (key thumbprint, `created`, method, authority,
    path, digest) for twice the signature window. If Redis cannot answer, the
    request is **refused** — this one fails closed.
12. **Allow-list**: the verified identity must be an exact member (host compared
    case-insensitively). An empty list means *nobody*; a missing provider, a
    provider without the method, an error, or a malformed return all refuse. This
    is the inverse of the bearer allow-list next to it, which fails open because
    its caller is already an authenticated Trinity principal.
13. **Dispatch**: the same `dispatch_and_await_terminal` path every other trigger
    uses, with `triggered_by="a2a"` and **no source user** — the caller is not a
    Trinity user and no owner role travels with it.

### Refusals

| Outcome | Status | Carries |
|---|---|---|
| Signature/token/digest/replay failure | 401 | `Signature-Error: error=<code>` + RFC 9457 problem details (`type: urn:ietf:params:sig-error:<code>`) |
| Verified but not allow-listed | 403 | Plain detail, no `Signature-*` headers — nothing is wrong with the signature |
| Not exposed / no such agent | 404 | Uniform, indistinguishable |
| Replay store unavailable | 503 | Fails closed rather than admitting an unreplayable request |

Codes follow the drafts: `invalid_signature`, `invalid_input`, `clock_skew`,
`unsupported_scheme`, `unsupported_algorithm`, `invalid_key`, `invalid_jwt`,
`expired_jwt`, `unknown_key`, `issuer_mismatch`, `issuer_missing`.

### Attribution on the callee

Every decision is recorded, not just the successful one:

- **Audit**: `a2a_task` (ran), `a2a_aauth_denied` (verified, not listed),
  `a2a_aauth_refused` (verification failed, *only once the token verified* — noise
  from strangers is logged, not stored). Actor type is **`external_agent`** with the
  identity as actor id, so a remote caller is never recorded as the platform itself.
  Details carry the identity, issuer, verification result, allow-list decision and
  key thumbprint — never the token.
- **Activity**: one `agent_activities` row per inbound call, so the agent's timeline
  shows who called and what was decided.
- **Task scoping**: the execution id is mapped to the identity that started it, and
  `tasks/get` / `tasks/cancel` answer an AAuth caller only for its own tasks
  (fail-closed if the owner cannot be established). `message/stream` is refused for
  AAuth callers in this prototype.
- **Dedup**: the `messageId` idempotency scope is namespaced per identity, so two
  callers' message ids cannot collide.

## Flow 4 — The served card

While AAuth is live, an exposed agent's card declares an `aauth` security scheme
**after** `bearerAuth`, with the resource-metadata URL in its description. Both
schemes are alternatives, so an AAuth-unaware client that takes the first entry is
unaffected. With the flag off the card is byte-identical to before.

## Trust model

- **What the callee verifies:** the caller controls the private key matching the
  public key in a token signed by the key published at `https://<issuer-host>/.well-known/…`,
  and this exact request (method, host, path, body) was signed by it, recently, once.
- **What it does not verify:** anything about *who runs* that instance. Trusting
  `aauth:x@partner.example` is trusting whoever controls that domain, its TLS and
  the key set it publishes.
- **Unit of trust is the peer instance**, not the single agent: one instance key
  signs tokens for all of that instance's agents, so a compromised instance can
  present itself as any of its own agents.
- **Authorization stays coarse.** `message/send` dispatches free-form text and the
  agent runs it with its normal tools; curated skills narrow what a card
  *advertises*, never what a caller may *ask*. An allow-list entry means "this
  remote agent may ask mine for anything it can do". Narrowing that is the next
  problem, not this one.
- **Revocation** is deleting the entry on the callee — no key rotation, no
  coordination with the peer.

## Stated limits (prototype)

1. One software-held instance key; no rotation, no HSM. The write-up says so
   rather than claiming a property we do not have.
2. The backend signs on the agent's behalf; the agent container never holds a key.
   The intended shape is the agent holding its own key with the backend only
   issuing tokens for it — that needs an agent-runtime change.
3. Identity is keyed by **agent name**, so a deleted name recreated later inherits
   the identity. An immutable id is the productization fix.
4. Inbound AAuth is inert without the private allow-list module (the seam fails
   closed), so an OSS-only build can call out but cannot accept.
5. `message/stream` is refused for AAuth callers; no Person Server, missions,
   budgets, or sub-agent identities (`parent+child@domain` is refused explicitly).

## Diagrams worth drawing

1. **Two instances, one call** — A's agent → MCP tool → A's backend (signer, instance
   key) → HTTPS → B's `/a2a/{name}` → verifier → B's agent; with B's *second* arrow
   back to A's `/.well-known/aauth-agent.json` + JWKS. The two arrows in opposite
   directions are the whole idea: the request carries the claim, the callee fetches
   the proof material itself.
2. **The inbound gauntlet** — a vertical funnel of the 13 steps above, annotated with
   what each refusal returns, and a marker at step 8 for where a refusal becomes
   auditable, at step 6 for the last check before any network I/O, and at step 10
   for the first moment the body is read.
3. **Token vs signature** — one box for the agent token (claims + `cnf.jwk`, signed by
   the instance key) and one for the RFC 9421 signature (covered components, signed
   by the per-agent key), with an arrow showing `cnf.jwk` is the link between them.
4. **Bearer vs AAuth on one endpoint** — the route class fork, showing which
   conditions send a request down which path and that the bearer branch is untouched.
5. **State/config map** — the flag, issuer, instance key, exposure flag, allow-list
   and endpoint `auth_scheme`, with which of them live on the caller, on the callee,
   or on both.

## Related

- `feature-flows/a2a-inbound-server.md` — the serving half this extends (ent#157)
- `feature-flows/a2a-outbound-call.md` — the calling half this extends (#736)
- `docs/memory/requirements/mcp.md` §32.6 — the requirement, including out-of-scope
- `docs/memory/learnings.md` (2026-09-17) — why a second instance needs its own Docker daemon
