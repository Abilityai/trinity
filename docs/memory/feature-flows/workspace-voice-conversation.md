# Feature: voice in the agent Workspace — one conversation, two modalities

> **Status**: ✅ Implemented (2026-08-24)
> **Issue**: abilityai/trinity-enterprise#440
> **Requirement**: `docs/memory/requirements/public-access.md` §48.3
> **Related**: [voice-chat.md](voice-chat.md) (VOICE-001 — the platform's Gemini Live session, and why it is deliberately NOT what runs here), `docs/memory/requirements/public-access.md` §48.2 (#2157 — narration), [workspace-absorbs-session.md](workspace-absorbs-session.md) (why this composer is the only one left), [workspace-composer-typeahead.md](workspace-composer-typeahead.md) (the other pure-module portal feature)

## Overview

The Workspace already had both halves of a voice conversation and neither of them
was one. A client could hold the mic button to dictate a message (#2212) and could
switch a speaker on to hear replies read aloud (#2157) — two manual controls,
driven turn by turn, with the client doing the switching between them.

One control now closes the loop: the mic listens, the utterance becomes an
**ordinary Workspace turn**, the reply is spoken, and the mic reopens. No mode
switch, no separate voice page, no second product.

**OSS-core by decision (ent#440): deliberately ungated** — no
`requires_entitlement`, logic stays in the OSS tree. Recorded explicitly because
CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled
otherwise*, so the ruling must never be inferred later from the mere fact that it
merged (the ent#326 / ent#384 / ent#392 discipline). The Workspace is OSS-core
(ent#356) and this consumes only surfaces that already shipped there.

## The one decision: what a voice turn IS

A spoken utterance is submitted through the **same path a typed one takes**:

```
mic ──► /stt ──► submitUserText(text) ──► deliver() ──► POST .../chat/stream
                                                              │
                        the same portal session ◄─────────────┤
                        the same resumed Claude session       │
                        the same enterprise_portal_messages   │
                                                              ▼
                              reply text ──► spokenReply() ──► /tts ──► speaker
```

Everything the acceptance criteria ask for follows from that line rather than from
machinery built to satisfy it:

| AC | Why it holds |
|----|--------------|
| Shares context with the text conversation | It IS the text conversation — same thread, same `cached_claude_session_id` |
| Voice turns appear in history like text turns | The row is the same row; nothing marks it as spoken |
| Permissions and exposure match the text surface | No new route, so no new gate — `/stt`, `/tts`, `/chat/stream` are already roster-scoped and rate-limited per (client, agent) |
| Canvas and deliverables reachable during a session | The loop is inline in the composer, never modal — files, reports and the agent page stay operable |

### Why not the platform's Gemini Live session

`routers/voice.py` (VOICE-001) already runs a real-time voice session, and porting
it into the Workspace was the obvious-looking move. It was rejected, and the
reason is the feature's whole premise:

- **A different model answers.** Gemini Live holds a *summarised copy* of the
  thread as a system prompt and writes its transcript back when the session ends.
  That is a parallel conversation wearing the same page — precisely what "one
  conversation, two modalities" is not.
- **A summary is not the session.** The agent's own tool memory, mid-skill state
  and reasoning state live in the resumed Claude session. A voice session that
  bypasses it cannot use the agent's tools, files or canvas — AC 4 would need
  rebuilding from scratch on the other side.
- **It cannot authenticate here.** Its WebSocket takes a platform JWT in a query
  param and resolves a `users` row. A Workspace client holds a portal session
  token and has no `users` row at all, so the surface would need a second auth
  path into an audio stream — new boundary, new blast radius, for a conversation
  we did not want anyway.

The issue's technical note asks to *consolidate rather than replace* the existing
voice work. Consolidating here means using the platform's STT and TTS layers
(shared with channels since ent#117) and letting the agent answer as itself.

## Where the rules live

`src/frontend/src/components/portal/voiceConversation.js` — pure, no Vue, no DOM.
`vitest.config.js` is `environment: 'node'` with no component-mount harness, so a
rule expressed inside a `.vue` file is a rule no test can execute. The component
is a dispatcher over the machine and nothing else decides a transition.

**Transition table** (`nextVoiceState`):

```
        start                utterance          transcript         reply
 off ──────────► listening ────────────► transcribing ──────► thinking ──────► speaking
                    ▲  ▲                      │                                   │
        silence ────┘  │  transcript-empty ───┘                    narration-ended │
                       └───────────────────────────────────────────────────────────┘
                       ▲                                    barge-in (stop narration + capture)
   stop / error  ──►  off   (always: release the microphone)
```

Properties the tests pin, each because the obvious implementation gets it wrong:

- **`start` is idempotent.** A second press while live must not open a second
  recorder on one device.
- **Every event is inert in `off`.** A `/stt` response or an `onstop` callback
  arriving after teardown must not restart a loop the user ended.
- **`transcript` sends from exactly one state.** A slow transcription answering
  after the state moved on cannot dispatch the same words twice.
- **Every exit releases.** `stop` and `error` from any state emit `release`, plus
  `stop-capture` / `stop-narration` for the state they left.

## Utterance boundaries, and the interrupt

There is no recognition here — just a level meter. An `AnalyserNode` on the shared
mic stream is sampled every 100 ms and reduced to RMS; `utteranceVerdict` turns
that into `continue` / `end` / `idle`:

- **`end`** — speech was heard and has now paused for `SILENCE_HOLD_MS` (1.2 s),
  or the utterance hit its 30 s cap. The clip is transcribed and sent.
- **`idle`** — the mic has been open 15 s having heard nothing. The conversation
  ends **with a sentence**; a hot mic on a client's own device is not left open.
- Nothing is uploaded when no speech was heard: room tone re-opens the mic instead
  of spending a transcription.

**Interruption (AC 5)** is the same meter read while the agent is speaking.
Sustained level above `BARGE_IN_RMS` for `BARGE_IN_HOLD_MS` stops playback and
reopens the mic. Two details are load-bearing rather than tuning:

- The interrupt threshold is **higher** than the listening threshold, and capture
  requests `echoCancellation`. Without both, the mic hears the agent's own
  narration through the speakers and the agent interrupts itself on every reply.
- The hold window means a cough does not cut the agent off mid-sentence.

## What gets spoken

`spokenReply()` rewrites a reply for the ear before synthesis: code fences become
one spoken sentence ("I've put the code in the chat"), inline code loses its
backticks, links read as their text and never their URL, markdown furniture is
dropped, and a long reply is cut at a sentence boundary with "The rest is in the
chat." Reading a fenced diff aloud is unlistenable, and it burns the TTS character
cap on characters no one can follow.

The rendered message is untouched — the chat shows the full reply, formatted, as
it always did.

## Degradation, with words (AC 6)

| Condition | What happens |
|-----------|--------------|
| No microphone / insecure origin | Control does not render; a click through a stale capability answers with the microphone reason |
| Platform has no STT provider | Control does not render (same rule as the mic button — no dead affordance) |
| Agent has no configured voice | The loop **runs**; replies arrive as text and the status line says so |
| Transcription fails / provider outage | Loop stops with `/stt`'s own user-facing message in the existing voice-error line |
| Synthesis fails mid-conversation | The narration message is named, and the loop advances to listening rather than hanging on audio that never plays |
| Turn fails | The failure renders on its own message bubble with a retry, and the loop stops rather than talking over an error the user needs to read |

The composer keeps working in every row of that table. Voice is an assist here,
never a mode that can trap the surface.

## Files

| File | Role |
|------|------|
| `src/frontend/src/components/portal/voiceConversation.js` | The pure module: availability, transition table, utterance/barge-in thresholds, spoken-text rewrite, state labels |
| `src/frontend/src/components/portal/PortalConversation.vue` | Dispatcher: the control, the shared mic stream + analyser, the recorder per utterance, and `submitUserText` — the extracted tail that typed and spoken turns now share |
| `src/frontend/tests/unit/portalVoiceConversation.spec.js` | The machine, the thresholds, the rewrite, plus source assertions that only source can answer (dispatch goes through the machine; a spoken turn uses the same submit path; no double narration) |

**No backend change, no new endpoint, no migration.**

## Known limits

- The ~350 ms of speech that establishes a barge-in is not captured (the recorder
  starts once the interrupt is confirmed), so an interruption's first word can be
  lost.
- Utterance boundaries are energy-based, so a very noisy room ends turns early.
  Recognition-based endpointing would need a streaming STT surface Trinity does
  not have.
- Narration is per-reply, not streamed: a long answer is spoken only once the turn
  completes. The turn's live activity trail (ent#286) still renders throughout.
- Rooms (`PortalRoom.vue`) are out of scope — turn-taking with several agents is a
  different problem from turn-taking with one.
