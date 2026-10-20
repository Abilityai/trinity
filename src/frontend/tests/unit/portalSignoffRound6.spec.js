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
import { replyExcerpt, messageReplyTarget } from '@/components/portal/portalInbox'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'
import {
  useConversationAnchor, ANCHOR_HIGHLIGHT_CLASSES, ANCHOR_HIGHLIGHT_MS,
} from '@/composables/useConversationAnchor'

afterEach(() => { vi.useRealTimers() })

describe('the anchor highlight is a soft glow, not a frame (round 8: wider, eased)', () => {
  it('is one animation class, drawing no ring or border', () => {
    expect(ANCHOR_HIGHLIGHT_CLASSES).toEqual(['anchor-glow'])
  })

  it('the glow reaches past the message and eases in and out, from the token', () => {
    const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'style.css'), 'utf8')
    const block = css.slice(css.indexOf('@keyframes anchor-glow'))
    expect(block).toMatch(/box-shadow:\s*0 0 0 (1[0-9]|2[0-4])px var\(--anchor-tint\)/)   // spread: a wider area, no layout shift
    expect(css).toMatch(/\.anchor-glow\s*\{[^}]*--anchor-tint:\s*theme\('colors\.action-primary/)
    expect(css).toMatch(/\.dark \.anchor-glow\s*\{[^}]*--anchor-tint:\s*theme\('colors\.action-primary/)
    expect(css).toMatch(/prefers-reduced-motion: reduce\)[\s\S]{0,200}\.anchor-glow\s*\{[^}]*animation:\s*none/)
  })

  it('the class lands, then leaves once the animation has run', async () => {
    vi.useFakeTimers()
    const el = document.createElement('div')
    el.dataset.messageId = 'm1'
    const box = document.createElement('div')
    box.appendChild(el)
    const scope = effectScope()
    const api = scope.run(() => useConversationAnchor({
      scrollEl: ref(box), detach: () => {}, pinToBottom: async () => {},
      route: { query: { anchor: 'm:m1' }, path: '/x', hash: '' }, router: { replace: async () => {} },
    }))
    await api.afterHistory()
    expect(el.classList.contains('anchor-glow')).toBe(true)
    vi.advanceTimersByTime(ANCHOR_HIGHLIGHT_MS - 1)
    expect(el.classList.contains('anchor-glow')).toBe(true)
    vi.advanceTimersByTime(1)
    expect(el.classList.contains('anchor-glow')).toBe(false)
    scope.stop()
  })
})

describe('reply to one message from the Inbox pane', () => {
  it('replyExcerpt: the first paragraph as one plain line, capped (round 8: a chip, not "> " text)', () => {
    expect(replyExcerpt('Heads up: the API returned 429 twice.')).toBe('Heads up: the API returned 429 twice.')
    expect(replyExcerpt('It has **bold**, `code` and a [link](https://x.y).\n\nSecond para')).toBe('It has bold, code and a link.')
    const long = 'word '.repeat(80).trim()
    const e = replyExcerpt(long)
    expect(e.length).toBeLessThanOrEqual(160)
    expect(e.endsWith('…')).toBe(true)
    expect(replyExcerpt('')).toBe('')
    expect(replyExcerpt('```python\nprint(1)\n```')).toBe('print(1)')
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
