// @vitest-environment jsdom
/**
 * trinity#3737 — a starred or pinned chat in the "N more" menu kept its glyph
 * at the left edge while its title drifted right. The menu row was
 * `flex justify-between`, and the glyphs were the row's first flex children, so
 * the free space was spread between the glyph and the label. A shorter title
 * drifted further right. The same rule floated the Draft mark to mid-row.
 *
 * The fix groups the leading glyphs and the label in one `[data-menu-lead]`
 * span, which takes the free space (`flex-1 min-w-0`). The trailing items
 * (Draft, badge or activity dot) sit at the right edge, and the close × stays
 * outside the button.
 *
 * jsdom has no layout, so these assertions are structural: which element holds
 * the label, and in what order. The inline row and the hidden mirror row never
 * used `justify-between` and must not change (the mirror's measured width is
 * the drawn width).
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

// Starred/pinned rows sit LAST so the 100px-tab / 200px-strip stub pushes them
// into the menu. The first tab is starred too, so the inline row has a glyph to check.
const TABS = [
  { id: 'lead', label: 'Lead', starred: true },
  { id: 'alpha', label: 'Alpha' },
  { id: 'beta', label: 'Beta' },
  { id: 'gamma', label: 'Gamma' },
  { id: 'plain', label: 'Plain' },
  { id: 'drafty', label: 'Drafty', hasDraft: true, badge: 3 },
  { id: 'sig', label: 'Sig', signal: 'idle' },
  { id: 'badgesig', label: 'BadgeSig', badge: 5, signal: 'live' },
  { id: 'closer', label: 'Closer', closable: true },
  { id: 'both', label: 'Both', pinned: true, starred: true },
  { id: 'pinned', label: 'Pinned', pinned: true },
  { id: 'starred', label: 'Starred', starred: true },
]

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

// Force an overflow: a 200px strip of 100px tabs (the overflowTabsTablist harness).
async function openMenu(props = {}) {
  const proto = HTMLElement.prototype
  const rect = proto.getBoundingClientRect
  const cw = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')
  proto.getBoundingClientRect = function () { return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 } }
  Object.defineProperty(Element.prototype, 'clientWidth', { configurable: true, get() { return 200 } })
  try {
    w = mount(OverflowTabs, { props: { tabs: TABS, modelValue: 'alpha', ...props }, attachTo: document.body })
    await nextTick(); await nextTick()
    await w.find('[data-overflow-trigger]').trigger('click')
    await nextTick()
  } finally {
    proto.getBoundingClientRect = rect
    if (cw) Object.defineProperty(Element.prototype, 'clientWidth', cw)
  }
}

// The menu row for a label. Asserted to exist, so no test passes on an empty menu.
function row(label) {
  const item = w.findAll('[data-menu-item]').find((i) => i.text().startsWith(label))
  expect(item, `menu row "${label}"`).toBeTruthy()
  return item.element
}
// The innermost span whose text is exactly the label.
const labelOf = (btn, label) => [...btn.querySelectorAll('span')].find((s) => s.children.length === 0 && s.textContent.trim() === label)

function expectLeadHolds(btn, label) {
  const lead = btn.querySelector('[data-menu-lead]')
  expect(lead, 'leading group').not.toBeNull()
  expect(btn.firstElementChild).toBe(lead)
  expect(lead.classList.contains('flex-1')).toBe(true)
  expect(lead.classList.contains('min-w-0')).toBe(true)
  const lbl = labelOf(btn, label)
  expect(lbl).toBeTruthy()
  expect(lbl.parentElement).toBe(lead)
  expect(lbl.parentElement).not.toBe(btn)
  expect(btn.classList.contains('justify-between')).toBe(false)
  return { lead, lbl }
}

describe('menu row: leading glyphs and label stay together (#3737)', () => {
  it('a starred row holds the star and the label in one leading group', async () => {
    await openMenu()
    const btn = row('Starred')
    const { lead, lbl } = expectLeadHolds(btn, 'Starred')
    const star = btn.querySelector('[data-tab-star]')
    expect(star.parentElement).toBe(lead)
    expect(star.nextElementSibling).toBe(lbl)
  })

  it('a pinned row, and a pinned+starred row, keep bookmark → star → label in the group', async () => {
    await openMenu()
    const p = row('Pinned')
    const { lead: pl, lbl: plbl } = expectLeadHolds(p, 'Pinned')
    expect(pl.firstElementChild.tagName.toLowerCase()).toBe('svg')
    expect(pl.firstElementChild.nextElementSibling).toBe(plbl)

    const b = row('Both')
    const { lead: bl, lbl: blbl } = expectLeadHolds(b, 'Both')
    const kids = [...bl.children]
    expect(kids.length).toBe(3)
    expect(kids[0].hasAttribute('data-tab-star')).toBe(false)
    expect(kids[1].hasAttribute('data-tab-star')).toBe(true)
    expect(kids[2]).toBe(blbl)
  })

  it('a plain row has the same structure, so every title starts at the same edge', async () => {
    await openMenu()
    const { lead } = expectLeadHolds(row('Plain'), 'Plain')
    expect(lead.children.length).toBe(1)
  })
})

describe('menu row: trailing items cluster at the right edge', () => {
  it('Draft then badge follow the leading group as direct children of the button', async () => {
    await openMenu()
    const btn = row('Drafty')
    const kids = [...btn.children]
    expect(kids.length).toBe(3)
    expect(kids[0].hasAttribute('data-menu-lead')).toBe(true)
    expect(kids[1].getAttribute('data-testid')).toBe('draft-mark')
    expect(kids[2].textContent.trim()).toBe('3')
  })

  it('the #2794 chain: a badge wins over the dot, and a signal alone renders the dot', async () => {
    await openMenu()
    const bs = [...row('BadgeSig').children]
    expect(bs.length).toBe(2)
    expect(bs[1].textContent.trim()).toBe('5')

    const s = [...row('Sig').children]
    expect(s.length).toBe(2)
    expect(s[0].hasAttribute('data-menu-lead')).toBe(true)
    expect(s[1].textContent.trim()).toBe('')
    expect(s[1].classList.contains('rounded-full')).toBe(true)
    expect(s[1].classList.contains('bg-action-primary-500')).toBe(true)
  })

  it('the close × stays outside the button, a sibling inside its wrapper', async () => {
    await openMenu()
    const btn = row('Closer')
    expectLeadHolds(btn, 'Closer')
    const x = btn.parentElement.querySelector('[data-tab-close]')
    expect(x).not.toBeNull()
    expect(x.parentElement).toBe(btn.parentElement)
    expect(btn.contains(x)).toBe(false)
  })
})

describe('fixedWidth: the label still clamps and the full title stays reachable', () => {
  it('label span keeps max-w-[20rem] truncate; the button title is the full label', async () => {
    await openMenu({ fixedWidth: true })
    const btn = row('Starred')
    const { lbl } = expectLeadHolds(btn, 'Starred')
    expect(lbl.classList.contains('max-w-[20rem]')).toBe(true)
    expect(lbl.classList.contains('truncate')).toBe(true)
    expect(btn.getAttribute('title')).toBe('Starred')
  })
})

describe('scope guard: the inline row and the mirror row are unchanged', () => {
  it('no leading group in the strip; the glyph stays a direct child of its tab button', async () => {
    await openMenu()
    expect(w.element.querySelector('nav [data-menu-lead]')).toBeNull()
    const inline = [...w.element.querySelectorAll('nav button:not([data-measure-tab]):not([data-overflow-trigger])')]
      .find((b) => b.textContent.trim() === 'Lead')
    expect(inline).toBeTruthy()
    const star = inline.querySelector('[data-tab-star]')
    expect(star.parentElement).toBe(inline)
    const lbl = labelOf(inline, 'Lead')
    expect(lbl.parentElement).toBe(inline)
    expect(lbl.classList.contains('min-w-0')).toBe(true)
    expect(lbl.classList.contains('truncate')).toBe(true)

    const mirror = [...w.element.querySelectorAll('[data-measure-tab]')].find((b) => b.textContent.trim() === 'Starred')
    expect(mirror).toBeTruthy()
    expect(mirror.querySelector('[data-tab-star]').parentElement).toBe(mirror)
  })
})
