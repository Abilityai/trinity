/**
 * #2462 — the nightly's per-PR verdict, the one path that can publish a lie.
 *
 * The sweep runs one leg per (PR, seed) and a PR's verdict is the union of its
 * seeds, so the aggregation decides whether a green tick appears on a PR whose
 * suite may not have finished. That is exactly the failure #2029 fixed one
 * layer down ("absence of a verdict is its own state"), and the reason the
 * logic is a module rather than inline `github-script` prose: an inline body
 * cannot be executed by a test, and a false ALL-CLEAR on a regression detector
 * is the error nothing downstream corrects.
 */
import { describe, it, expect } from 'vitest'
import { createRequire } from 'node:module'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const require_ = createRequire(import.meta.url)
const { verdictFor, groupByPr } = require_(
  resolve(HERE, '../../../../scripts/ci/nightly-verdict.js'),
)

const SEEDS = ['12345', '67890', '99999']
const leg = (seed, over = {}) => ({
  seed,
  pr_number: 1,
  head_sha: 'abc123',
  regression: false,
  merge_conflict: false,
  ...over,
})

describe('an incomplete answer is never a clean one', () => {
  it('a missing seed is unverified, not clean', () => {
    const v = verdictFor([leg('12345'), leg('67890')], SEEDS)
    expect(v.status).toBe('unverified')
    expect(v.missing).toEqual(['99999'])
  })

  it('two green seeds do not vouch for a third that never reported', () => {
    // The precise shape of the twelve dead nights, had the old code shipped a
    // tick from a partial set: everything that ran was fine, and that says
    // nothing whatever about the leg that was cancelled at the cap.
    expect(verdictFor([leg('12345'), leg('67890')], SEEDS).status).not.toBe('clean')
  })

  it('no legs at all is unverified, not clean', () => {
    expect(verdictFor([], SEEDS).status).toBe('unverified')
  })

  it('unverified outranks a regression seen in the seeds that did report', () => {
    // Deliberate: with a seed missing we do not know the full answer, and
    // "unverified" is the honest word for that. The regressed seed is still
    // in the warning log; what must not happen is a confident verdict built
    // from a partial set, in either direction.
    const v = verdictFor([leg('12345', { regression: true }), leg('67890')], SEEDS)
    expect(v.status).toBe('unverified')
  })
})

describe('a complete answer', () => {
  it('is clean when every seed is clean', () => {
    const v = verdictFor(SEEDS.map((s) => leg(s)), SEEDS)
    expect(v.status).toBe('clean')
    expect(v.total).toBe(3)
    expect(v.headSha).toBe('abc123')
  })

  it('condemns on ANY seed — order-dependence is the finding, not noise', () => {
    // pytest-randomly exists here because a failure that appears under one
    // seed and not another is real. Averaging it away deletes the signal the
    // seeds are for.
    const v = verdictFor(
      [leg('12345'), leg('67890'), leg('99999', { regression: true })],
      SEEDS,
    )
    expect(v.status).toBe('regression')
    expect(v.regressed.map((l) => l.seed)).toEqual(['99999'])
    expect(v.total).toBe(3)
  })

  it('reports a merge conflict ahead of any regression arm', () => {
    // A conflict means the suite never ran, so it is not a test result and
    // must not be rendered as one.
    const v = verdictFor(
      SEEDS.map((s) => leg(s, { merge_conflict: true, regression: true })),
      SEEDS,
    )
    expect(v.status).toBe('merge_conflict')
  })
})

describe('grouping', () => {
  it('splits legs by PR and keeps every one', () => {
    const legs = [
      { ...leg('12345'), pr_number: 10 },
      { ...leg('67890'), pr_number: 11 },
      { ...leg('99999'), pr_number: 10 },
    ]
    const byPr = groupByPr(legs)
    expect([...byPr.keys()].sort()).toEqual([10, 11])
    expect(byPr.get(10)).toHaveLength(2)
    expect(byPr.get(11)).toHaveLength(1)
  })

  it('tolerates an empty sweep without inventing a PR', () => {
    expect([...groupByPr([]).keys()]).toEqual([])
  })
})
