// @vitest-environment jsdom
/**
 * #3264 — the bar tooltip opened upward as an `absolute bottom-full` child of
 * the bar, so on the Workspace agent band (a 23px chart just under the header,
 * inside ScanlineReveal's clip-path) its top rows slid under the header and the
 * date and top buckets could not be read.
 *
 * The tooltip now renders in <body>, `position: fixed`, placed from the hovered
 * bar's rect: above when it fits, flipped below when it does not, and clamped
 * inside the viewport horizontally. Mounted (#2918): placement is the bug, so
 * the test reads the coordinates the chart actually applied.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import StackedBarChart from '@/components/StackedBarChart.vue'

const VW = 1000
const VH = 800
const TIP = { width: 160, height: 120 }

const DATA = [
  { date: '2026-10-01', total: 5, by_type: { schedule: 3, chat: 2 } },
  { date: '2026-10-02', total: 0, by_type: {} },
  { date: '2026-10-03', total: 4, by_type: { schedule: 4 } },
]

let wrapper = null

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

function rect({ left, top, width, height }) {
  return { left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON() {} }
}

// Every bar sits at `barTop`; bar i spans [barLeft + i*20, +20).
async function hoverBar(i, { barTop, barLeft = 400 }) {
  window.innerWidth = VW
  window.innerHeight = VH
  wrapper = mount(StackedBarChart, {
    attachTo: document.body,
    props: { data: DATA, buckets: ['schedule', 'chat'], colors: {}, height: 23, legend: 'none', axis: false },
  })
  const bars = wrapper.findAll('[data-chart-bar]')
  bars.forEach((b, j) => {
    b.element.getBoundingClientRect = () => rect({ left: barLeft + j * 20, top: barTop, width: 20, height: 23 })
  })
  const orig = HTMLElement.prototype.getBoundingClientRect
  HTMLElement.prototype.getBoundingClientRect = function () {
    if (this.hasAttribute('data-chart-tooltip')) return rect({ left: 0, top: 0, ...TIP })
    return orig.call(this)
  }
  try {
    await bars[i].trigger('mouseenter')
    await nextTick()
    await nextTick()
  } finally {
    HTMLElement.prototype.getBoundingClientRect = orig
  }
  return document.body.querySelector('[data-chart-tooltip]')
}

const px = (el, prop) => parseFloat(el.style[prop])

describe('StackedBarChart tooltip placement (#3264)', () => {
  it('renders in <body>, outside the chart, so no parent can clip or cover it', async () => {
    const tip = await hoverBar(0, { barTop: 400 })
    expect(tip).not.toBeNull()
    expect(wrapper.element.contains(tip)).toBe(false)
    expect(tip.classList.contains('fixed')).toBe(true)
    expect(tip.textContent).toContain('Total')
    expect(tip.style.visibility).not.toBe('hidden')
  })

  it('opens above the bar when there is room', async () => {
    const tip = await hoverBar(0, { barTop: 400 })
    expect(tip.dataset.side).toBe('above')
    expect(px(tip, 'top') + TIP.height).toBeLessThanOrEqual(400)
  })

  it('flips below the bar when it would run off the top (the Workspace band under the header)', async () => {
    const tip = await hoverBar(0, { barTop: 60 })
    expect(tip.dataset.side).toBe('below')
    expect(px(tip, 'top')).toBeGreaterThanOrEqual(60 + 23)
    expect(px(tip, 'top') + TIP.height).toBeLessThanOrEqual(VH)
  })

  it('stays inside the viewport at the left edge', async () => {
    const tip = await hoverBar(0, { barTop: 400, barLeft: 0 })
    expect(px(tip, 'left')).toBeGreaterThanOrEqual(8)
  })

  it('stays inside the viewport at the right edge', async () => {
    const tip = await hoverBar(2, { barTop: 400, barLeft: VW - 60 })
    expect(px(tip, 'left') + TIP.width).toBeLessThanOrEqual(VW - 8)
  })

  it('shows nothing for an empty day, and goes away on leave', async () => {
    expect(await hoverBar(1, { barTop: 400 })).toBeNull()
    wrapper.unmount()
    const tip = await hoverBar(0, { barTop: 400 })
    expect(tip).not.toBeNull()
    await wrapper.findAll('[data-chart-bar]')[0].trigger('mouseleave')
    expect(document.body.querySelector('[data-chart-tooltip]')).toBeNull()
  })
})
