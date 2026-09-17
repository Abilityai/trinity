import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  scanLoadingGates,
  countBareLoadingGates,
  isBareLoadingGate,
} from '../../scripts/scan-loading-gates.mjs'

/**
 * #1927 — bare loading-gate RATCHET (design-system p13/p14).
 *
 * The class: a `v-if="<loading flag>"` with no other operand swaps rendered data
 * for a spinner on every background poll. PR #1939 measured ~40 such gates
 * across ~30 files and noted #1927 names only four. Sweeping them all in one PR
 * is the wrong shape (CLAUDE.md minimal change), and the repo has NO ESLint
 * toolchain, so the mechanical guard is this: the committed
 * `loading-gate-baseline.json` freezes today's per-file counts and this test
 * fails when any file GROWS (or a new file gains a gate). Counts may only
 * shrink. Same idea as `raw-color-baseline.json` — freeze, then pay down.
 *
 * The baseline is ALSO pinned exact: a file whose count dropped below its
 * baseline entry (someone fixed a gate) fails with a "regenerate" message, so
 * the file can never silently rot into a rubber stamp that permits regressions
 * back up to a stale ceiling.
 *
 *   node scripts/scan-loading-gates.mjs src --baseline loading-gate-baseline.json
 */

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')
const BASELINE_PATH = resolve(FRONTEND, 'loading-gate-baseline.json')
const REGEN = `node scripts/scan-loading-gates.mjs src --baseline loading-gate-baseline.json`

describe('scanner — what counts as a bare loading gate', () => {
  it('matches a whole-expression loading flag, in any of its house spellings', () => {
    for (const e of ['loading', 'loading.queue', 'executionsLoading', 'isLoading', 'store.loading', ' loading ', 'loadingHistory']) {
      expect(isBareLoadingGate(e), e).toBe(true)
    }
  })
  it('does NOT match a gate that already encodes "no data yet" or any other operand', () => {
    for (const e of [
      'loading && !hasLoaded', 'loading && items.length === 0', '!loading', 'loading || error',
      'firstLoad', 'queueFirstLoad', 'refreshing', 'busy', 'state === "loading"', 'loading ? a : b',
    ]) {
      expect(isBareLoadingGate(e), e).toBe(false)
    }
  })
  it('counts v-if and v-else-if inside <template> only, and ignores commented-out markup', () => {
    const sfc = `
<template>
  <div v-if="loading">spinner</div>
  <div v-else-if="loading.x">x</div>
  <div v-else-if="error">e</div>
  <!-- <div v-if="loading">commented out</div> -->
  <div v-if="loading && !hasLoaded">gated</div>
</template>
<script setup>
const s = 'v-if="loading"'
</script>`
    const { count, samples } = countBareLoadingGates(sfc)
    expect(count).toBe(2)
    expect(samples.map(s => s.expr)).toEqual(['loading', 'loading.x'])
  })
})

describe('ratchet — per-file bare loading-gate counts may only shrink', () => {
  const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  const scan = scanLoadingGates(resolve(FRONTEND, 'src'))

  it('baseline file has the expected shape', () => {
    expect(baseline).toHaveProperty('files')
    expect(typeof baseline.files).toBe('object')
  })

  it('no file has MORE bare loading gates than its baseline entry (new files count as 0)', () => {
    const grew = []
    for (const [file, count] of Object.entries(scan.files)) {
      const allowed = baseline.files[file] ?? 0
      if (count > allowed) {
        const where = (scan.samples[file] || []).map(s => `:${s.line} v-if="${s.expr}"`).join(', ')
        grew.push(`${file} — ${count} > ${allowed} (${where})`)
      }
    }
    expect(
      grew,
      `Bare loading gates GREW. Gate on "no data yet" (loading && !hasLoaded / viewState()) instead — design-system p13/p14:\n  ${grew.join('\n  ')}`
    ).toEqual([])
  })

  it('baseline is exact — a fixed file must lower its entry (no stale ceilings)', () => {
    const stale = []
    for (const [file, allowed] of Object.entries(baseline.files)) {
      const actual = scan.files[file] ?? 0
      if (actual < allowed) stale.push(`${file}: baseline ${allowed}, now ${actual}`)
    }
    expect(
      stale,
      `Baseline entries are above reality (a gate got fixed — nice). Regenerate so the ratchet keeps biting:\n  ${REGEN}\n  ${stale.join('\n  ')}`
    ).toEqual([])
  })
})
