/**
 * trinity-enterprise#620 — the Work card's activity line.
 *
 * Pure rules (`utils/workActivity.js`): the ONE vocabulary every surface
 * composes from `{tool, summary}`; the stream-frame parser that reads the
 * REAL raw shape; the summariser's parity with the agent server (shared
 * fixture); the min-display queue; the two-feed resolver. Then the
 * placement guards for what a node-environment spec cannot mount.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  ACTIVITY_MAX_AGE_S,
  ACTIVITY_MIN_DISPLAY_MS,
  ACTIVITY_POLL_MS,
  activityAgeSeconds,
  activityFromStreamEvent,
  activityLine,
  createActivityLineQueue,
  displayToolName,
  resolveActivityText,
  summariseToolInput,
} from '../../src/utils/workActivity'
import { getStatusFromStreamEvent } from '../../src/utils/execution-status'

const src = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))
const FIXTURE = JSON.parse(readFileSync(fileURLToPath(new URL('../../../../tests/fixtures/tool_input_summary.json', import.meta.url)), 'utf8'))

// ---------------------------------------------------------------------------
// The vocabulary
// ---------------------------------------------------------------------------
describe('activityLine — one vocabulary', () => {
  it('names the tool and its object in plain words (AC #1)', () => {
    expect(activityLine({ tool: 'Read', summary: '.../routers/agents.py' })).toEqual({ text: 'Reading .../routers/agents.py', object: '.../routers/agents.py', mono: true })
    expect(activityLine({ tool: 'Bash', summary: 'pytest tests/unit -q' }).text).toBe('Running pytest tests/unit -q')
    expect(activityLine({ tool: 'Grep', summary: '"sync_health"' }).text).toBe('Searching for "sync_health"')
    expect(activityLine({ tool: 'WebSearch', summary: 'vue transition' }).text).toBe('Searching for vue transition')
    expect(activityLine({ tool: 'WebFetch', summary: 'docs.example.com' }).text).toBe('Fetching docs.example.com')
    expect(activityLine({ tool: 'Edit', summary: '.../c/d.py' }).text).toBe('Editing .../c/d.py')
    expect(activityLine({ tool: 'mcp:github', summary: 'repo: a/b' }).text).toBe('Using github')
    expect(activityLine({ tool: 'mcp:trinity', summary: 'agent_name: sidekick' }).text).toBe('Delegating to sidekick')
    expect(activityLine({ tool: 'mcp:trinity', summary: 'agent_name: another agent' }).text).toBe('Delegating to another agent')
    expect(activityLine({ tool: 'Task:explore', summary: 'Map the Work card' }).text).toBe('Delegating to explore: Map the Work card')
    expect(activityLine({ tool: 'Task', summary: null }).text).toBe('Delegating to an agent')
    expect(activityLine({ tool: null, summary: null }).text).toBe('Thinking')
    expect(activityLine({ tool: 'Reply' }).text).toBe('Writing a reply')
  })

  it('an unknown tool still says its name, never "Using a tool…"', () => {
    expect(activityLine({ tool: 'NotebookRun', summary: 'cell: 3' }).text).toBe('Using NotebookRun: cell: 3')
    expect(activityLine({ tool: 'NotebookRun' }).text).toBe('Using NotebookRun')
  })

  it('a tool with no usable object keeps the verb honest', () => {
    expect(activityLine({ tool: 'Read', summary: '...' }).text).toBe('Reading…')
    expect(activityLine({ tool: 'AskUserQuestion', summary: 'Asking question' }).text).toBe('Asking a question')
    expect(activityLine({ tool: 'TodoWrite', summary: 'Updating todos' }).text).toBe('Planning the next steps')
    expect(activityLine(null)).toBeNull()
    expect(activityLine('Read')).toBeNull()
  })

  it('machine objects are marked mono; prose is not', () => {
    expect(activityLine({ tool: 'Bash', summary: 'ls' }).mono).toBe(true)
    expect(activityLine({ tool: 'WebSearch', summary: 'x' }).mono).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The stream feed — the REAL frame shape
// ---------------------------------------------------------------------------
describe('activityFromStreamEvent — reads the raw stream-json shape', () => {
  const toolUse = (name, input) => ({ type: 'assistant', message: { role: 'assistant', content: [{ type: 'tool_use', id: 't1', name, input }] } })

  it('a tool_use block inside an assistant message becomes the two facts', () => {
    expect(activityFromStreamEvent(toolUse('Read', { file_path: '/home/developer/src/backend/routers/agents.py' })))
      .toEqual({ tool: 'Read', summary: '.../routers/agents.py' })
    expect(activityFromStreamEvent(toolUse('mcp__trinity__chat_with_agent', { agent_name: 'sidekick', message: 'hi' })))
      .toEqual({ tool: 'mcp:trinity', summary: 'agent_name: sidekick' })
    expect(activityFromStreamEvent(toolUse('Task', { subagent_type: 'explore', description: 'Map it' })))
      .toEqual({ tool: 'Task:explore', summary: 'Map it' })
  })

  it('the pre-#620 handler shape never occurs — and is not what is matched', () => {
    // The old Workspace handler looked for these; a raw frame never has them.
    expect(activityFromStreamEvent({ type: 'tool_use', tool_name: 'Read' })).toBeNull()
    expect(activityFromStreamEvent({ type: 'thinking' })).toBeNull()
  })

  it('a tool_result / thinking block means between tools; a text block means the reply', () => {
    expect(activityFromStreamEvent({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } })).toEqual({ tool: null, summary: null })
    expect(activityFromStreamEvent({ type: 'assistant', message: { content: [{ type: 'thinking', thinking: '…' }] } })).toEqual({ tool: null, summary: null })
    expect(activityFromStreamEvent({ type: 'assistant', message: { content: [{ type: 'text', text: 'Here is' }] } })).toEqual({ tool: 'Reply', summary: null })
  })

  it('the backend-injected frames and junk say nothing', () => {
    expect(activityFromStreamEvent({ type: 'stream_end' })).toBeNull()
    expect(activityFromStreamEvent({ type: 'error', message: 'x' })).toBeNull()
    expect(activityFromStreamEvent({ type: 'result', subtype: 'success' })).toBeNull()
    expect(activityFromStreamEvent(null)).toBeNull()
    expect(activityFromStreamEvent({ type: 'assistant', message: { content: 'not an array' } })).toBeNull()
  })

  it('the Chat tab label is composed from the same facts (one vocabulary)', () => {
    expect(getStatusFromStreamEvent(toolUse('Read', { file_path: '/a/b/c.py' }))).toBe('Reading .../b/c.py...')
    expect(getStatusFromStreamEvent(toolUse('mcp__github__search', { q: 'x' }))).toBe('Using github...')
    expect(getStatusFromStreamEvent({ type: 'assistant', message: { content: [{ type: 'text', text: 'hi' }] } })).toBe('Responding...')
    expect(getStatusFromStreamEvent({ type: 'user', message: { content: [{ type: 'tool_result' }] } })).toBe('Processing results...')
    expect(getStatusFromStreamEvent({ type: 'init' })).toBe('Starting session...')
    expect(getStatusFromStreamEvent({ type: 'result' })).toBeNull()
  })
})

describe('summariseToolInput — parity with the agent server', () => {
  for (const c of FIXTURE.cases) {
    it(`${c.tool}: ${JSON.stringify(c.input).slice(0, 50)}`, () => {
      expect(summariseToolInput(c.tool, c.input)).toBe(c.summary)
    })
  }
  it('display names follow the agent server too', () => {
    expect(displayToolName('mcp__trinity__chat_with_agent', {})).toBe('mcp:trinity')
    expect(displayToolName('Task', { subagent_type: 'explore' })).toBe('Task:explore')
    expect(displayToolName('Task', {})).toBe('Task')
    expect(displayToolName('Read', {})).toBe('Read')
    expect(displayToolName('', {})).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Motion rules
// ---------------------------------------------------------------------------
describe('createActivityLineQueue — a burst becomes a calm sequence (AC #2/#4)', () => {
  function clock(start = 0) {
    let t = start
    return { now: () => t, advance: (ms) => { t += ms } }
  }

  it('the first line shows at once; a follower waits out the minimum', () => {
    const c = clock()
    const q = createActivityLineQueue({ minDisplayMs: 700, now: c.now })
    expect(q.offer('Reading a')).toEqual({ text: 'Reading a', key: 1 })
    c.advance(100)
    expect(q.offer('Reading b')).toEqual({ text: 'Reading a', key: 1 })   // held
    expect(q.pending).toBe('Reading b')
    c.advance(599)
    expect(q.tick()).toEqual({ text: 'Reading a', key: 1 })              // 699 ms: still held
    c.advance(1)
    expect(q.tick()).toEqual({ text: 'Reading b', key: 2 })              // 700 ms: promoted
  })

  it('a burst collapses to its latest member — the person wants NOW, not a replay', () => {
    const c = clock()
    const q = createActivityLineQueue({ minDisplayMs: 700, now: c.now })
    q.offer('a'); c.advance(10)
    q.offer('b'); q.offer('c'); q.offer('d')
    expect(q.pending).toBe('d')
    c.advance(700)
    expect(q.tick().text).toBe('d')
    expect(q.tick().text).toBe('d')                                      // nothing left to promote
  })

  it('an identical line never re-keys (no re-animation) and cancels a pending change', () => {
    const c = clock()
    const q = createActivityLineQueue({ minDisplayMs: 700, now: c.now })
    const first = q.offer('a')
    c.advance(10); q.offer('b')
    expect(q.offer('a')).toBe(first)                                     // same object, same key
    expect(q.pending).toBeNull()
    c.advance(1000)
    expect(q.tick()).toBe(first)
  })

  it('a quiet run keeps its last line; clear() empties for terminal', () => {
    const c = clock()
    const q = createActivityLineQueue({ minDisplayMs: 700, now: c.now })
    q.offer('Running pytest'); c.advance(60_000)
    expect(q.tick().text).toBe('Running pytest')
    q.clear()
    expect(q.current).toBeNull()
    expect(q.tick()).toBeNull()
  })

  it('offering null after the minimum clears the row (the feed went silent)', () => {
    const c = clock()
    const q = createActivityLineQueue({ minDisplayMs: 700, now: c.now })
    q.offer('a'); c.advance(800)
    expect(q.offer(null)).toBeNull()
  })

  it('the constants are what the issue asked for', () => {
    expect(ACTIVITY_MIN_DISPLAY_MS).toBeGreaterThanOrEqual(500)
    expect(ACTIVITY_POLL_MS).toBeLessThanOrEqual(5000)
    expect(ACTIVITY_MAX_AGE_S).toBe(30)
  })
})

// ---------------------------------------------------------------------------
// The two-feed resolver
// ---------------------------------------------------------------------------
describe('resolveActivityText — own turn streams, everything else reads the heartbeat (AC #3)', () => {
  it('the stream wins while it has facts; the Work read otherwise', () => {
    expect(resolveActivityText({ live: true, streamActivity: { tool: 'Read', summary: 'a/b' }, activity: { tool: 'Bash', summary: 'ls' } })).toBe('Reading a/b')
    expect(resolveActivityText({ live: true, streamActivity: null, activity: { tool: 'Bash', summary: 'ls' } })).toBe('Running ls')
    expect(resolveActivityText({ live: true, streamActivity: null, activity: { tool: null } })).toBe('Thinking')
  })
  it('no signal is no line — never invented; terminal is no line', () => {
    expect(resolveActivityText({ live: true })).toBeNull()
    expect(resolveActivityText({ live: false, streamActivity: { tool: 'Read', summary: 'a' } })).toBeNull()
  })
  it('a heartbeat line the agent stopped renewing is dropped (never stuck)', () => {
    const now = 1_000_000
    const fresh = { tool: 'Bash', summary: 'sleep', age_seconds: 4, fetchedAtMs: now - 2000 }
    expect(activityAgeSeconds(fresh, now)).toBe(6)
    expect(resolveActivityText({ live: true, activity: fresh, nowMs: now })).toBe('Running sleep')
    const dead = { tool: 'Bash', summary: 'sleep', age_seconds: 4, fetchedAtMs: now - 27_000 }
    expect(activityAgeSeconds(dead, now)).toBe(31)
    expect(resolveActivityText({ live: true, activity: dead, nowMs: now })).toBeNull()
    expect(activityAgeSeconds({ tool: 'x' }, now)).toBe(0)               // no provenance: shown, not dropped
  })
})

// ---------------------------------------------------------------------------
// Placement guards (no mount harness in this suite)
// ---------------------------------------------------------------------------
describe('placement (source guards)', () => {
  const CARD = src('../../src/components/portal/PortalWorkCard.vue')
  const CONV = src('../../src/components/portal/PortalConversation.vue')
  const WORK = src('../../src/components/portal/PortalWork.vue')
  const ROOM = src('../../src/components/portal/PortalRoom.vue')
  const STORE = src('../../src/stores/portalWork.js')
  const PORTAL_STORE = src('../../src/stores/clientPortal.js')
  const SESSION = src('../../src/composables/useSessionActivity.js')

  it('the card reserves one fixed-height row for the whole live life, keyed on the queue key', () => {
    expect(CARD).toContain('<div v-if="live" class="mt-1.5 relative h-4 overflow-hidden" data-testid="portal-work-activity"')
    expect(CARD).toContain('<Transition name="portal-activity">')
    expect(CARD).toContain(':key="shownStep.key"')
    expect(CARD).toContain('createActivityLineQueue()')
    expect(CARD).toContain('prefers-reduced-motion: reduce')
    expect(CARD).not.toContain('<p v-if="live && liveStep"')             // the shifting row is gone
  })

  it('the conversation reads the REAL frame shape and resolves both feeds', () => {
    expect(CONV).toContain('const next = activityFromStreamEvent(evt)')
    expect(CONV).not.toContain("evt.type === 'tool_use' || evt.tool_name")
    expect(CONV).toContain('streamActivity: streaming.value ? liveStreamActivity.value : null')
    expect(CONV).toContain('activity: workStore.activityFor(liveCardItem.value)')
  })

  it('the own send re-pins only while following (AC #5, #2624)', () => {
    const block = CONV.slice(CONV.indexOf('watch(sending, async (isSending)'))
    expect(block).toContain('await nextTick()')
    expect(block).toContain('if (following.value) await pinToBottom()')
    expect(CONV).toContain('  following,\n  unread: unreadBelow,')
  })

  it('the Work tab and the room pass the line to every live card', () => {
    expect(WORK.split(':live-step="stepOf(it)"').length - 1).toBe(2)     // grouped + flat forms
    expect(ROOM).toContain(':live-step="stepOf(it)"')
    expect(WORK).toContain('store.activityFor(item)')
    expect(ROOM).toContain('workStore.activityFor(it)')
  })

  it('the store polls the cheap read only while live and clears it when nothing is', () => {
    expect(STORE).toContain('_activityTimer = setInterval(() => { refreshActivity() }, ACTIVITY_POLL_MS)')
    const stop = STORE.slice(STORE.indexOf('function stopPolling()'), STORE.indexOf('async function refreshActivity'))
    expect(stop).toContain('activity.value = {}')
    expect(PORTAL_STORE).toContain("portalHttp.get('/api/enterprise/client-portal/work/activity'")
  })

  it('Agent Detail composes from the same vocabulary', () => {
    expect(SESSION).toContain("import { activityLine } from '../utils/workActivity'")
    expect(SESSION).toContain('activityLine({ tool: active.name, summary: active.input_summary })')
  })
})
