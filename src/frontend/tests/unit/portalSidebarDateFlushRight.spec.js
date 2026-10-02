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
 * ent#610 sign-off round 7 moved the chip under the name, so nothing is reserved
 * any more and the list-level predicate went with it (#3054 review). The source
 * assertions below pin what only source can answer — where the chip renders,
 * and that #2580's date column is still a fixed, right-aligned, always-rendered
 * `tabular-nums` column.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')

// ent#610 sign-off: #2641 reserved the chip's 72px on EVERY row once any
// visible row could show one. "Show all" revealed three stopped agents and, in
// a ~250px sidebar (avatar + the 56px date + that 72px + two count pills), the
// name got 0px on every row — the agents vanished. The chip now sits on the
// row's SECOND line, under the name, so the meta strip reserves nothing for it
// and no row pays for another row's state. The list-level predicate #2641 added
// (`reservesAvailabilitySlot`) lost its last caller here and was removed with its
// tests (#3054 review).
describe('ent#610 sign-off — the availability chip lives under the name', () => {
  const nameBlock = () => {
    const i = SIDEBAR.indexOf('{{ agentLabel(a) }}')
    return SIDEBAR.slice(i, SIDEBAR.indexOf('w-14 text-right', i))
  }
  it('the chip renders inside the name block, on the subtitle line', () => {
    expect(nameBlock()).toMatch(/<BaseBadge v-if="chipFor\(a\)"[^>]*:variant="chipFor\(a\)\.variant"/)
  })
  it('nothing after the date reserves width for it', () => {
    const tail = SIDEBAR.slice(SIDEBAR.indexOf('w-14 text-right'), SIDEBAR.indexOf('data-testid="agent-unread-count"'))
    expect(tail).not.toMatch(/min-w-\[4\.5rem\]/)
    expect(tail).not.toMatch(/chipFor\(a\)/)
    expect(SIDEBAR).not.toMatch(/v-if="reserveAvailability"/)
  })
  it('the subtitle line still truncates its text beside the chip', () => {
    expect(nameBlock()).toMatch(/min-w-0 truncate[^"]*"[^>]*>\{\{ rowMeta\[a\.name\]\.preview \}\}/)
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
    // ent#610 §3g B7a: the span's ink moved to a `:class="META_INK"` binding;
    // the column is still unconditional, which is what this pins.
    const m = SIDEBAR.match(/<span class="shrink-0 w-14 text-right[^"]*"(?:\s+:class="META_INK")?>\s*<template v-if="rowMeta\[a\.name\]\?\.time">/)
    expect(m, 'the date column must be unconditional with the v-if inside').toBeTruthy()
  })

  it('the ask and waiting badges keep their conditional rendering', () => {
    // AC: nothing else gains an unconditional footprint while we are here.
    expect(SIDEBAR).toMatch(/v-if="askCountFor\(a\.name\)"/)
    expect(SIDEBAR).toMatch(/v-if="waitingFor\(a\.name\)"/)
  })
})
