/**
 * ent#523 / ent#524 — agents at the centre, and files onto the conversation.
 *
 * There is no component-mount harness in this project (vitest runs
 * `environment: 'node'`), which is why every decidable rule here lives in a
 * plain `.js` module and is tested directly — the ent#392 precedent. The parts
 * no unit test can reach (which component mounts what, which prop is passed)
 * are source-structure guards, comments stripped first so prose about a rule is
 * not scanned as code.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

import {
  agentChatTabs,
  landingThread,
  orderRosterAgents,
  agentPreview,
  composerAvailabilityNotice,
  resolveAgentLanding,
  MAIN_TAB_LABEL,
} from '@/components/portal/portalUtils'
import {
  isFileDrag,
  rejectionFor,
  uploadFailureReason,
  rateLimitMessage,
  attachmentState,
  MAX_UPLOAD_BYTES,
} from '@/composables/usePortalFileDrop'

const read = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))
const CONVERSATION = read('../../src/components/portal/PortalConversation.vue')
const ROOM = read('../../src/components/portal/PortalRoom.vue')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')
const RAIL_FILES = read('../../src/components/portal/PortalRailFiles.vue')
const PORTAL = read('../../src/views/Portal.vue')
const BAND = read('../../src/components/portal/PortalAgentBand.vue')
const DETAILS = read('../../src/components/portal/PortalAgentDetails.vue')

const main = (over = {}) => ({ id: 'main', agent_name: 'a', is_main: true, ...over })
const chat = (id, over = {}) => ({ id, agent_name: 'a', ...over })

// ---------------------------------------------------------------------------
// The tab strip
// ---------------------------------------------------------------------------

describe('ent#523 — Main is pinned, archives are not tabs', () => {
  it('puts Main first even when it is the least recently active', () => {
    const tabs = agentChatTabs([
      chat('c1', { last_message_at: '2026-09-07T12:00:00Z' }),
      main({ created_at: '2026-01-01T00:00:00Z' }),
    ], 'a')
    expect(tabs.map((t) => t.id)).toEqual(['main', 'c1'])
  })

  it('names Main by its ROLE, never by a title', () => {
    // Main is the same thread for the life of the pair. A title derived from
    // whatever was said in it first would make the pinned tab wander.
    const [tab] = agentChatTabs([main({ title: 'Some old topic' })], 'a')
    expect(tab.label).toBe(MAIN_TAB_LABEL)
  })

  it('leaves archived chats out of the strip', () => {
    // Reset would otherwise add one permanent tab per use, pushing the live
    // chats under "N more" to make room for retired ones.
    const tabs = agentChatTabs([
      main(),
      chat('old', { archived_at: '2026-09-07T10:00:00Z', last_message_at: '2026-09-07T10:00:00Z' }),
    ], 'a')
    expect(tabs.map((t) => t.id)).toEqual(['main'])
  })

  it('still sorts the rest by recency', () => {
    const tabs = agentChatTabs([
      chat('old', { last_message_at: '2026-09-01T00:00:00Z' }),
      chat('new', { last_message_at: '2026-09-07T00:00:00Z' }),
      main(),
    ], 'a')
    expect(tabs.map((t) => t.id)).toEqual(['main', 'new', 'old'])
  })
})

// ---------------------------------------------------------------------------
// Which chat you land in
// ---------------------------------------------------------------------------

describe('ent#523 — landing', () => {
  it('opens the chat you were most recently active in', () => {
    const landed = landingThread([
      main(),
      chat('c1', { last_message_at: '2026-09-07T09:00:00Z' }),
      chat('c2', { last_message_at: '2026-09-07T12:00:00Z' }),
    ], 'a')
    expect(landed.id).toBe('c2')
  })

  it('falls back to Main for a first-time visitor', () => {
    // An unused Main has no `last_message_at` and would sort last on recency
    // alone, so a first visit would otherwise land on nothing.
    expect(landingThread([main()], 'a').id).toBe('main')
  })

  it('never lands you in an archived chat', () => {
    const landed = landingThread([
      main(),
      chat('old', { archived_at: 'x', last_message_at: '2026-09-07T23:00:00Z' }),
    ], 'a')
    expect(landed.id).toBe('main')
  })

  it('returns null when there is nothing for this agent', () => {
    expect(landingThread([chat('other', { agent_name: 'b' })], 'a')).toBeNull()
    expect(landingThread([], 'a')).toBeNull()
    expect(landingThread([main()], '')).toBeNull()
  })

  it('is the SAME rule the ?agent= deep link uses', () => {
    // Two answers to "which chat do I land in" is how a deep link and a
    // sidebar click put a first-time visitor in different places.
    const threads = [main()]
    const agents = [{ name: 'a' }]
    expect(resolveAgentLanding({ agent: 'a', agents, threads }).sessionId).toBe('main')
  })
})

// ---------------------------------------------------------------------------
// The sidebar
// ---------------------------------------------------------------------------

describe('ent#523 — roster order and preview', () => {
  it('orders by most recent collaboration, then name', () => {
    const agents = [{ name: 'zeta' }, { name: 'alpha' }, { name: 'beta' }]
    const threads = [
      { agent_name: 'beta', last_message_at: '2026-09-07T12:00:00Z' },
      { agent_name: 'zeta', last_message_at: '2026-09-01T12:00:00Z' },
    ]
    expect(orderRosterAgents(agents, threads).map((a) => a.name))
      .toEqual(['beta', 'zeta', 'alpha'])
  })

  it('honours the primary companion first — the ent#491 seam', () => {
    const agents = [{ name: 'alpha' }, { name: 'beta' }]
    const threads = [{ agent_name: 'alpha', last_message_at: '2026-09-07T12:00:00Z' }]
    expect(orderRosterAgents(agents, threads, 'beta').map((a) => a.name))
      .toEqual(['beta', 'alpha'])
  })

  it('does not mutate the roster it was handed', () => {
    const agents = [{ name: 'zeta' }, { name: 'alpha' }]
    orderRosterAgents(agents, [])
    expect(agents.map((a) => a.name)).toEqual(['zeta', 'alpha'])
  })

  it('previews the newest chat, and nothing when there is no history', () => {
    expect(agentPreview([
      { agent_name: 'a', title: 'Older', last_message_at: '2026-09-01T00:00:00Z' },
      { agent_name: 'a', title: 'Newest', last_message_at: '2026-09-07T00:00:00Z' },
    ], 'a')).toBe('Newest')
    expect(agentPreview([main()], 'a')).toBeNull()
    expect(agentPreview([], 'a')).toBeNull()
  })

  it('shows no preview for an untitled chat rather than the words "New chat"', () => {
    // A row reading "New chat" under every agent is noise, not a preview.
    expect(agentPreview([{ agent_name: 'a', last_message_at: '2026-09-07T00:00:00Z' }], 'a'))
      .toBeNull()
  })

  it('orders the roster BEFORE the collapse', () => {
    // Bounding first would sort a slice chosen by the old order — the same bug
    // one step later.
    expect(SIDEBAR).toMatch(/const orderedRoster = computed\(\(\) => orderRosterAgents\(/)
    expect(SIDEBAR).toMatch(/visibleAgentRows\(orderedRoster\.value/)
  })

  it('does not list an unused Main as a recent chat', () => {
    // It exists for every pair the moment the agent is opened, so listing it
    // would put a row under every agent the person never talked to. Filtered
    // for the SIDEBAR only — the tab strip must show Main from the first visit.
    expect(PORTAL).toMatch(/const sidebarThreads = computed\(/)
    expect(PORTAL).toMatch(/t\.is_main && !t\.last_message_at/)
    expect(PORTAL).toMatch(/:threads="sidebarThreads"/)
    // The conversation still gets the full list.
    expect(PORTAL).toMatch(/<PortalConversation[\s\S]{0,600}:threads="threads"/)
  })
})

// ---------------------------------------------------------------------------
// Honest composer
// ---------------------------------------------------------------------------

describe('ent#523 — the composer says what will happen', () => {
  it('speaks up for a stopped or unavailable agent', () => {
    expect(composerAvailabilityNotice({ availability: 'stopped', owner: 'sam' }).state).toBe('stopped')
    expect(composerAvailabilityNotice({ availability: 'unavailable' })).toBeTruthy()
  })

  it('says nothing for ready or unknown', () => {
    // `unknown` is not evidence of anything — #2196's fail-open direction.
    expect(composerAvailabilityNotice({ availability: 'ready' })).toBeNull()
    expect(composerAvailabilityNotice({ availability: 'unknown' })).toBeNull()
    expect(composerAvailabilityNotice(null)).toBeNull()
  })

  it('names who to ask', () => {
    expect(composerAvailabilityNotice({ availability: 'stopped', owner: 'sam' }).message)
      .toContain('sam')
  })

  it('labels, never disables', () => {
    // Disabling relocates the dead state rather than removing it: a client
    // whose agents are all stopped would get an entirely inert Workspace.
    const at = CONVERSATION.indexOf('portal-availability-notice')
    expect(at).toBeGreaterThan(-1)
    expect(CONVERSATION).not.toMatch(/:disabled="[^"]*availabilityNotice/)
  })
})

// ---------------------------------------------------------------------------
// ent#524 — drop and batch
// ---------------------------------------------------------------------------

describe('ent#524 — a drag is only a file drag when it carries files', () => {
  it('accepts a file drag', () => {
    expect(isFileDrag({ types: ['Files'] })).toBe(true)
    // DOMStringList in some browsers, array in others.
    expect(isFileDrag({ types: { length: 1, 0: 'Files', [Symbol.iterator]: Array.prototype[Symbol.iterator] } })).toBe(true)
  })

  it('rejects dragged text, links and nothing at all', () => {
    expect(isFileDrag({ types: ['text/plain'] })).toBe(false)
    expect(isFileDrag({ types: ['text/uri-list'] })).toBe(false)
    expect(isFileDrag({})).toBe(false)
    expect(isFileDrag(null)).toBe(false)
  })
})

describe('ent#524 — a refused file names itself and the limit', () => {
  it('names the size limit', () => {
    const msg = rejectionFor({ name: 'big.zip', size: MAX_UPLOAD_BYTES + 1 })
    expect(msg).toMatch(/limit/)
    expect(msg).toMatch(/MB/)
  })

  it('refuses an empty file', () => {
    expect(rejectionFor({ name: 'empty.txt', size: 0 })).toMatch(/empty/i)
  })

  it('passes a normal file', () => {
    expect(rejectionFor({ name: 'notes.md', size: 1024 })).toBeNull()
  })
})

describe('ent#524 — a failure explains itself', () => {
  it('renders the server sentence when there is one', () => {
    expect(uploadFailureReason({ response: { status: 400, data: { detail: 'Unsupported type.' } } }))
      .toBe('Unsupported type.')
  })

  it('says when to retry a rate-limited batch', () => {
    const err = { response: { status: 429, headers: { 'retry-after': '30' } } }
    expect(uploadFailureReason(err)).toMatch(/30s/)
    expect(rateLimitMessage({ response: { headers: { 'retry-after': '120' } } })).toMatch(/2 min/)
  })

  it('degrades to a sentence, never to [object Object]', () => {
    expect(uploadFailureReason({})).toMatch(/upload/i)
    expect(uploadFailureReason({ response: { status: 400, data: { detail: { code: 'x', message: 'Named.' } } } }))
      .toBe('Named.')
  })
})

describe('ent#524 — one chip state, three outcomes', () => {
  it('reads failed before uploading, so a rejected file never spins', () => {
    expect(attachmentState({ uploading: true, error: 'Too large' })).toBe('failed')
    expect(attachmentState({ uploading: true, error: '' })).toBe('uploading')
    expect(attachmentState({ uploading: false, error: '', done: true })).toBe('sent')
  })
})

describe('ent#524 — one implementation, three consumers', () => {
  it('every surface reads the shared module rather than its own copy', () => {
    for (const [name, src] of [['conversation', CONVERSATION], ['room', ROOM], ['rail files', RAIL_FILES]]) {
      expect(src, name).toMatch(/from '@\/composables\/usePortalFileDrop'/)
    }
  })

  it('no surface reads only the first file any more', () => {
    // The defect in the issue: both existing paths took `[0]` and reported
    // success, so four of five dropped files vanished with no notice.
    for (const [name, src] of [['conversation', CONVERSATION], ['rail files', RAIL_FILES]]) {
      expect(src, name).not.toMatch(/files\?\.\[0\]/)
      expect(src, name).not.toMatch(/dataTransfer\?\.files\?\.\[0\]/)
    }
  })

  it('both pickers accept several files', () => {
    expect(CONVERSATION).toMatch(/<input[^>]*type="file"[^>]*multiple/)
    expect(RAIL_FILES).toMatch(/<input[^>]*type="file"[^>]*multiple/)
  })

  it('the conversation and the room are drop targets with an affordance', () => {
    for (const [name, src, testid] of [
      ['conversation', CONVERSATION, 'portal-drop-overlay'],
      ['room', ROOM, 'portal-room-drop-overlay'],
    ]) {
      expect(src, name).toMatch(/@drop="dropHandlers\.onDrop"/)
      expect(src, name).toContain(testid)
      // The overlay must not swallow the drop it announces.
      expect(src, name).toMatch(new RegExp(`${testid}[\\s\\S]{0,400}`))
      const at = src.indexOf(testid)
      expect(src.slice(Math.max(0, at - 400), at), name).toContain('pointer-events-none')
    }
  })

  it('a room drop fans out to every participating agent and names them', () => {
    // Operator decision 13: a room is one conversation, so its files should
    // match its transcript.
    expect(ROOM).toMatch(/for \(const name of names\) await store\.uploadDocument\(name, file\)/)
    expect(ROOM).toMatch(/recipientLabel/)
  })

  it('uploads sequentially, so a batch does not trip the per-email limiter', () => {
    const SHARED = read('../../src/composables/usePortalFileDrop.js')
    expect(SHARED).not.toContain('Promise.all')
    expect(SHARED).toMatch(/for \(const \{ file, entry, rejection \} of mine\)/)
  })
})

// ---------------------------------------------------------------------------
// The dismantle — no capability lost
// ---------------------------------------------------------------------------

describe('ent#523 — the agent page was dismantled, not dropped', () => {
  it('the stats and the chart are in the always-visible band', () => {
    expect(BAND).toContain('StackedBarChart')
    expect(BAND).toMatch(/tasks · last/)
    expect(BAND).toMatch(/completed/)
    expect(BAND).toMatch(/first try/)
  })

  it('chats, what it can do and reports are in Agent details', () => {
    expect(DETAILS).toMatch(/>Your chats</)
    expect(DETAILS).toMatch(/>What it can do</)
    expect(DETAILS).toMatch(/>Reports</)
  })

  it('Canvas and Files are NOT duplicated into details — they are rail tabs', () => {
    // Two homes for one capability is what the dismantle removed.
    expect(DETAILS).not.toContain('CanvasPanel')
    expect(DETAILS).not.toMatch(/>Files</)
  })

  it('the "Start a chat" button and its handler are gone', () => {
    expect(PORTAL).not.toContain('onStartChatFromPage')
    expect(PORTAL).not.toContain('start-chat')
  })

  it('Main is not renameable, and says so by rendering a label not an editor', () => {
    const at = CONVERSATION.indexOf('v-if="isMainChat"')
    expect(at).toBeGreaterThan(-1)
    expect(CONVERSATION.slice(at, at + 200)).toContain('MAIN_TAB_LABEL')
  })

  it('Reset is offered on Main only, with no confirmation dialog', () => {
    // Operator, 2026-09-06: "we are not losing info". ConfirmDialog is not a
    // caller here.
    expect(CONVERSATION).toMatch(/v-if="isMainChat"[\s\S]{0,600}portal-reset-main/)
    expect(CONVERSATION).not.toContain('ConfirmDialog')
  })
})
