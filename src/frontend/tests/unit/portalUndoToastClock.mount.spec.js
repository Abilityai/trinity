// @vitest-environment jsdom
/**
 * ent#841 (#3358) — the Undo toast's clock belongs to the CLOSE, not to the
 * message text.
 *
 * Every close shows the same words ("Chat archived"), so a timer keyed on the
 * message never restarts for a second close: chat B's Undo vanished on chat
 * A's timer, as little as a moment after it appeared. The toast restarts on
 * `closeKey`, which the Workspace changes once per completed close.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalUndoToast from '@/components/portal/PortalUndoToast.vue'

let w
beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { w?.unmount(); w = null; vi.useRealTimers(); document.body.innerHTML = '' })
const mk = (props) => (w = mount(PortalUndoToast, { props, attachTo: document.body }))
const dismissed = () => (w.emitted('dismiss') || []).length

describe('ent#841 Undo toast clock', () => {
  it('a single close dismisses at 8s, not before', () => {
    mk({ message: 'Chat archived', closeKey: 1 })
    expect(w.find('[data-testid="portal-undo-toast"]').exists()).toBe(true)
    vi.advanceTimersByTime(7999)
    expect(dismissed()).toBe(0)
    vi.advanceTimersByTime(1)
    expect(dismissed()).toBe(1)
  })

  it('a second close with the SAME message restarts the clock', async () => {
    mk({ message: 'Chat archived', closeKey: 1 })   // close A
    vi.advanceTimersByTime(7000)
    await w.setProps({ closeKey: 2 })               // close B — same words
    vi.advanceTimersByTime(1500)                    // 8.5s after A, 1.5s after B
    expect(dismissed()).toBe(0)
    vi.advanceTimersByTime(6499)
    expect(dismissed()).toBe(0)
    vi.advanceTimersByTime(1)                       // B's own full 8s
    expect(dismissed()).toBe(1)
  })

  it('clearing the message cancels the timer', async () => {
    mk({ message: 'Chat archived', closeKey: 1 })
    vi.advanceTimersByTime(3000)
    await w.setProps({ message: '' })
    expect(w.find('[data-testid="portal-undo-toast"]').exists()).toBe(false)
    vi.advanceTimersByTime(20000)
    expect(dismissed()).toBe(0)
  })
})
