// @vitest-environment jsdom
/**
 * trinity#3520 — a Work card's title folds open to read the whole sentence.
 *
 * Platform notices (a gated run's "Finished successfully — your request …") are
 * one long sentence, and the two-line clamp hid what they said. The card offers
 * "Show more" only when the clamp actually hides text, toggles to "Show less",
 * keeps the open state across a refresh of the same item and resets it for a
 * different one.
 *
 * jsdom has no layout, so "clipped" is stated by stubbing the title's
 * `scrollHeight` / `clientHeight` — the same two numbers the card measures.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import PortalWorkCard from '../../src/components/portal/PortalWorkCard.vue'

const LONG = '[Trinity] Finished successfully — your request gate-836c509f to run the skill zz-probe on proj-alpha was approved and ran as execution VAHvU8sG. It finished successfully. Its reply: "PROBE-DONE".'

const item = (over = {}) => ({
  id: 'exec-1', agent_name: 'proj-alpha', status: 'success', outcome: 'success',
  kind: 'background', title: LONG, steps: null, ...over,
})

const original = {
  scroll: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight'),
  client: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
}
function layout({ scroll, client }) {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get: () => scroll })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => client })
}
afterEach(() => {
  for (const [key, prop] of [['scroll', 'scrollHeight'], ['client', 'clientHeight']]) {
    if (original[key]) Object.defineProperty(HTMLElement.prototype, prop, original[key])
    else delete HTMLElement.prototype[prop]
  }
})

// The card measures in `onMounted`; the toggle renders on the next tick.
async function mountCard(props) {
  const w = mount(PortalWorkCard, { props: { compact: true, ...props } })
  await nextTick()
  return w
}

const title = (w) => w.get('[data-testid="portal-work-title"]')
const toggle = (w) => w.find('[data-testid="portal-work-title-toggle"]')

describe('PortalWorkCard — the title fold (trinity#3520)', () => {
  it('offers "Show more" when the clamp hides text, and the title starts clamped', async () => {
    layout({ scroll: 80, client: 40 })
    const w = await mountCard({ item: item() })
    expect(toggle(w).exists()).toBe(true)
    expect(toggle(w).text()).toBe('Show more')
    expect(toggle(w).attributes('aria-expanded')).toBe('false')
    expect(toggle(w).attributes('aria-controls')).toBe(title(w).attributes('id'))
    expect(title(w).classes()).toContain('line-clamp-2')
  })

  it('unfolds to the whole sentence and folds back', async () => {
    layout({ scroll: 80, client: 40 })
    const w = await mountCard({ item: item() })
    await toggle(w).trigger('click')
    expect(title(w).classes()).not.toContain('line-clamp-2')
    expect(toggle(w).text()).toBe('Show less')
    expect(toggle(w).attributes('aria-expanded')).toBe('true')
    expect(title(w).text()).toBe(LONG)
    await toggle(w).trigger('click')
    expect(title(w).classes()).toContain('line-clamp-2')
    expect(toggle(w).text()).toBe('Show more')
  })

  it('offers nothing when the title fits', async () => {
    layout({ scroll: 40, client: 40 })
    const w = await mountCard({ item: item({ title: 'Short' }) })
    expect(toggle(w).exists()).toBe(false)
  })

  it('stays open across a refresh of the same item and resets for a different one', async () => {
    layout({ scroll: 80, client: 40 })
    const w = await mountCard({ item: item() })
    await toggle(w).trigger('click')
    await w.setProps({ item: item({ status: 'success', outcome: 'success' }) })   // same id, refreshed
    expect(toggle(w).text()).toBe('Show less')
    await w.setProps({ item: item({ id: 'exec-2' }) })
    expect(title(w).classes()).toContain('line-clamp-2')
    expect(toggle(w).text()).toBe('Show more')
  })

  it('is a real button the keyboard reaches, not a hand-rolled lookalike', async () => {
    layout({ scroll: 80, client: 40 })
    const w = await mountCard({ item: item() })
    expect(toggle(w).element.tagName).toBe('BUTTON')
    expect(toggle(w).attributes('type')).toBe('button')
  })
})
