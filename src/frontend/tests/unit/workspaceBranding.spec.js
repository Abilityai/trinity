/**
 * ent#556 — the Workspace says which product it is.
 *
 * The top left was a hand-drawn outline `<svg>` and the bare word "Workspace",
 * in two places that had to agree and had no shared source. The real marks
 * (`assets/trinity-logo.svg` / `-white.svg`) already shipped and were already
 * used by `NavBar` and `PublicChat`; the Workspace simply never adopted them.
 *
 * A source-guard spec, this project's pattern for wiring only source can answer
 * — `vitest` runs `environment: 'node'` with no component-mount harness. The one
 * genuinely decidable fact (the name, and where the mark may point) lives in a
 * plain module and is asserted as a value, not as a string in markup.
 *
 * Comments are stripped before every scan: a comment explaining what NOT to
 * write necessarily contains the offending string.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import { WORKSPACE_BRAND_NAME, WORKSPACE_ROOT } from '@/components/portal/portalBrand'

const read = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))

const BRAND = read('../../src/components/portal/PortalBrand.vue')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')
const PORTAL = read('../../src/views/Portal.vue')
const NAVBAR = read('../../src/components/NavBar.vue')
const ROUTER = read('../../src/router/index.js')

// The generic outline icon the brand corner used to draw. It is still a
// legitimate EMPTY-STATE illustration elsewhere in `Portal.vue` (the stage
// zones), so the guard is scoped to the brand blocks rather than the file.
const GENERIC_ICON_PATH = 'M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z'

describe('ent#556 the name is a value, not a string in two templates', () => {
  it('names the product', () => {
    expect(WORKSPACE_BRAND_NAME).toBe('Trinity Workspace')
  })

  it('points the mark at the Workspace root and nowhere else', () => {
    // A client session holds no `users` row, so a platform route would be a
    // door that bounces exactly the audience this surface exists for.
    expect(WORKSPACE_ROOT).toBe('/workspace')
  })

  it('renders the name from the module rather than re-typing it', () => {
    expect(BRAND).toContain('WORKSPACE_BRAND_NAME')
    // Not a literal in the template — that is how the two surfaces drifted.
    expect(BRAND).not.toContain('>Trinity Workspace<')
  })
})

describe('ent#556 both surfaces carry the same mark, from one component', () => {
  it('the signed-in shell renders it', () => {
    expect(SIDEBAR).toContain('<PortalBrand')
    expect(SIDEBAR).toContain("import PortalBrand from './PortalBrand.vue'")
  })

  it('the sign-in screen renders it', () => {
    expect(PORTAL).toContain('<PortalBrand')
    expect(PORTAL).toContain("import PortalBrand from '@/components/portal/PortalBrand.vue'")
  })

  it('neither brand corner draws the old generic icon any more', () => {
    expect(SIDEBAR).not.toContain(GENERIC_ICON_PATH)
    // `Portal.vue` legitimately keeps it in the stage empty-states; what must
    // be gone is the brand block, identified by the wording it carried.
    expect(PORTAL).not.toContain('<span class="font-semibold text-lg">Workspace</span>')
  })

  it('no bare "Workspace" wordmark is left in either brand block', () => {
    expect(SIDEBAR).not.toContain('<span class="font-semibold">Workspace</span>')
  })
})

describe('ent#556 dark mode is correct', () => {
  it('uses both marks with the light/dark swap', () => {
    expect(BRAND).toContain('trinity-logo.svg')
    expect(BRAND).toContain('trinity-logo-white.svg')
  })

  it('follows NavBar rather than inventing a second pattern', () => {
    // The standard mark hides in dark; the white mark shows only in dark. A
    // dark logo on a dark ground is the one failure this must not have.
    expect(BRAND).toMatch(/trinity-logo\.svg[\s\S]{0,240}?dark:hidden/)
    expect(BRAND).toMatch(/trinity-logo-white\.svg[\s\S]{0,240}?hidden dark:block/)
    expect(NAVBAR).toMatch(/trinity-logo\.svg[\s\S]{0,240}?dark:hidden/)
    expect(NAVBAR).toMatch(/trinity-logo-white\.svg[\s\S]{0,240}?hidden dark:block/)
  })
})

describe('ent#556 the mark is neither dead nor unlabelled', () => {
  it('is a link only when it has somewhere to go', () => {
    // `router-link` when `to` is set, a plain element when it is not — the
    // sign-in screen has no destination that would mean anything.
    expect(BRAND).toContain("to ? 'router-link' : 'div'")
  })

  it('carries an accessible name whether or not the wordmark is visible', () => {
    // The images are decorative — the name is on the wrapper, so a screen
    // reader says the product ONCE, and still says it if the wordmark is
    // truncated away at a narrow sidebar width.
    expect(BRAND).toContain(':aria-label="WORKSPACE_BRAND_NAME"')
    expect(BRAND).toContain('aria-hidden="true"')
    expect(BRAND).not.toContain('alt="Trinity"')
  })

  it('the signed-in shell links to the Workspace root, not a platform route', () => {
    expect(SIDEBAR).toContain(':to="WORKSPACE_ROOT"')
  })

  it('the sign-in screen renders it inert', () => {
    // No `:to` on that instance: signed out, the reader is already at the root.
    expect(PORTAL).toMatch(/<PortalBrand(?![^>]*:to=)[^>]*\/>/)
  })
})

describe('ent#556 it fits the header it lives in', () => {
  it('keeps the mark at the size the icon it replaces used', () => {
    // ent#547 is SHORTENING this band; the brand must not grow it.
    expect(BRAND).toContain("markSize: { type: String, default: 'h-6 w-6' }")
  })

  it('truncates rather than pushing the badges out of the band', () => {
    // The sidebar resizes down to SIDEBAR_MIN (200px, ent#492) and this row
    // also carries the ask and unread badges.
    expect(BRAND).toContain('truncate min-w-0')
    expect(BRAND).toContain("class=\"flex items-center gap-2 min-w-0\"")
  })
})

describe('ent#556 the tab title identifies the product and the subject', () => {
  it('every Workspace route still carries a title', () => {
    // The router renders `Trinity — <label>`, so a Workspace tab reads
    // "Trinity — Workspace". The platform has ONE title format (#1418) and
    // this surface does not get a second one.
    const workspaceTitles = ROUTER.match(/title: 'Workspace'/g) || []
    expect(workspaceTitles.length).toBeGreaterThanOrEqual(3)
  })

  it('names the agent on the one route whose subject the route knows', () => {
    expect(ROUTER).toContain('`Workspace · ${agentTabTitle(to.params.agentName)}`')
  })

  it('keeps the agent-scoped platform titles it must not have broken', () => {
    expect(ROUTER).toContain('title: (to) => agentTabTitle(to.params.name)')
  })
})
