# Feature: Workspace voice mode — the orb takes the conversation

> **Status**: ✅ Implemented (2026-09-07)
> **Issue**: abilityai/trinity-enterprise#534 (supersedes ent#440)
> **Requirement**: `docs/memory/requirements/runtimes.md` §29.10 · `docs/memory/requirements/public-access.md` §48.3
> **Related**: [voice-chat.md](voice-chat.md) (the real-time session itself — orb, tools, canvas verbs, session lifetime), [agent-canvas.md](agent-canvas.md) (the canvas the call draws on, and who reads it), [workspace-chat-tabs-and-titles.md](workspace-chat-tabs-and-titles.md) (the thread the call binds to), [voice-chat.md § Front doors](voice-chat.md) and `trinity#2559` (which retired the Agent Detail front door and turned it into a door into this one), ent#354 (a second realtime provider behind the same seam)

## Overview

The Workspace is where people work with agents; the real-time voice orb lived
only on Agent Detail. Ruled 2026-09-07: put the orb **inside the Workspace
conversation**, modal the way ChatGPT's voice mode is — you are either in the
chat or in the call. While the call is on the orb takes the conversation column,
the chat is visible but inert, and the agent's **canvas takes the right column**
so it can show materials while it talks. Ending the call returns you to the chat
exactly where it was, and the call's transcript is kept there.

**OSS-core by the standing ruling — deliberately ungated.** The Workspace and
everything around it is OSS (ent#356/#438/#440 record it); this consumes only
surfaces that already shipped there. Recorded explicitly because the default for
an enterprise-tracker feature is *gated unless ruled otherwise*.

**Provider-neutral by construction (ent#354).** Gemini Live is today's realtime
provider; gpt-realtime is an incubating alternative. Everything this feature
adds on the Workspace side — the start route, the persistence module, the frame
protocol the browser reads, the roster field, the pure rules — names no
provider. The provider-specific lifetime handling (compression, resumption,
`go_away`) lives inside `services/gemini_voice.py` behind the existing
`connect_and_stream` contract, so a twin can implement the same callbacks.

## Why the ent#440 loop went

ent#440 built a hands-free STT → typed-turn → TTS loop inline in the composer
and rejected porting the orb on three grounds. Two are moot under the rulings
since: "a parallel conversation" is exactly the modal model wanted here, and
"cannot authenticate a Workspace client" is only true for **external**
portal-token clients — the Workspace's audience is internal users (#78, ruled
2026-09-06), who hold the platform JWT. The third (a stateless `run_task` cannot
act as the agent) is answered by #535, which lands after this. One voice entry
point, so the loop, its pure module (`voiceConversation.js`) and its spec are
**deleted**. Hold-to-dictate (#2212) and spoken replies (#2157) stay — they are
input/output aids, not a mode.

## The shape

```
 Portal.vue (shell)
 ┌──────────────┬──────────────────────────────────┬──────────────────────────────┐
 │ PortalSidebar│ <main 40%>  PortalConversation   │  PortalVoiceCanvas (60%)     │
 │ (clicks     │   header: picker · title · [Voice]│  GET …/voice/{sid}/panel     │
 │  refused    │   tabs (inert)                    │  refetch on panel tool_result│
 │  while on)  │   status line: Listening · Esc    │  + 3 s safety poll           │
 │              │   ┌── relative ───────────────┐  │  → CanvasPanel (viewer=client)│
 │              │   │ thread   VoiceOverlay orb │  │                              │
 │              │   └───────────────────────────┘  │                              │
 │              │   composer (inert)               │                              │
 └──────────────┴──────────────────────────────────┴──────────────────────────────┘
          │ POST /api/enterprise/client-portal/agents/{name}/voice/start {portal_session_id}
          ▼
 client_portal/router.py::portal_voice_start   Depends(get_portal_principal)
   is_platform? ── no ──► 404 "Conversation not found"
   _require_roster(include_owned=True) ── miss ──► 404
   rate_limiter.enforce("portal_voice_start:{email}:{agent}", 10, 60)
   user = get_current_user(request, token)          (the WS ownership gate is by user id)
          ▼
 client_portal/voice.py::start_workspace_voice
   agent_on_roster ∧ get_portal_session(thread, agent, email) ── evaluated BEFORE branching → one 404
   realtime_voice_capability ── off ──► 503 with the reason
   prompt = voice_prompt_service.get_voice_system_prompt + thread context + WORKSPACE_PANEL_INSTRUCTIONS
   voice_service.create_session(chat_session_id=None, portal_session_id, client_email,
                                workspace_mode=True, canvas_audience="operator",
                                max_duration=WORKSPACE_VOICE_MAX_DURATION)
          ▼
 WS /ws/voice/{sid}?token=JWT  (routers/voice.py — shared with Agent Detail)
   JWT decoded BEFORE the session lookup (no 4004-vs-4001 oracle)
   on_turn → client_portal.voice.persist_voice_turn   (portal-bound sessions only)
   finally → persist_voice_call_end → {type:"saved"} → close
```

## The start route lives under the portal principal

Not an extension of the OSS `voice/start`. Both independent plan reviews landed
on this: a Workspace start that re-spelled `get_portal_principal`'s checks would
already have dropped one (the blocked-client check), and a module-level
`client_portal` import in `routers/voice.py` would drag SQLAlchemy into the
voice router's stubbed unit harness. Under the portal principal every current
and future Workspace gate is inherited. `/stop`, the WebSocket and `/panel`
stay the OSS ones. The agent's voice prompt resolver moved from the router into
`services/voice_prompt_service.py` so `client_portal` never imports a router.

**One uniform 404.** Off-roster, a thread that is not the caller's, and a
portal-token principal are indistinguishable: roster and thread ownership are
evaluated before either answers. A thread id is a 128-bit secret and "not
yours" vs "does not exist" must not be tellable apart (Invariant #8).

## Write-as-you-go, on the live worker

The Agent Detail path saves its transcript at the end of the call. Here that
shape was rejected twice over (both plan reviews):

- **Two uvicorn workers.** `useVoiceSession.stop()` used to send `end` on the
  socket AND `POST /stop`. `/stop` landing on the other worker reconstructs an
  **empty** session from Redis and would have written "Voice call · 0 min"
  before deleting the key; an in-process guard cannot see it.
- **A restart mid-call** loses everything in `session.transcript`.

So each completed turn is inserted the moment the provider reports it, by the
worker holding the live socket (`_record_turn` → `on_turn` →
`persist_voice_turn`): a row with `role`, `source='voice'` and
`voice_call_id=<voice session id>`, scrubbed through `runtime_secret_scrub`
(the ent#279 rule), stamped **strictly increasing** (`get_portal_messages`
orders by `created_at` alone and the two rows of one turn arrive in one tick).
The call closes with one `system` row — `Voice call · N min`, plus `· ended at
the 30-minute limit` / `· ended early: <reason>` — and
`touch_portal_session(added=n, title_if_empty=<first spoken line>)` (the
ent#457 pairing). **A call in which nothing was said writes nothing.** The
Workspace client sends `end` only on the socket (`restStop: false`); the Agent
Detail save-at-end path keeps its shape but is now idempotent (empty-skip,
in-process flag, Redis `SETNX voice_session:{sid}:saved` claim).

**The `saved` frame.** The bridge's `status: ended` frame is sent from
`connect_and_stream`'s `finally`, *before* the WebSocket handler writes. A client
that reloaded the thread on `ended` raced the DB. The handler now sends
`{type: "saved", messages_saved, duration_seconds, reason, message}` after the
rows exist and before `close()`; the composable stops the media on `ended`,
keeps the socket for `saved` (5 s timeout), and flips `active` only then — the
conversation reloads the thread on that falling edge.

## Grouped by call id, never by an opener row

`get_portal_messages` returns the newest 100 rows; a 30-minute call is ~180. A
grouping keyed on a header row falls apart exactly when the call was long
enough to matter, and a typed turn that lands mid-call would split the block.
So the key rides on **every** row (`voice_call_id`), and
`portalVoiceMode.js::groupVoiceBlocks` folds a thread into render items: typed
rows pass through with their original index (Retry needs it); every row with a
call id joins that call's block, placed where the call's first row sits; the
trailing `system` row is the label, and when the window cut it off the label is
derived from what is left ("Voice call · 7 spoken turns"), never an invented
duration. The block renders collapsed (`<details>`), visibly spoken, with no
rating control.

**Readers of the table** (the ent#428 lesson — a column that changes what a row
*is* must be met by every existing reader): `_format_history_context` labels
spoken rows `Client (voice):` / `You (voice):` and **budgets** them to the last
12 per call with an omission line; the resend dedup in `_persist_user_turn`
ignores spoken rows (typing the words you just said is a new message, not a
retry); `_title_plan`'s two-message window is bypassed by `title_if_empty` from
the first spoken line; `PortalHistoryMessage` carries `source` /
`voice_call_id`; ratings on spoken rows are accepted via the API but the block
renders no control.

## The session outlives the connection

Gemini Live ends an uncompressed audio session at ~15 minutes and recycles the
connection at ~10, announcing it with `go_away`. The 300 s Agent Detail cap hid
both; a 30-minute cap does not — without this, the receive loop would break on
the provider error and the whole "cap with words" path would never fire (the
strategy review's critical finding). Every session now requests
`context_window_compression` (sliding window) and `session_resumption`;
`_receive_audio_loop` stores each `session_resumption_update.new_handle` and
returns on `go_away`; `connect_and_stream` reconnects with the latest handle
(≤ `MAX_RECONNECTS_PER_SESSION`), the orb shows *connecting* during the swap,
and the watchdog, the browser socket and the transcript are session-scoped.
Types are read through `getattr` so a stubbed or older SDK still connects with
the provider's default lifetime rather than failing at connect time. Benefits
Agent Detail and VoIP too.

**The cap, with words.** `_timeout_watchdog` sleeps to T-30 s, sends the model
a text turn on the realtime channel (`send_realtime_input(text=…)`, not
`send_client_content`, which the SDK says not to interleave with realtime audio)
asking it to say the call is ending and wrap up in a sentence, then at the cap
sets `end_reason="cap"` **before** `end_session` (which cancels the watchdog's
own task). The `status` frame carries `reason` + a client-safe `message`; the
label row records it.

## Modal, with every failure in words

- **Voice control** (`data-testid="portal-voice-call"`) — **moved to the composer
  row by ent#547** (2026-09-07), left of attach, where ChatGPT puts it; it was in
  the header. Same control, same modal call, restyled to the composer's 44px box
  (#2259) because `portalComposerAlignment.spec.js` holds every button in that form
  to it.
  It sits **outside** the wrapper that carries `pointer-events-none` while a call
  is active, and that is the whole reason the move needed care: this is a TOGGLE —
  the same button starts and ends the call — so inside the inert region it would
  render in its pressed styling for the call's duration and refuse the click that
  ends it. A visibly-live control that does nothing is worse than none, and it is
  the dead affordance ent#547's own AC forbids, manufactured by ent#547's own move.
  Escape and the status line's "End call" remain as they were.
  The composer row is now voice-call · attach · dictate · send. The dictation mic
  (browser STT) is a **separate** control and stays: speaking into the field and
  holding a live call with the orb are different capabilities.
- **Voice control (original placement note)**: rendered for platform
  sessions only (`voiceEntryState`); disabled with the reason as its title when
  the instance cannot (`realtime_voice.reason`: voice turned off / no provider
  key). A portal-token client sees nothing — the socket needs a JWT they do not
  hold, and a disabled control explaining a limitation that is not theirs would
  be a dead affordance with a footnote.
- **The Talk door** (`?voice=1`, #2559): `/workspace?agent=<name>&voice=1` starts
  the call on landing, but the param alone is **never** the authority. The intent
  is armed IN THE APP — `AgentHeader`'s Talk button calls
  `portalVoiceMode.js::armVoiceAutoStart()` before `router.push` — and
  `voiceAutoStart()` honours it only when armed **and** an agent actually landed
  **and** the principal is a platform session. The armed flag is a module-scoped
  `let`, so it lives exactly as long as the document: a pasted, bookmarked or
  mailed link always arrives on a fresh document and is never honoured. That
  rules out the signed-out attack a browser activation heuristic cannot —
  `bootstrap()` does not run while signed out, so such a link would survive
  unconsumed until exactly the sign-in click that satisfies the heuristic.
  `bootstrap()` owns the strip: **once**, in the `finally`, keyed on the key's
  **presence** (so `?voice=0` is cleaned too), covering the `/workspace/c/:sid`
  early return, the no-`?agent=` fall-through and a throw. Then, after a
  `nextTick`, the shell calls the conversation's exposed `startVoiceCall()`
  through a template ref — the first consumer of that `defineExpose`. A
  `?voice=1` link clicked from *inside* the Workspace is a no-op: nothing watches
  `route.query`, exactly as with `?agent=`.
- **Pre-flight** before any request: insecure origin ("voice needs a secure
  page"), no microphone API, a turn in flight.
- **New chat**: the thread is created (`store.createSession`) and adopted
  BEFORE the call starts, so the transcript has a home before the first word.
  Since #2579 the adoption runs through the conversation's one `adoptSession()`
  seam rather than a hand-written emit — it also raises the `bornHere` flag
  that keeps the provisional "New chat" tab on screen across the round trip,
  and this was one of the three sites that would otherwise have been missed
  (starting a call from a fresh chat would drop the whole strip until the list
  refreshed).
- **While on**: `VoiceOverlay` over the thread region; New chat, the picker,
  star, Reset, the tabs (`PortalChatTabs :disabled`), attach, mic, textarea and
  Send inert; the speaker toggle hidden; one status line
  (`portal-voice-line`) reads the orb state or the tool at work, plus "End the
  call to switch chats · Esc ends". **Escape** ends the call before the
  turn-cancel rule runs. The shell refuses `openThread`, `newChatWithAgent` and
  ⌘J while `voiceCall.active`; a route-driven change (browser back) ends the
  call gracefully inside the conversation instead; unmount ends it too — the
  bridge keeps the transcript in every case.
- **Ended other than by End**: the status line shows the server's words or the
  reason's sentence (`endedNotice`) with Dismiss; a failed start lands in the
  existing `voiceError` line above the composer with the server's `detail`
  (`startFailureReason`). The chat is unblocked the instant `active` falls.

## The canvas column

`PortalVoiceCanvas.vue` replaces the rail (and Agent details) for the call's
duration — two flex **shares** of a zero basis, `sm:flex-[3_1_0%]` against
`<main>`'s `sm:flex-[2_1_0%]`, never `w-[60%]` / `w-[40%]`: those summed to
100% + the 18rem sidebar and the shell's `overflow-hidden` clipped the canvas
column off the right edge (#2581, measured at 296px on a 1280px viewport by
#2583). Below `sm` the orb has the
stage and the canvas stays behind the strip's Canvas tab (mobile is
trinity#710). It reads `GET /api/agents/{name}/voice/{sid}/panel` — the agent's
`main` canvas whatever its audience — through `CanvasPanel` (one rendering
layer), refetching when the bridge reports a panel verb finished
(`panelVersion` bumps on `tool_result` frames whose tool is one of the six) plus
a 3 s safety poll for boards rewritten some other way. The 300 ms poll of the
retired page was ~6,000 reads per 30-minute call for a surface the socket
already narrates. After the call the rail's Canvas tab shows the same board:
a **platform principal reads every audience in the Workspace**
(`agent_page.canvas_audience_for`), which is why the session writes at
`operator` like Agent Detail — `roster` would only add exposure. See
[agent-canvas.md](agent-canvas.md) → Audience.

### The swap is one motion, and the orb stays round (#2640)

Starting a call used to snap: `<main>` toggled its share, the rail was swapped
for the canvas in the same frame, and End call jumped back. The orb was then
drawn as an ellipse.

**The layout.** `flex-grow` is a `<number>` and therefore animatable, so the
share transitions rather than toggling — 300 ms ease-out on `<main>`, and the
canvas column ramps its own grow `0 → 3` over the same curve through Vue
enter/leave classes (a newly inserted element has no value to transition
*from*, so the ramp cannot be a class toggle). Opacity rides the same
transition, so the canvas's content is not re-wrapping in view while the column
is still moving — the layout-stability rule in `design-system-contract.md`.
Instant under `prefers-reduced-motion` — and that needs **two** classes on every
transitioning element, not one. `motion-reduce:transition-none` emits only
`transition-property: none`; the `duration-300` beside it still applies, so
`transitionDuration` stays `.3s`, and that is the exact property Vue's
`getTransitionInfo` reads to decide how long to keep a leaving element alive.
With `transition-none` alone nothing animates but `@after-leave` still resolves
on a 300 ms fallback timer — a reduced-motion user saw the canvas vanish, an
empty right column, then the rail appear. `motion-reduce:duration-0` drives that
timeout to 0. Caught in review on #2640; the class-counting test passed because
it asserted the class string rather than the behaviour, so it now counts both.

Two consequences worth writing down:

* **The `v-if` / `v-else-if` chain is gone.** A `<Transition>` wrapper breaks
  the adjacency a chain needs, so the exclusivity the chain used to guarantee by
  construction is now a named computed both arms read
  (`voiceCanvasHasColumn`). Deriving one from the other is what keeps them from
  drifting into both claiming the column.
* **The rail waits for the canvas to finish leaving** (`voiceCanvasLeaving`,
  set by the transition's own `before-leave` / `after-leave`). Vue keeps a
  leaving element in the DOM for the duration of its transition; without the
  gate the rail would mount at its full fixed width beside a canvas that is
  still shrinking — three columns in a row sized for two, `<main>` squeezed by
  flex for 300 ms, a worse jump than the one being fixed.

**The rail column is a width, not a step (#2676).** Two of the three columns
interpolated at first and the rail did not: it was a bare `v-if` on a `shrink-0`
flex sibling whose `<aside>` carries no width transition, so on call end it
appeared at full size in one frame — 48 px collapsed, 384 px open, or whatever
`--ws-rail` had been dragged to, which on a wide rail is a **larger** step than
the 211 px snap this work set out to remove.

The width now lives on a **wrapper this view owns**, not on `PortalRail`'s own
`<aside>` — the same shape the sidebar column three columns to the left already
has (`shrink-0 overflow-hidden` plus an explicit `--ws-` width). `--ws-rail` is
the *rendered* width (48 px collapsed, the dragged width open), so one binding
covers both states and agrees with the inner aside at rest.

It ramps through Vue enter/leave classes rather than an always-on
`transition-[width]`, and that is the load-bearing part: the same variable is
rewritten on every `pointermove` of a rail drag, so a permanently-transitioned
width would make dragging the rail rubber-band by 300 ms. Vue adds the active
class only for the enter/leave window and removes it afterwards, so a drag stays
instant. `!w-0` is `!`-marked for the reason the canvas's `!grow-0` is — both are
single-class selectors setting the same property, so without it Tailwind's output
order would decide the winner.

Measured in a headless Chromium against the real built stylesheet rather than
argued from the classes: mid-transition the column is at 322 px of its 384 px
(so it interpolates rather than stepping), it settles at 384 px, under
`prefers-reduced-motion` it is at 384 px immediately, and a drag issued after the
active classes are removed lands at its new width in the same frame.

**This retires `voiceCanvasLeaving`.** That flag held the rail out of the row for
the length of the canvas's leave transition, because a rail mounting at its full
width beside a still-shrinking canvas put three columns in a row sized for two. A
rail entering from zero width is complementary to a canvas leaving towards zero
grow — the row's total is conserved at every frame — so the hazard is gone by
construction rather than held off by a flag, and the two motions now overlap
instead of running back to back. The reduced-motion reasoning the flag's comment
carried is not lost: it belongs to the transition classes themselves and is
pinned by the class-counting test, which requires both `motion-reduce` classes on
every transitioning element, including the two added here.

**One accepted discontinuity.** `thirdColumnResizable` is gated on
`!voiceCall.active`, so the 8 px resize handle still appears and disappears in
one frame at the ends of a call. It is 8 px against a column that moves 384, and
giving the seam its own transition would mean a second animated element whose
only content is a 1 px line.

**The orb.** `VoiceOverlay.vue::resizeCanvas` sized the canvas bitmap **once**,
from the `watch(canvasEl)` that fires on mount — no `ResizeObserver`, no window
listener, no per-frame check — while the canvas is `absolute inset-0 w-full
h-full`. Any later change to the column's width therefore left CSS stretching a
stale bitmap, which is the squashed orb; the overlay also mounts in the same
tick the call re-lays out the columns, so the single measurement could capture
the pre-call width on its own. It now observes:

* a `ResizeObserver` catches the box moving under a stable window (the column
  swap, a rail drag, a flex reflow), which a window listener never sees;
* a `resize` listener catches a `devicePixelRatio` change — dragging the window
  to a different-density monitor resizes no box at all, so it fires no observer.

The bitmap is sized at `css × devicePixelRatio` (capped at 2 — a 3x display
would quadruple the fill cost of a full-column particle field for detail nobody
can see at this blur radius) and the render loop draws in **CSS pixels** via
`ctx.setTransform`, so the 45px core and the particles' fixed radii keep meaning
what they meant before DPR scaling existed. Resizing **re-scales, never
re-seeds**: the particle field lives in a fixed coordinate space around (0,0)
and is seeded once in `startLoop`, so the orb does not restart when the column
moves. A zero-sized box (hidden, or not yet laid out) is ignored rather than
throwing the last good size away, and a same-size measurement does not touch
the bitmap — assigning to `canvas.width` clears the canvas, so an observer
firing on a sub-pixel reflow would otherwise blank the orb continuously.

## Files

| File | Role |
|------|------|
| `src/backend/client_portal/voice.py` | `realtime_voice_capability`, `start_workspace_voice`, `build_portal_context_summary`, `persist_voice_turn`, `persist_voice_call_end`, `call_label` |
| `src/backend/client_portal/router.py` | `POST /agents/{name}/voice/start` under `get_portal_principal`; canvas routes pass `canvas_audience_for(principal.is_platform)` |
| `src/backend/client_portal/{models,db,service,agent_page}.py` | `PortalRealtimeVoice`, `PortalVoiceStart{Request,Response}`, `PortalHistoryMessage.source/voice_call_id`; `add_portal_message(source, voice_call_id)`, `get_portal_messages` selects both; `_format_history_context` labels + budgets, dedup ignores spoken rows, roster field; `canvas_audience_for` |
| `src/backend/services/voice_prompt_service.py` | the voice prompt resolver, lifted out of the router |
| `src/backend/services/gemini_voice.py` | `VoiceSession.portal_session_id/client_email/end_reason/end_message`, Redis metadata + reconstruction of every field, `_build_live_config` (compression + resumption), `go_away` reconnect loop, `_record_turn` → `on_turn`, the T-30 s cap notice, `claim_transcript_save` |
| `src/backend/routers/voice.py` | JWT before session lookup; `on_turn` → portal persistence (function-local import); `finally` → `persist_voice_call_end` or the claimed `_save_transcript`; `saved` frame; `status` frames carry `reason`/`message`; `/stop` never writes for a portal-bound session |
| `src/backend/db/{schema,tables,migrations}.py`, `migrations/versions/0057_portal_messages_voice_source.py` | `enterprise_portal_messages.source`, `.voice_call_id` (nullable), both tracks |
| `src/backend/config.py`, `docker-compose*.yml`, `.env.example` | `WORKSPACE_VOICE_MAX_DURATION` (1800) |
| `src/frontend/src/components/portal/portalVoiceMode.js` | the pure rules: `voiceEntryState`, `voicePreflight`, `voiceHeaderLine`, `endedNotice`, `startFailureReason`, `groupVoiceBlocks`, `voiceCallLabel*`, `VOICE_SPLIT`, `isPanelTool`, `canvasChanged` |
| `src/frontend/src/components/portal/PortalConversation.vue` | the Voice control, the modal state, the orb mount, the status line, the collapsed transcript block, Escape, the graceful stops; the ent#440 loop removed |
| `src/frontend/src/components/portal/PortalVoiceCanvas.vue` | the right column |
| `src/frontend/src/components/portal/PortalChatTabs.vue` | `disabled` prop |
| `src/frontend/src/views/Portal.vue` | `voiceCall` state, the 40/60 swap, navigation refused while on |
| `src/frontend/src/composables/useVoiceSession.js` | `startWith(requestFn, {restStop})`, `endReason`/`endMessage`, `panelVersion`, `awaitSaved`, the `saved` handshake, pre-flight |
| `src/frontend/src/stores/clientPortal.js` | `realtimeVoice` from the roster; `startWorkspaceVoice` |
| `tests/unit/test_ent534_workspace_voice.py` | the backend properties above (28 tests) |
| `src/frontend/tests/unit/portalVoiceMode.spec.js` | the pure rules + the source guards (42 tests) |

## Known limits

- The Workspace client cannot yet tell the model to *act as the agent*
  (`run_task` is the stateless task endpoint) — #535.
- Mobile keeps the orb full-stage with the canvas behind the strip (trinity#710).
- Rooms are out of scope — turn-taking with several agents is a different problem.
- The 30-minute default is trusted only after a live call past 15 minutes on a
  real key (the reconnect path is unit-tested, not soak-tested here).
- The JWT rides the WebSocket query string (pre-existing; debt registered).
