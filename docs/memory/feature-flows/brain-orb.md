# Feature: Brain Orb — The Self-Rendering Mind (trinity-enterprise#58)

> **Type**: feature · P2 · `theme-ui-ux` · first child of the tighter-Cornelius-integration epic
>
> **One-line**: Capability-gated per-agent page that renders a Cornelius-class agent's live 3D knowledge-graph orb from data the agent produces in its own container, with live scope control and a client-held voice tile. **Shipped: Phase 1 (static render + read path) + Phase 2 (scope mount/unmount → re-export → live rebuild) + Phase 3 (client-held Gemini Live voice + read-only KB search) + Phase 4a (owner-gated KB-write actions: capture + link) + Phase 4b (voice-transcript capture + configurable post-session processing, #66) + the write→graph refresh loop (#67/#68).** Only `run_skill` (arbitrary headless exec from the orb) remains out of scope.

## Scope (and what is deferred)

The full issue describes voice (client-held Gemini Live), a scope mount→re-export→rebuild loop, KB-write actions, automatic transcript capture, and headless-skill injection. Delivered in self-contained, default-OFF phases:

- **Phase 1 — static render + read path.** First-party CSP-clean orb assets, gated route/tab, read-only `data.json` proxy.
- **Phase 2 — live scope control.** Button-driven scope mount/unmount → agent re-export → live in-place rebuild, via owner-gated broker + agent convention hooks. No Gemini/voice.
- **Phase 3 — client-held voice tile + read-only KB search.** The browser holds its own Gemini Live socket (ephemeral-token broker); visual tools + scope-by-voice run in-browser; a read-only `search` hook backs whole-vault lookup. KB WRITES stay off.
- **Phase 4a — owner-gated KB-write actions (capture + link).** Owner/admin-only capture-a-note and link-two-notes via a new broker `POST /action` route → an agent `~/.trinity/brain-orb/action` convention hook. Behind its own `BRAIN_ORB_WRITE_ENABLED` kill-switch, rate-limited, audit-logged, `Idempotency-Key`-deduped. Voice owners get capture/link folded into the locked manifest.
- **Phase 4b — voice-transcript capture + post-session processing (#66).** The mint enables Gemini `input/output_audio_transcription` (locked into the constrained token); `voice.js` buffers per-turn transcription into conversation events and, on `endConversation`, relays them to `orb.js` which POSTs `capture_transcript` (session-id = Idempotency-Key). The `action` hook renders a markdown transcript into `resources/inbox/Voice Conversations/` (ported from the original `transcript_io.py`). Optional **post-session processing**: `process_transcript` (or `capture_transcript {process:true}`) runs the agent's **configured prompt** (`~/.trinity/brain-orb/voice-postprocess.md` — configuring it is the opt-in) over the transcript via a **detached** `claude -p` (transcript piped on stdin — no shell string), writing a processed note. Mirrors `cornelius-internal/resources/agent-visualization/voice/`.
- **Write → graph refresh loop (#67 / #68).** Closes the loop so captured notes / links actually appear on the orb. `POST /brain-orb/refresh` (`OwnedAgentByName`, 200s, audited) → agent-server → the `action` hook's `refresh` verb reindexes + re-exports `data.json` (folds inbox notes + `_links.md` edges into the graph) → `orb.js` `refreshGraph()` refetches `/data` and rebuilds in place (same machinery as `setScope`). Auto-triggered after capture/link (voice writes debounced), plus a visible **"↻ integrate & refresh"** control + "integrating…" state + a "graph updated · +N notes, +M links" confirmation toast. The refresh **preserves the current selection** across the rebuild (snapshots the inspected node / highlighted group by ID, re-selects after) and **skips the rebuild entirely when nothing changed** so a no-op refresh never disturbs the selection (#72). After a capture it **auto-selects the newly-created note** once integrated (a `_pendingFocusTitle` the refresh consumes) — "add a node" focuses it, no separate "select it" step.
- **Brain tab settings — post-voice-processing config (#73).** The Brain tab (`BrainPanel.vue`) is no longer a bare launch button: it carries a **"Post-voice processing"** section — an **on/off toggle** ("run a processing step after each voice session") + a **prompt editor**. Config is **agent-owned** (`~/.trinity/brain-orb/voice-postprocess.json` = `{enabled, prompt}`, legacy `.md` read-fallback); Trinity brokers it via `GET/PUT /api/agents/{name}/brain-orb/postprocess` (`OwnedAgentByName`, write-flag-gated, PUT audited) → agent-server direct file I/O. The `action` hook's `_spawn_postprocess` gates on `enabled` — off keeps the saved prompt but skips the `claude -p` (default OFF). Shown only when `brain_orb_write_available` + agent running.
- **Navigate/focus framing + link contract (#70 / #71).** `navigate_to_note`/`focusNode` frame the node **with its neighbours** (so a just-made link's other endpoint is on-screen) and derive the zoom distance from the selection's radius + angular spread (single note zooms in, cluster zooms out) — `frameNodes()`, replacing the old fixed 195/250. On a navigate-miss with an integration pending, it flushes the refresh and retries so a just-created note is navigable. The `action` hook's `link` verb **validates both endpoints exist** and returns a clear "note not found" error otherwise — no silent dangling references (agent-owned per Invariant #8; the orb surfaces the error).

**Still out of scope:** `run_skill` (arbitrary allow-listed headless exec from the orb) — the full exec surface with a template.yaml allow-list ceiling + #1083 detached-execution integration remains unbuilt; open a fresh issue if it's ever wanted.

## Phase 2 — live scope control

The orb's scope panel (the `S` key, un-hidden in Phase 2) lists the agent's vault scopes; toggling one mounts/unmounts it, the agent re-exports at the new scope (rewriting `data.json`), and the orb re-fetches `/data` and rebuilds **in place** (no reload). The old localhost voice proxy's per-start `X-Orb-Token` is replaced by the platform JWT (carried on the brokered fetches) + an owner gate on the mutation.

```
orb scope toggle → setScope(tokens)
  POST /api/agents/{name}/brain-orb/scope   (OwnedAgentByName; Bearer; body {tokens|mount|unmount})
    → agent-server POST /api/brain-orb/scope
        → runs ~/.trinity/brain-orb/scope  (stdin = body)  → mutate active set + re-export → rewrite data.json
        → stdout JSON {ok, active, nodes, edges}
  ← orb re-fetches GET .../brain-orb/data → applyData() rebuilds the graph in place

orb panel open → loadScopes()
  GET /api/agents/{name}/brain-orb/scopes   (AuthorizedAgentByName; Bearer)
    → agent-server GET /api/brain-orb/scopes → runs ~/.trinity/brain-orb/scopes → {active, available}
```

**Agent convention (Invariant #8 — agent owns scope state + generation):** the agent ships two executable hooks under `~/.trinity/brain-orb/` (mirrors `~/.trinity/pre-check`, #454) — `scopes` (prints `{active, available}` JSON) and `scope` (reads a `{tokens|mount|unmount}` JSON request on stdin, mutates the active set, re-runs its exporter, prints `{ok, active, nodes, edges}`). Trinity stays generic: the agent-server runs the hooks via **hardened async subprocess** (timeout-kill, 4 MB output cap, JSON-parse + non-zero-exit guards), and **404s when a hook is absent** (the agent doesn't support scope control → the orb's scope panel shows a degraded hint). The real Cornelius agent adopts the convention in its own repo — this PR ships the generic surface (like `pre-check`, `data_paths`, pipelines).

**Auth split:** `GET /scopes` is `AuthorizedAgentByName` (read); **`POST /scope` is the only mutating brain-orb route and is `OwnedAgentByName`** (owner/admin) — body-capped at 64 KB, with a 200s backend timeout sitting just above the agent hook's 180s so a slow re-export surfaces as the agent's 504, not a premature one.

## Phase 3 — client-held Gemini Live voice tile + read-only KB search (#60)

The orb's voice tile (`#voiceTile`, un-hidden when voice is available) holds its **own** Gemini Live session **client-side**: the browser connects DIRECTLY to `wss://generativelanguage.googleapis.com` and streams mic/playback inside the same-origin iframe. Trinity never proxies the audio. This is a deliberate design choice (distinct from Trinity's backend-proxied workspace voice, VOICE-001) — it keeps the voice→tool→orb loop entirely in the browser.

```
voice tile Start → (voice iframe) requests a token from the parent orb page
  orb.js POST /api/agents/{name}/brain-orb/voice-token   (AuthorizedAgentByName; Bearer JWT; rate-limited)
    → brain_orb_voice_service.mint_voice_token()
        v1alpha genai.Client.auth_tokens.create(CreateAuthTokenConfig(
            uses=1, new_session_expire_time≈60s, expire_time=VOICE_MAX_DURATION,
            live_connect_constraints=LiveConnectConstraints(model=VOICE_MODEL, config=<LOCKED: prompt+voice+tools>)))
    ← {ephemeral_token, model, voice_name, expires_at, tools:[…]}   (field is NOT `token`)
  orb.js relays {token,model,…} to the voice iframe over postMessage  (JWT stays in orb.js)
  voice iframe → wss://…/v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=<token>
                 setup = {model}    (config is locked in the token)

Gemini toolCall → voice iframe → postMessage 'orb-tool' → orb.js ORB_TOOLS[name]() runs in-browser
                → 'orb-tool-result' → voice iframe → tool_response back to Gemini   (no server hop)
  · visual tools (highlight/navigate/list/…)  — pure Three.js, in-browser
  · mount_scope/unmount_scope                  — reuse the Phase-2 POST /scope broker
  · navigate_to_note vault fallback            — POST /brain-orb/tool → agent ~/.trinity/brain-orb/search
```

**Why client-held is safe without proxying audio:** the security envelope is the ephemeral token's own constraints, minted server-side — model-locked, whole-config-locked (so the browser can't widen the tool surface), single new-session use, and a short expiry. Trinity's only job is the JWT gate on the mint + a per-(user,agent) rate limit. No Redis ticket/intent dance (that pattern authenticates a socket back to *Trinity's* `/ws`; here the browser talks to Google).

**The JWT relay (three documents deep):** the voice tile is a nested iframe (host `AgentBrainOrb.vue` → `/brain-orb/index.html` → `voice/orb.html`) and must never hold the platform JWT. So the **orb page** (which received the JWT via the Phase-1 postMessage handshake) mints the token and relays only the short-lived Google token down to the voice iframe. Reconnect (the token is `uses=1`) re-requests a fresh token through the same relay.

**Writes stay off by construction (KB writes are a later child):** (1) the locked tool manifest declares only read/visual/scope tools — the browser cannot add `capture_note`/`find_connections`/`synthesize_insights`; (2) there is no `/session` route, so orb.js's `initActions()` keeps `ACTIONS.enabled=false` and the write panel stays hidden; (3) the voice-token field is `ephemeral_token`, never `token` (a `token` would flip `ACTIONS.enabled` on). Regression-locked by `tests/unit/test_brain_orb.py`.

**Read-only KB search:** `POST /api/agents/{name}/brain-orb/tool` (`AuthorizedAgentByName` — read) proxies to the agent-server `POST /api/brain-orb/tool`, which runs the agent's `~/.trinity/brain-orb/search` hook (a third convention sibling next to `scopes`/`scope`) via the same hardened `_run_hook` runner. Scope-aware and read-only by hook contract (fails closed to the core scope when none is mounted); 404 when the agent ships no `search` hook (the orb degrades to in-graph search).

**CSP / assets (no nginx change):** `connect-src` already allows bare `wss:`, so the direct browser→Gemini socket needs no CSP edit. `script-src 'self'` means the Gemini client is **hand-rolled** (no vendored SDK), the voice logic is externalized to `voice/voice.js`, and the mic AudioWorklet ships as a same-origin `voice/mic-worklet.js` (a `blob:` worklet would be blocked). The standalone Cornelius page's **hardcoded API key is stripped**; its **p5.js audio-reactive voice orb is retained but vendored locally** (`voice/vendor/p5.min.js`, no `eval`/`Function` → CSP-clean) instead of CDN-loaded — the output audio is routed through an `AnalyserNode` so the orb pulses with Cornelius's speech. The host iframe carries `allow="microphone"` so the nested `getUserMedia` resolves.

**Voice orb animation:** the voice tile renders its own audio-reactive orb (a p5 smoke/core sketch) that breathes when idle and pulses with the spoken audio (`enqueueAudio` connects each output buffer through `outAnalyser`). It was briefly cut with the CDN p5 removal and then restored by vendoring p5 locally — a separate visualization from the main three.js knowledge orb.

**Voice gating:** the platform flag `brain_orb_voice_available` = `base && voice && GEMINI_API_KEY` (default OFF; flags runtime-resolved since #85, see Gating) — **distinct** from the static `brain_orb_available` (which has no Gemini dependency). The orb un-hides the voice tile only when the host passes `voiceAvailable:true` in the init handshake (platform flag) AND the agent is brain-orb-capable. The mint route is independently gated on `base && voice` (404 when either is off, 503 when no key), so the flag is UI-only and can't be bypassed.

**SDK bump (required — the pinned SDK lacked ephemeral tokens):** `google-genai==1.12.1` has **no `auth_tokens` attribute**, so the mint 502'd on the shipped backend; the pin is bumped to **`1.63.0`** (`docker/backend/Dockerfile`). Verified in the local stack: `POST /voice-token` returns a real `auth_tokens/…` token with the model+voice locked and the 11 read/visual/scope tools (zero write tools, no bare `token` field), and `services/gemini_voice.py` (the existing backend-proxied VOICE-001 path) still imports cleanly under 1.63. **Pre-merge:** a live VOICE-001 smoke test (an actual browser voice session) confirming the bump didn't regress `aio.live.connect`.

**Residual /verify-only risk (documented):** the exact ephemeral-token **browser** handshake (`BidiGenerateContentConstrained` + `access_token=` + v1alpha) is only verifiable against the live Gemini API from a real browser — mirror the SDK's `live.py` Constrained path (setup = `{model}` only, config deleted). Non-blocking function calling for the 180s scope re-export (now available on 1.63) should be confirmed in the same spike. A real agent must ship an executable `~/.trinity/brain-orb/search` hook.

## Phase 4a — owner-gated KB-write actions (capture + link, #61)

The orb's action panel (`#actions`, the `A` key) and the inspector "connect" affordance — hidden since Phase 1 — are un-hidden and rewired from the dead standalone voice-proxy (`VOICE_PROXY+'/session'|'/action'` + `X-Orb-Token`) to the platform broker with Bearer JWT. Two write verbs: **capture** (a note into the agent's inbox) and **link** (`[[wikilink]]` two notes). `run_skill` (exec) + transcript capture are Phase 4b (#66) — deliberately out.

```
orb action (capture / link, or a voice capture_note/link_notes tool call)
  orb.js postAction(type,args)
    POST /api/agents/{name}/brain-orb/action   (OwnedAgentByName; Bearer; Idempotency-Key: <uuid>)
      body {action:"capture"|"link", ...}
    → routers/agent_brain_orb.py:
        _require_write_enabled()            # BRAIN_ORB_WRITE_ENABLED (distinct kill-switch)
        enum-validate action ∈ {capture, link}   # run_skill/capture_transcript → 400 (never forwarded)
        idempotency begin(scope=agent:{name}, key=action:{Idempotency-Key})   # replay → snapshot / 409 in-flight
        rate_limiter.enforce(user,agent,action)  # 429 → release the claim
        _agent_request → agent-server POST /api/brain-orb/action
            → _run_hook(~/.trinity/brain-orb/action, stdin=body)   # agent owns the write (Inv #8)
        idempotency complete(snapshot)  ·  platform_audit_service.log(brain_orb_capture|link)
  ← orb reflects the write (addLiveNote / addLiveEdge); joins the graph at the next reindex

orb panel open → initActions()
  GET /api/agents/{name}/brain-orb/actions   (OwnedAgentByName; Bearer)
    → agent-server GET /api/brain-orb/actions → _run_hook(action, {action:"list"}) → {enabled, skills}
  200 ⇒ body.brain-orb-write added → panel revealed; 403 (non-owner) / 404 (flag off / no hook) ⇒ stays hidden
```

**Auth boundary (airtight):** every write route is `OwnedAgentByName` (owner/admin) — a shared user gets 403 and the orb hides the panel. The voice-token mint stays `AuthorizedAgentByName` (shared users may voice-chat), but the mint route computes `can_write = is_brain_orb_write_enabled() and db.can_user_share_agent(user, agent)` and only then folds the `capture_note`/`link_notes` write tools into the **locked** manifest — so a shared-user session gets the read-only Phase-3 manifest, and even a crafted call hits the owner-gated `/action` route. The manifest is a UX affordance; the backend gate is the security boundary.

**Idempotency (Invariant #18, not #1084):** these are user→backend producer POSTs with no `execution_id`, so the effect_guard (`effect:{execution_id}`-scoped) doesn't fit; the trigger-boundary `Idempotency-Key` path is used instead, with the key folded per action verb (`brain_orb_action:{action}:{key}`) so a reused client key can't replay a different verb's snapshot. Dedups the HTTP POST (double-click / retry) — exactly-once at the KB sink remains the agent hook's responsibility.

**Agent convention (Invariant #8):** the agent ships one executable `~/.trinity/brain-orb/action` hook (a fourth sibling next to `scopes`/`scope`/`search`) that dispatches on the request's `action` field (`list` → allow-list, `capture`, `link`) and owns the write semantics; Trinity runs it via the same hardened `_run_hook` and 404s when absent. The backend enum-restricts the verb before forwarding.

**Kill-switch:** the write flag (runtime-resolved since #85: `brain_orb_write_enabled` setting → `BRAIN_ORB_WRITE_ENABLED` env opt-in → OFF — flippable from Settings → General without a restart) — distinct from the base flag so writes can be disabled without downing the read path or voice tile. Surfaced as `brain_orb_write_available` (`base && write`) in `GET /api/settings/feature-flags`; the host relays it to the orb (`writeAvailable`), which only attempts `initActions` when on.

## Phase 4b — voice-transcript capture + post-session processing (#66)

Mirrors the original `cornelius-internal/resources/agent-visualization/voice/` (proxy_server.py + transcript_io.py): the **client** captures per-turn transcription and the **agent** renders + saves it. Trinity brokers.

```
mint: brain_orb_voice_service adds input_audio_transcription + output_audio_transcription
      to the LOCKED LiveConnectConfig → the constrained ephemeral token returns transcription

voice.js (nested iframe, no JWT):
  handleServerMessage → buffer content.inputTranscription / outputTranscription per turn
    → on turnComplete: push {event:user_turn|model_turn, text}; tool calls → {event:tool_call}
  endConversation()  ← the correct flush seam (onclose early-returns on wsClosedByUs)
    → flushTranscript(): append {event:session_end} → postMessage 'orb-capture-transcript'
                         {session_id, events} to the parent orb

orb.js (holds the JWT):
  on 'orb-capture-transcript' (owner + ACTIONS.enabled) →
    postAction('capture_transcript', {session_id, events, process:true}, idemKey=session_id)
    POST /api/agents/{name}/brain-orb/action  (OwnedAgentByName; Idempotency-Key=session_id)

backend → agent-server → ~/.trinity/brain-orb/action hook:
  capture_transcript → render markdown (ported transcript_io) → resources/inbox/Voice Conversations/Voice - <ts>.md
  process (opt-in): if ~/.trinity/brain-orb/voice-postprocess.md exists → detached `claude -p <prompt>`
                    with the transcript piped on STDIN (no shell string) → "<name> - processed.md"
```

- **Transcription is proven on the constrained path**: `auth_tokens.create` accepts the transcription config, and the mint returns a real token. (Full live-audio confirmation that Gemini *streams* transcription events is a manual voice-session check — the config-acceptance + save path are automated.)
- **Idempotency by session**: the session id is the `Idempotency-Key`, so a double `session_end`/`onclose` saves exactly one transcript.
- **Post-session processing = agent-configured prompt** (`voice-postprocess.md`) — configuring it is the opt-in; absent ⇒ a no-op. Runs **detached** so the session-end call returns promptly; transcript on stdin (never a shell arg) closes command injection; owner-only + owner-authored prompt + owner's-own-transcript keeps the injection surface minimal. Body cap raised to 1 MiB (backend + agent-server) for whole conversations.
- **Owner-only**: transcripts save only when the caller owns the agent (`ACTIONS.enabled` + `OwnedAgentByName`) — a shared user's session isn't persisted (no cross-user KB write).

## Why first-party + iframe (the CSP nuance, #979)

The orb is a vanilla Three.js page with an inline ES-module, CDN deps, and a `localhost:8770` voice proxy. Prod CSP is `script-src 'self'; font-src 'self'; frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN`. #979 only bit because it iframed **agent-origin** content with **inline** scripts. The resolution:

- Ship the orb as **first-party static assets** under `src/frontend/public/brain-orb/` (served from the frontend origin) → same-origin, external scripts → CSP-clean with **no nginx change**.
- Host it in a thin Vue view via a **same-origin iframe** — first-party, not agent-origin, so it does not trip #979.

Mechanical edits only (AC #1): externalize the inline module to `orb.js`; vendor `three`/`marked`/`DOMPurify`/JetBrains-Mono locally (drop the importmap + Google-Fonts link); repoint the data fetch at the backend proxy; neutralize the deferred voice-proxy base (`VOICE_PROXY=''`); hide the voice/scope/action panels via `orb-trinity.css`. DOMPurify sanitizes rendered note bodies (H-005).

## End-to-end flow

```
AgentDetail.vue  (visibleTabs: Brain tab when brainOrbAvailable && capabilities⊇'brain-orb')
   │ select tab → router.push
   ▼
/agents/:name/brain  (router beforeEnter: redirect unless brainOrbAvailable AND agent /info capabilities ⊇ 'brain-orb')
   ▼
AgentBrainOrb.vue  ── same-origin iframe ──>  /brain-orb/index.html  (first-party static page)
   │  postMessage handshake (origin-pinned):
   │    iframe → host:  {type:'brain-orb:ready'}
   │    host  → iframe: {type:'brain-orb:init', agentName, apiBase:'', authToken: <JWT>}
   │    iframe → host:  {type:'brain-orb:error'}  → host shows "hasn't rendered its mind yet"
   ▼ orb.js loadData()
GET /api/agents/{name}/brain-orb/data   (Authorization: Bearer <JWT>)
   ▼ routers/agent_brain_orb.py  — AuthorizedAgentByName (owner/shared); flag-gated
     agent_httpx_client(name) (#1159 per-agent token)  →  byte pass-through (no re-serialize)
   ▼
GET http://agent-{name}:8000/api/brain-orb/data   (X-Trinity-Agent-Token; auto-gated by #1159 middleware)
   ▼ agent_server/routers/brain_orb.py
FileResponse(~/resources/agent-visualization/data.json)   (agent owns generation — Invariant #8)
```

## Gating

`brainOrbAvailable = brain_orb_available (platform flag) && template.yaml.capabilities ⊇ 'brain-orb' (per-agent)`.

- **All three platform flags are runtime-resolved and admin-configurable (trinity-enterprise#85):**
  `system_settings` row (`brain_orb_enabled` / `brain_orb_voice_enabled` / `brain_orb_write_enabled`,
  wins in both directions) → `BRAIN_ORB_*` env var as **opt-in** fallback → default OFF. Resolution
  lives in `settings_service.is_brain_orb_*_enabled()` (one shared `_resolve_bool_flag` helper;
  fail-open on a settings-read failure so `feature-flags` never 500s; uncached — `--workers 2`,
  #506 rationale). Admin surface: `GET/PUT /api/settings/brain-orb` (per-flag `{value, source:
  override|env|default}`; `clear: [flag,…]` reverts a flag to env/default — once a stored row
  exists the env var is ignored until cleared) + a Settings → General panel. Route gates and
  `feature-flags` read the resolvers, so an admin flip needs **no restart**; open user sessions
  pick it up on the next page load (`feature-flags` is fetched at load, not WS-pushed; the
  admin's own session force-refetches after a save).
- Base flag → `brain_orb_available` in `GET /api/settings/feature-flags`. The static render has **no** Gemini dependency.
- Voice flag (Phase 3): `brain_orb_voice_available = base && voice && GEMINI_API_KEY` (the key stays env-only — secret; #85 added the base term so a downed orb never advertises its voice tile). Un-hides the voice tile only when on AND the agent is brain-orb-capable; the `voice-token` mint route gates on `base && voice` itself, so the flag is UI-only and can't be bypassed.
- Per-agent capability: a **generalizable** `brain-orb` token in the agent's `template.yaml capabilities` list (surfaced by `GET /api/agents/{name}/info`, read frontend-side) — never a hardcoded agent/template name. Mirrors the `sessionAvailable` + `hasDashboard` idioms.
- The **route guard AND the tab both enforce the per-agent capability** (#60): the guard (`router/index.js beforeEnter`) fetches `GET /api/agents/{name}/info` and redirects to AgentDetail unless the agent declares `brain-orb` (and the platform flag is on) — so the orb is **never launchable on a non-Cornelius agent, even via a raw URL** (a stopped/uncapable agent redirects, not an empty orb). Because the route is capability-gated, the voice tile inside it only needs the platform voice flag.

## Auth

The data route uses standard `AuthorizedAgentByName` Bearer auth like every other `/api/agents/{name}/*` route — no new ticket primitive. A `fetch()` from the same-origin iframe doesn't auto-carry the JWT, so the host hands it over via origin-pinned `postMessage` (`targetOrigin = window.location.origin`); the token never enters a URL. The agent-server route is auto-gated by the #1159 `X-Trinity-Agent-Token` middleware (only `/health` is exempt).

## Files

| Layer | Path |
|-------|------|
| Orb assets | `src/frontend/public/brain-orb/{index.html, orb.js, styles.css, orb-trinity.css, vendor/*}` |
| Voice tile (Phase 3) | `src/frontend/public/brain-orb/voice/{orb.html, voice.js, mic-worklet.js}` |
| Frontend host | `src/frontend/src/views/AgentBrainOrb.vue` (`allow="microphone"`, relays `voiceAvailable`) |
| Route | `src/frontend/src/router/index.js` (`/agents/:name/brain`) |
| Flag (FE) | `src/frontend/src/stores/sessions.js` (`brainOrbAvailable`, `brainOrbVoiceAvailable`) |
| Tab + capability | `src/frontend/src/views/AgentDetail.vue` (`visibleTabs`, `checkBrainOrbCapability`) |
| Backend proxy | `src/backend/routers/agent_brain_orb.py` (data/scopes/scope + voice-token/tool + **actions/action** — Phase 4a) |
| Mint service (Phase 3/4a) | `src/backend/services/brain_orb_voice_service.py` (v1alpha ephemeral token + locked tool manifest; **`can_write` → owner-only capture/link tools**) |
| Flag (BE) | `src/backend/services/settings_service.py` (`_resolve_bool_flag` + `is_brain_orb_*_enabled` + `BRAIN_ORB_FLAGS` registry — #85; `config.py` holds no constants anymore), `src/backend/routers/settings.py` (feature-flags + `GET/PUT /api/settings/brain-orb`), `src/backend/models.py` (`BrainOrbSettingsUpdate`), **`docker-compose.yml`** (each `BRAIN_ORB_*` env var must be in the backend `environment:` block for the env-fallback leg to reach the container) |
| Agent-server | `docker/base-image/agent_server/routers/brain_orb.py` (data/scopes/scope + search + **action** hook — Phase 4a) |
| Flag (FE) | `src/frontend/src/stores/sessions.js` (`brainOrbAvailable`, `brainOrbVoiceAvailable`, **`brainOrbWriteAvailable`**) |
| Admin panel (#85) | `src/frontend/src/views/Settings.vue` (General → Brain Orb panel), `src/frontend/src/stores/settings.js` (`get/setBrainOrbSettings`) |
| Tests | `tests/unit/test_brain_orb.py`, `tests/unit/test_85_brain_orb_settings.py` |

## Invariants honored

#5 agent-server mirror · #8 agent owns generation (Trinity only reads) · #4 route order (the 5-segment path never collides with the `/{name}` catch-all) · #15 agent-scoped nesting. No MCP tool (this is a UI page, not an agent-facing tool), no DB change, no migration, no new secret.

## Known limitations / follow-ups

- `data.json` is multi-MB and re-fetched per visit (`Cache-Control: no-store`); a future refresh/cache strategy can ride the same proxy.
- `/brain-orb/orb.js` is a non-hashed asset under nginx's 1y-immutable static cache — a future orb update needs a cache-bust (query param / rename). The orb is frozen (verbatim) for now.
- Visual + functional parity (AC) is verified at the asset level (vendored bundle renders the real `data.json`); full in-stack parity needs a real Cornelius agent.
- Scope `POST /scope` is owner-gated (which bounds abuse) but not yet rate-limited; a per-agent rate limit on the re-export trigger is a cheap hardening follow-up. The agent-side `scope` hook is responsible for serializing concurrent re-exports (the Cornelius proxy uses an RLock).
