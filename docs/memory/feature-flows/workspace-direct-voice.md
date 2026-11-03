# Feature: Workspace Direct calls — every spoken turn goes to the agent

> **Status**: ✅ Implemented (2026-11-02)
> **Issue**: abilityai/trinity-enterprise#1046 (epic ent#78)
> **Requirement**: `docs/memory/requirements/public-access.md` §48.5
> **Related**: [workspace-voice-conversation.md](workspace-voice-conversation.md) (the Live call, unchanged), [voice-chat.md](voice-chat.md)

## Overview

A Live call (ent#534) puts a realtime voice model in front of the agent. A **Direct** call
has no model in the path: the person's words become an ordinary Workspace turn and the
agent's own reply is read aloud. Slower than Live, and in the agent's words. OSS-core,
ungated, like the rest of the Workspace.

## Flow

```
mic ─▶ endpointer (browser) ─▶ socket `utterance` ─▶ STT ─▶ `transcript_live`
                                                              │
browser send() ─▶ POST …/chat/stream {voice_call_id: vd_…} ◀──┘   (same route as typing)
        │
        └─▶ socket `speak{execution_id}` ─▶ bridge: stream (early) + persisted row (always)
                                              ─▶ sentences ─▶ TTS ─▶ `audio` ─▶ speaker
```

1. **Start.** `POST …/voice/start {mode:"direct"}` → `client_portal.voice.start_workspace_voice`:
   roster + thread (uniform 404), `direct_voice_capability` (503 with a reason), one call per
   thread across both modes (409), then `direct_voice_service.create_session` (`vd_` id, Redis
   + local copy). Returns `runtime`, `provider`, `early_speech`.
2. **Socket.** `/ws/voice/{vd_…}` (`routers/voice.py`): the shared token → revoke → user checks,
   then **owner only**, then `claim_call` takes the thread atomically (a second socket gets
   `busy`). `run_bridge` holds the call until End, a dropped socket, or the duration cap.
3. **Hearing.** `DirectBridge.hear`: size cap, strict base64, 30/min per person, provider STT,
   `transcript_live{seq,text,stt_ms}`. Silence is `not_heard{empty}`; an STT failure says
   "I didn't catch that." Nothing is started.
4. **The turn.** The browser holds an unfinished sentence up to 3 s
   (`portalDirectCall.looksUnfinished`), then calls `submitUserText(text, {spoken:true})` —
   the composer's draft is untouched. `deliver` adds `voiceCallId` for any turn during the call,
   typed or spoken. The route validates it with `turn_may_join_call`; `portal_chat` writes both
   rows with the id and adds `DIRECT_CALL_ETIQUETTE` to the system prompt.
5. **Speaking.** `speak{execution_id}` → `execution_belongs_to_caller` → `speak_execution`:
   `normalize_stream_entry` per runtime (subagent text dropped), tool status lines on silence,
   then `_await_turn_end` reads the persisted row and `unspoken_tail` speaks what the stream
   did not. A held turn's notice is not read; a failed turn gets "The agent couldn't answer
   that."; a reply with nothing speakable (only code, a table, links) gets "The reply is in the
   chat."; a cancelled one is silent. `SpokenBudget` closes a long reply with "The rest is in
   the chat." Spoken text is scrubbed of staged secrets.
6. **Barge-in.** Speech over playback pauses it; a real utterance sends `interrupt` (drop the
   queue, `audio_flush`) and the browser stops the running turn through the existing cancel
   route with `restore:false`.
7. **Approvals.** A new pending ask in the thread → `speak_ask` (read aloud: title and the
   person's own request). A whole-utterance yes/no (`classifyAnswer`) that starts ≥300 ms after
   the prompt's playback ended is posted by the browser via `store.answerAsk` — the person's
   route — then `ask_followup`. Anything longer is the next turn and the ask stays open. A card
   click gets the same follow-up. The approved run is spoken only if it is the caller's own.
8. **End.** `persist_direct_call_end` writes one `Direct call · N min` label row (none for a
   call with no turns); `saved` tells the browser to reload once any running turn has landed.
   The thread folds the call's rows into one block (`groupVoiceBlocks`, `liveCallId` keeps them
   open during the call).

## Delta cursor (#2694)

A Direct reply is the agent's own session answering, so it **is** the cursor
(`get_platform_rows_since_last_reply`); otherwise every turn after a Direct call would be
re-sent the thread since the last typed reply. The call's label row is excluded from the delta.

## Settings

`GET/PUT /api/settings/direct-voice` (admin, audit-logged): `enabled` (default on; env
`DIRECT_VOICE_ENABLED=false` turns it off until an admin decides) and `provider`
(`auto` → Gemini then ElevenLabs, or a fixed choice that never falls back). Models:
`GEMINI_STT_MODEL`, `GEMINI_TTS_MODELS` (tried in order).

## Frames

In: `utterance{seq,data}`, `speak{execution_id}`, `speak_ask{ask_id}`,
`ask_followup{ask_id,outcome}`, `interrupt`, `end`.
Out: `status`, `transcript_live`, `not_heard`, `speak_state`, `activity`, `spoken{text,ask}`,
`audio`, `audio_flush`, `notice`, `timing`, `saved`.

## Tests

`tests/unit/test_ent1046_direct_voice.py` (bridge, sessions, gates, start, socket door, chat
route, prompt, settings), `test_ent1046_direct_voice_db.py` (cursor on a real DB),
`test_ent1046_speech_providers.py` (providers over a mock transport);
`src/frontend/tests/unit/portalDirectCall.spec.js` (rules, endpointer, fold, protocol, the two call
buttons, the call bars, settings card), `portalDirectCall.mount.spec.js` (the real conversation with the socket, mic and
speaker faked).
