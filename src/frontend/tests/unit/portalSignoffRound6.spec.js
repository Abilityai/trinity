// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — manual sign-off, round 6.
 *
 * "Open in chat" landed on its message and drew a 2px blue FRAME round the
 * whole row. It now tints the row softly and fades the tint out — a highlight
 * of the message, not a box around it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { effectScope, ref } from 'vue'
import { mount } from '@vue/test-utils'
import PortalAgentBubble from '@/components/portal/PortalAgentBubble.vue'
import { quoteForReply, messageReplyTarget } from '@/components/portal/portalInbox'
import {
  useConversationAnchor, ANCHOR_HIGHLIGHT_CLASSES, ANCHOR_FADE_CLASSES, ANCHOR_HIGHLIGHT_MS, ANCHOR_FADE_MS,
} from '@/composables/useConversationAnchor'

afterEach(() => { vi.useRealTimers() })

describe('the anchor highlight is a fading tint, not a frame', () => {
  it('draws no ring or border', () => {
    for (const c of ANCHOR_HIGHLIGHT_CLASSES) expect(c).not.toMatch(/^(dark:)?(ring|border|outline)/)
    expect(ANCHOR_HIGHLIGHT_CLASSES).toContain('bg-action-primary-50')
  })

  it('the tint lands, then leaves while the fade transition is still on, then the fade goes', async () => {
    vi.useFakeTimers()
    const el = document.createElement('div')
    el.dataset.messageId = 'm1'
    const box = document.createElement('div')
    box.appendChild(el)
    el.scrollIntoView = () => {}
    const scope = effectScope()
    const api = scope.run(() => useConversationAnchor({
      scrollEl: ref(box), detach: () => {}, pinToBottom: async () => {},
      route: { query: { anchor: 'm:m1' }, path: '/x', hash: '' }, router: { replace: async () => {} },
    }))
    await api.afterHistory()
    for (const c of [...ANCHOR_HIGHLIGHT_CLASSES, ...ANCHOR_FADE_CLASSES]) expect(el.classList.contains(c)).toBe(true)
    vi.advanceTimersByTime(ANCHOR_HIGHLIGHT_MS)
    for (const c of ANCHOR_HIGHLIGHT_CLASSES) expect(el.classList.contains(c)).toBe(false)
    for (const c of ANCHOR_FADE_CLASSES) expect(el.classList.contains(c)).toBe(true)
    vi.advanceTimersByTime(ANCHOR_FADE_MS)
    for (const c of ANCHOR_FADE_CLASSES) expect(el.classList.contains(c)).toBe(false)
    scope.stop()
  })
})

describe('reply to one message from the Inbox pane', () => {
  it('quoteForReply: the first paragraph as plain text, quoted, then a blank line', () => {
    expect(quoteForReply('Heads up: the API returned 429 twice.')).toBe('> Heads up: the API returned 429 twice.\n\n')
    expect(quoteForReply('It has **bold**, `code` and a [link](https://x.y).\n\nSecond para'))
      .toBe('> It has bold, code and a link.\n\n')
    const long = 'word '.repeat(80).trim()
    const q = quoteForReply(long)
    expect(q.startsWith('> ')).toBe(true)
    expect(q.length).toBeLessThanOrEqual(2 + 160 + 1 + 2)
    expect(q.trimEnd().endsWith('…')).toBe(true)
    expect(quoteForReply('')).toBe('')
    expect(quoteForReply('```python\nprint(1)\n```')).toBe('> print(1)\n\n')
  })
  it('messageReplyTarget: the chat, anchored at that message', () => {
    expect(messageReplyTarget({ type: 'thread', id: 's 1' }, { id: 'm9' })).toBe('/workspace/c/s%201?anchor=m%3Am9')
    expect(messageReplyTarget({ type: 'thread', id: 's1' }, {})).toBe('/workspace/c/s1')
    expect(messageReplyTarget({ type: 'ask', id: 'a' }, { id: 'm' })).toBeNull()
  })
  it('a bubble can drop Copy; its slot still renders', () => {
    const w = mount(PortalAgentBubble, { props: { content: 'hi', copyable: false }, slots: { default: '<button data-testid="x">→</button>' } })
    expect(w.find('[aria-label="Copy message"]').exists()).toBe(false)
    expect(w.find('[data-testid="x"]').exists()).toBe(true)
    const c = mount(PortalAgentBubble, { props: { content: 'hi' } })
    expect(c.findAll('button').length).toBeGreaterThan(0)
  })
})
