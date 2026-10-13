// @vitest-environment jsdom
/**
 * An ask's fenced code block was unreadable in the LIGHT theme (Andrii's
 * sign-off, 2026-10-01, the Inbox pane): typography draws `<pre>` dark with
 * light text, and `prose-code:bg-gray-100` — the inline-code pill — landed on
 * the `<code>` INSIDE that `<pre>` too, so the block read light-on-light.
 * The pill is for inline code only; a block's code sits on the block.
 *
 * Mounted; jsdom does not compute Tailwind, so the root's class list is the
 * assertion (the colours are measured live in the round-2 sign-off notes).
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AskMarkdown from '@/components/operator/AskMarkdown.vue'

describe('an ask with a code block', () => {
  it('renders the block, and the inline-code pill does not reach code inside it', () => {
    const w = mount(AskMarkdown, { props: { text: "Run this:\n\n```python\nprint('hello from the agent')\n```\n\nand `inline` too." } })
    expect(w.find('pre code').text()).toContain("print('hello from the agent')")
    const cls = w.classes()
    expect(cls).toContain('[&_pre_code]:bg-transparent')
    expect(cls).toContain('[&_pre_code]:p-0')
    expect(cls).toContain('prose-code:bg-gray-100')          // inline code keeps its pill
  })
})
