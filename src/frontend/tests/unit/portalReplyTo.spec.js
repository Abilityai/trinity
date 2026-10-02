// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — sign-off round 8: reply to one message.
 *
 * The Inbox pane's arrow opens the chat with a Codex-style "replying to" chip
 * above the composer, instead of pasting "> message" into the text. The send
 * carries only the message ID (`reply_to_message_id`); the server quotes the
 * stored row into the agent's prompt (tests/unit/test_ent610_reply_to_message.py).
 *
 * @source-text-pin: the chat's chip placement and its send wiring live in
 * PortalConversation, whose mount needs the whole chat stack; the chip, the
 * store payloads and the pane's arrow are proven mounted/behaviourally here.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalReplyChip from '@/components/portal/PortalReplyChip.vue'

const PORTAL = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'components', 'portal')
const src = (rel) => readFileSync(join(PORTAL, rel), 'utf8')

beforeEach(() => { setActivePinia(createPinia()); axios.post.mockReset() })

describe('the chip', () => {
  it('shows what you are replying to and can be removed', async () => {
    const w = mount(PortalReplyChip, { props: { excerpt: 'Heads up: the API returned 429 twice.' } })
    expect(w.text()).toContain('Heads up: the API returned 429 twice.')
    await w.find('[data-testid="portal-reply-chip-remove"]').trigger('click')
    expect(w.emitted('remove')).toHaveLength(1)
  })
  it('the sent form has no remove control', () => {
    const w = mount(PortalReplyChip, { props: { excerpt: 'x', removable: false } })
    expect(w.find('[data-testid="portal-reply-chip-remove"]').exists()).toBe(false)
  })
})

describe('both send paths carry the id', () => {
  it('streaming and sync each post reply_to_message_id (and null without one)', async () => {
    axios.post.mockResolvedValue({ data: {} })
    const store = useClientPortalStore()
    await store.startPortalChat('scout', 'hi', 's1', { replyToMessageId: 'm9' })
    await store.sendPortalChat('scout', 'hi', 's1', { replyToMessageId: 'm9' })
    await store.startPortalChat('scout', 'hi', 's1')
    const bodies = axios.post.mock.calls.map((c) => c[1])
    expect(bodies[0].reply_to_message_id).toBe('m9')
    expect(bodies[1].reply_to_message_id).toBe('m9')
    expect(bodies[2].reply_to_message_id).toBeNull()
  })
})

describe('wiring', () => {
  it('the pane arrow hands over the reply target, not quoted text', () => {
    const pane = src('PortalInboxPane.vue')
    expect(pane).toMatch(/\$emit\('reply', messageReplyTarget\(item, m\), \{ sessionId: item\.id, messageId: m\.id, excerpt: replyExcerpt\(m\.content\) \}\)/)
    expect(pane).not.toMatch(/quoteForReply/)
  })
  it('the chat shows the chip above the composer and sends the id with the turn', () => {
    const conv = src('PortalConversation.vue')
    expect(conv).toMatch(/<PortalReplyChip[\s\S]{0,200}v-if="replyTo"/)
    expect(conv).toMatch(/replyToMessageId: replyId/)
    expect((conv.match(/replyToMessageId: replyId/g) || []).length).toBe(2)
  })
})
