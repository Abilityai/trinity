/**
 * What one memory change did, as whole lines added and removed against the
 * version before (trinity-enterprise#962 ruling 13: the Workspace "Changes"
 * rows). A line is compared with its trailing whitespace dropped, blank lines
 * are not changes, and every line break counts (`\r\n`, `\r`, `\n`).
 *
 * An LCS over the two line lists, so a repeated line counts once per copy and
 * a moved line reads as one removal plus one addition. Notes are bounded
 * (the write door caps them), but the table is guarded anyway: past
 * MAX_CELLS the diff falls back to per-line counts, which is exact for
 * additions and removals and only loses move detection.
 *
 * Returns `{ added: string[], removed: string[] }`, each in document order.
 */
const MAX_CELLS = 250_000

function linesOf(text) {
  return String(text ?? '')
    .split(/\r\n|\r|\n/)
    .map((line) => line.replace(/\s+$/, ''))
    .filter((line) => line.trim() !== '')
}

function byCounts(before, after) {
  const left = new Map()
  for (const line of before) left.set(line, (left.get(line) || 0) + 1)
  const added = []
  for (const line of after) {
    const n = left.get(line) || 0
    if (n > 0) left.set(line, n - 1)
    else added.push(line)
  }
  const right = new Map()
  for (const line of after) right.set(line, (right.get(line) || 0) + 1)
  const removed = []
  for (const line of before) {
    const n = right.get(line) || 0
    if (n > 0) right.set(line, n - 1)
    else removed.push(line)
  }
  return { added, removed }
}

export function lineDiff(previous, next) {
  const a = linesOf(previous)
  const b = linesOf(next)
  if (a.length * b.length > MAX_CELLS) return byCounts(a, b)
  // lcs[i][j] = LCS length of a[i:] and b[j:]
  const lcs = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const added = []
  const removed = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { i += 1; j += 1 }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { removed.push(a[i]); i += 1 }
    else { added.push(b[j]); j += 1 }
  }
  while (i < a.length) { removed.push(a[i]); i += 1 }
  while (j < b.length) { added.push(b[j]); j += 1 }
  return { added, removed }
}
