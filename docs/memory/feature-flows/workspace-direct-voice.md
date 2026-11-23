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
4. **The turn.** A pause is not the end of a turn (#4456). Transcripts are collected as
   `{seq, text}` and sent as ONE turn (`submitUserText(text, {spoken:true})`, joined in spoken
   order by `portalDirectCall.joinHeld`, since each utterance is transcribed on its own and
   can come back out of order) once the person has been quiet for `GRACE_MS` (1.5 s), or
   `HOLD_UNFINISHED_MS` (3 s) when `looksUnfinished`. The quiet period is counted from the END
   of the last utterance, so transcription runs inside it. `useDirectVoiceSession.armHold` is
   the one place the send is scheduled:
   - nothing is sent while the person is speaking (the endpointer's onset cancels the pending
     send; `onUtterance` / `onDiscard` / mute end the speech) or while any transcript is still
     on its way; `HOLD_LOST_MS` (10 s) covers a transcript that never arrives;
   - noise (`onDiscard`) and `not_heard` resume the wait where it was, they do not restart it;
   - a yes/no that answers a waiting approval and a stop word are NOT delayed;
   - with words collected and nothing running, `heldWord` reads "never mind" / "cancel" /
     "stop" as taking them back (nothing sent, no "Stopped.") and "wait" / "hold on" /
     "one sec" as asking for a moment (the filler is not sent, the wait becomes the long one);
   - a continuation that arrives after the words were sent but BEFORE the agent answered —
     no `audio` frame and no tool `activity` yet — stops that turn and sends both halves again
     as one (`lastSent`; dropped on `speak_state idle`, on a stop, when the dispatch fails,
     and after 15 s if its turn never started). The chat then shows the first half twice:
     once as the cancelled message, once inside the merged one. After a tool has started the
     new words go alone, so an action is not run twice. Words parked in the conversation's
     `spokenQueue` behind a settling turn are joined there instead;
   - the status goes back to `listening` at an onset, so "Got it" is never shown while the
     person is talking;
   - ending the call sends what was collected (after the turn in front of it has settled,
     if there is one), except when it ends because the person left for another agent or
     thread (`stop({flush:false})`): those words go nowhere.

   `VAD.endMs` stays 850 ms: the quiet period covers a mid-sentence pause, and a longer
   endpoint would also delay yes/no and stop words. The cost is up to `GRACE_MS` before every
   spoken reply, less the time transcription takes anyway. The composer's draft is untouched. `deliver` adds `voiceCallId` for any turn during the call,
   typed or spoken. The route validates it with `turn_may_join_call`; `portal_chat` writes both
   rows with the id and adds `DIRECT_CALL_ETIQUETTE` to the system prompt.
5. **Speaking.** `speak{execution_id}` → `execution_belongs_to_caller` → `speak_execution`:
   `normalize_stream_entry` per runtime (subagent text dropped), tool status lines on silence,
   then `_await_turn_end` reads the persisted row and `unspoken_tail` speaks what the stream
   did not. A held turn's notice is not read; a failed turn gets "The agent couldn't answer
   that."; a reply with nothing speakable (only code, a table, links) gets "The reply is in the
   chat."; a cancelled one is silent. `SpokenBudget` closes a long reply with "The rest is in
   the chat." Staged secret values are scrubbed from each stream text block and from the
   saved reply before `sentences()` runs `speakable()`, which rewrites markup and would stop an
   exact match; what is spoken says `redacted`, and a stored `***REDACTED***` in the saved reply
   is said the same way. `_speak_sentence` scrubs each line again before TTS and the `spoken`
   frame ([runtime-secret-scrub.md](runtime-secret-scrub.md) → Speech sinks, #4359).
6. **Barge-in.** Speech over playback pauses it; a real utterance sends `interrupt` (drop the
   queue, `audio_flush`) and the browser stops the running turn through the existing cancel
   route with `restore:false`. Over playback the endpointer's bar is
   `max(idle, PLAY_MIN, ECHO_K × playbackLevel)`, where `playbackLevel` is the player's
   `getLevel()` (linear RMS of the output, peak-held 200 ms, 0 while paused); onset needs
   `startMsPlayback` (250 ms), and an utterance that got that far is sent however short.
6a. **Stopping (trinity-enterprise#1087).** `useDirectVoiceSession.interrupt({cancelTurn})` is
   the one stop: `player.stop()`, `interrupt` (then `audio` frames are dropped until
   `audio_flush`), `stopSeq = seq` (later transcripts at or below it are dropped), held text
   cleared, the ask window disarmed, `onBargeIn({executionId})` for the run being spoken when
   `cancelTurn`, `onStop` (the conversation clears `spokenQueue`), a "Stopped." notice. Callers:
   a whole-utterance stop phrase (`isStopUtterance`, checked after an approval answer and before
   echo unless it is the agent's own sentence word for word), the strip's Stop button, the
   composer's Stop (`cancelTurn` silences first, `{cancelTurn:false}`), and Escape
   (`turnCancel.directEscapeAction`: `silence` while the voice plays, else `end`; overlays own
   it first). `PortalConversation.stopCallTurn` cancels the composer's turn, or the bridge's
   `executionId` (an approved run) through the same `cancelPortalTurn`. Server side,
   `_speak_sentence` re-checks the generation before every `audio` send, so an interrupt that
   lands while the `spoken` send is in flight lets no chunk out after its flush.
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
speaker faked);
`tests/unit/test_ent1087_direct_call_interrupt.py` (no audio after a flush) and
`src/frontend/tests/unit/directCallInterrupt.spec.js` (stop words, Escape, endpointer over
playback, player level, stop protocol), plus the stopping block in the mount spec.
`tests/unit/test_4359_portal_tts_scrub.py` drives `speak_execution` with a staged value
`speakable()` would rewrite, one staged after the reply was saved, and a stored marker.
