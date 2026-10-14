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

> **Status:** Flows 1–4 graduated for Trinity-to-Trinity calls behind
> `aauth_enabled`, **OFF by default**, switchable from Settings → Integrations
> (ent#862 PR1). Flows 5–6 stay prototype behind a second switch,
> `aauth_prototype_enabled`, and need both. With the switch off, inbound,
> outbound and the served card behave exactly as they did before (§32.8 in
> `docs/memory/requirements/mcp.md`).

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
| **Person Server (PS)** | The operator-configured issuer (`aauth_person_server`, e.g. Hellō) an agent **binds** through to obtain a person token — the token that says *whose* agent this is (ent#878, Flow 5). This instance never plays the PS |
| **Access Server / missions** | Not used. Out of scope |

Two tokens/documents matter: the **agent token** (`aa-agent+jwt`, ≤1 h, signed by
the instance key, carrying the per-agent public key in `cnf.jwk`) and the
**RFC 9421 HTTP message signature** over the request, made with that per-agent
key. The token says *who this is*; the signature proves *this request is theirs*.

## Layers

| Layer | File | Notes |
|---|---|---|
| Switches + issuer | `services/aauth/config.py` | `aauth_enabled` (Flows 1–4, the single input of `active_issuer()`), `aauth_prototype_enabled` (Flows 5–6, read once at `require_live` via `prototype_active_issuer()`), `aauth_issuer` — all `system_settings` → env → default. Issuer must be `https://<host>`: no port, no path, lowercase |
| Settings surface (ent#862) | `services/aauth/settings.py`, `routers/settings/aauth.py`, `components/settings/AAuthPanel.vue`, `stores/a2aFederation.js`, `utils/aauthSettings.js` | `GET/PUT /api/settings/aauth`: each value with its source, `live` = switch AND valid issuer, the readiness checklist; the write is admin + interactive, diff-only, `invalid_issuer` by name; the panel asks before changing a live issuer |
| JOSE | `services/aauth/jose.py` | Compact JWS with the fully-specified `Ed25519` alg (PyJWT only registers `EdDSA`, which the draft forbids verifiers to accept), JWK ⇄ key, RFC 7638 thumbprints |
| RFC 9421 profile | `services/aauth/httpsig.py` | Signature base, `@authority` normalisation, Content-Digest, and a **strict** RFC 8941 subset (anything unexpected is refused, not interpreted) |
| Instance key | `services/aauth/keys.py` | One Ed25519 key per instance, AES-256-GCM in `system_settings.aauth_signing_key_encrypted` (Invariant #12), created on first use via insert-if-absent |
| Signer (caller) | `services/aauth/signer.py` | Per-agent key + token cache; `sign_request()` returns the headers for one request. Since ent#878 the key is **HKDF-derived** from the instance seed per hour window (every worker holds the same key; `cnf` is portable) and a caller's `min_remaining` is honoured against the window end on a cache hit as on a miss, so a whole challenge chain signs with one key; `current_agent_key()` exposes key + thumbprint |
| Person-server metadata (ent#878) | `services/aauth/person_server.py` | `{ps}/.well-known/aauth-person.json`: four required fields, `issuer` must equal the configured PS, both token endpoints on its origin, `jwks_uri` MAY be off-host; pinned egress, 64 KB, 8 s, 5 min / 60 s negative cache |
| PS response grammar (ent#878) | `services/aauth/requirement.py` | `AAuth-Requirement` dictionary (strict), interaction url/code rules (same-origin url, Crockford code 4–32 symbols), `Retry-After` clamped 1–60 s, `link = url?code=` |
| PS egress (ent#878) | `services/aauth/egress.py` | Every status with headers + capped body (the protocol is driven by 202/401/403/408/410/429 headers); the `a2a_client` guards — pinned address, no redirects, identity encoding only, wire-size cap, token scrubbed from transport errors — pinned by test |
| Person token (ent#878) | `services/aauth/person_token.py` | `person_token()` / `resume()` / `held_token()`: worker-memory held token stamped with the key thumbprint, Redis pending records `aauth:pt:*` (TTL 30 min, ≤ 3 open per agent), the draft's deferred-response state machine, fail-closed `state_unavailable` |
| Person-token route (ent#878) | `routers/aauth_person.py` | `POST /api/agents/{name}/aauth/person-token` — flag gate as a dependency, own-key only, rate budgets, audit row, HTTP mapping |
| MCP tool (ent#878) | `src/mcp-server/src/tools/aauth.ts` | `request_person_token(resource, pending_handle?)`, self-acting, always registered (`operatorOnly`), relays outcome + link/code, never the token |
| Live binding script (ent#878) | `scripts/dev/aauth_prototype/bind.sh` | Binds one agent against the real PS with the agent's own key; prints link + code, polls honouring `retry_after`, exits 0 on `issued` |
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
| POST | `/api/agents/{name}/aauth/person-token` | the agent's **own** key only | ent#878: obtain a person token for `resource` as the agent, or resume a pending binding by `pending_handle` (Flow 5). Uniform 404 while inert — before body validation |
| POST | `/api/agents/{name}/aauth/resource-call` | the agent's **own** key only | ent#879: call an allow-listed AAuth resource (`{resource}{path}`) as the agent — the whole requirement ladder, every token held by the platform — or resume a pending call by `pending_handle` (Flow 6). Same gates as the person-token route, consulted before the allow-list |
| GET/PUT | `/api/settings/aauth` | admin; the write **interactive-only** | ent#862: the switches, issuer, person server and resource allow-list with their sources, `live`, the identity example and the readiness checklist. PUT writes only the fields sent; `422 invalid_{issuer,person_server,resource_allowlist}`; audited. The generic catch-all refuses all five keys |

All three documents answer a uniform **404** unless AAuth is live (flag on **and**
a valid issuer), so a default install publishes nothing. They are per-IP rate
limited like the public card route.

## Configuration state

| Key | Where | Meaning |
|---|---|---|
| `aauth_enabled` | `system_settings` → `AAUTH_ENABLED` | The switch for Flows 1–4, default **off**; Settings → Integrations → AAuth (ent#862) |
| `aauth_prototype_enabled` | `system_settings` → `AAUTH_PROTOTYPE_ENABLED` | The Flow 5–6 prototypes, default **off**; inert without `aauth_enabled` |
| `aauth_issuer` | `system_settings` → `AAUTH_ISSUER` | This instance's AAuth identity domain, `https://<host>` |
| `aauth_signing_key_encrypted` | `system_settings` | The instance key envelope. Blocked on the generic settings PUT/DELETE; included in the credential-rotation script |
| `aauth_person_server` | `system_settings` → `AAUTH_PERSON_SERVER` | ent#878: the person server agents bind through, `https://<host>` (same strict rule as the issuer, never repaired). Unset → `person_server_unconfigured` |
| `aauth_resource_allowlist` | `system_settings` → `AAUTH_RESOURCE_ALLOWLIST` | ent#879: the AAuth resources agents may call — a JSON list or comma-separated `https://<host>` identifiers, origin equality. **Fail-closed**: unset, malformed or one bad entry → nothing is allowed. Admin settings catch-all only (the grant half); no UI row yet |
| `agent_ownership.a2a_exposed` | OSS column | Pre-existing per-agent exposure, default off |
| inbound allow-list | private A2A module | One list per agent; `aauth:`-prefixed entries are the AAuth audience |
| endpoint registry `auth_scheme` | `system_settings` (AES-GCM envelope) | Per outbound endpoint: `bearer` (default) or `aauth` |

Flag on **without** a valid issuer means AAuth is *inert*, never half-on: the
well-knowns 404, signing is refused, and an inbound signed request falls back to
the ordinary bearer 401.

## Flow 1 — Enablement (per instance)

1. An admin sets the issuer to the instance's public origin and turns the switch on — in Settings → Integrations → "Calls between Trinity instances" (or `AAUTH_ISSUER` / `AAUTH_ENABLED` for bootstrap; a stored row wins over the environment in both directions). The panel reports "On, inactive" until the issuer is valid, and asks before changing a live issuer, since that re-keys every agent's identity for every peer.
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

## Flow 5 — Binding an agent to its owner (person token, ent#878)

The person-server leg. An agent obtains a **person token** (`aa-person+jwt`) for one
resource from its owner's person server (PS), as itself, after its owner has bound it
once through the PS's link-and-code flow. The token says *whose* agent this is; the
platform signs, holds and refreshes it, and the agent only ever sees its metadata.
Presenting it to a resource is ent#879.

1. The agent calls `request_person_token(resource)` (MCP; self-acting — the agent
   comes from the key, agent scope only). The route takes the agent's **own** key:
   flag live (a dependency, so the uniform 404 precedes body validation) → own key
   (403 `aauth_agent_only` for a human, a sibling or a `system` key) → the three rate
   budgets (10 new / min / agent, 30 polls / min / agent, 120 / min fleet).
2. **Cache first.** A held token for `(issuer, PS, agent, resource)` with ≥ 5 min left
   whose `cnf` is still the agent's current key → `issued` (`cached: true`, not
   audited). Otherwise a pending record for the same scope → the same handle again,
   with **no network call** before its `next_poll_at`.
3. PS metadata (`issuer` must equal the configured PS, both token endpoints on its
   origin, `jwks_uri` may be off-host) → the agent token is refreshed so ≥ 20 min
   remain (one key for the whole chain) → a signed
   `POST person_token_endpoint {"resource", "capabilities": ["interaction"]}` with
   `Prefer: wait=5`.
4. **200** — a `aa-person+jwt` whose `iss` (the PS), `aud` (the resource), `cnf` (our
   key) and `exp` (≤ 1 h, clamped by `expires_in` and the agent token's own `exp`) are
   checked, then held in worker memory. The response carries `jti/aud/sub/exp` and
   nothing else. Audit `aauth_person_token_issued` with `token_jti`, never the token.
5. **202** + `AAuth-Requirement: requirement=interaction; url=…; code=…` + `Location`
   + `Retry-After` — every PS-chosen string is validated before it is fetched, relayed
   or stored (same-origin url, Crockford code, same-origin pending `Location`,
   `Retry-After` clamped 1–60 s), then a Redis record under a 192-bit random handle
   (TTL 30 min, ≤ 3 open per agent). The answer is `pending` with
   `interaction{url, code, link, pending_handle, retry_after}` and a `next_step` that
   names the actor: **relay the link only to your OWNER, privately — whoever approves
   first becomes the person this agent acts for** (use `ask_operator` when no person
   is in the conversation). Audit `aauth_person_token_pending` once, on creation.
6. The owner opens the link, signs in at the PS, confirms the code, approves. The
   agent calls again with `pending_handle`: a signed GET of the pending URL with
   `Prefer: wait=15`. 202 → still pending (the record is refreshed; if it cannot be
   persisted the answer is `slow_down` so the client backs off anyway); 200 → step 4
   and the record is deleted; 403 denied / abandoned / revoked, 408 expired, 410 gone
   → a terminal `refused` (record deleted, audit `aauth_person_token_denied`); 429 →
   `slow_down` with a +5 s interval, record kept; an empty 404 → `poll_rejected`,
   record kept (never delete a live binding on an ambiguous status).
7. **Window edge.** A 200 on resume whose `cnf` is no longer our key (the agent key
   rolled, e.g. on the other worker) discards the record and asks afresh — a bound
   agent gets a direct 200 — reported as `reissued_for_key`.
8. A bound agent's later requests, on either worker, are a direct 200 with no
   interaction: the binding lives at the PS, the platform only re-fetches the token.

Live proof: `scripts/dev/aauth_prototype/bind.sh <agent> <resource>` (`demo.sh up`
sets `aauth_person_server`; do not `rewire` after binding — a new issuer host is a new
agent to the PS).

### Refusals (Flow 5)

| Outcome | HTTP | Carries |
|---|---|---|
| Prototype inert (flag off / no issuer) — also for a malformed body | 404 | Uniform |
| Not the agent's own key (human, sibling, `system`) | 403 | `aauth_agent_only` |
| `resource` is not an https server identifier | 422 | `invalid_resource` |
| `aauth_person_server` unset or invalid | 409 | `person_server_unconfigured` |
| Pending state unreadable / unwritable (Redis) | 503 | `state_unavailable` — never an in-memory fallback |
| PS unreachable, or its metadata fails the rules | 502 | `person_server_unreachable`, `person_server_metadata_invalid` |
| A rate budget exhausted | 429 | the rate limiter's detail |
| Protocol outcomes | 200 | `outcome: refused` + `reason`: `denied` / `abandoned` / `revoked`, `expired`, `gone` (terminal — the agent must not retry on its own), `slow_down` (POST: call again without a handle), `signature_rejected`, `unsupported_requirement`, `token_invalid`, `pending_unknown` (another agent's, another PS's or an expired handle — uniform), `too_many_pending`, `poll_rejected`, `ps_protocol_error`, `ps_error` |

The response model has no field that can carry the token (pinned by test); the audit
row carries `token_jti`, the key thumbprint, the outcome, a bounded `ps_error`,
`execution_id` and `trigger_source` — never the token, the handle, the url or the code.

## Flow 6 — Calling a resource: the requirement ladder (ent#879)

The resource leg. An agent calls an **allow-listed** AAuth resource (`{resource}{path}`,
e.g. the Senzing sanctions API) as itself; the platform climbs the draft's requirement
ladder — agent token → person token (Flow 5) → an **auth token** the person server mints
against a **resource token** the resource issued — presents the right token, holds all of
them, and relays the answer. The agent sees `token_kind`, the ordered `challenges`, the
resource's body and `AAuth-Budget`, or the link + code its owner must consent at. Nothing
it sees is a credential.

1. The agent calls `call_aauth_resource(resource, method, path, body, justification,
   dedup_label)` (MCP; self-acting, agent scope only). The route takes the agent's
   **own** key through Flow 5's gates — flag live (the uniform 404 precedes body
   validation) → own key (403 `aauth_agent_only`) → the rate budgets (20 calls / min /
   agent, 30 resumes, 120 fleet) — and only then the **allow-list**: origin equality
   against `aauth_resource_allowlist`, fail-closed (403 `resource_not_allowed`). A
   refused principal never learns what is listed.
2. **Effect guard.** A new call runs under `effect_guard("aauth_resource", {resource,
   method, path}, dedup_label)` — the body is not identity (#1084). Only a terminal
   `result` / `denied` completes the claim; a pending answer, an expiry or a transient
   refusal releases it, so the post-consent retry under the same label runs for real
   instead of replaying a dead pending. A completed replay answers the stored snapshot
   with `replayed: true` and sends nothing. A resume never enters the guard.
3. **Resource metadata** (`/.well-known/aauth-resource.json`): `issuer` = the resource,
   same-origin `jwks_uri` and control endpoints; `access_mode` picks the starting rung
   (Senzing says `auth-token`, so the client starts at the person token — the resource
   never has to challenge `person-token`); `additional_signature_components` honoured
   for `content-type` / `content-digest` only. `path` may not be a declared control
   endpoint, `/.well-known/…`, contain `..`, `//`, `?`, `#` or `%` — it is signed
   byte-for-byte as sent.
4. **Send**, signed by the agent key with the presented token in `Signature-Key` and
   `AAuth-Capabilities: interaction`. 2xx → `result` (body capped + sanitised, every
   token the call held scrubbed, `AAuth-Budget` parsed). 401 `requirement=person-token`
   → Flow 5's `person_token` (an `interaction_required` if the owner has not bound yet)
   → retry once. 401 `requirement=auth-token; resource-token="…"` → step 5.
5. **Verify the resource token** (the sink, one red test per check): JWKS `kid` +
   Ed25519 signature + no forbidden headers; `iss` = the resource, `dwk` =
   `aauth-resource.json`, `aud` = our PS (three-party; an Access-Server `aud` is refused
   `unsupported_aud`), `agent_jkt` = our current key, `ps` = ours, `sub` +
   `presented_jti` = the token we just presented, `exp` future, `iat` not future,
   lifetime ≤ 1 h. A failure is a named refusal + a WARN "possible tampering", never a
   PS trip. An auth-token challenge while presenting the **agent** token is
   `unexpected_challenge` (the agent token is never a `presented_token`).
6. **Exchange** at the PS: signed `POST auth_token_endpoint {resource_token,
   presented_token, justification, capabilities: ["interaction"]}`, `Prefer: wait=10`.
   200 → the `aa-auth+jwt` is inspected (`typ`, `iss` = the resource token's `aud`,
   `aud` = the resource, `cnf` = our key, `sub`, `exp` ≤ the presented token's; the PS
   signature is **not** verified, plan T5), cached in Redis under
   `aauth:at:{sha256(issuer|ps|agent|key_thumbprint|resource|scope)}` with TTL
   `exp − 300`, and the original request is re-sent with it → `result`
   (`token_kind: auth`, `challenges: ["auth-token"]`, budget cost 2 on Senzing). 202
   `requirement=interaction; url; code` → `interaction_required` with a pending handle
   (kind `ps_interaction`) and the same **relay only to your OWNER, privately** rule;
   202 `requirement=approval` → `pending` (`ps_approval`). 400
   `expired_presented_token` / `invalid_presented_token` → forget the person token, ONE
   fresh chain. 403 `user_unreachable` → `denied`; 502 → `ps_unreachable`.
7. **Second call** (a new `dedup_label`): the held auth token is presented first →
   `result` with `challenges: []`, **zero PS trips** — the AC, and the demo's beat.
   A 401 `Signature-Error: expired_jwt` on it → evict, fall to the person token, ONE
   re-authorization; a step-up `requirement=auth-token` while presenting an auth token
   → one more exchange with that token as `presented_token`.
8. **Resume** (`pending_handle` alone): the record (Redis, Flow 5's namespace; carries
   the STORED method / path / body / key thumbprint, never a token) is loaded for this
   agent, issuer and PS; inside `retry_after` the stored shape is answered with no
   network; otherwise one signed `GET` of the pending URL with `Prefer: wait=15` —
   PS 200 → cache the auth token and **re-send the stored request** (what runs is what
   the person consented to, never caller arguments); 403 → `denied`; 408 → `expired`;
   410 → `gone`; 429 → `+5 s`. A resource-side 202 (`resource_polling`) is polled the
   same way with the auth token. A rotated key since the consent → `key_rotated`
   (start over); another agent's / issuer's / PS's handle → `handle_unknown`.
9. Every call writes one audit row (`EXECUTION aauth_resource_call` after egress,
   `AUTHORIZATION aauth_resource_refused` before it) with the agent, resource, method,
   path, token kind, outcome, status, budget, `challenges`, `replayed` / `resumed`,
   `execution_id` + `trigger_source` — never a token, the body, the justification, a
   handle, a url or a code — and one activity row (direction `aauth_resource`).

Bounds: ≤ 6 resource sends, ≤ 3 PS exchanges, one 25 s budget, per-leg `min(10 s,
remaining)`; nothing is polled inline on the first call; a POST that times out after it
was sent is reported `possibly_executed` (#914) and the tool tells the agent not to
re-send blindly.

Live proof: `demo.sh run` step 7 (`bind.sh`, waits for the owner) + step 8
(`senzing_check.py` with the agent's own key: identity, the challenge sequence actually
seen, the top candidates' `match_key` / `match_level`, the budget cost, then the second
call with zero PS trips, then A's audit rows); `demo.sh rehearse` runs 1–8 with no
pause. `senzing_check.py` works from a laptop with the agent's MCP key.

### Refusals (Flow 6)

| Outcome | HTTP | Carries |
|---|---|---|
| Prototype inert (flag off / no issuer) — also for a malformed body, both shapes | 404 | Uniform |
| Not the agent's own key (human, sibling, `system`) — before the allow-list | 403 | `aauth_agent_only` |
| Resource not listed (or the list is empty / malformed / unreadable) | 403 | `resource_not_allowed` |
| Resource wants payment | 402 | `payment_required` (surfaced, never retried) |
| `aauth_person_server` unset; a concurrent call holds the same effect claim | 409 | `person_server_unconfigured`, `effect_in_progress` |
| Body shape (call XOR resume), bad path / method / body size, unsupported signature components, pull-mode agent without an execution id | 422 | pydantic detail, `invalid_*`, `unsupported_signature_components`, `effect_unguarded` |
| The resource's 429, or a local rate budget | 429 | `rate_limited` + `retry_after`, or the limiter's detail |
| Pending state unreadable / unwritable (Redis) | 503 | `state_unavailable` — never an in-memory fallback |
| The call budget ran out | 504 | `timeout` (+ `possibly_executed` after a POST was sent) |
| The peer did something we refused or could not use | 502 | `resource_metadata_invalid`, `resource_unreachable`, `resource_token_invalid:<check>`, `unsupported_requirement:<value>`, `unexpected_challenge`, `agent_token_rejected`, `person_token_rejected`, `ps_rejected:<code>`, `ps_unreachable`, `gone`, `key_rotated`, `too_many_rounds`, `step_up_loop`, `token_unavailable`, `unsupported_aud`, `endpoint_*` |
| Protocol outcomes | 200 | `outcome`: `result` · `interaction_required` / `pending` (handle + `retry_after`, link + code on an interaction) · `denied` (`denied` / `abandoned` / `revoked` / `user_unreachable`, `do_not_retry`) · `expired` (`pending_expired` / `handle_unknown`, `do_not_retry`) |

No request or response field can carry a token (pinned by test): `token_kind` is one of
`agent` / `person` / `auth`, and the body text has every token the call held scrubbed on
top of the platform patterns.

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
5. `message/stream` is refused for AAuth callers; no missions, budgets, or
   sub-agent identities (`parent+child@domain` is refused explicitly). The person
   server is used only for the binding leg (Flow 5); no Access Server.
6. **Binding hijack** (ent#878, plan T7). The PS binds the agent to whoever approves
   FIRST, and the link is relayed by the agent in whatever channel it is in — a
   public-link visitor, a Slack group or an inbound A2A caller can drive that turn.
   The demo mitigation is the tool description + Flow 5 ("relay only to your owner,
   privately") and the audit row's `execution_id` + `trigger_source`; owner-only
   delivery of the link (notification / operator queue) is the productization fix,
   registered in DEBT_INBOX and noted on #558 task G.
7. The instance key signs for every agent, so the PS binds the **instance's** name
   for the agent (`aauth:<agent>@<issuer-host>`): a compromised instance can bind as
   any of its agents, and a new issuer host (a tunnel restart) is a new agent to the
   PS — rebind, never `rewire` after binding.
8. The held person token is per worker (a second worker re-requests; a bound agent
   gets a direct 200, no second consent), and the PS's signature over it is **not**
   verified here (plan T4; the resource verifies it — #558 task G).
9. `request_person_token` and `call_aauth_resource` are **advertised but inert** while
   the flag is off: always registered, with the backend's 404 reported as
   `aauth_disabled` (plan T3 deviation; dynamic registration through the #846
   reconciler is registered debt).
10. **Resource allow-list is a settings key** (ent#879, T6): `aauth_resource_allowlist`
    is edited by an admin through the settings catch-all; fail-closed, origin equality,
    no Settings-UI row (registered debt). It is the only thing between an LLM-supplied
    URL and a signed server-side request, which is why it fails closed on every
    unreadable state rather than honouring the lines that parsed.
11. **Auth tokens and pending records live in Redis** under the transient-secret
    posture (Invariant #12): proof-of-possession-bound (useless without the agent key),
    TTL-bounded (`exp − 300`, 30 min for a record), never persisted to SQLite, never
    logged. A Redis outage reads as "not held" (one more PS exchange) or
    `state_unavailable`, never as an unsigned call or an in-memory fallback.
12. **The PS signature over the auth token is not verified** (plan T5): the token is
    inspected (`typ`, `iss`, `aud`, `cnf`, `sub`, `exp`) and the resource verifies it;
    RS256/ES256 verification against an operator-listed PS JWKS is #558 task G.
13. **Four-party `aud` deferred**: a resource token whose `aud` names an Access Server
    rather than our PS is refused `unsupported_aud`; only the three-party exchange
    (Senzing's) is implemented. The PS `interaction_endpoint` relay and
    `authorization_endpoint` pre-authorisation (T8) are likewise out of scope.
14. **The effect guard arms only for a real execution** of the agent (the MCP path): a
    script run with `execution_id=manual` proceeds unguarded and says so; an unknown id
    is refused on a pull-pilot agent (#2392). A `result` snapshot (the sanitised answer —
    for Senzing, sanctions candidates) persists 24 h in `idempotency_keys`, the #736
    precedent.

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
- `docs/memory/requirements/mcp.md` §32.8 — the requirement, including out-of-scope
- `docs/memory/requirements/mcp.md` §32.9 — the person-token binding leg (ent#878), Flow 5
- `docs/memory/requirements/mcp.md` §32.10 — the resource-call ladder (ent#879), Flow 6
- `docs/memory/learnings.md` (2026-09-17) — why a second instance needs its own Docker daemon
