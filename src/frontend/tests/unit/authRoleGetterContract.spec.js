/**
 * The auth store's role getter is a CONTRACT, and a Pinia getter name is one
 * that fails silently: `authStore.userRole` on a store that only defines
 * `role` is `undefined`, so `=== 'admin'` is false forever and a role-gated
 * card is simply never seen. That is how the #2380 hardening guide and the
 * #2381 sign-in-email nudge shipped permanently hidden — nothing threw, no
 * test reached the SFC wiring (vitest runs in `node` with no mount harness),
 * and the eyeball step for ent#437 was the first thing to notice.
 *
 * So the name is pinned statically: the store must define `role`, and no
 * component may read `userRole`.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', '..', 'src')

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|js)$/.test(name)) out.push(p)
  }
  return out
}

describe('auth store role getter contract', () => {
  it('the store defines a `role` getter', () => {
    const src = readFileSync(join(SRC, 'stores', 'auth.js'), 'utf8')
    expect(src).toMatch(/\n\s{4}role\s*\(\)\s*\{/)
  })

  it('no component reads the nonexistent `userRole`', () => {
    const offenders = walk(SRC)
      .filter((p) => /\.userRole\b/.test(readFileSync(p, 'utf8')))
      .map((p) => p.slice(SRC.length + 1))
    expect(offenders).toEqual([])
  })
})
