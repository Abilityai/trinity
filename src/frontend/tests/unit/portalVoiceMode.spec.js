/**
 * trinity-enterprise#534 — Workspace voice mode: the orb takes the conversation.
 *
 * Two halves, for the reason every Workspace spec has two halves: vitest runs
 * `environment: 'node'` with no component-mount harness, so
 *
 *   1. every RULE lives in `components/portal/portalVoiceMode.js` and is tested
 *      as a function here — the entry control per principal kind, the pre-flight,
 *      the header line, the transcript grouping, the labels, the refresh rule;
 *   2. the WIRING that only source can answer is pinned as source guards — the
 *      #440 loop is gone, the orb is the Agent Detail one (reused, not forked),
 *      Escape ends the call, the tabs/composer go inert, an unmount ends the call,
 *      a new chat gets its thread BEFORE the call starts, and the shell swaps the
 *      rail for the canvas column and refuses navigation.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync, existsSync } from 'fs'
import { fileURLToPath } from 'url'
import {
  CANVAS_SAFETY_POLL_MS,
  VOICE_QUERY_KEY,
  armVoiceAutoStart,
  disarmVoiceAutoStart,
  voiceAutoStart,
  voiceAutoStartArmed,
  voiceQueryRequested,
  END_REASON_TEXT,
  PANEL_TOOL_NAMES,
  VOICE_INSECURE_REASON,
  VOICE_LOCKED_CONTROLS,
  VOICE_NO_MIC_REASON,
  VOICE_SPLIT,
  VOICE_UNAVAILABLE_FALLBACK,
  canvasChanged,
  endedNotice,
  groupVoiceBlocks,
  isPanelTool,
  startFailureReason,
  voiceCallLabel,
  voiceCallLabelFromTurns,
  voiceEntryState,
  voiceHeaderLine,
  threadChangeEndsCall,
  isMuteHotkey,
  applyTaskFrame,
  backgroundTasksLabel,
  voiceTaskCaption,
  VOICE_TASK_CAPTION,
  voicePreflight,
} from '../../src/components/portal/portalVoiceMode'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
// HTML comments are stripped until none remain: a single pass leaves a live
// `<!--` behind on a nested/overlapping comment (the hardeningGuide.spec shape;
// CodeQL js/incomplete-multi-character-sanitization).
const stripHtmlComments = (src) => {
  let out = src
  for (;;) {
    const next = out.replace(/<!--[\s\S]*?-->/g, '')
    if (next === out) return out
    out = next
  }
}
const stripComments = (src) => stripHtmlComments(src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, ''))

const CONVERSATION = read('../../src/components/portal/PortalConversation.vue')
const SHELL = read('../../src/views/Portal.vue')
const COMPOSABLE = read('../../src/composables/useVoiceSession.js')
const CANVAS_COLUMN = read('../../src/components/portal/PortalVoiceCanvas.vue')
const TABS = read('../../src/components/portal/PortalChatTabs.vue')
const STORE = read('../../src/stores/clientPortal.js')
const HEADER = read('../../src/components/AgentHeader.vue')
const CODE = stripComments(CONVERSATION)
const SHELL_CODE = stripComments(SHELL)

// ---------------------------------------------------------------------------
// 1. Rules
// ---------------------------------------------------------------------------

describe('the Voice control — who sees it, and what it says when it cannot work', () => {
  it('is not rendered at all for a portal-token (external) client', () => {
    expect(voiceEntryState({ isPlatform: false, realtimeVoice: { available: true } }))
      .toEqual({ render: false, enabled: false, reason: '' })
  })

  it('is enabled for a platform user when the instance can do it', () => {
    expect(voiceEntryState({ isPlatform: true, realtimeVoice: { available: true, reason: null } }))
      .toEqual({ render: true, enabled: true, reason: '' })
  })

  it('is disabled WITH the reason for a platform user when it cannot — never a dead button', () => {
    const s = voiceEntryState({ isPlatform: true, realtimeVoice: { available: false, reason: 'No voice provider key is configured on this instance.' } })
    expect(s.render).toBe(true)
    expect(s.enabled).toBe(false)
    expect(s.reason).toBe('No voice provider key is configured on this instance.')
  })

  it('falls back to a sentence when the roster gave no reason (an older backend)', () => {
    expect(voiceEntryState({ isPlatform: true, realtimeVoice: null }).reason).toBe(VOICE_UNAVAILABLE_FALLBACK)
    expect(voiceEntryState({ isPlatform: true, realtimeVoice: { available: 'true' } }).enabled).toBe(false)
  })
})

describe('pre-flight — every failure says why before a request leaves the browser', () => {
  it('names the insecure origin, not "permission denied"', () => {
    expect(voicePreflight({ canCapture: true, secureContext: false })).toBe(VOICE_INSECURE_REASON)
  })
  it('names a missing microphone API', () => {
    expect(voicePreflight({ canCapture: false, secureContext: true })).toBe(VOICE_NO_MIC_REASON)
  })
  it('is null when the call may start', () => {
    expect(voicePreflight({ canCapture: true, secureContext: true })).toBeNull()
  })
})

describe('the header line', () => {
  it('follows the orb state', () => {
    expect(voiceHeaderLine({ status: 'connecting' })).toBe('Connecting…')
    expect(voiceHeaderLine({ status: 'listening' })).toBe('Listening')
    expect(voiceHeaderLine({ status: 'speaking' })).toBe('Speaking')
    expect(voiceHeaderLine({ status: 'ended' })).toBe('Call ended')
  })
  it('names the tool while one runs', () => {
    expect(voiceHeaderLine({ status: 'tool_calling', toolName: 'run_task' })).toBe('Working: run task')
    expect(voiceHeaderLine({ status: 'tool_calling' })).toBe('Working…')
  })
  it('says Muted rather than Listening when the mic is off', () => {
    expect(voiceHeaderLine({ status: 'listening', muted: true })).toBe('Muted')
  })
  it('lets an error win over the state', () => {
    expect(voiceHeaderLine({ status: 'listening', error: 'Microphone access denied.' })).toBe('Microphone access denied.')
  })
})

describe('how a call ended, in words', () => {
  it('is silent when the person ended it', () => {
    expect(endedNotice({ reason: null })).toBe('')
  })
  it('uses the server\'s words when it sent any, else the reason\'s sentence', () => {
    expect(endedNotice({ reason: 'cap', message: 'The call reached its 30-minute limit.' })).toBe('The call reached its 30-minute limit.')
    expect(endedNotice({ reason: 'cap' })).toBe(END_REASON_TEXT.cap)
    expect(endedNotice({ reason: 'error' })).toBe(END_REASON_TEXT.error)
    expect(endedNotice({ reason: 'provider_closed' })).toBe(END_REASON_TEXT.provider_closed)
    expect(endedNotice({ reason: 'something-new' })).toBe('The call ended.')
  })
  it('turns a failed start into a sentence, preferring the server\'s detail', () => {
    expect(startFailureReason({ status: 404, detail: 'Conversation not found' })).toBe('Conversation not found')
    expect(startFailureReason({ status: 404 })).toMatch(/could not be found/)
    expect(startFailureReason({ status: 429 })).toMatch(/Too many/)
    expect(startFailureReason({ status: 503 })).toBe(VOICE_UNAVAILABLE_FALLBACK)
    expect(startFailureReason({})).toBe('The voice call could not start.')
  })
})

describe('the transcript block — grouped by call id, never by an opener row', () => {
  const typed = (i, role = 'user') => ({ role, content: `typed ${i}`, source: null, voiceCallId: null })
  const spoken = (call, i, role = 'user') => ({ role, content: `said ${i}`, source: 'voice', voiceCallId: call })
  const label = (call, text) => ({ role: 'system', content: text, source: 'voice', voiceCallId: call })

  it('passes typed rows through with their original index (Retry needs it)', () => {
    const items = groupVoiceBlocks([typed(0), typed(1, 'assistant')])
    expect(items.map((i) => i.kind)).toEqual(['message', 'message'])
    expect(items.map((i) => i.index)).toEqual([0, 1])
  })

  it('folds one call into one block placed where the call started, with its label', () => {
    const items = groupVoiceBlocks([typed(0), spoken('c1', 1), spoken('c1', 2, 'assistant'), label('c1', 'Voice call · 4 min'), typed(4)])
    expect(items.map((i) => i.kind)).toEqual(['message', 'voice-call', 'message'])
    expect(items[1].turns).toHaveLength(2)
    expect(items[1].label).toBe('Voice call · 4 min')
    expect(items[2].index).toBe(4)
  })

  it('keeps two back-to-back calls apart', () => {
    const items = groupVoiceBlocks([spoken('c1', 0), label('c1', 'Voice call · 1 min'), spoken('c2', 2), label('c2', 'Voice call · 2 min')])
    expect(items.map((i) => i.kind)).toEqual(['voice-call', 'voice-call'])
    expect(items.map((i) => i.callId)).toEqual(['c1', 'c2'])
  })

  it('does not let a typed turn that landed mid-call split the block', () => {
    const items = groupVoiceBlocks([spoken('c1', 0), typed(1, 'assistant'), spoken('c1', 2, 'assistant'), label('c1', 'Voice call · 3 min')])
    expect(items.map((i) => i.kind)).toEqual(['voice-call', 'message'])
    expect(items[0].turns).toHaveLength(2)
  })

  it('names a block whose summary row fell off the history window by what is left of it', () => {
    const items = groupVoiceBlocks([spoken('c1', 0), spoken('c1', 1, 'assistant'), spoken('c1', 2)])
    expect(items[0].label).toBe('Voice call · 3 spoken turns')
    expect(voiceCallLabelFromTurns([])).toBe('Voice call')
    expect(voiceCallLabelFromTurns([spoken('c', 0)])).toBe('Voice call · 1 spoken turn')
  })

  it('treats a row with a call id but no voice source as typed (never mis-folds)', () => {
    const items = groupVoiceBlocks([{ role: 'user', content: 'x', source: null, voiceCallId: 'c1' }])
    expect(items[0].kind).toBe('message')
  })

  // trinity#2694 — the thread is one timeline. The block sits exactly where the
  // call happened: after every turn before it, before every turn after it, and
  // a second call is a second block at its own position.
  it('renders typed → call → typed → call → typed in that order, each call at its own place (#2694)', () => {
    const rows = [
      typed(0), typed(1, 'assistant'),
      spoken('c1', 2), spoken('c1', 3, 'assistant'), label('c1', 'Voice call · 4 min'),
      typed(5), typed(6, 'assistant'),
      spoken('c2', 7), spoken('c2', 8, 'assistant'), label('c2', 'Voice call · 1 min'),
      typed(10), typed(11, 'assistant'),
    ]
    const items = groupVoiceBlocks(rows)
    expect(items.map((i) => (i.kind === 'voice-call' ? `call:${i.callId}` : `m:${i.index}`)))
      .toEqual(['m:0', 'm:1', 'call:c1', 'm:5', 'm:6', 'call:c2', 'm:10', 'm:11'])
    // the two rows of one exchange keep their order inside the block
    expect(items[2].turns.map((t) => t.content)).toEqual(['said 2', 'said 3'])
    expect(items[5].turns.map((t) => t.content)).toEqual(['said 7', 'said 8'])
    expect(items[2].label).toBe('Voice call · 4 min')
    expect(items[5].label).toBe('Voice call · 1 min')
  })

  it('anchors a block at the call’s first row in the array, never hoisted (#2694)', () => {
    // The array IS the timeline (the server orders by created_at). A block must
    // never be moved ahead of a typed row that precedes the call's first row.
    const rows = [typed(0), typed(1, 'assistant'), typed(2), spoken('c1', 3), label('c1', 'Voice call · 1 min')]
    const items = groupVoiceBlocks(rows)
    expect(items.map((i) => i.kind)).toEqual(['message', 'message', 'message', 'voice-call'])
  })

  it('says when the window’s ceiling cut the old end of the thread (#2694)', () => {
    // The read is a window of typed turns under a row ceiling; a thread that
    // silently starts mid-call is the very symptom the window fix removes.
    const src = stripComments(CONVERSATION)
    expect(src).toContain('data-testid="portal-history-truncated"')
    expect(src).toMatch(/v-if="historyTruncated"/)
    expect(src).toContain('historyTruncated.value = truncated === true')
  })

  it('polls for the reply with the narrow read, never the window (#2694)', () => {
    const src = stripComments(CONVERSATION)
    expect(src).toContain('{ limit: REPLY_POLL_ROWS }')
    expect(src).toContain('replyBaseline(')
    expect(src).not.toContain('persistedAssistantCount')
    // the store threads `limit` through as a query param
    const store = read('../../src/stores/clientPortal.js')
    expect(store).toContain('if (limit) params.limit = limit')
  })

  it('labels a call by rounded minutes, never under one, and says when the cap ended it', () => {
    expect(voiceCallLabel(12)).toBe('Voice call · 1 min')
    expect(voiceCallLabel(250)).toBe('Voice call · 4 min')
    expect(voiceCallLabel(1800, { capped: true, capMinutes: 30 })).toBe('Voice call · 30 min · ended at the 30-minute limit')
  })
})

describe('layout and refresh rules', () => {
  it('keeps the retired page\'s 40/60 split', () => {
    expect(VOICE_SPLIT).toEqual({ orb: 40, canvas: 60 })
  })
  it('refetches the canvas on a finished panel verb and keeps a slow safety poll', () => {
    for (const n of ['show_markdown', 'update_panel', 'append_to_panel', 'clear_panel', 'show_diagram', 'show_image']) {
      expect(isPanelTool(n)).toBe(true)
    }
    expect(isPanelTool('run_task')).toBe(false)
    expect(PANEL_TOOL_NAMES).toHaveLength(6)
    expect(CANVAS_SAFETY_POLL_MS).toBeGreaterThanOrEqual(2000)
  })
  it('replaces the board only when the stamp moved', () => {
    expect(canvasChanged(null, { updated_at: 'a' })).toBe(true)
    expect(canvasChanged({ updated_at: 'a' }, { updated_at: 'a' })).toBe(false)
    expect(canvasChanged({ updated_at: 'a' }, { updated_at: 'b' })).toBe(true)
    expect(canvasChanged({ updated_at: 'a' }, null)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 2. Wiring — what only the source can answer
// ---------------------------------------------------------------------------

describe('the #440 hands-free loop is retired — one voice entry point', () => {
  it('the pure module and its spec are gone', () => {
    expect(existsSync(fileURLToPath(new URL('../../src/components/portal/voiceConversation.js', import.meta.url)))).toBe(false)
    expect(existsSync(fileURLToPath(new URL('./portalVoiceConversation.spec.js', import.meta.url)))).toBe(false)
  })
  it('the conversation imports nothing from it and keeps none of its state machine', () => {
    expect(CODE).not.toContain("from './voiceConversation'")
    for (const id of ['nextVoiceState', 'voiceConvLive', 'voiceDispatch', 'monitorTick', 'runVoiceTurn', 'narrateReply', 'toggleVoiceConversation']) {
      expect(CODE, id).not.toContain(id)
    }
  })
  it('hold-to-dictate and spoken replies stay as composer affordances', () => {
    expect(CODE).toContain('function toggleMic()')
    expect(CODE).toContain('const voiceMode = ref(loadVoiceMode())')
    expect(CODE).toContain('async function speak(text)')
  })
})

describe('the call is the shared platform orb, reused — not forked (and since #2559 this is its only consumer)', () => {
  it('mounts VoiceOverlay and drives it from useVoiceSession', () => {
    expect(CODE).toContain("import VoiceOverlay from '../chat/VoiceOverlay.vue'")
    expect(CODE).toContain("import { useVoiceSession } from '../../composables/useVoiceSession'")
    expect(CODE).toMatch(/<VoiceOverlay :voice="voice" @end="endVoiceCall" \/>/)
    expect(CODE).toContain('const voice = useVoiceSession(props.agent.name)')
  })
  it('starts through the portal-principal route, with no REST stop (the bridge closes the call)', () => {
    expect(CODE).toMatch(/voice\.startWith\(\s*\(\) => store\.startWorkspaceVoice\(props\.agent\.name, sid\),\s*\{ restStop: false \}/)
    expect(STORE).toContain("`/api/enterprise/client-portal/agents/${agentName}/voice/start`")
    expect(COMPOSABLE).toContain('async function startWith(requestFn, { restStop = true } = {})')
  })
  it('a brand-new chat gets its thread BEFORE the call starts, and adopts it', () => {
    const start = CODE.slice(CODE.indexOf('async function startVoiceCall()'), CODE.indexOf('async function endVoiceCall()'))
    expect(start.indexOf('store.createSession(props.agent.name)')).toBeGreaterThan(-1)
    expect(start.indexOf('store.createSession(props.agent.name)')).toBeLessThan(start.indexOf('voice.startWith('))
    // #2579: adoption runs through the one `adoptSession` seam now — this site
    // no longer emits by hand, because raising `bornHere` at only some of the
    // three adoption sites drops the provisional tab for the whole round trip
    // of starting a call. The emit still happens, inside it.
    expect(start).toContain('adoptSession(sid)')
  })
  it('the composable exposes the end reason, the saved handshake and the panel version', () => {
    for (const name of ['endReason', 'endMessage', 'panelVersion', 'awaitSaved', 'startWith']) {
      expect(COMPOSABLE, name).toContain(name)
    }
    // `ended` stops the media but leaves the socket open for `saved`; the
    // thread is reloaded on the falling edge of `active`, i.e. after `saved`.
    expect(COMPOSABLE).toMatch(/msg\.type === 'saved'[\s\S]{0,400}_resolveSaved\(msg\)[\s\S]{0,40}_cleanup\(\)/)
    expect(COMPOSABLE).toMatch(/function _onEnded\(msg\) \{[\s\S]{0,300}_stopMedia\(\)/)
    expect(COMPOSABLE).toContain("if (PANEL_TOOL_NAMES.includes(msg.tool)) panelVersion.value += 1")
  })
})

describe('modal: while the call is on, the chat is visible but inert', () => {
  it('disables every locked control (the list is data, the template obeys it)', () => {
    expect(VOICE_LOCKED_CONTROLS).toEqual(
      expect.arrayContaining(['new-chat', 'agent-picker', 'star', 'reset-main', 'chat-tabs', 'composer', 'mic', 'attach', 'send']),
    )
    expect(CODE).toMatch(/data-testid="new-chat-header"[\s\S]{0,40}|:disabled="voiceCallActive"[\s\S]{0,80}data-testid="new-chat-header"/)
    expect(CODE).toMatch(/:disabled="voiceCallActive"\s+data-testid="portal-agent-picker"/)
    expect(CODE).toMatch(/<PortalStarButton[\s\S]{0,120}voiceCallActive \? 'opacity-40 pointer-events-none'/)
    expect(CODE).toContain(':disabled="sending || resetting || voiceCallActive"')
    expect(CODE).toMatch(/<PortalChatTabs[\s\S]{0,200}:disabled="voiceCallActive"/)
    // ent#547: the inert class moved OFF the <form> and onto a wrapper inside
    // it, around every control except the voice-call toggle. The rule this
    // asserts is unchanged — the composer goes inert for the call's duration —
    // but the toggle must stay live, because it is the control that ENDS the
    // call. Inside the inert region it would render pressed and refuse the
    // click, a dead affordance manufactured by the move itself.
    // #2662 stacked the composer, so "everything except the toggle" is now TWO
    // regions on two rows — the field's wrapper and the control row's wrapper —
    // and `opacity` needs a real box on each. Both must carry the inert pair;
    // one without the other leaves half the composer live during a call.
    const INERT = ":class=\"voiceCallActive \\? 'opacity-60 pointer-events-none' : ''\""
    expect(CODE).toMatch(new RegExp('<div ref="composerWrap" class="relative" ' + INERT))
    expect(CODE).toMatch(new RegExp('<div class="flex-1 min-w-0 flex items-center gap-1" ' + INERT))
    // The toggle is a SIBLING of the control row's wrapper, not a descendant.
    // Positional, so it fails if a later edit moves the button inside.
    const formStart = CODE.indexOf('<form')
    const inertAt = CODE.indexOf('flex-1 min-w-0 flex items-center gap-1', formStart)
    const callAt = CODE.indexOf('data-testid="portal-voice-call"', formStart)
    expect(inertAt).toBeGreaterThan(-1)
    expect(callAt).toBeGreaterThan(-1)
    expect(callAt, 'the call toggle must precede the inert wrapper').toBeLessThan(inertAt)
    expect(CODE).toContain(':disabled="transcribing || voiceCallActive"')
  })

  it('takes the shell chrome OFF for the call, because the shell cannot join the inert regions', () => {
    // #2662. The shell is the PARENT of both regions above and it is what now
    // carries the border and fill, so leaving it static rendered a full-contrast
    // frame around opacity-60 contents — the pre-#2662 field dimmed with them.
    // It cannot simply join them: the call toggle lives inside it and must stay
    // bright, and `opacity` on a parent is not something a child can undo. So
    // the chrome is REMOVED for the call's duration, and the resting pair is the
    // false arm. A static border/fill on this element is the regression back.
    //
    // BOTH arms are bound and the static class holds no chrome colour, which is
    // load-bearing: with `border-transparent bg-transparent` left static and only
    // the resting pair bound, the LIGHT composer renders with no border at all —
    // Tailwind emits `.border-transparent` after `.border-gray-300` but
    // `.bg-transparent` before `.bg-white`, so the two disagree about which of an
    // equal-specificity pair survives, and every `dark:` variant hides it.
    expect(CODE).toMatch(/class="rounded-2xl border px-2 py-2 transition has-\[textarea:focus\]/)
    expect(CODE).not.toMatch(/class="rounded-2xl border border-transparent/)
    expect(CODE).toMatch(
      /:class="voiceCallActive \? 'border-transparent bg-transparent' : 'border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800'"/
    )
    expect(CODE).toContain(':disabled="sending || !input.trim() || voiceCallActive"')
    expect(TABS).toContain('disabled: { type: Boolean, default: false }')
    expect(TABS).toMatch(/function onSelect\(id\) \{\s*if \(props\.disabled\) return/)
  })
  it('hides the speaker toggle for the duration and never narrates over the call', () => {
    expect(CODE).toContain('v-if="ttsEnabled && !voiceCallActive"')
    expect(CODE).toMatch(/voiceMode\.value && ttsEnabled\.value && data\.response[\s\S]{0,120}!voiceCallActive\.value\) speak\(data\.response\)/)
  })
  it('Escape ends the call before the turn-cancel rule runs', () => {
    // #2598 changed the SPELLING, not this property: the call is still asked
    // first. The condition used to be the inline
    // `voiceCallActive.value && event.key === 'Escape'`, which read none of the
    // preconditions `shouldCancelOnEscape` reads — so an overlay that claimed
    // Escape in the capture phase with `preventDefault()` closed AND ended the
    // call. It now dispatches on the shared `shouldEndCallOnEscape` rule; the
    // ORDERING assertion below is what ent#534 actually cares about and is
    // unchanged.
    const esc = CODE.slice(CODE.indexOf('function onEscapeKeydown(event)'), CODE.indexOf('async function cancelTurn()'))
    const call = esc.indexOf('shouldEndCallOnEscape(event, { callActive: voiceCallActive.value })')
    expect(call).toBeGreaterThan(-1)
    expect(call).toBeLessThan(esc.indexOf('shouldCancelOnEscape(event'))
    expect(esc).toContain('void endVoiceCall()')
  })
  it('the header line names the state and the way out; End always works', () => {
    expect(CODE).toMatch(/data-testid="portal-voice-line"/)
    expect(CODE).toContain('End the call to switch chats')
    expect(CODE).toMatch(/data-testid="portal-voice-end"[\s\S]{0,40}@click="endVoiceCall\(\)"/)
  })
})

describe('leaving mid-call ends it gracefully — the transcript is kept', () => {
  it('unmount, agent switch and a route-driven thread change all stop the call', () => {
    expect(CODE).toMatch(/function cleanupVoice\(\) \{[\s\S]{0,200}if \(voice\.isActive\.value\) void voice\.stop\(\)/)
    expect(CODE).toMatch(/watch\(\(\) => props\.agent\?\.name, \(\) => \{[\s\S]{0,400}if \(voiceCallActive\.value\) void voice\.stop\(\)/)
    // ent#551: the thread-change watcher asks the RULE, so a call's own new
    // thread being adopted (route replace → sessionId null → id) does not end it.
    expect(CODE).toMatch(/watch\(\(\) => \[props\.agent\.name, props\.sessionId\][\s\S]{0,700}if \(threadChangeEndsCall\(\{[\s\S]{0,400}\}\)\) await voice\.stop\(\)/)
    expect(CODE).toContain('boundSessionId: voice.portalSessionId.value || currentSessionId.value,')
  })
  it('reloads the thread on the falling edge of the call, so the persisted block is the truth', () => {
    expect(CODE).toMatch(/watch\(voiceCallActive, async \(on, was\) => \{\s*if \(!was \|\| on\) return[\s\S]{0,500}await loadThread\(currentSessionId\.value\)/)
    expect(CODE).toContain('source: m.source || null, voiceCallId: m.voice_call_id || null')
    expect(CODE).toContain('const threadItems = computed(() => groupVoiceBlocks(messages.value))')
    expect(CODE).toMatch(/data-testid="portal-voice-call-block"/)
  })
})

describe('the shell: the canvas takes the right column, and navigation waits', () => {
  it('swaps the rail (and the details panel) for PortalVoiceCanvas while the call is on', () => {
    expect(SHELL).toContain("import PortalVoiceCanvas from '@/components/portal/PortalVoiceCanvas.vue'")
    // The column mounts only once the session id is known — `active` rises before
    // the start request answers (found live: a fetch of `/voice//panel`).
    // #2640 moved the condition into a named computed (the canvas is inside a
    // <Transition> now, so the rail can no longer be its `v-else-if` and both
    // arms have to read the SAME rule); the rule itself is unchanged.
    expect(SHELL_CODE).toMatch(/<PortalVoiceCanvas[\s\S]{0,200}v-if="voiceCanvasHasColumn"/)
    expect(SHELL_CODE).toMatch(
      /const voiceCanvasHasColumn = computed\(\(\) => Boolean\(\s*voiceCall\.value\.active && voiceCall\.value\.voiceSessionId && activeAgent\.value/
    )
    expect(CODE).toMatch(/watch\(\[voiceCallActive, \(\) => voice\.voiceSessionId\.value\]/)
    expect(CANVAS_COLUMN).toContain('if (inFlight || !props.voiceSessionId) return')
    // ent#547: there is no details SIBLING to swap out any more — it is the
    // rail's Info tab, so the voice canvas now displaces the rail itself and the
    // chain is two arms rather than three.
    expect(SHELL_CODE).not.toContain('detailsOpen')
    // #2640: `v-if` with the negated shared condition, not `v-else-if` — the
    // <Transition> wrapper broke the adjacency that chain needs. Exclusivity is
    // the property; which construct expresses it is not.
    //
    // #2676 moved the rail's half onto the column WRAPPER, which is where the
    // animatable width lives. Same rule, one element out: both arms still read
    // the one shared computed, so they still cannot both claim the column.
    expect(SHELL_CODE).toMatch(/v-if="railHasColumn"/)
    expect(SHELL_CODE).toMatch(
      /const railHasColumn = computed\([\s\S]{0,200}!voiceCanvasHasColumn\.value/
    )
    // The 40 / 60 split is two flex SHARES of a zero basis (2 : 3), never
    // percentages of the row: `w-[40%]` + `w-[60%]` beside the 18rem sidebar
    // summed to 100% + 18rem and the shell's overflow-hidden clipped the
    // canvas column off the right edge (#2581, measured by the gallery #2583).
    expect(SHELL_CODE).toMatch(/<PortalVoiceCanvas[\s\S]*?class="hidden min-w-0 sm:flex sm:flex-\[3_1_0%\]"/)
    expect(SHELL_CODE).not.toMatch(/sm:w-\[60%\]|sm:w-\[40%\]/)
    expect(SHELL_CODE).toMatch(/voiceCall\.active \? 'flex-1 sm:flex-\[2_1_0%\]' : 'flex-1'/)
    expect(SHELL_CODE).toContain('@voice-call="onVoiceCall"')
  })
  it('refuses New chat, ⌘J and opening another thread while the call is on', () => {
    expect(SHELL_CODE).toMatch(/function newChatWithAgent\(name\) \{\s*if \(voiceCall\.value\.active\) return/)
    expect(SHELL_CODE).toMatch(/function openThread\(t\) \{\s*if \(voiceCall\.value\.active\) return/)
    expect(SHELL_CODE).toMatch(/function onGlobalKeydown\(e\) \{[\s\S]{0,200}if \(voiceCall\.value\.active\) return/)
  })
  it('clears the call state when the conversation remounts', () => {
    expect(SHELL_CODE).toMatch(/watch\(\[convKey, activeRoomIdFromRoute\], \(\) => \{\s*onVoiceCall\(null\)/)
  })
  it('the canvas column renders through CanvasPanel and refetches on the panel version', () => {
    expect(CANVAS_COLUMN).toContain("import CanvasPanel from '@/components/canvas/CanvasPanel.vue'")
    expect(CANVAS_COLUMN).toMatch(/watch\(\(\) => props\.panelVersion, \(\) => \{ void refresh\(\) \}\)/)
    expect(CANVAS_COLUMN).toContain('/voice/${encodeURIComponent(props.voiceSessionId)}/panel')
    expect(CANVAS_COLUMN).toContain('CANVAS_SAFETY_POLL_MS')
  })
})

describe('the capability field is named for the capability, not the provider', () => {
  it('the store reads `realtime_voice` strictly and keeps `voice_available` meaning TTS', () => {
    expect(STORE).toContain("available: data.realtime_voice?.available === true")
    expect(STORE).toContain('realtimeVoice: { available: false, reason: null }')
    expect(CODE).toContain('const ttsEnabled = computed(() => !!props.agent.voice_available)')
    for (const src of [CODE, SHELL_CODE, COMPOSABLE, CANVAS_COLUMN]) {
      expect(src.toLowerCase()).not.toContain('gemini')
    }
  })
})


// ---------------------------------------------------------------------------
// 3. The Talk door — `?voice=1` (trinity#2559)
// ---------------------------------------------------------------------------

describe('the `?voice=1` query is read strictly', () => {
  it('accepts only the literal string "1"', () => {
    expect(voiceQueryRequested('1')).toBe(true)
    for (const v of ['0', 'true', 'yes', '', 'on', ' 1', '1 ', undefined, null, 1, true]) {
      expect(voiceQueryRequested(v), String(v)).toBe(false)
    }
  })
  it('takes the first entry when vue-router hands back an array for a repeated key', () => {
    expect(voiceQueryRequested(['1', '0'])).toBe(true)
    expect(voiceQueryRequested(['0', '1'])).toBe(false)
    expect(voiceQueryRequested([])).toBe(false)
  })
  it('names the key once, so the parser and the strip cannot drift', () => {
    expect(VOICE_QUERY_KEY).toBe('voice')
  })
})

describe('the auto-start intent is ARMED IN THE APP, never by the URL alone', () => {
  // The flag is module state by design (it must die with the document), so each
  // case starts from a known position rather than inheriting the previous one.
  beforeEach(() => disarmVoiceAutoStart())

  const ok = { query: { voice: '1' }, landed: true, isPlatform: true, armed: true }

  it('starts only when every condition holds at once', () => {
    expect(voiceAutoStart(ok)).toEqual({ start: true, why: '' })
  })

  it('REFUSES a perfect request that was not armed — the pasted-link case', () => {
    // This is the regression test for the signed-out hot mic: `Portal.vue` runs
    // `bootstrap()` only while signed in, so a pasted `?voice=1` survives
    // unconsumed until the sign-in click — which is exactly the click that
    // satisfies a browser activation heuristic. The arm flag does not care.
    expect(voiceAutoStart({ ...ok, armed: false })).toEqual({ start: false, why: 'unarmed' })
  })

  it('refuses when the agent did not land, or the principal is not a platform session', () => {
    expect(voiceAutoStart({ ...ok, landed: false })).toEqual({ start: false, why: 'unreachable' })
    expect(voiceAutoStart({ ...ok, isPlatform: false })).toEqual({ start: false, why: 'principal' })
  })

  it('is silent — no `why` — when the key is simply absent or rejected', () => {
    expect(voiceAutoStart({ ...ok, query: {} })).toEqual({ start: false, why: '' })
    expect(voiceAutoStart({ ...ok, query: { voice: '0' } })).toEqual({ start: false, why: '' })
    expect(voiceAutoStart()).toEqual({ start: false, why: '' })
    expect(voiceAutoStart({ ...ok, query: null })).toEqual({ start: false, why: '' })
  })

  it('the arm lifecycle: false, armed, disarmed', () => {
    expect(voiceAutoStartArmed()).toBe(false)
    armVoiceAutoStart()
    expect(voiceAutoStartArmed()).toBe(true)
    expect(voiceAutoStart({ ...ok, armed: voiceAutoStartArmed() }).start).toBe(true)
    disarmVoiceAutoStart()
    expect(voiceAutoStartArmed()).toBe(false)
    expect(voiceAutoStart({ ...ok, armed: voiceAutoStartArmed() }).start).toBe(false)
  })

  it('returns no verdict about stripping — one site owns that', () => {
    // A helper that stripped on its own verdict left `?voice=0` resident,
    // because a rejected value is still PRESENT. `bootstrap()` keys on presence.
    expect(voiceAutoStart(ok)).not.toHaveProperty('strip')
    expect(voiceAutoStart({ ...ok, query: { voice: '0' } })).not.toHaveProperty('strip')
  })
})

describe('the door and the hand-off are wired (source, since there is no mount harness)', () => {
  it('the Talk button arms the one-shot BEFORE it navigates', () => {
    const header = stripComments(HEADER)
    expect(header).toContain("import { armVoiceAutoStart } from './portal/portalVoiceMode'")
    const fn = header.slice(header.indexOf('function goToTalk()'), header.indexOf('function goToBrain()'))
    expect(fn).toContain('armVoiceAutoStart()')
    expect(fn).toMatch(/router\.push\(\{ path: '\/workspace', query: \{ agent: props\.agent\.name, voice: '1' \} \}\)/)
    expect(fn.indexOf('armVoiceAutoStart()')).toBeLessThan(fn.indexOf('router.push'))
  })

  it('the shell records the intent, and strips NOTHING, inside resolveAgentQuery', () => {
    const fn = SHELL_CODE.slice(SHELL_CODE.indexOf('function resolveAgentQuery()'), SHELL_CODE.indexOf('const ASKS_POLL_MS'))
    expect(fn).toContain('pendingVoiceStart = voiceAutoStart({')
    expect(fn).toContain('armed: voiceAutoStartArmed(),')
    expect(fn).not.toContain('stripVoiceQuery()')
    // ...but it DOES record that its own replace happened. That replace takes a
    // bare path, so it drops the whole query — `voice` included.
    expect(fn).toContain('landingReplaced = true; router.replace(`/workspace/c/${landing.sessionId}`)')
  })

  it('bootstrap resets the intent, reads the key before the first await, and strips ONCE in the finally', () => {
    const fn = SHELL_CODE.slice(SHELL_CODE.indexOf('async function bootstrap()'), SHELL_CODE.indexOf('onMounted(async () =>'))
    // The reset must precede the try: bootstrap has no `catch`, so a throw would
    // otherwise carry a billed intent into the next sign-in re-bootstrap.
    expect(fn.indexOf('pendingVoiceStart = false')).toBeLessThan(fn.indexOf('try {'))
    // Read before the first await, or the landing replace has already rewritten it.
    const keyRead = fn.indexOf('const voiceKeyPresent = route.query[VOICE_QUERY_KEY] !== undefined')
    expect(keyRead).toBeGreaterThan(-1)
    expect(keyRead).toBeLessThan(fn.indexOf('await '))
    // One strip, in the finally, keyed on PRESENCE — and skipped when the
    // landing replace already dropped the query. Two `router.replace` calls
    // started in one tick do not compose: vue-router cancels the first
    // (NAVIGATION_CANCELLED), so an unconditional strip here would land the
    // door on `/workspace?agent=X` instead of `/workspace/c/<sid>`. `route`
    // updates asynchronously, so the strip cannot detect that itself.
    expect(fn).toMatch(/\} finally \{[\s\S]{0,600}if \(voiceKeyPresent\) \{ if \(!landingReplaced\) stripVoiceQuery\(\); disarmVoiceAutoStart\(\) \}/)
    expect(fn.match(/stripVoiceQuery\(\)/g)).toHaveLength(1)
    // Reset with the intent, for the same reason: a stale `true` from a throw
    // would suppress the next bootstrap's strip.
    expect(fn.indexOf('landingReplaced = false')).toBeLessThan(fn.indexOf('try {'))
    // Then the hand-off, after the finally.
    expect(fn.indexOf('conversationRef.value?.startVoiceCall?.()')).toBeGreaterThan(fn.indexOf('if (voiceKeyPresent)'))
    expect(fn).toContain('await nextTick()')
  })

  it('the strip reads the CURRENT route, so it composes with the landing replace', () => {
    const fn = SHELL_CODE.slice(SHELL_CODE.indexOf('function stripVoiceQuery()'), SHELL_CODE.indexOf('function stripVoiceQuery()') + 300)
    expect(fn).toContain('const query = { ...route.query }')
    expect(fn).toContain('delete query[VOICE_QUERY_KEY]')
    expect(fn).toContain('router.replace({ path: route.path, query })')
  })

  it('the shell holds the conversation ref, and the conversation exposes the call', () => {
    expect(stripHtmlComments(SHELL)).toMatch(/<PortalConversation[\s\S]{0,120}ref="conversationRef"/)
    expect(CODE).toMatch(/defineExpose\(\{[^}]*startVoiceCall[^}]*\}\)/)
  })

  it('does NOT use `navigator.userActivation` — it is a heuristic, not a provenance check', () => {
    for (const src of [SHELL, HEADER, read('../../src/components/portal/portalVoiceMode.js')]) {
      // The rationale lives in comments; no CODE path may consult it.
      expect(stripComments(src)).not.toContain('userActivation')
    }
  })
})

// ---- ent#551 — background tasks: a long task runs while the conversation continues ----
describe('ent#551 — background tasks', () => {
  it('a task frame adds by id, is idempotent on started, and clears on finished or failed', () => {
    let tasks = applyTaskFrame([], { state: 'started', task_id: 't1', label: 'the deck' })
    expect(tasks).toEqual([{ taskId: 't1', label: 'the deck' }])
    tasks = applyTaskFrame(tasks, { state: 'started', task_id: 't1', label: 'the deck' })
    expect(tasks).toHaveLength(1)
    tasks = applyTaskFrame(tasks, { state: 'started', task_id: 't2', label: 'the numbers' })
    expect(tasks.map((t) => t.taskId)).toEqual(['t1', 't2'])
    // The first to land must not clear the other — a list by id, never a count.
    tasks = applyTaskFrame(tasks, { state: 'finished', task_id: 't1' })
    expect(tasks).toEqual([{ taskId: 't2', label: 'the numbers' }])
    tasks = applyTaskFrame(tasks, { state: 'failed', task_id: 't2' })
    expect(tasks).toEqual([])
    // Unknown ids and frames without one are no-ops.
    expect(applyTaskFrame(tasks, { state: 'finished', task_id: 't9' })).toEqual([])
    expect(applyTaskFrame([{ taskId: 't1', label: '' }], {})).toEqual([{ taskId: 't1', label: '' }])
  })

  it('the badge says WHAT is running, in one line; a bare count only when that is all it has', () => {
    expect(backgroundTasksLabel([])).toBe('')
    expect(backgroundTasksLabel(0)).toBe('')
    // One task: its own one-liner — "a task" told the person nothing.
    expect(backgroundTasksLabel([{ taskId: 't1', label: 'Count files in home directory' }])).toBe('Count files in home directory')
    expect(backgroundTasksLabel([{ taskId: 't1', label: '' }])).toBe('1 task running')
    // Several: the count, then the labels.
    expect(backgroundTasksLabel([{ taskId: 't1', label: 'Count files' }, { taskId: 't2', label: 'Write the audit note' }]))
      .toBe('2 tasks · Count files · Write the audit note')
    // Long labels are clipped, and the whole line is bounded.
    const long = 'x'.repeat(200)
    expect(backgroundTasksLabel([{ taskId: 't1', label: long }]).length).toBeLessThanOrEqual(48)
    expect(backgroundTasksLabel([{ taskId: 't1', label: long }, { taskId: 't2', label: long }, { taskId: 't3', label: long }]).length)
      .toBeLessThanOrEqual(96)
    // A count alone.
    expect(backgroundTasksLabel(1)).toBe('1 task running')
    expect(backgroundTasksLabel(2)).toBe('2 tasks running')
  })

  it('the header line carries the running count and never drops the state', () => {
    const one = [{ taskId: 't1', label: 'Counting files' }]
    expect(voiceHeaderLine({ status: 'listening', backgroundTasks: one })).toBe('Listening · Counting files')
    expect(voiceHeaderLine({ status: 'speaking', backgroundTasks: 2 })).toBe('Speaking · 2 tasks running')
    expect(voiceHeaderLine({ status: 'listening', muted: true, backgroundTasks: one })).toBe('Muted · Counting files')
    expect(voiceHeaderLine({ status: 'tool_calling', toolName: 'show_markdown', backgroundTasks: one }))
      .toBe('Working: show markdown · Counting files')
    // Nothing running: the line is exactly what it was before ent#551.
    expect(voiceHeaderLine({ status: 'listening' })).toBe('Listening')
    // An error still wins the whole line.
    expect(voiceHeaderLine({ status: 'listening', error: 'Mic lost', backgroundTasks: 3 })).toBe('Mic lost')
  })

  it('a typed row from a voice-call task gets the caption; a spoken row and a reply do not', () => {
    expect(voiceTaskCaption({ role: 'user', voiceCallId: 'vs_1', source: null })).toBe(VOICE_TASK_CAPTION)
    expect(voiceTaskCaption({ role: 'assistant', voiceCallId: 'vs_1', source: null })).toBe('')
    expect(voiceTaskCaption({ role: 'user', voiceCallId: 'vs_1', source: 'voice' })).toBe('')
    expect(voiceTaskCaption({ role: 'user', voiceCallId: null })).toBe('')
    expect(voiceTaskCaption(undefined)).toBe('')
    // …and such a row stays OUT of the spoken block: it was not spoken.
    const items = groupVoiceBlocks([
      { id: 'a', role: 'user', content: 'hi', source: 'voice', voiceCallId: 'vs_1' },
      { id: 'b', role: 'user', content: 'count the PRs', source: null, voiceCallId: 'vs_1' },
    ])
    expect(items.map((i) => i.kind)).toEqual(['voice-call', 'message'])
  })

  it('the composable keeps the tasks from the task frame and refetches the canvas when one lands', () => {
    const src = read('../../src/composables/useVoiceSession.js')
    expect(src).toContain("msg.type === 'task'")
    expect(src).toContain('applyTaskFrame(backgroundTasks.value, msg)')
    const branch = src.split("msg.type === 'task'")[1].split('} else if')[0]
    expect(branch).toContain("if (msg.state !== 'started') panelVersion.value += 1")
    expect(src).toContain('backgroundTasks, hasBackgroundTasks,')
  })

  it('the orb shows work in flight as its own badge, and the conversation captions the ask', () => {
    const orb = read('../../src/components/chat/VoiceOverlay.vue')
    expect(orb).toContain('data-testid="voice-background-tasks"')
    expect(orb).toContain('backgroundTasksLabel(voice.backgroundTasks.value)')
    const conv = read('../../src/components/portal/PortalConversation.vue')
    expect(conv).toContain('backgroundTasks: voice.backgroundTasks.value,')
    // While the call is on, what lands in the thread is read, not unread: the
    // read cursor advances on spoken turns and task landings (no list refresh).
    const marker = conv.split("watch([() => voice.transcriptEntries.value.length, () => voice.panelVersion.value]")[1] || ''
    expect(marker).toContain("store.markChatRead('thread', currentSessionId.value)")
    expect(marker.slice(0, 700)).not.toContain("emit('sessions-changed'")
    expect(conv).toContain('data-testid="portal-voice-task-caption"')
    expect(conv).toContain('voiceTaskCaption(item.message)')
  })
})

// ---- ent#551 QA — a call started from a new chat must survive its own thread being adopted ----
describe('a call started from a new chat is not ended by its own thread arriving', () => {
  it('the rule: only a real thread change, or an agent change, ends the call', () => {
    // No call: nothing to end.
    expect(threadChangeEndsCall({ callActive: false, newSessionId: 'b', boundSessionId: 'a' })).toBe(false)
    // The call's own thread being adopted (null → id, same id the call is bound to).
    expect(threadChangeEndsCall({ callActive: true, newSessionId: 't1', boundSessionId: 't1' })).toBe(false)
    // A route-driven switch to another thread.
    expect(threadChangeEndsCall({ callActive: true, newSessionId: 't2', boundSessionId: 't1' })).toBe(true)
    // The thread going away under the call.
    expect(threadChangeEndsCall({ callActive: true, newSessionId: null, boundSessionId: 't1' })).toBe(true)
    // An agent switch always ends it, whatever the thread.
    expect(threadChangeEndsCall({ callActive: true, agentChanged: true, newSessionId: 't1', boundSessionId: 't1' })).toBe(true)
  })

  it('the mic worklet is a same-origin file, not a blob: script the CSP blocks', () => {
    const audio = read('../../src/utils/audio.js')
    expect(audio).toContain("export const MIC_WORKLET_URL = '/mic-capture.worklet.js'")
    expect(audio).toContain('audioContext.audioWorklet.addModule(MIC_WORKLET_URL)')
    expect(audio).not.toContain('createObjectURL')
    const worklet = read('../../public/mic-capture.worklet.js')
    expect(worklet).toContain("registerProcessor('trinity-mic-capture', MicCapture)")
    // Both CSPs allow it as 'self'; neither needs (or gets) blob: in script-src.
    const devCsp = read('../../vite.config.js')
    const prodCsp = read('../../security-headers.conf')
    // The policy LITERALS, not the comment above them that also says "script-src".
    expect(devCsp.match(/"script-src ([^;"]*);/)[1]).toBe("'self' 'unsafe-inline' 'unsafe-eval'")
    expect(prodCsp.match(/script-src ([^;]*);/)[1]).not.toContain('blob:')
  })
})

// ---- ent#551 QA — the overlay's bottom stack, and M to mute ----
describe('the status line never sits on the buttons, and M mutes', () => {
  it('the rule: plain M during a call, not in a field, not a claimed key, no modifier', () => {
    const ev = (over = {}) => ({ key: 'm', metaKey: false, ctrlKey: false, altKey: false, defaultPrevented: false, target: { tagName: 'DIV' }, ...over })
    expect(isMuteHotkey(ev(), { callActive: true })).toBe(true)
    expect(isMuteHotkey(ev({ key: 'M' }), { callActive: true })).toBe(true)
    expect(isMuteHotkey(ev(), { callActive: false })).toBe(false)
    expect(isMuteHotkey(ev({ key: 'n' }), { callActive: true })).toBe(false)
    expect(isMuteHotkey(ev({ metaKey: true }), { callActive: true })).toBe(false)       // ⌘M is the window's
    expect(isMuteHotkey(ev({ ctrlKey: true }), { callActive: true })).toBe(false)
    expect(isMuteHotkey(ev({ defaultPrevented: true }), { callActive: true })).toBe(false) // an overlay claimed it
    expect(isMuteHotkey(ev({ target: { tagName: 'INPUT' } }), { callActive: true })).toBe(false)
    expect(isMuteHotkey(ev({ target: { tagName: 'TEXTAREA' } }), { callActive: true })).toBe(false)
    expect(isMuteHotkey(ev({ target: { tagName: 'DIV', isContentEditable: true } }), { callActive: true })).toBe(false)
    expect(isMuteHotkey(null, { callActive: true })).toBe(false)
  })

  it('the keydown handler mutes on the rule, after Escape and before the turn-cancel rule', () => {
    const esc = CODE.slice(CODE.indexOf('function onEscapeKeydown(event)'), CODE.indexOf('async function cancelTurn()'))
    const end = esc.indexOf('shouldEndCallOnEscape(event, { callActive: voiceCallActive.value })')
    const mute = esc.indexOf('isMuteHotkey(event, { callActive: voiceCallActive.value })')
    expect(mute).toBeGreaterThan(end)
    expect(mute).toBeLessThan(esc.indexOf('shouldCancelOnEscape(event'))
    expect(esc.slice(mute, mute + 200)).toContain('voice.toggleMute()')
  })

  it('status text and controls are one bottom-anchored column (no overlap at any height)', () => {
    const orb = read('../../src/components/chat/VoiceOverlay.vue')
    expect(orb).toContain('data-testid="voice-bottom-stack"')
    expect(orb).toMatch(/voice-bottom-stack"[\s\S]{0,40}/)
    expect(orb).not.toContain('bottom-16')
    const stack = orb.slice(orb.indexOf('voice-bottom-stack'))
    expect(stack.indexOf('statusLabel')).toBeLessThan(stack.indexOf('voice.toggleMute()'))
    expect(orb).toContain("'Unmute (M)' : 'Mute (M)'")
  })
})
