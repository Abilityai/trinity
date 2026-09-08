/**
 * #2620 — a frontend gate must not name an entitlement that no longer exists.
 *
 * When a module moves from the entitled seam into OSS core, its feature id
 * stops being registered. Any `isEntitled('<that id>')` left behind is then
 * false on EVERY build, so the capability ships while the UI that reaches it
 * silently disappears — and nothing fails, which is what makes this class
 * expensive to find. It has now happened twice:
 *
 *   * `client_portal` (ent#356) — the NavBar's Workspace link, fixed there
 *     with the reasoning written into the template.
 *   * `shared_sessions` (ent#443) — the Room budget defaults panel, which was
 *     hidden on every install for the whole life of the OSS move. Rooms
 *     worked; the only dial for their message/cost/TTL budgets did not
 *     render, and it was found by an operator asking why a room stopped at
 *     60 messages.
 *
 * So the retired ids are listed here and the gates are checked against them.
 * Adding an id to this list is the second half of moving a module to OSS.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'fs'
import { fileURLToPath } from 'url'
import path from 'path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '../../src')

/** Feature ids that are OSS core now — gating on them is always false. */
const RETIRED_ENTITLEMENTS = ['client_portal', 'shared_sessions']

function stripComments(code) {
  return code
    .replace(/<!--[\s\S]*?-->/g, '')      // template comments
    .replace(/\/\*[\s\S]*?\*\//g, '')     // block comments
    .replace(/^[ \t]*\/\/.*$/gm, '')      // whole-line // comments
}

function walk(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) return walk(full)
    return /\.(vue|js)$/.test(full) ? [full] : []
  })
}

describe('#2620 retired entitlement gates', () => {
  it('no gate references a feature id that moved to OSS core', () => {
    const offenders = []
    for (const file of walk(SRC)) {
      const code = stripComments(readFileSync(file, 'utf8'))
      for (const id of RETIRED_ENTITLEMENTS) {
        // The comments in NavBar.vue and RoomBudgetDefaultsPanel.vue explain
        // this history on purpose, so they are stripped before matching —
        // documenting the trap must not read as falling into it.
        if (new RegExp(`isEntitled\\(\\s*['"\`]${id}['"\`]`).test(code)) {
          offenders.push(`${path.relative(SRC, file)} gates on '${id}'`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('the room budget panel renders without an entitlement check', () => {
    // The specific regression. The panel is the ONLY UI that writes
    // room_default_max_messages / _max_cost_usd / _ttl_hours; if it hides,
    // those three settings become API-only with nothing saying so.
    const file = path.join(SRC, 'components/settings/RoomBudgetDefaultsPanel.vue')
    const code = stripComments(readFileSync(file, 'utf8'))
    expect(code).not.toMatch(/isEntitled\(/)
    expect(code).not.toMatch(/v-if="entitled"/)
    // and it must still actually fetch on mount, or it renders empty instead
    expect(code).toMatch(/onMounted\(load\)/)
  })

  it('the panel still relies on the server for authorization', () => {
    // Removing the gate must not read as removing the protection: the routes
    // are `require_admin`, and the Retention tab is adminOnly.
    const settings = readFileSync(path.join(SRC, 'views/Settings.vue'), 'utf8')
    expect(settings).toMatch(/id: 'retention',[^\n]*adminOnly: true/)
  })
})
