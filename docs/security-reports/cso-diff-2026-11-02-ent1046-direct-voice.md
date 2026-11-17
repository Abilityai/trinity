# /cso --diff — trinity-enterprise#1046 Workspace Direct calls (2026-11-02)

cso v1.2.3 · diff mode, daily gate (8/10) · branch `feature/1046-direct-voice-mode` vs `origin/dev`

## Scope

New: `services/direct_voice_service.py` (call session + audio bridge), `services/speech/` (Gemini and
ElevenLabs STT/TTS over REST), `GET/PUT /api/settings/direct-voice`, a `vd_` branch on the existing
`/ws/voice/{id}` door, `voice_call_id` accepted on `POST …/chat/stream`, `mode` on `…/voice/start`, the
`#2694` delta cursor, and the Workspace frontend for the call.

## Stack CVE quicklist

No dependency, image, workflow or compose-hardening change in the diff; compose gains three env pass-throughs
with defaults. Every row is unchanged from the last full run: not re-derived here (`coverage gap` for a
diff run, see below).

## Attack surface added

| Surface | Principal | Gate |
|---|---|---|
| `/ws/voice/vd_*` | platform JWT owner of the session | existing token → revoke → user checks, then `user.id == session.user_id`; **no admin attach** |
| `utterance` frame | socket owner | 1.4 MB base64 cap, strict base64 decode, 30/min per person (Redis limiter, fail-open) |
| `speak{execution_id}` | socket owner | `execution_belongs_to_caller` (agent + `source_user_email`) |
| `speak_ask`, `ask_followup` | socket owner | `_owned_ask` (uniform 404); an approved run is spoken only if it is the caller's own execution |
| `voice_call_id` on chat/stream | platform principal | `turn_may_join_call`: session exists, same agent, same email, same thread; Live `vs_` ids never pass; uniform 404 |
| `GET/PUT /api/settings/direct-voice` | admin | `assert_admin` (rejects agent and unknown scopes, #1890/#2323); audit-logged; Literal provider |

## Checks run

- A01 access control: each row above traced to its gate; mutation tests red when each gate is removed
  (stranger execution, other email/thread on the call id, admin attach, route without validation, settings
  read without `assert_admin`). Guard suites green: `test_ent435_settings_sink_guard`, `test_293_*`,
  `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_models_centralized`, `test_2996_*`,
  `test_1028_settings_package`.
- A04 / credentials: provider keys go in request headers (`x-goog-api-key`, `xi-api-key`), never the URL;
  `SpeechError` text carries status codes only, never response bodies (test with the key in the body);
  spoken text runs through `runtime_secret_scrub` (test). No new stored credential; settings rows hold a
  boolean and a provider name.
- A05 injection: no SQL interpolation (new query parameterised; `LIKE 'vd\_%' ESCAPE` on a constant); no
  subprocess; no `v-html`; no YAML.
- A09 logging: logs carry ids and timings only; no transcript or reply text is logged.
- A10 failure direction: limiter and call-marker reads fail open (stated); session lookup treats Redis as
  the authority and falls back to the local copy only when Redis is unreachable, and only for the session's
  owner; capability read fails closed to "unavailable" (stated).
- ASI01 / LLM01: the transcript is the person's own words in their own turn (exclusion 11). The etiquette
  suffix is a platform constant, added only for a validated `vd_` id.
- ASI09: the bridge never answers an ask. A spoken yes is posted by the browser on the person's own
  `POST /asks/{id}/answer`, whole-utterance only, after the prompt has played.
- LLM10: speech is bounded per person (30 utterances/min), per turn (1,200 spoken chars), and per call
  (`WORKSPACE_VOICE_MAX_DURATION`); call starts stay at 10/min per person and agent.
- Data flow disclosure: audio and transcripts go to the chosen provider; the UI menu and the Settings card
  state it.
- Secrets and hidden Unicode: no key-shaped strings, internal addresses or emails in the diff; no tag-block,
  zero-width or bidi characters in new files.

## Findings

None at or above the 8/10 gate.

Fixed during this pass (below the gate, noted for the record): the per-minute utterance limit was per socket,
so a person holding many calls multiplied provider spend. It is now per person (`direct_voice_utterance:{email}`),
with a test across two calls.

## Hypotheses

None.

## Coverage gaps

- Diff run: no Docker pass, no dependency scanner (the diff changes no dependency or image).
- No live end-to-end call against a running stack yet; reported separately with timings before the PR.
