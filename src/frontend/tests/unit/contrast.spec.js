import { describe, it, expect } from 'vitest'
import colors from 'tailwindcss/colors.js'
import {
  parseHex, relativeLuminance, contrastRatio, ratio, meetsAA, failures,
  AA_NORMAL, AA_LARGE,
} from '../../src/utils/contrast.js'

/**
 * #2201 — the design system's colour pairings, checked rather than asserted in
 * prose.
 *
 * The contract prescribes ink ladders and badge recipes; nothing measured
 * whether the pairs it prescribes clear AA, and several did not. These drive the
 * real Tailwind palette the tokens alias, so a palette bump or a token remap
 * turns this red instead of silently darkening the app below AA.
 */
const G = colors.gray
const WHITE = '#ffffff'
const GRAY_750 = '#2a303c' // the custom dark chrome shade, in the config

describe('the arithmetic itself', () => {
  it('matches the WCAG reference pairs', () => {
    expect(ratio('#ffffff', '#000000')).toBe(21)
    expect(ratio('#ffffff', '#ffffff')).toBe(1)
    expect(ratio('#000000', '#000000')).toBe(1)
  })

  it('is order-independent', () => {
    expect(ratio(G[500], WHITE)).toBe(ratio(WHITE, G[500]))
  })

  it('linearizes, rather than averaging channels', () => {
    // The distinction that made the issue's first numbers wrong in both
    // directions: a naive channel average puts mid-green far from its real
    // luminance. #22c55e is green-500.
    expect(relativeLuminance('#22c55e')).toBeGreaterThan(0.4)
    expect(relativeLuminance('#22c55e')).toBeLessThan(0.5)
  })

  it('accepts both hex forms and refuses anything else', () => {
    expect(parseHex('#fff')).toEqual([255, 255, 255])
    expect(parseHex('ffffff')).toEqual([255, 255, 255])
    for (const bad of ['', null, undefined, 'rgb(0,0,0)', '#ff', 'nope']) {
      expect(() => parseHex(bad), String(bad)).toThrow()
    }
  })

  it('never guesses "large" — the caller states it', () => {
    // `AUTO` and `Running` pass AA-large and are rendered at badge size; a
    // checker that inferred largeness would have blessed exactly the two the
    // report calls out.
    const amber600 = colors.amber[600]
    expect(meetsAA(amber600, WHITE)).toBe(false)
    expect(meetsAA(amber600, WHITE, { large: true })).toBe(true)
    expect(AA_NORMAL).toBe(4.5)
    expect(AA_LARGE).toBe(3)
  })

  it('reports a failure with its measured number', () => {
    const out = failures([{ name: 'muted', fg: G[400], bg: WHITE }])
    expect(out).toHaveLength(1)
    expect(out[0]).toContain('2.54:1')
    expect(out[0]).toContain('needs 4.5:1')
  })
})

// ---------------------------------------------------------------------------
// The contract's own ladders. A failure here means the DOCUMENT is wrong, not a
// component — which is the whole point of checking tokens rather than pages.
// ---------------------------------------------------------------------------

describe('ink ladders (design-system-contract.md)', () => {
  it('light ink clears AA on the surfaces it is used on', () => {
    expect(failures([
      { name: 'light primary on white', fg: G[900], bg: WHITE },
      { name: 'light primary on ground', fg: G[900], bg: G[50] },
      { name: 'light secondary on white', fg: G[600], bg: WHITE },
      { name: 'light secondary on ground', fg: G[600], bg: G[50] },
      { name: 'light secondary on chrome', fg: G[600], bg: G[100] },
      { name: 'light tertiary on white', fg: G[500], bg: WHITE },
      { name: 'light tertiary on ground', fg: G[500], bg: G[50] },
    ])).toEqual([])
  })

  it('dark ink clears AA on the surfaces it is used on', () => {
    expect(failures([
      { name: 'dark primary on surface', fg: G[100], bg: G[800] },
      { name: 'dark secondary on surface', fg: G[300], bg: G[800] },
      { name: 'dark secondary on chrome', fg: G[300], bg: GRAY_750 },
      { name: 'dark tertiary on surface', fg: G[400], bg: G[800] },
      { name: 'dark tertiary on ground', fg: G[400], bg: G[900] },
      { name: 'dark tertiary on chrome', fg: G[400], bg: GRAY_750 },
    ])).toEqual([])
  })

  it('pins the two grounds the tertiary tier does NOT clear', () => {
    // Not a bug to fix by darkening every muted label — a measured boundary the
    // contract now states: tertiary ink is for surface and ground, never for a
    // chrome fill. Written as an expectation so that if a future palette change
    // makes it pass, someone revisits the rule rather than leaving a stale
    // prohibition in the doc.
    expect(meetsAA(G[500], G[100])).toBe(false) // 4.39:1, light chrome
    expect(meetsAA(G[400], G[700])).toBe(false) // 4.06:1, dark border-strong
  })

  it('keeps gray-500 out of dark meta text, as the contract says', () => {
    // "gray-500 is the floor — disabled/decoration only, never meta text."
    expect(meetsAA(G[500], G[800])).toBe(false) // 3.04:1
    expect(meetsAA(G[500], G[700])).toBe(false) // 2.13:1
  })
})

describe('status text tiers', () => {
  const FAMILIES = {
    'status-success': colors.green,
    'status-warning': colors.yellow,
    'status-danger': colors.red,
    'status-info': colors.blue,
    'status-urgent': colors.orange,
    'state-autonomous': colors.amber,
    'state-locked': colors.rose,
    'accent-purple': colors.purple,
    'action-primary': colors.indigo,
  }

  it('700 on a light surface clears AA for every family', () => {
    expect(failures(
      Object.entries(FAMILIES).map(([name, f]) => ({ name: `${name}-700 on white`, fg: f[700], bg: WHITE }))
    )).toEqual([])
  })

  it('400 on a dark surface clears AA for every family', () => {
    expect(failures(
      Object.entries(FAMILIES).map(([name, f]) => ({ name: `${name}-400 on gray-800`, fg: f[400], bg: G[800] }))
    )).toEqual([])
  })

  it('records WHY the light tier is 700 and not 600', () => {
    // The warm families fail at 600 and the cool ones pass, which is why the
    // rule is stated per-tier rather than per-family: one number nobody has to
    // look up.
    expect(meetsAA(colors.green[600], WHITE)).toBe(false)   // 3.30
    expect(meetsAA(colors.yellow[600], WHITE)).toBe(false)  // 2.94
    expect(meetsAA(colors.amber[600], WHITE)).toBe(false)   // 3.19
    expect(meetsAA(colors.orange[600], WHITE)).toBe(false)  // 3.56
    expect(meetsAA(colors.red[600], WHITE)).toBe(true)      // 4.83
    expect(meetsAA(colors.blue[600], WHITE)).toBe(true)     // 5.17
  })
})

describe('solid badges carrying white ink', () => {
  it('the Operations count badge clears AA in both states', () => {
    expect(failures([
      { name: 'white on urgent-700 (pending)', fg: WHITE, bg: colors.orange[700] },
      { name: 'white on danger-600 (critical)', fg: WHITE, bg: colors.red[600] },
    ])).toEqual([])
  })

  it('records the shades it replaced', () => {
    expect(ratio(WHITE, colors.orange[500])).toBe(2.8)
    expect(ratio(WHITE, colors.red[500])).toBe(3.76)
  })
})

describe('BaseBadge recipe (token-100 ground, token-700 ink)', () => {
  it('clears AA for every family in light', () => {
    const fams = [colors.green, colors.yellow, colors.red, colors.blue, colors.orange,
                  colors.amber, colors.rose, colors.purple, colors.indigo]
    expect(failures(fams.map((f, i) => ({ name: `family ${i}`, fg: f[700], bg: f[100] })))).toEqual([])
  })
})
