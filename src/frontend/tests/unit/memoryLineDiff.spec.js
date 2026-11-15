/**
 * trinity-enterprise#962 ruling 13 — what one memory change did, as whole lines
 * added and removed against the version before. Pure function, no DOM: the
 * rendering is proven by mounting PortalAgentMemory (portalAgentMemory.spec.js).
 */
import { describe, expect, it } from 'vitest'
import { lineDiff } from '../../src/utils/lineDiff'

describe('lineDiff', () => {
  it('a first write is all additions', () => {
    expect(lineDiff('', 'Prefers PDF.\nBased in Lyon.')).toEqual({
      added: ['Prefers PDF.', 'Based in Lyon.'], removed: [],
    })
  })

  it('clearing the notes is all removals', () => {
    expect(lineDiff('Prefers PDF.\nBased in Lyon.', '')).toEqual({
      added: [], removed: ['Prefers PDF.', 'Based in Lyon.'],
    })
  })

  it('an identical rewrite changes nothing', () => {
    expect(lineDiff('A\nB', 'A\nB')).toEqual({ added: [], removed: [] })
  })

  it('only the lines that changed are listed, unchanged ones are not', () => {
    expect(lineDiff('Prefers short answers.\nOpen loop #22: the Q4 budget memo',
      'Prefers short answers.\nOpen loop #23: the Q4 budget memo')).toEqual({
      added: ['Open loop #23: the Q4 budget memo'],
      removed: ['Open loop #22: the Q4 budget memo'],
    })
  })

  it('a repeated line counts once per copy, and a moved line is a removal plus an addition', () => {
    expect(lineDiff('A\nA\nB', 'A\nB')).toEqual({ added: [], removed: ['A'] })
    expect(lineDiff('A\nB\nC', 'C\nA\nB')).toEqual({ added: ['C'], removed: ['C'] })
  })

  it('blank lines and trailing whitespace are not changes; any line break counts', () => {
    expect(lineDiff('A\n\nB', 'A\r\nB  \n')).toEqual({ added: [], removed: [] })
  })

  it('tolerates missing values', () => {
    expect(lineDiff(null, undefined)).toEqual({ added: [], removed: [] })
  })
})
