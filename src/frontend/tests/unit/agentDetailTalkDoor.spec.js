/**
 * trinity#2559 — the Agent Detail voice overlay is retired; Talk is a door.
 *
 * This is a source-guard spec, the project's pattern for wiring that only source
 * can answer: vitest runs `environment: 'node'` with no component-mount harness.
 * The rules themselves (the `?voice=1` one-shot) are pure functions and are
 * tested as functions in `portalVoiceMode.spec.js`; what is left here is
 * *absence* — that a removal stayed removed — plus the two things this PR added
 * to `AgentHeader` and the one line it must NOT have removed from the composable.
 *
 * The load-bearing case is the last one. `useVoiceSession` returned
 * `start, startWith, stop, …` as shorthand. Deleting `start()` and leaving the
 * shorthand throws `ReferenceError: start is not defined` at setup in EVERY
 * consumer — including the Workspace conversation, the surface this whole change
 * hands off to — and it ships green through the rest of the suite, because
 * `src/frontend` has no lint script and no eslint config on disk, Rollup does not
 * error on an unresolved shorthand, and no spec *imports* the composable.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

// Comments are stripped so a mention in the RATIONALE never satisfies — or
// trips — an assertion about the code: a comment explaining what NOT to write
// necessarily contains the offending string. The shared helper (#2161) is the
// one to use; do not re-copy it.
//
// **`ChatInput.vue` is the one file it cannot be used on unmodified.** It
// contains `accept="image/*"`, and the helper's JS block-comment pass reads that
// `/*` as an opener and swallows everything to the next `*/` — 74% of the file,
// including the `defineEmits` line this spec pins. Found by this spec failing on
// arrival, which is the good outcome; the bad one is the mirror image, where a
// swallowed file makes an *absence* assertion pass for the wrong reason. That is
// why every stripped source below is checked against a sentinel it must still
// contain: a future stripping change can then only fail loudly, never quietly.
const maskAcceptGlob = (src) => src.replace(/accept="[^"]*"/g, 'accept=""')

const SOURCES = {
  PANEL: ['../../src/components/ChatPanel.vue', 'const loadSessions = async'],
  INPUT: ['../../src/components/chat/ChatInput.vue', 'const emit = defineEmits('],
  COMPOSABLE: ['../../src/composables/useVoiceSession.js', 'async function startWith'],
  HEADER: ['../../src/components/AgentHeader.vue', 'function goToTalk()'],
  DETAIL: ['../../src/views/AgentDetail.vue', 'sessionsStore.loadFeatureFlags'],
  BUBBLE: ['../../src/components/chat/ChatBubble.vue', "source === 'voice'"],
  CONVERSATION: ['../../src/components/portal/PortalConversation.vue', 'defineExpose('],
}

const RAW = Object.fromEntries(Object.entries(SOURCES).map(([k, [rel]]) => [k, read(rel)]))
const PANEL = stripComments(RAW.PANEL)
const INPUT = stripComments(maskAcceptGlob(RAW.INPUT))
const COMPOSABLE = stripComments(RAW.COMPOSABLE)
const HEADER = stripComments(RAW.HEADER)
const DETAIL = stripComments(RAW.DETAIL)
const BUBBLE = stripComments(RAW.BUBBLE)
const CONVERSATION = stripComments(RAW.CONVERSATION)
const STRIPPED = { PANEL, INPUT, COMPOSABLE, HEADER, DETAIL, BUBBLE, CONVERSATION }
const HEADER_RAW = RAW.HEADER

describe('the guard can still see the code it guards', () => {
  it.each(Object.keys(SOURCES))('%s survives comment-stripping intact', (key) => {
    // Without this, a stripping bug turns every absence assertion in this file
    // into a vacuous pass — the failure mode that is invisible by construction.
    expect(STRIPPED[key]).toContain(SOURCES[key][1])
  })
})

describe('ChatPanel no longer runs a voice call', () => {
  it('mounts no orb and holds no session', () => {
    expect(PANEL).not.toContain('VoiceOverlay')
    expect(PANEL).not.toContain('useVoiceSession')
    expect(PANEL).not.toContain('voice.start(')
    expect(PANEL).not.toContain('endVoice')
  })

  it('no longer probes per-agent voice availability', () => {
    expect(PANEL).not.toContain('/voice/status')
    expect(PANEL).not.toContain('checkVoiceAvailability')
  })

  it('gives Escape back to the session menu alone', () => {
    // The overlay was the other claimant. `turnCancel.js` still owns the rule;
    // only this surface's declared list shrank.
    expect(PANEL).toContain('overlays: [showSessionDropdown.value],')
  })

  it('the composer is disabled by loading alone', () => {
    expect(PANEL).toContain(':disabled="loading"')
    expect(PANEL).not.toContain('voice.isActive.value')
  })

  it('STILL renders historic voice rows — the badge is their only reader', () => {
    // Removing the ability to create new `source: 'voice'` rows here must not
    // un-render the ones already in these chats.
    expect(PANEL).toContain("source: msg.source || 'text',")
    expect(BUBBLE).toContain("source === 'voice'")
  })
})

describe('ChatInput has no mic, and no props for one', () => {
  it('drops the button, both props and the emit', () => {
    expect(INPUT).not.toContain('voiceAvailable')
    expect(INPUT).not.toContain('voiceActive')
    expect(INPUT).not.toContain("$emit('voice')")
  })

  it('declares exactly the three emits it still has', () => {
    expect(INPUT).toContain("defineEmits(['update:modelValue', 'submit', 'cancel'])")
  })
})

describe('AgentHeader offers the door', () => {
  const talk = HEADER_RAW.slice(
    HEADER_RAW.indexOf('data-testid="agent-talk"') - 400,
    HEADER_RAW.indexOf('data-testid="agent-talk"') + 900,
  )

  it('renders Talk with a stable test id and a click that navigates', () => {
    expect(HEADER).toContain('data-testid="agent-talk"')
    expect(HEADER).toContain('@click="goToTalk"')
    expect(talk).toContain('>\n            Talk\n          </button>')
  })

  it('is NOT gated — no `v-if` on the button (#2559 G-1)', () => {
    // The instance-wide flag the old mic used is the same boolean the Workspace
    // itself checks, so a gate here would only add a cold-load pop-in and a
    // sticky-false hide. A dead button is a worse answer than a page that says
    // why — and the Workspace says why.
    const openTag = talk.slice(talk.indexOf('<button'), talk.indexOf('>', talk.indexOf('data-testid="agent-talk"')))
    expect(openTag).not.toContain('v-if')
  })

  it('stays in the same tab — the one-shot and the AudioContext both need it', () => {
    // Scoped to the Talk block on purpose: the Git remote link elsewhere in this
    // header legitimately carries `target="_blank"`, so a file-wide check would
    // fail on arrival.
    expect(talk).not.toContain('target="_blank"')
    expect(HEADER_RAW).toContain('target="_blank"')   // the Git link, still there
  })

  it('says where the call happens and that the conversation changes home', () => {
    expect(talk).toMatch(/title="[^"]*Workspace[^"]*"/)
    expect(talk).toMatch(/title="[^"]*not in this chat[^"]*"/)
  })

  it('declares no `voiceAvailable` prop, and AgentDetail passes none', () => {
    // Dead since ent#438. The honest fix for a dead prop is to delete it, not to
    // invent a consumer for it.
    expect(HEADER).not.toContain('voiceAvailable')
    expect(DETAIL).not.toContain(':voice-available')
  })
})

describe('useVoiceSession has exactly one start path', () => {
  it('no longer carries the Agent-Detail-shaped start()', () => {
    expect(COMPOSABLE).not.toContain('async function start(')
    expect(COMPOSABLE).not.toContain('/voice/start')
  })

  it('DOES NOT list `start,` in its return object — the ReferenceError guard', () => {
    // If this fires, `start()` was deleted but the shorthand `start, startWith`
    // was left behind, and every `useVoiceSession(...)` call now throws at setup
    // — the Workspace conversation included. Nothing else in the suite catches
    // it; see this file's header.
    expect(COMPOSABLE).not.toMatch(/\bstart,\s*startWith\b/)
    expect(COMPOSABLE).toMatch(/\bstartWith,\s*stop,/)
  })

  it('keeps the pinned startWith signature the Workspace drives', () => {
    expect(COMPOSABLE).toContain('async function startWith(requestFn, { restStop = true } = {})')
  })
})

describe('the orb kept its consumer', () => {
  it('PortalConversation still mounts VoiceOverlay and drives useVoiceSession', () => {
    // The whole premise: this PR removes a front door, it does not remove voice.
    expect(CONVERSATION).toContain("import VoiceOverlay from '../chat/VoiceOverlay.vue'")
    expect(CONVERSATION).toContain("import { useVoiceSession } from '../../composables/useVoiceSession'")
    expect(CONVERSATION).toContain('const voice = useVoiceSession(props.agent.name)')
  })
})
