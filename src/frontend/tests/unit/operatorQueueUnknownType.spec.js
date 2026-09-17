/**
 * ent#499 prerequisite — an operator-queue item of an unrecognised type can
 * still be CLOSED.
 *
 * `operator_queue.type` is free TEXT with no CHECK, and the platform has emitted
 * non-protocol types since #1410 (`skill_not_found`). `QueueCard.vue` and
 * `QueueItemDetail.vue` each carried their own `approval → question → alert`
 * v-if chain and rendered **no control at all** for anything else — so those
 * items could not be acknowledged from the queue, which for a BUDGETED alert
 * type means five of them jam the per-(agent, type) pending cap permanently.
 *
 * `utils/operatorQueue.js` is the module whose docstring already declares itself
 * "the ONE home of … the controls-kind switch". The two cards were the second
 * producer it exists to prevent, so the fix is to consume it — and the source
 * guard below is what keeps them consuming it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { queueResponseKind } from '../../src/utils/operatorQueue.js'

const read = (rel) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const CARD = '../../src/components/operator/QueueCard.vue'
const DETAIL = '../../src/components/operator/QueueItemDetail.vue'

describe('an unrecognised queue type is informational, so it acknowledges', () => {
  it.each([
    ['skill_not_found (#1410, live since it landed)', 'skill_not_found'],
    ['workspace_problem_report (ent#499)', 'workspace_problem_report'],
    ['a type this build has never heard of', 'invented_tomorrow'],
  ])('%s → acknowledge', (_label, type) => {
    expect(queueResponseKind({ type })).toBe('acknowledge')
  })

  it('offers acknowledge even when the item carries options', () => {
    // Options are an approval's vocabulary. On an unknown type they are not a
    // decision anyone is waiting for.
    expect(queueResponseKind({ type: 'weird', options: ['a', 'b'] })).toBe('acknowledge')
  })

  it('still gives an approval without options a text box', () => {
    // The one case that must NOT take the new default: the operator has a real
    // decision to express and acknowledging would discard it.
    expect(queueResponseKind({ type: 'approval', options: [] })).toBe('question')
  })

  it('leaves the three protocol types exactly as they were', () => {
    expect(queueResponseKind({ type: 'approval', options: ['y'] })).toBe('approval')
    expect(queueResponseKind({ type: 'question' })).toBe('question')
    expect(queueResponseKind({ type: 'alert' })).toBe('acknowledge')
  })
})

describe('neither card re-implements the switch', () => {
  it.each([['QueueCard', CARD], ['QueueItemDetail', DETAIL]])(
    '%s branches on responseKind, not on item.type',
    (_name, path) => {
      const src = read(path)
      expect(src).toContain('queueResponseKind')
      // A `v-if="item.type === …"` is the second producer coming back. Comments
      // are stripped first so an explanatory line cannot fail the guard.
      const code = src.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\/[^\n]*/g, '')
      expect(code).not.toMatch(/v-(?:else-)?if="item\.type\s*===/)
    },
  )

  it('QueueCard ends its chain with a v-else so no type falls through', () => {
    // The bug was a terminal `v-else-if`: an item matching none of the branches
    // got an empty response area and no way out of the queue.
    const src = read(CARD)
    expect(src).toMatch(/v-else\s+class="flex justify-end"/)
  })
})
