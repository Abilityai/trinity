/**
 * ent#545 — the activation-funnel footer's one decision, pure.
 *
 * The enterprise funnel read no longer mints `installation_id` on a GET, so the
 * wire may carry `null`; the footer must say so instead of rendering a blank
 * "Install  ·" — and must not claim "not minted" for a value that is merely
 * absent or malformed. The pure function is the only unit-testable home
 * (`vitest.config.js` pins `environment: 'node'`, no mount harness); a
 * source-structure guard pins that the panel actually routes through it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  INSTALL_ID_NOT_MINTED,
  INSTALL_ID_UNAVAILABLE,
  installIdFooter,
} from '../../src/components/settings/funnelFormat'

const ID = '9c3f00a1-1111-2222-3333-444455556666'

describe('installIdFooter', () => {
  it('names the id when it is minted', () => {
    expect(installIdFooter(ID)).toEqual({ state: 'minted', text: `Install ${ID}` })
  })

  it('trims padding rather than rendering it', () => {
    expect(installIdFooter(`  ${ID}\n`)).toEqual({ state: 'minted', text: `Install ${ID}` })
  })

  it("reads the backend's explicit null as not minted, and says how an id comes to exist", () => {
    expect(installIdFooter(null)).toEqual({ state: 'not_minted', text: INSTALL_ID_NOT_MINTED })
    expect(INSTALL_ID_NOT_MINTED).toMatch(/minted/)
  })

  it.each([undefined, '', '   ', 42, 0, true, {}, [], [ID]])(
    'does not claim "not minted" for %j — that is unavailable, not evidence',
    (value) => {
      expect(installIdFooter(value)).toEqual({ state: 'unavailable', text: INSTALL_ID_UNAVAILABLE })
    }
  )

  it('never yields a blank leading phrase, whatever the wire carries', () => {
    for (const value of [null, undefined, '', ' ', ID, 7]) {
      const { text } = installIdFooter(value)
      expect(text.trim().length).toBeGreaterThan('Install'.length)
      expect(text).not.toMatch(/^Install\s*$/)
    }
  })

  it('the copy leaks no JS-isms', () => {
    for (const copy of [INSTALL_ID_NOT_MINTED, INSTALL_ID_UNAVAILABLE]) {
      expect(copy).not.toMatch(/undefined|null|NaN|\[object/)
    }
  })
})

describe('ActivationFunnelPanel wiring (source-structure guard)', () => {
  const panel = stripComments(
    readFileSync(
      fileURLToPath(new URL('../../src/components/settings/ActivationFunnelPanel.vue', import.meta.url)),
      'utf8'
    )
  )

  it('routes the footer through the pure decision', () => {
    expect(panel).toMatch(/import \{[^}]*\binstallIdFooter\b[^}]*\} from '\.\/funnelFormat'/)
    expect(panel).toMatch(/installIdFooter\(installationId\.value\)/)
    // The last inch: the computed must actually reach the DOM. Without this the
    // guard passes with the footer rendered as static text (ent#545 review).
    expect(panel).toMatch(/\{\{\s*installFooter\.text\s*\}\}/)
  })

  it('passes the wire value through untouched — null stays null, absent stays absent', () => {
    // Positive anchor first: a negative-only guard passes vacuously on an
    // absent or renamed file. The exact assignment is what the negatives
    // below then constrain.
    expect(panel).toMatch(/installationId\.value = r\.data\?\.installation_id\s*$/m)
    expect(panel).not.toMatch(/installation_id\s*(\|\||\?\?)\s*(''|null|"")/)
    expect(panel).not.toMatch(/Install \{\{ installationId \}\}/)
  })
})
