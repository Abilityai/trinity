// @vitest-environment jsdom
/**
 * #2964 — the live chat card keeps one shape from first paint to terminal.
 *
 * The chat's card starts from a synthetic item (`steps: undefined`, no
 * execution id yet); ~2 s later the feed's row arrives and the 202's id makes
 * Stop possible. Before the fix both arrivals INSERTED an element: the steps
 * sentence grew the card by one line, and Stop landed before Open in Work.
 * With `reserveLiveRows` (set by the chat host only) both rows exist from
 * first paint and are patched in place.
 *
 * jsdom has no layout, so this pins STRUCTURE: the ordered rows, their layout
 * classes, and element identity across the transition. The pixel claim
 * (constant height, Open in Work stationary) is the Playwright probe's job —
 * swapping `h-4` for `min-h-4` would still pass here.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalWorkCard from '../../src/components/portal/PortalWorkCard.vue'

const LAYOUT = /^(mt-|h-|min-h-|leading-|flex$|flex-wrap$|truncate$|whitespace-|min-w-|shrink-0$)/

const synthetic = (over = {}) => ({
  id: 'pending', agent_name: 'cornelius', status: 'running', outcome: 'running',
  kind: 'turn', title: 'hi', steps: undefined, ...over,
})

function mountLive(props = {}) {
  return mount(PortalWorkCard, {
    props: { item: synthetic(), reserveLiveRows: true, showOpenInWork: true, canStop: false, ...props },
  })
}

// One entry per rendered child of the card: tag + layout classes, and for the
// actions row its ordered buttons (by label). Visibility is NOT part of it —
// Stop is meant to go from invisible to visible without changing layout.
function signature(w) {
  return [...w.element.children].map((el) => {
    const cls = [...el.classList].filter((c) => LAYOUT.test(c)).sort().join(' ')
    const buttons = [...el.querySelectorAll('button')].map((b) => b.textContent.trim())
    return `${el.tagName.toLowerCase()}[${cls}]${buttons.length ? `{${buttons.join('|')}}` : ''}`
  })
}
const stepsRow = (w) => w.element.querySelector('[data-testid="portal-work-reserved-steps"], [data-testid^="portal-work-steps-"]')
const stopButton = (w) => w.element.querySelector('[data-testid^="portal-work-stop-"]')
const actionLabels = (w) => [...w.element.querySelectorAll('button')].map((b) => b.textContent.trim())

describe('#2964 — the chat card reserves its rows from first paint', () => {
  it('1. pending: the steps row and the Stop slot are reserved, blank and inert', () => {
    const w = mountLive()
    const row = w.find('[data-testid="portal-work-reserved-steps"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('aria-hidden')).toBe('true')
    expect(row.text()).toBe('')
    for (const c of ['mt-1.5', 'h-4', 'leading-4', 'flex', 'min-w-0']) expect(row.classes()).toContain(c)

    const stop = w.find('[data-testid="portal-work-stop-reserved"]')
    expect(stop.exists()).toBe(true)
    expect(stop.classes()).toContain('invisible')
    expect(stop.attributes('disabled')).toBeDefined()
    expect(stop.attributes('aria-hidden')).toBe('true')
    expect(stop.attributes('tabindex')).toBe('-1')
    expect(actionLabels(w)).toEqual(['Stop', 'Open in Work'])
    // Smoke only: VTU's trigger skips a disabled element, so this proves no more than `disabled`.
    stop.trigger('click')
    expect(w.emitted('stop')).toBeUndefined()
  })

  it('2. pending → none swaps the text in place; the shape does not change', async () => {
    const w = mountLive()
    const before = signature(w)
    const row = stepsRow(w)
    await w.setProps({ item: synthetic({ id: 'e1', steps: { state: 'none' } }) })
    expect(signature(w)).toEqual(before)
    expect(stepsRow(w)).toBe(row)
    const now = w.find('[data-testid="portal-work-steps-none"]')
    expect(now.text()).toBe("cornelius doesn't report steps.")
    expect(now.attributes('aria-hidden')).toBeUndefined()
    expect(w.find('[data-testid="portal-work-reserved-steps"]').exists()).toBe(false)
  })

  it('3. pending → unknown swaps in place and says the unknown sentence, not the none one', async () => {
    const w = mountLive()
    const before = signature(w)
    const row = stepsRow(w)
    await w.setProps({ item: synthetic({ id: 'e1', steps: { state: 'unknown' } }) })
    expect(signature(w)).toEqual(before)
    expect(stepsRow(w)).toBe(row)
    const now = w.find('[data-testid="portal-work-steps-unknown"]')
    expect(now.text()).toBe('Steps could not be read right now.')
    expect(now.text()).not.toContain("doesn't report")
  })

  it('3b. a long name truncates; the claim never does', async () => {
    const w = mountLive({ item: synthetic({ agent_name: 'acme-customer-support-scribe', steps: { state: 'none' } }) })
    const who = w.find('[data-testid="portal-work-sentence-who"]')
    const claim = w.find('[data-testid="portal-work-sentence-claim"]')
    expect(who.text()).toBe('acme-customer-support-scribe')
    expect(who.classes()).toContain('truncate')
    expect(claim.element.textContent).toBe(" doesn't report steps.")
    expect(claim.classes()).toContain('shrink-0')
    expect(claim.classes()).not.toContain('truncate')
    await w.setProps({ item: synthetic({ agent_name: null, steps: { state: 'none' } }) })
    expect(w.find('[data-testid="portal-work-sentence-who"]').text()).toBe('This agent')
  })

  it('4. Stop arrives in place: same element, same layout, now visible and usable', async () => {
    const w = mountLive({ item: synthetic({ id: 'e1', steps: { state: 'none' } }) })
    const before = signature(w)
    const btn = stopButton(w)
    await w.setProps({ canStop: true })
    expect(signature(w)).toEqual(before)
    expect(stopButton(w)).toBe(btn)
    const stop = w.find('[data-testid="portal-work-stop-e1"]')
    expect(stop.classes()).not.toContain('invisible')
    expect(stop.attributes('aria-hidden')).toBeUndefined()
    expect(stop.attributes('tabindex')).toBeUndefined()
    expect(stop.attributes('disabled')).toBeUndefined()
    await stop.trigger('click')
    expect(w.emitted('stop')[0][0].id).toBe('e1')
  })

  it('5. pending → stages is the allowed growth: the list replaces the reserved row', async () => {
    const w = mountLive()
    await w.setProps({ item: synthetic({ id: 'e1', steps: { state: 'reported', stages: [{ id: 'a', name: 'Collect', state: 'current' }] } }) })
    expect(w.find('[data-testid="portal-work-stages"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-work-reserved-steps"]').exists()).toBe(false)
    expect(w.find('[data-testid^="portal-work-steps-"]').exists()).toBe(false)
  })
})

describe('#2964 — every other host renders as before (default props)', () => {
  const plain = (props) => mount(PortalWorkCard, { props })

  it('6a. a rail-shaped row: no Stop element, and the sentence is today\'s plain paragraph', () => {
    const w = plain({ item: synthetic({ id: 'r1', steps: { state: 'none' } }), canStop: false, showOpenInWork: true })
    expect(w.find('[data-testid^="portal-work-stop-"]').exists()).toBe(false)
    const row = w.find('[data-testid="portal-work-steps-none"]')
    expect(row.text()).toBe("cornelius doesn't report steps.")
    for (const c of ['h-4', 'leading-4', 'truncate', 'flex']) expect(row.classes()).not.toContain(c)
    expect(row.element.children.length).toBe(0)
    expect(row.attributes('title')).toBeUndefined()
  })

  it('6b. steps not read yet, without the prop: no steps element at all', () => {
    const w = plain({ item: synthetic(), showOpenInWork: true })
    expect(w.find('[data-testid="portal-work-reserved-steps"]').exists()).toBe(false)
    expect(w.find('[data-testid^="portal-work-steps-"]').exists()).toBe(false)
  })

  it('6c. canStop without the prop: a visible, usable Stop under its item id', () => {
    const w = plain({ item: synthetic({ id: 'r2', steps: { state: 'none' } }), canStop: true })
    const stop = w.find('[data-testid="portal-work-stop-r2"]')
    expect(stop.exists()).toBe(true)
    expect(stop.classes()).not.toContain('invisible')
    expect(stop.attributes('tabindex')).toBeUndefined()
    expect(stop.attributes('aria-hidden')).toBeUndefined()
  })

  it('7. a terminal card with the prop set reserves nothing', () => {
    const w = plain({
      item: synthetic({ id: 't1', status: 'failed', outcome: 'failed', steps: undefined }),
      reserveLiveRows: true, showOpenInWork: true,
    })
    expect(w.find('[data-testid="portal-work-reserved-steps"]').exists()).toBe(false)
    expect(w.find('[data-testid^="portal-work-stop-"]').exists()).toBe(false)
  })
})
