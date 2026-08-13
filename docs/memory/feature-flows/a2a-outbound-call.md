# A2A Outbound Calls — `call_a2a_agent` (#736)

The **calling** half of A2A interop. [a2a-inbound-server.md](a2a-inbound-server.md)
makes a Trinity agent *reachable*; this makes it a *caller* — a Trinity agent
asks Trinity to task an external A2A agent (Google ADK, LangChain, AWS Bedrock,
a remote Trinity) and gets the answer back inside the same tool call.

**OSS-core by decision**, not by omission. The parent epic
(trinity-enterprise#156) records the owner's ruling — *"Outbound = OSS. A
Trinity agent calling out to an external A2A agent (#736) stays open-core."* —
so there is no `requires_entitlement` anywhere on the call path. OSS also ships
a working target source, not merely the seam: a seam with no registered
provider resolves nothing, so the tool would answer "no targets configured" on
every install, which is not what the ruling can mean. Recorded here so the
ruling is never re-inferred from the mere fact that this merged (the ent#326 /
ent#384 discipline).

> **Note for the owner.** Option 4 (an OSS platform-scope list) narrows a
> private module's role here to **per-agent scoping plus the managed UI**. If
> that scoping was deliberate monetization, the OSS list becomes the floor and
> a per-agent provider gates on top — a one-line change at this seam. That is a
> product call, not an engineering one, and it is deliberately not made here.

Requirements: `docs/memory/requirements/mcp.md` §32.5.

---

## Why now, stated honestly

The driver is **release-narrative completeness plus #738**. Trinity shipped the
inbound half (ent#157/#158/#160) and a user doc that already promises *"This
registry feeds the agent's outbound A2A calls"* — a promise the tree did not
keep. #738 (Trinity-to-Trinity federation) is explicitly downstream of this
issue and cannot start without it.

It is **not** "the low-risk entry point" the original research called it. That
framing assumed outbound would ship *first*, before any A2A surface existed;
inbound shipped first instead, and the security argument below is what "low
risk" turned out to cost. Saying so matters because the stale framing is what
would justify a thinner implementation.

Two reframes were considered and rejected, recorded so they are not
re-litigated:

* **Collapse into #738.** Largely *absorbed* rather than rejected: the loopback
  round-trip test drives this client against Trinity's own inbound server, so
  Trinity↔Trinity is already the proving case. What remains separate is the
  registry, the credential handling and the SSRF path — all of which #738 would
  need anyway.
* **Model an external A2A agent as an MCP entry in the agent's `.mcp.json`**
  (#2007). A genuinely different product, not a cheaper version of this one: an
  MCP server is not an A2A peer, so it needs a per-peer adapter — which is
  precisely the integration cost A2A exists to remove.

**`a2a-python` (the reference SDK) is not used.** Two methods are needed, and
the part that carries the risk — validation, IP pinning, byte ceilings,
sanitisation — is the part we must own rather than inherit.

---

## Where the fetcher lives, and why that is a decision

Three placements were available:

| Placement | Rejected because |
|---|---|
| **MCP server** | Invariant #13 makes it a proxy over the backend API; the SSRF controls are Python; and it would need the plaintext credential, breaking the property `tools/a2a.ts` documents as a guarantee ("no tool echoes a secret back"). |
| **Agent container** | Genuinely tempting — it would put the egress on the agent network, away from the platform network. But the credential is an AES-256-GCM envelope only the backend can open, the response must be credential-sanitised before an LLM sees it, and the audit row is a Python write. Handing an agent container the decryption key to move the socket one network over is a bad trade. |
| **Backend** ✅ | Where the credential, the sanitiser, the SSRF module and the audit writer already are. |

---

## Layers

| Layer | File | Notes |
|-------|------|-------|
| MCP tools | `src/mcp-server/src/tools/a2a_call.ts` (+ `a2a_call.test.ts`) | `call_a2a_agent`, `get_a2a_task`. Separate module from the ent#160 management plane; registered in `toolGroups` → `operatorOnly` allow-list |
| MCP client | `src/mcp-server/src/client.ts` | `callA2AAgent` / `getA2ATask` — own `AbortController` (`MCP_A2A_TIMEOUT_MS`, 40s), so **we** abort before the MCP gateway does |
| Router | `src/backend/routers/a2a.py` | `POST /{name}/a2a/call`, `POST /{name}/a2a/task`. Auth + HTTP error map + audit only |
| Orchestration | `src/backend/services/a2a_outbound_service.py` | kill switch → bounds → resolve → validate → `effect_guard` → call → activity |
| Target seam | `src/backend/services/a2a_outbound.py` | **Fail-CLOSED** provider seam + the OSS `system_settings` provider |
| Protocol client | `src/backend/services/a2a_client.py` | Card fetch, dialect, RPC, pinning, caps, sanitisation. FastAPI-free; raises `A2ACallError` |
| Shared vocabulary | `src/backend/services/a2a_protocol.py` | JSON-RPC + A2A error codes, the dialect table, envelope helpers — used by **both** directions so they cannot drift |
| SSRF gate | `src/backend/utils/url_validation.py` | `validate_a2a_endpoint_url` over the shared `_validate_public_https_url` |
| Admin registry | `src/backend/routers/settings.py` | `GET/PUT /api/settings/a2a-endpoints`, `DELETE /api/settings/a2a-endpoints/{ref}` |
| Kill switch | `services/settings_service._resolve_bool_flag` | `system_settings` → `A2A_OUTBOUND_ENABLED` env → **OFF** |

**No DB change.** No table, no SQLite migration, no Alembic revision — the
endpoint list is one AES-256-GCM envelope in `system_settings`, the shape
Invariant #12 already blesses for `elevenlabs_api_key_encrypted`.

---

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/agents/{name}/a2a/call` | `AuthorizedAgentByName` + agent self-check | Task a registered external A2A agent |
| POST | `/api/agents/{name}/a2a/task` | same | Poll a remote task by id |
| GET | `/api/settings/a2a-endpoints` | admin + human-only | List registered endpoints (`has_credentials` only) |
| PUT | `/api/settings/a2a-endpoints` | admin + human-only | Register/update one by name; credential write-only |
| DELETE | `/api/settings/a2a-endpoints/{ref}` | admin + human-only | Remove one |

Both call routes **404 when the kill switch is off** — Trinity's answer for
"this capability is not present" everywhere else.

---

## One request, end to end

```
agent container (LLM — prompt-injectable)
   │  call_a2a_agent(agent_name, endpoint="partner", message, dedup_label)
   ▼
mcp-server  tools/a2a_call.ts        canAccess: operatorOnly {user,agent,system}
   │  self-only check for scope=="agent"        AbortController 40s
   ▼
backend  routers/a2a.py              AuthorizedAgentByName + self-check
   │                                 kill switch → 404 when off
   ▼
services/a2a_outbound_service.py
   ├─ rate bounds: a2a_out:agent:{name} 30/60s  AND  a2a_out:fleet 120/60s
   ├─ resolve  → services/a2a_outbound.py     FAIL-CLOSED, isinstance-checked
   ├─ validate → validate_a2a_endpoint_url    off-loop, returns validated IPs
   ├─ effect_guard("a2a_call", {endpoint_id, resolved_url, context_id, task_id},
   │               dedup_label=<required>)
   ▼
services/a2a_client.py               trust_env=False · follow_redirects=False
   ├─ GET  {origin}/.well-known/agent-card.json   uncredentialed, ≤256 KiB
   │        pinned IP · Host+SNI = registered hostname · identity encoding
   │        └─ same-origin pin on card.url · dialect from protocolVersion
   └─ POST {rpc_url}  Authorization: Bearer <credential>   ≤1 MiB, same pin
            └─ parse body for `error` EVEN ON HTTP 200
   ▼
scrub_secret_and_urls → sanitize_text → redact_url_userinfo → 32 KiB truncate
   ▼
agent_activities row (host + endpoint name only) · audit row (same) · response
```

Everything is under one `asyncio.wait_for` wall-clock deadline wrapping
genuinely cancellable awaits.

---

## The gates, in order

1. **Kill switch** (`A2A_OUTBOUND_ENABLED`, default OFF) → 404. First, so a
   disabled feature costs nothing and advertises nothing.
2. **`AuthorizedAgentByName`** — owner, admin, or shared.
3. **Agent self-check** — an agent-scoped key may call only **as itself**.
   `AuthorizedAgentByName` resolves an agent key to its OWNER, so without this
   a *permitted sibling* could place calls under a neighbour's name. Be precise
   about what that buys under the OSS provider, because the obvious reading is
   wrong: endpoints there are **platform-scope**, so there is no "own" versus
   "neighbour" credential — every agent may name every registered endpoint.
   What the check protects is **attribution**: the rate-limit key, the audit row
   and the `agent_activities` row all name the agent that actually spent the
   call, so a sibling cannot launder its egress through a neighbour. It also
   holds the line for a future per-agent provider, where the obvious reading
   becomes the literal one. `reject_agent_principal` is **not** used — this is a
   *use* of a capability an admin already granted by registering the endpoint,
   not a *grant* (Invariant #8's grant-vs-use line).
4. **Rate bounds** — per-agent *and* fleet, both through the Redis limiter. A
   per-agent limit bounds one agent; the fleet is the actual exhaustion path.
5. **Resolution** — fail-closed. No provider, a raising provider, a malformed
   return, or an endpoint with no URL all refuse.
6. **SSRF validation** — on every call, on every URL, including one that came
   out of the registry.
7. **`effect_guard`** — see below.

**Behaviour that is deliberate and worth knowing** (so nobody "fixes" it):

* the agent **container need not be running** — the backend makes the call;
* **read-only mode** governs source writes in the container, and
  **`autonomy_enabled`** governs whether the scheduler fires proactive work.
  Neither gates a caller-initiated outbound call;
* an **ephemeral ghost's own key is refused by construction** — the
  trinity-enterprise#69 fence is an allow-list at the auth entry point and
  these routes are not on it. A disposable agent running untrusted work is the
  last principal that should spend a stored credential.

---

## Why the target is a name and not a URL

This is the whole security story; everything else defends it in depth.

An agent's tool arguments are LLM-generated and prompt-injectable. A URL
parameter turns **any document the agent reads** into a lever on an
authenticated, credentialed, server-side request from inside the platform
network — where Redis, the Docker socket proxy, the agent containers and cloud
metadata live. No amount of IP filtering makes that safe, because filtering is
a blocklist race (DNS rebinding, CGNAT, IPv6-mapped forms, redirect chains)
while a registry is a whitelist of things a human deliberately typed.

It is also not an invention. The shipped `register_a2a_endpoint` tool already
says so: *"this feeds the runtime `call_a2a_agent` (abilityai/trinity#736)"*.

**The cost, stated:** an agent cannot discover-and-call a novel A2A peer at
runtime. An operator registers it first. Reversible later behind an explicit
operator opt-in; not reversible is the alternative.

---

## Why the registry URL is still not trusted

Registration validates a URL with `startswith("http://") or
startswith("https://")` and a 2048-char cap. **No SSRF check, and plain
`http://` accepted.** So "it is in the registry" cannot mean "safe to fetch",
and `validate_a2a_endpoint_url` runs on **every call**:

* HTTPS only — an `http://` row is refused at **use**, with a message telling
  the operator to re-register. Not silently upgraded: a client that quietly
  rewrites a scheme is one that will quietly rewrite it back.
* No userinfo — refused, never stripped (a stripped credential is one the
  operator believes is in use).
* IDNA/A-label normalised **once**, so the parser and the resolver cannot
  disagree about which host was approved. A codec failure on a pure-ASCII host
  is *not* fatal (canonicalising ASCII is a no-op, and underscores are legal in
  real hostnames); on a non-ASCII host it is, because that is where the
  homograph lives.
* **Every** address `getaddrinfo` returns must be public. One internal record
  among public ones refuses the whole endpoint — which record a resolver hands
  out is not our choice.
* DNS failure is fatal, and resolution runs **off the event loop**: a
  synchronous `getaddrinfo` on a per-call agent path freezes an entire worker's
  loop, which no per-agent rate limit bounds.
* Refusal messages are fixed strings, never built from a resolved address.

---

## DNS rebinding is closed, not accepted

`validate_template_registry_url` documents rebinding as an accepted residual.
That is right *there* — it fetches a display-only catalog — and **wrong here,
because this request carries a credential**.

One resolution produces one validated address. Both hops connect to it while
presenting the registered hostname for `Host`, SNI **and certificate
verification** (httpx per-request `sni_hostname` extension), so TLS still
authenticates the registered name.

**The availability trade:** pinning one address means a host whose selected A
record is down fails even though its siblings are up.

**`trust_env=False`** is the other half, and it was missed by every reviewer of
the sibling fetchers: all this validation reasons about the *target* IP, and an
`HTTPS_PROXY` in the environment makes the target irrelevant because the socket
goes to the proxy. Because that flag also switches off httpx's
`SSL_CERT_FILE`/`SSL_CERT_DIR` handling, the CA context is rebuilt explicitly —
we refuse the environment's **proxies**, we still honour its **trust store**.

---

## The card is a hint, not an authority

ent#159 (signed Agent Cards) is `status-blocked`, and this does not wait for
it — because the design removes the card's authority rather than trying to
verify it. A signature scheme whose own scope is *"validate when signed, warn
when unsigned"* cannot bound a credentialed fetch: an attacker simply does not
sign.

* the card's declared `url` must be **same-origin** with the registered
  endpoint. Default-port equivalence is load-bearing — **Trinity's own card
  emits no explicit port**, so getting it wrong makes Trinity unreachable by
  its own rule and breaks #738 on day one;
* `securitySchemes` **never** selects the credential;
* the card is always derived from the **origin**, never by appending
  `/.well-known/...` to a registered path (which would silently fetch a
  different agent's card). If the registered URL carries a path, it is accepted
  as the RPC target only when the card's `url` matches it exactly; ambiguity is
  refused by name;
* **no redirects** on either hop — a 3xx is a failure. The bounded
  re-validated redirect loops elsewhere (Slack, WhatsApp) exist because those
  vendors genuinely 302 to CDNs; A2A has no such requirement, and "no
  redirects" is strictly safer than "three validated ones";
* the card fetch is **uncredentialed** — it is the one hop made before anything
  about the peer is known.

**Consequence, recorded rather than hidden:** an unreachable card blocks the
RPC, because the same-origin pin depends on it. A card outage takes a healthy
endpoint down. That is the fail-closed direction.

---

## `effect_guard` identity — the bug that returns a wrong answer

Wired as the **fifth** `effect_guard` sink (#1084), because an outbound A2A
call is an irreversible external effect in the same class as `send_message` /
`call_user` / `share_file`. Invariant #18 does not apply: no execution is
created.

The identity is `{endpoint_id, resolved_url, context_id, task_id}` **and
`dedup_label` is a required tool parameter.**

Inheriting `send_message`'s keying — the recipient alone — would be a
*wrong-answer* bug rather than a failure: a second question to the same
endpoint inside one execution reads as a completed replay, and the agent
receives **the answer to its first question**, with no error, no 4xx and
nothing logged. It then reasons confidently on stale data. "One message per
recipient per turn" is the desired shape for a notification sink and the bug
for a request/response conversation — which this is, as its own `context_id` /
`task_id` parameters concede.

The message body is deliberately **not** in the key (#1084's rule: an
LLM-generated body is non-deterministic across a re-run and would defeat
re-delivery dedup entirely), so an explicit label is what distinguishes two
calls — and requiring it keeps the agent's intent legible instead of inferred.

`get_a2a_task` is **not** guarded: a poll is a read, and deduping it would
answer "has it finished yet?" from a snapshot of the last time it had not.

**Honest scope:** `execution_id` is an agent-supplied parameter and the guard
**fails open when it is absent**, so this is best-effort and agent-cooperative,
not at-most-once. It enlarges the debt architecture.md already names as a
blocking prerequisite for pull-mode default-ON.

---

## Degradation under infrastructure loss, stated rather than emergent

The two bounds on this path do **not** share a failure domain, so the tempting
"lose Redis, lose both" reading is wrong in both halves.

| Control | Backing store | With Redis unreachable |
|---|---|---|
| `rate_limiter.enforce` | Redis ZSET | fails **soft** — `_check_inprocess`, a bounded per-worker sliding window. The limit survives; its scope narrows from fleet-wide to per-worker, so `--workers 2` means an effective 2x, not unbounded |
| `effect_guard` (`idempotency_service`) | the `idempotency_keys` **table** | unaffected. It is not on Redis at all; it fails open only on a DB write failure, at which point the platform is already down |

The genuine fail-open here is the one FR-8 names: `execution_id` is an
agent-supplied parameter and the guard fails open when it is absent. That, not a
Redis outage, is where the residual lives. The kill switch remains the control
an operator reaches for — but it is not compensating for a bound that
disappears.

---

## Errors

Errors ride the JSON-RPC envelope at **HTTP 200** (Trinity's own inbound server
does exactly this), so the client parses the body for `error` even on 200. A
status-only check would read every remote failure as a success and hand the
agent an error object as if it were an answer.

| Reason | Status | Cause |
|---|---|---|
| `endpoint_not_found` | 404 | No registered endpoint matched (or the seam refused) |
| *(kill switch off)* | 404 | `A2A_OUTBOUND_ENABLED` off |
| `endpoint_not_https` | 400 | A registry row holding `http://` |
| `endpoint_private_address` | 400 | Resolved inside the perimeter |
| `endpoint_dns_failure` | 400 | Unresolvable, or resolution timed out |
| `message_too_long` | 422 | Over the 100 000-char outbound cap |
| `card_redirect` / `rpc_redirect` | 502 | A 3xx — a failure, not a hop |
| `card_too_large` / `rpc_too_large` | 502 | Wire-byte ceiling hit mid-stream |
| `card_encoding` / `rpc_encoding` | 502 | Compressed body refused, not decoded |
| `card_origin_mismatch` | 502 | Card `url` on another origin (logged ERROR) |
| `card_url_invalid` | 502 | Card `url` embeds credentials — `_same_origin` compares `hostname`, which strips userinfo, so this would otherwise compare equal to the registered origin |
| `card_url_ambiguous` | 502 | Registered path the card does not declare |
| `unsupported_protocol_version` | 502 | A `1.x` card (documented, not claimed) |
| `remote_error` | 502 | JSON-RPC error, including on HTTP 200 |
| `timeout` | 504 | RPC timeout or the wall-clock deadline |
| *(in-flight duplicate)* | 409 | `EffectInProgressError` — never a silent skip |
| *(rate bound)* | 429 | Per-agent or fleet |

---

## Credential handling, and the limit of it

Decrypted server-side only, attached as `Authorization: Bearer` on the RPC POST
only, never on the card fetch, never returned, never logged, never in audit
`details` (which carry endpoint id/name, **host**, state and remote task id —
not the URL, not the message, not the response).

`ResolvedEndpoint` overrides `__repr__` so the plaintext cannot reach a log
line, an exception `repr` or a Pydantic 422 `input` field — the last is a real
precedent (`error_handlers.validation_error_without_input` exists because
rejecting a bad secret at the Pydantic boundary was found to *echo* it).

Text coming back passes `scrub_secret_and_urls(text, credential)` →
`sanitize_text` → `redact_url_userinfo`, **over a 2× window before
truncation** — a bare `[:cap]` slice can cut a secret so the redaction no
longer matches, publishing the surviving prefix.

> **The trust boundary, disclosed rather than papered over.** Exact-value
> redaction removes the *literal* credential. A cooperating remote that base64s,
> rot13s or splits it defeats that, and no programmatic control fixes it.
> **Registering an endpoint grants that endpoint the ability to exfiltrate its
> own credential.** Registration is a trust decision about a peer; it is worded
> that way in the user doc and in `.env.example`.

---

## Dialect

| Card `protocolVersion` | Methods | `A2A-Version` |
|---|---|---|
| absent / unparseable / `0.3.x` | `message/send`, `tasks/get` | *(omitted — the spec says empty ⇒ v0.3)* |
| `1.x` | *(defined, refused)* | — |

The issue's *"Target v1.0 only"* is rejected on evidence: Trinity's own card
pins `0.3.0` and its server dispatches slash names, so a v1.0-only client
**cannot talk to Trinity** and #738 would be dead on arrival. The `1.x` arm is
documented in `a2a_protocol.py` and deliberately **not claimed** — there is no
v1.0 peer to verify it against, and §FR-6 already used "untestable ⇒ do not
ship it" to reject SSE.

The negotiated dialect and the resolved RPC target are cached briefly **per
registered endpoint** — keyed on the registered URL, never on the origin. One
host can carry several separately-registered endpoints (a multi-tenant peer with
an agent per path is exactly what the registered-path rule supports), and two
registry rows are two trust relationships with two credentials; an origin key
let a call to one of them decide where a *sibling's* poll sent its credential.

Only `get_a2a_task` reads the cache. A poll would otherwise pay a second full
egress to re-read one field and burn the caller's own rate budget doing it,
whereas `call_a2a_agent` re-reads the card on every call by design, so the
same-origin pin is re-evaluated against fresh peer state before every
credentialed send.

---

## What is not here

* **Streaming.** An MCP tool call is one request and one response — a FastMCP
  `execute()` returns a string, so there is no channel for token chunks to
  reach the calling agent's turn. It would also buy nothing against Trinity,
  whose `message/stream` awaits the whole atomic turn and emits two events. No
  `stream` parameter is accepted at all: a parameter that is accepted and
  silently does not stream is a lie in a schema agents read.
* **A Trinity execution row.** An outbound call is an execution of a *remote*
  agent. Minting a `schedule_executions` row would pollute fleet cost/analytics
  (EXEC-022, #1107), capacity accounting and the canary invariants that reason
  about `running` rows (E-01/E-05). No new `triggered_by` value exists, so
  `_VALID_TRIGGERS` / `_TRIGGER_BUCKETS` / `_AUTONOMOUS_TRIGGERS` are correctly
  untouched — a later revision that mints a row must update all three.
* **`tasks/cancel` outbound, push notifications, non-text Parts, an `X-API-Key`
  scheme, per-agent endpoint scoping** (the enterprise delta), and **card
  signature verification** (ent#159, defence in depth rather than a blocker).

---

## Testing

| File | Covers |
|---|---|
| `tests/unit/test_736_a2a_url_validation.py` | The validator, table-driven: scheme, userinfo, every IP class, DNS failure, IDNA/A-label, the ASCII-codec asymmetry, and that a refusal never echoes a resolved address |
| `tests/unit/test_736_a2a_outbound_transport.py` | Transport properties over a **real** `httpx` client on `MockTransport` with genuinely streaming responses: byte ceilings vs a lying `Content-Length`, refused compression, refused redirects, connect-time IP pin + SNI, proxy neutrality, dialect, HTTP-200-with-error, redaction with a **non-pattern** credential, the truncation-boundary case |
| `tests/unit/test_736_a2a_outbound_call.py` | Route/auth/dedup/seam over `TestClient` — including the **loopback round trip** against Trinity's own inbound server and the two-different-messages replay regression |
| `src/mcp-server/src/tools/a2a_call.test.ts` | Proxy shape, self-only gate, schema (no URL, no `stream`, required `dedup_label`), error→flag mapping, `possibly_delivered` on abort |
| `src/mcp-server/src/tools/a2a.test.ts` | The F8 addition: the two management **reads** now gate agent-scoped keys |
| `src/mcp-server/src/tool-visibility.test.ts` | The outbound tools are operator-scope only |

> **A transport test whose mock does not stream is not a transport test.**
> `httpx.Response(content=…)` decodes and buffers in the constructor, so
> `aiter_raw()` raises `StreamConsumed` against it and a byte-ceiling test built
> that way is green against a shape production cannot even read. The
> `_as_streaming` helper is lifted from `test_ent14_registry_fetch.py`, which
> exists because that exact mistake shipped once.

**The MCP-server TypeScript suite is run by no CI workflow** and not by
`verify-local`, so "tests pass" for that half means someone ran
`cd src/mcp-server && npm test` by hand.
