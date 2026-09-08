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
import { describe, it, expect } from 'vitest'
import { readFileSync, existsSync } from 'fs'
import { fileURLToPath } from 'url'
import {
  CANVAS_SAFETY_POLL_MS,
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

describe('the call is the Agent Detail orb, reused — not forked', () => {
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
    expect(CODE).toMatch(/<form[\s\S]{0,120}:class="voiceCallActive \? 'opacity-60 pointer-events-none' : ''"/)
    expect(CODE).toContain(':disabled="transcribing || voiceCallActive"')
    expect(CODE).toContain(':disabled="sending || !input.trim() || voiceCallActive"')
    expect(TABS).toContain('disabled: { type: Boolean, default: false }')
    expect(TABS).toMatch(/function onSelect\(id\) \{\s*if \(props\.disabled\) return/)
  })
  it('hides the speaker toggle for the duration and never narrates over the call', () => {
    expect(CODE).toContain('v-if="ttsEnabled && !voiceCallActive"')
    expect(CODE).toMatch(/voiceMode\.value && ttsEnabled\.value && data\.response[\s\S]{0,120}!voiceCallActive\.value\) speak\(data\.response\)/)
  })
  it('Escape ends the call before the turn-cancel rule runs', () => {
    const esc = CODE.slice(CODE.indexOf('function onEscapeKeydown(event)'), CODE.indexOf('async function cancelTurn()'))
    expect(esc.indexOf("voiceCallActive.value && event.key === 'Escape'")).toBeGreaterThan(-1)
    expect(esc.indexOf("voiceCallActive.value && event.key === 'Escape'")).toBeLessThan(esc.indexOf('shouldCancelOnEscape(event'))
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
    expect(CODE).toMatch(/watch\(\(\) => \[props\.agent\.name, props\.sessionId\][\s\S]{0,300}if \(voiceCallActive\.value\) await voice\.stop\(\)/)
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
    expect(SHELL_CODE).toMatch(/<PortalVoiceCanvas\s+v-if="voiceCall\.active && voiceCall\.voiceSessionId && activeAgent"/)
    expect(CODE).toMatch(/watch\(\[voiceCallActive, \(\) => voice\.voiceSessionId\.value\]/)
    expect(CANVAS_COLUMN).toContain('if (inFlight || !props.voiceSessionId) return')
    expect(SHELL_CODE).toMatch(/<PortalAgentDetails\s+v-else-if="detailsOpen && activeAgent"/)
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
