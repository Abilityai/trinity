/**
 * ent#553 review (the fourth ejection) — the two gate expressions in
 * CanvasPanel.vue are RUN, not grepped.
 *
 * `v-if="visible.length > 1 || manage"` gated the chip strip on the filtered
 * list, so a search narrowing to exactly one match hid the strip, the
 * no-match line stayed hidden too, and the auto-select watcher — keyed off the
 * unfiltered `props.canvases` — never selected the match. Proven by
 * execution at the time: 7 canvases, query "Topic 3" → strip `false`, message
 * `false`. The rule now lives in `canvasSelectorVisible` / `canvasAutoSelect`
 * (own cases in canvasUtils.spec.js); THIS file pins that the SFC actually
 * consumes them — the `selectorVisible` computed is sliced out of the source
 * and evaluated against the pure functions with the ejection's numbers, and
 * the template's `v-if` is asserted to read that computed rather than a
 * re-derived length test.
 *
 * ent#724 — the chip strip became ONE always-rendered dropdown (principle 30:
 * the control row never changes shape), so the same rule now decides whether
 * the dropdown is ENABLED rather than whether the strip exists. The computed is
 * `selectorEnabled`, and manage mode no longer feeds it (manage opens a list
 * below the row instead of swapping the selector's layout).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { canvasSelectorVisible, canvasAutoSelect, canvasSearchVisible } from '../../src/components/canvas/canvasUtils.js'

const SRC = fileURLToPath(new URL('../../src/components/canvas/CanvasPanel.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

/** Slice `const selectorEnabled = computed(() => …)` and run its body. */
function selectorVisibleFor({ visible, manage, query }) {
  const m = source.match(/const selectorEnabled = computed\(\(\) => (canvasSelectorVisible\(\{[\s\S]*?\}\))\)/)
  expect(m, 'selectorEnabled computed is gone').toBeTruthy()
  const body = m[1]
  // eslint-disable-next-line no-new-func
  return new Function('canvasSelectorVisible', 'visible', 'manage', 'query', `return ${body}`)(
    canvasSelectorVisible,
    { value: Array.from({ length: visible }) },
    { value: manage },
    { value: query },
  )
}

/** Slice `const showSearch = computed(() => …)` and run its body. */
function showSearchFor({ count, query }) {
  const m = source.match(/const showSearch = computed\(\(\) => (canvasSearchVisible\([^)]*\))\)/)
  expect(m, 'showSearch computed is gone, or no longer reads canvasSearchVisible').toBeTruthy()
  const body = m[1]
  // eslint-disable-next-line no-new-func
  return new Function('canvasSearchVisible', 'ordered', 'SEARCH_THRESHOLD', 'query', `return ${body}`)(
    canvasSearchVisible,
    { value: Array.from({ length: count }) },
    6,
    { value: query },
  )
}

describe('CanvasPanel selector gate (ent#553 review)', () => {
  it('the dropdown is always rendered and its ENABLED state reads the computed (ent#724)', () => {
    const sel = source.match(/<BaseSelect[\s\S]*?data-testid="canvas-select"[\s\S]*?>/)
    expect(sel, 'the canvas-select BaseSelect is gone').toBeTruthy()
    expect(sel[0]).not.toMatch(/v-if=/)
    expect(sel[0]).toMatch(/:disabled="!selectorEnabled"/)
    expect(source).not.toMatch(/v-if="visible\.length > 1 \|\| manage"/)
  })

  it('manage mode never disables the dropdown (it opens a list below instead)', () => {
    expect(selectorVisibleFor({ visible: 1, manage: true, query: '' })).toBe(false)
    expect(selectorVisibleFor({ visible: 5, manage: true, query: '' })).toBe(true)
  })

  it('the ejection repro: 7 canvases, query narrowing to ONE match keeps the dropdown enabled', () => {
    expect(selectorVisibleFor({ visible: 1, manage: false, query: 'Topic 3' })).toBe(true)
  })

  it('and with no query, one canvas is no choice — the dropdown is disabled, the row stays', () => {
    expect(selectorVisibleFor({ visible: 1, manage: false, query: '' })).toBe(false)
    expect(selectorVisibleFor({ visible: 2, manage: false, query: '' })).toBe(true)
  })

  it('the selection follows the matches while a query is active', () => {
    // The watcher body: `canvasAutoSelect(visible, selectedId, query)` → select(next)
    expect(source).toMatch(/canvasAutoSelect\(visible\.value, selectedId\.value, query\.value\)/)
    const visible = [{ canvas_id: 'topic-3', title: 'Topic 3' }]
    expect(canvasAutoSelect(visible, 'topic-1', 'Topic 3')).toBe('topic-3')
  })

  // ent#553 review (the fifth ejection) — `query` has exactly one writer, the
  // search input's `v-model`, and that input was `v-if="showSearch"` with
  // `showSearch = ordered.length > SEARCH_THRESHOLD`. Seven canvases, type
  // "Topic 3", delete the one match: six canvases, the box unmounts, `visible`
  // still filters on the stale query, the strip collapses, and the panel says
  // *No canvas matches "Topic 3"* with no control left to clear it. Proven by
  // execution at the time. The gate spec above could not see it because it
  // drives `visible`/`query` in isolation from `showSearch`; this one runs the
  // real `showSearch` expression against the ejection's own numbers.
  describe('the search box survives a shrink below the threshold (the stale-query wedge)', () => {
    it('the input is gated on showSearch and is the only writer of query', () => {
      expect(source).toMatch(/<input\s+v-if="showSearch"\s+v-model="query"/)
      expect(source.match(/v-model="query"/g)).toHaveLength(1)
    })

    it('the ejection repro: 7 → 6 canvases with "Topic 3" typed keeps the box', () => {
      expect(showSearchFor({ count: 7, query: '' })).toBe(true)
      expect(showSearchFor({ count: 6, query: 'Topic 3' })).toBe(true)
    })

    it('and with no query the box still appears only past the threshold', () => {
      expect(showSearchFor({ count: 6, query: '' })).toBe(false)
      expect(showSearchFor({ count: 7, query: '' })).toBe(true)
    })

    it('so the no-match line always has its control: zero matches, query typed, box up', () => {
      expect(showSearchFor({ count: 3, query: 'zzz' })).toBe(true)
      expect(selectorVisibleFor({ visible: 0, manage: false, query: 'zzz' })).toBe(false)
    })
  })
})
