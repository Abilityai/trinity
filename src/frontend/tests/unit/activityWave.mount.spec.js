// @vitest-environment jsdom
/**
 * trinity-enterprise#968 — ActivityWave, mounted (#2918): the phase machine,
 * the hover tooltip, reduced motion and instance-safe SVG ids are behaviours,
 * so the test reads the rendered DOM rather than the source.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ActivityWave from '@/components/portal/ActivityWave.vue'
import { BEATS_PER_DAY, revealFallbackMs } from '@/utils/activityWave'

const COLORS = { 'Chat/Tasks': '#6366f1', 'MCP': '#a78bfa', 'Scheduled': '#22d3ee' }
const BUCKETS = Object.keys(COLORS)
const LABELS = { 'Chat/Tasks': 'Chat', 'MCP': 'Tool call' }

function day(date, by_type = {}) {
  return { date, total: Object.values(by_type).reduce((a, b) => a + b, 0), by_type }
}
// Eight points — the live `7d` payload's shape.
const WEEK = [
  day('2026-10-15'), day('2026-10-16', { 'Chat/Tasks': 8, 'Scheduled': 4 }), day('2026-10-17', { 'Chat/Tasks': 12 }),
  day('2026-10-18', { 'MCP': 1 }), day('2026-10-19', { 'Chat/Tasks': 18, 'Scheduled': 9, 'MCP': 4 }),
  day('2026-10-20'), day('2026-10-21', { 'Chat/Tasks': 9 }), day('2026-10-22', { 'Scheduled': 4 }),
]
const EMPTY = WEEK.map((d) => day(d.date))

function mountWave(props = {}) {
  return mount(ActivityWave, {
    props: { data: WEEK, buckets: BUCKETS, colors: COLORS, labels: LABELS, ...props },
    attachTo: document.body,
  })
}
const tooltip = () => document.body.querySelector('[data-wave-tooltip]')

let wrappers = []
beforeEach(() => { wrappers = [] })
afterEach(() => {
  for (const w of wrappers) w.unmount()
  vi.useRealTimers()
  delete window.matchMedia
})
const keep = (w) => { wrappers.push(w); return w }

describe('data face', () => {
  it('renders four beats per day and one hit rect per day, for the payload’s own day count', () => {
    const w = keep(mountWave())
    expect(w.findAll('[data-wave-beat]')).toHaveLength(8 * BEATS_PER_DAY)
    expect(w.findAll('[data-wave-day]')).toHaveLength(8)
    expect(w.find('[data-wave-idle]').exists()).toBe(false)
    expect(w.attributes('data-phase')).toBe('loaded')
    expect(w.attributes('aria-busy')).toBeUndefined()
  })

  it('names itself with the total, and says so when the week is empty', () => {
    expect(keep(mountWave({ label: 'Activity, last 7 days' })).attributes('aria-label')).toBe('Activity, last 7 days: 69 runs')
    const empty = keep(mountWave({ data: EMPTY }))
    expect(empty.attributes('aria-label')).toBe('Activity: no runs')
    expect(empty.find('[data-wave-data]').classes()).toContain('wave-empty')
    // an empty week is still a wave, never a sentence
    expect(empty.findAll('[data-wave-beat]')).toHaveLength(8 * BEATS_PER_DAY)
  })

  it('a cache-hit mount (loading=false from the start) carries no reveal animation', () => {
    const w = keep(mountWave())
    expect(w.find('[data-wave-data]').classes()).not.toContain('wave-reveal')
  })

  it('every beat has its own clip and blur, and no id can read as a hex colour', () => {
    const w = keep(mountWave())
    const html = w.html()
    const refs = [...html.matchAll(/url\(#([^)]+)\)/g)].map((m) => m[1])
    expect(refs.length).toBeGreaterThan(8 * BEATS_PER_DAY)
    for (const id of refs) expect(id).toMatch(/^w/)
    const HEX = /(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b/
    for (const id of refs) expect(`url(#${id})`).not.toMatch(HEX)
  })

  it('two instances on one page never share an SVG id', () => {
    const a = keep(mountWave()), b = keep(mountWave())
    const ids = (w) => w.findAll('clipPath').map((c) => c.attributes('id'))
    const ia = ids(a), ib = ids(b)
    expect(ia.length).toBe(8 * BEATS_PER_DAY)
    expect(ia.filter((id) => ib.includes(id))).toEqual([])
  })
})

describe('loading face and the phase machine', () => {
  it('while loading: breathing dots, aria-busy, no beats', () => {
    const w = keep(mountWave({ loading: true }))
    expect(w.attributes('data-phase')).toBe('loading')
    expect(w.attributes('aria-busy')).toBe('true')
    expect(w.attributes('aria-label')).toMatch(/loading$/)
    expect(w.find('[data-wave-idle]').exists()).toBe(true)
    expect(w.findAll('.wave-idle-beat')).toHaveLength(8 * BEATS_PER_DAY)
    expect(w.findAll('[data-wave-beat]')).toHaveLength(0)
    expect(w.find('animateTransform').exists()).toBe(true)
  })

  it('loading → loaded plays the reveal once, then settles; a lost animationend is forced closed', async () => {
    vi.useFakeTimers()
    const w = keep(mountWave({ loading: true }))
    await w.setProps({ loading: false })
    expect(w.attributes('data-phase')).toBe('revealing')
    expect(w.find('[data-wave-data]').classes()).toContain('wave-reveal')
    vi.advanceTimersByTime(revealFallbackMs(8 * BEATS_PER_DAY) + 1)
    await nextTick()
    expect(w.attributes('data-phase')).toBe('loaded')
    expect(w.find('[data-wave-data]').classes()).not.toContain('wave-reveal')
  })

  it('the last beat’s animationend ends the reveal early', async () => {
    vi.useFakeTimers()
    const w = keep(mountWave({ loading: true }))
    await w.setProps({ loading: false })
    const beats = w.findAll('[data-wave-beat]')
    await beats[0].trigger('animationend')
    expect(w.attributes('data-phase')).toBe('revealing') // not the last one
    await beats[beats.length - 1].trigger('animationend')
    expect(w.attributes('data-phase')).toBe('loaded')
  })

  it('an empty terminal snaps instead of celebrating', async () => {
    const w = keep(mountWave({ loading: true, data: EMPTY, reveal: false }))
    await w.setProps({ loading: false })
    expect(w.attributes('data-phase')).toBe('loaded')
    expect(w.find('[data-wave-data]').classes()).not.toContain('wave-reveal')
  })

  it('a background refresh (loading never rises again) changes nothing about the phase', async () => {
    const w = keep(mountWave())
    await w.setProps({ data: [...WEEK.slice(0, 7), day('2026-10-22', { 'Scheduled': 5 })] })
    expect(w.attributes('data-phase')).toBe('loaded')
    expect(w.find('[data-wave-data]').classes()).not.toContain('wave-reveal')
  })

  it('under prefers-reduced-motion: no glint sweep, no breathing, instant reveal', async () => {
    window.matchMedia = () => ({ matches: true })
    const w = keep(mountWave({ loading: true }))
    expect(w.find('animateTransform').exists()).toBe(false)
    expect(w.find('.wave-idle-beat').classes()).toContain('wave-still')
    await w.setProps({ loading: false })
    expect(w.attributes('data-phase')).toBe('loaded')
  })
})

describe('hover tooltip', () => {
  it('hovering a day shows its date, sources with their labels, and the total, teleported to <body>', async () => {
    const w = keep(mountWave())
    await w.findAll('[data-wave-day]')[4].trigger('mouseenter')
    await nextTick()
    const tip = tooltip()
    expect(tip).not.toBeNull()
    const text = tip.textContent.replace(/\s+/g, ' ')
    expect(text).toMatch(/Oct/)
    expect(text).toMatch(/19/)
    expect(text).toContain('Chat')      // translated via `labels`
    expect(text).toContain('Tool call') // translated via `labels`
    expect(text).toContain('Scheduled') // unmapped bucket shows its own name
    expect(text).toMatch(/Total\s*31/)
    expect(w.find('[data-wave-band]').exists()).toBe(true)
    await w.findAll('[data-wave-day]')[4].trigger('mouseleave')
    expect(tooltip()).toBeNull()
    expect(w.find('[data-wave-band]').exists()).toBe(false)
  })

  it('an empty day says so rather than showing an empty list', async () => {
    const w = keep(mountWave())
    await w.findAll('[data-wave-day]')[0].trigger('mouseenter')
    await nextTick()
    expect(tooltip().textContent).toContain('No runs')
  })

  it('the day hit rects paint above every beat (the pointer-events rule itself is CSS)', () => {
    const w = keep(mountWave())
    // the rule is CSS (`.wave-beat { pointer-events: none }`); what the DOM can
    // show is that the hit rects sit AFTER every beat in paint order, so they
    // are on top of them
    const html = w.find('svg').html()
    expect(html.lastIndexOf('data-wave-beat')).toBeLessThan(html.indexOf('data-wave-day'))
  })
})
