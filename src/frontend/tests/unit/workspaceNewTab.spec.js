/**
 * The console's Workspace entry points open a new tab (trinity-enterprise#456).
 *
 * The Workspace is where a conversation with an agent lives, and it is reached
 * from the operator console — so following the link threw away whatever the
 * operator was looking at. Both entry points now open a tab; the console tab
 * stays put.
 *
 * What is worth pinning, because each has a plausible wrong version:
 *
 *   * `rel="noopener"` on both. Same-origin, so this is hygiene rather than a
 *     boundary, but a `_blank` link without it is the pattern reviewers flag.
 *   * NO `window.open`. Vue Router's `guardEvent` skips interception when the
 *     target is `_blank` and on modified clicks, so `<router-link>` still
 *     resolves the href while the browser owns the click — hand-rolling the
 *     open would lose that and break cmd/ctrl-click.
 *   * The `?tab=session` redirect stays SAME-TAB. It is a `router.replace`
 *     rewrite of a navigation already in flight, not an entry point; making it
 *     spawn a tab would leave the original tab on a URL nobody asked for.
 */
import { describe, it, expect } from 'vitest'
import { buildNavLinks } from '../../src/utils/navLinks.js'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(here, '../../src', rel), 'utf8')

const NAVBAR = read('components/NavBar.vue')
const AGENT_DETAIL = read('views/AgentDetail.vue')
const PORTAL = read('views/Portal.vue')

// The element, not the file: a `target="_blank"` anywhere else in a 1500-line
// view would satisfy a whole-file assertion while the link stayed same-tab.
function linkAt(source, marker) {
  const at = source.indexOf(marker)
  expect(at, `marker not found: ${marker}`).toBeGreaterThan(-1)
  const open = source.lastIndexOf('<router-link', at)
  expect(open, `no <router-link> above: ${marker}`).toBeGreaterThan(-1)
  return source.slice(open, source.indexOf('>', at) + 1)
}

describe('the NavBar Workspace entry', () => {
  // #1925 turned the nav row into a priority+ strip, so the same link is
  // rendered three times (inline, overflow menu, hidden mirror) from one data
  // source. The attributes moved out of the template with it — pinning them
  // against the SFC's text now would pin whichever of the three copies happened
  // to be matched first, so they are read from the source of truth instead.
  const link = buildNavLinks({ path: '/' }).find((l) => l.id === 'workspace')

  it('exists', () => {
    expect(link).toBeTruthy()
    expect(link.to).toBe('/workspace')
  })

  it('opens a new tab', () => {
    expect(link.target).toBe('_blank')
  })

  it('carries rel="noopener"', () => {
    expect(link.rel).toMatch(/noopener/)
  })

  it('is the ONLY nav entry that leaves the console tab', () => {
    const blank = buildNavLinks({ path: '/', hasAnyEnterprise: true })
      .filter((l) => l.target === '_blank')
      .map((l) => l.id)
    expect(blank).toEqual(['workspace'])
  })

  it('and the strip actually binds those fields onto its links', () => {
    // The half a data-driven refactor can get wrong: correct data that never
    // reaches the DOM. Every row that renders a link must bind both.
    const rows = NAVBAR.match(/<router-link[\s\S]*?>/g) || []
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      if (!/v-for="link in/.test(row)) continue
      expect(row).toMatch(/:target="link\.target"/)
      expect(row).toMatch(/:rel="link\.rel"/)
    }
  })
})

describe('the Agent Detail "Continue in Workspace" link', () => {
  const link = linkAt(AGENT_DETAIL, "path: '/workspace'")

  it('opens a new tab', () => {
    expect(link).toMatch(/target="_blank"/)
  })

  it('carries rel="noopener"', () => {
    expect(link).toMatch(/rel="[^"]*noopener/)
  })
})

describe('what must NOT change', () => {
  it('the retired ?tab=session deep link still redirects in the SAME tab', () => {
    expect(AGENT_DETAIL).toMatch(/router\.replace\(\{ path: '\/workspace'/)
    const fn = AGENT_DETAIL.slice(AGENT_DETAIL.indexOf('function redirectRetiredSessionLink'))
    const body = fn.slice(0, fn.indexOf('\n}\n'))
    expect(body).not.toMatch(/_blank|window\.open/)
  })

  it('neither file hand-rolls the open', () => {
    // guardEvent already declines to intercept a _blank or modified click, so
    // window.open would only take that away — cmd/ctrl/shift-click included.
    for (const [name, src] of [['NavBar', NAVBAR], ['AgentDetail', AGENT_DETAIL]]) {
      expect(src, `${name} hand-rolls window.open`).not.toMatch(/window\.open\(/)
    }
  })

  it('the Workspace itself mounts no NavBar, so no entry can look active there', () => {
    // NavBar is mounted per view (App.vue renders none), and Portal.vue is not
    // one of the views that mounts it — which is why the entry's active-class
    // expression can never be true in the new tab. Pinning the ABSENCE is the
    // real guarantee; pinning the expression would be pinning dead code.
    expect(PORTAL).not.toMatch(/NavBar/)
  })
})
