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
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { canvasSelectorVisible, canvasAutoSelect, canvasSearchVisible } from '../../src/components/canvas/canvasUtils.js'

const SRC = fileURLToPath(new URL('../../src/components/canvas/CanvasPanel.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

/** Slice `const selectorVisible = computed(() => …)` and run its body. */
function selectorVisibleFor({ visible, manage, query }) {
  const m = source.match(/const selectorVisible = computed\(\(\) => (canvasSelectorVisible\(\{[\s\S]*?\}\))\)/)
  expect(m, 'selectorVisible computed is gone').toBeTruthy()
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
  it('the template gates the strip on the computed, not on a raw length test', () => {
    expect(source).toMatch(/data-testid="canvas-select"/)
    const strip = source.match(/<div\s+v-if="([^"]+)"[^>]*data-testid="canvas-select"/)
    expect(strip, 'the strip div lost its v-if or its testid').toBeTruthy()
    expect(strip[1]).toBe('selectorVisible')
    expect(source).not.toMatch(/v-if="visible\.length > 1 \|\| manage"/)
  })

  it('the ejection repro: 7 canvases, query narrowing to ONE match still shows the strip', () => {
    expect(selectorVisibleFor({ visible: 1, manage: false, query: 'Topic 3' })).toBe(true)
  })

  it('and with no query, one canvas is no choice — the strip collapses as before', () => {
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
