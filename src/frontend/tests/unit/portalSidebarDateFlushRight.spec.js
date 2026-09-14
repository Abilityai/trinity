/**
 * #2641 — the sidebar date is flush right, and an empty availability slot costs
 * the name nothing.
 *
 * The row was:
 *
 *   avatar · name (flex-1) · date (w-14) · availability (min-w-[4.5rem]) · badges
 *
 * `availabilityChip` returns null for every state except `stopped` and
 * `unavailable`, so on a fleet where everything is running — the normal case —
 * that 72px strip rendered EMPTY on every row. Both halves of the reported
 * defect follow from one fact: the dates stopped 72px short of the row's right
 * edge, and 72px per row was charged to the only element that wanted it, so
 * names truncated (`Chief ...`) beside a blank strip.
 *
 * The reservation itself is worth keeping — it stops a row reflowing when an
 * agent starts or stops between refreshes (#2196) — so it became a property of
 * the LIST rather than of a row: reserve on every row iff any VISIBLE row can
 * actually show a chip.
 *
 * This project has no component-mount harness (`package.json` carries no
 * @vue/test-utils, jsdom or happy-dom; vitest runs `environment: 'node'`), which
 * is why the decidable half lives in `portalUtils.js` and is genuinely executed
 * here. The source assertions below are scoped to what only source can answer —
 * that the template consumes the shared predicate, and that #2580's date column
 * is still a fixed, right-aligned, always-rendered `tabular-nums` column.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import {
  availabilityChip,
  reservesAvailabilitySlot,
} from '../../src/components/portal/portalUtils'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')

const running = (name) => ({ name, availability: 'ready' })
const stopped = (name) => ({ name, availability: 'stopped' })
const gone = (name) => ({ name, availability: 'unavailable' })

describe('#2641 — the slot is reserved for the LIST, not for every row', () => {
  it('an all-running fleet reserves nothing', () => {
    // The reported case. Every one of these yields a null chip, so holding
    // 72px on each row buys a reflow that can never happen.
    expect(reservesAvailabilitySlot([running('a'), running('b'), running('c')]))
      .toBe(false)
  })

  it('one stopped agent makes the whole list reserve', () => {
    // Uniform down the list, which is what keeps #2580's truncation point
    // identical on every row — a per-row reservation would give the stopped
    // row a different name width from its neighbours.
    expect(reservesAvailabilitySlot([running('a'), stopped('b'), running('c')]))
      .toBe(true)
  })

  it('an unavailable agent counts too — both chip states, not just stopped', () => {
    expect(reservesAvailabilitySlot([running('a'), gone('b')])).toBe(true)
  })

  it('derives from availabilityChip rather than re-listing the states', () => {
    // The predicate must not become a second copy of "which states get a chip".
    // Anything the chip rule answers null for reserves nothing, including a
    // state neither function has heard of yet.
    for (const state of ['ready', 'unknown', 'something-new', '', null, undefined]) {
      const agent = { name: 'x', availability: state }
      expect(availabilityChip(agent)).toBeNull()
      expect(reservesAvailabilitySlot([agent])).toBe(false)
    }
  })

  it('is unmoved by the detailed flag, which only changes the WORDING', () => {
    // An operator sees stopped-vs-no-container and a client sees one label for
    // both; neither changes whether there is a chip, so neither may change
    // whether space is held for one.
    const rows = [running('a'), stopped('b')]
    expect(reservesAvailabilitySlot(rows, { detailed: true })).toBe(true)
    expect(reservesAvailabilitySlot(rows, { detailed: false })).toBe(true)
  })

  it('an empty or malformed list reserves nothing', () => {
    expect(reservesAvailabilitySlot([])).toBe(false)
    expect(reservesAvailabilitySlot(null)).toBe(false)
    expect(reservesAvailabilitySlot(undefined)).toBe(false)
    expect(reservesAvailabilitySlot('not a list')).toBe(false)
  })
})

describe('#2641 — the template spends the predicate where it matters', () => {
  it('the availability slot is conditional on it', () => {
    expect(SIDEBAR).toMatch(
      /<span\s+v-if="reserveAvailability"[^>]*min-w-\[4\.5rem\]/
    )
  })

  it('removes the ELEMENT, not just its width', () => {
    // A zero-width flex child still sits between the date and the row edge, and
    // `gap-2.5` on the row would keep paying 10px for it — the date would still
    // not be flush. So the guard is a `v-if`, never a conditional class.
    const slot = SIDEBAR.slice(SIDEBAR.indexOf('reserveAvailability'))
    expect(slot).not.toMatch(/:class="reserveAvailability/)
  })

  it('is computed over the RENDERED rows, not the whole roster', () => {
    // A stopped agent hidden by search or by the collapse limit must not
    // reserve width on a list that shows no chip — that is the reported bug
    // with extra steps.
    expect(SIDEBAR).toMatch(
      /const reserveAvailability = computed\(\(\) => reservesAvailabilitySlot\(\s*shownAgents\.value/
    )
  })

  it('the date column is the last thing before the conditional slot', () => {
    // With nothing reserved after it, "flush right" is a consequence of the
    // date being the final always-rendered element — the badges after it are
    // content and have always been conditional.
    const dateIdx = SIDEBAR.indexOf('w-14 text-right')
    const slotIdx = SIDEBAR.indexOf('v-if="reserveAvailability"')
    expect(dateIdx).toBeGreaterThan(-1)
    expect(slotIdx).toBeGreaterThan(dateIdx)
  })
})

describe('#2641 — #2580 is not regressed', () => {
  it('the date column stays fixed-width, right-aligned and tabular', () => {
    expect(SIDEBAR).toMatch(/class="shrink-0 w-14 text-right[^"]*tabular-nums"/)
  })

  it('the date span still renders on every row, with the v-if INSIDE it', () => {
    // #2580's actual fix: the span disappearing on an agent you have never
    // talked to moved the whole row's truncation point. The conditional is on
    // the CONTENT, not on the column.
    const m = SIDEBAR.match(/<span class="shrink-0 w-14 text-right[^"]*">\s*<template v-if="rowMeta\[a\.name\]\?\.time">/)
    expect(m, 'the date column must be unconditional with the v-if inside').toBeTruthy()
  })

  it('the ask and waiting badges keep their conditional rendering', () => {
    // AC: nothing else gains an unconditional footprint while we are here.
    expect(SIDEBAR).toMatch(/v-if="askCountFor\(a\.name\)"/)
    expect(SIDEBAR).toMatch(/v-if="waitingFor\(a\.name\)"/)
  })
})
