/**
 * WCAG contrast, as arithmetic (#2201).
 *
 * The design system prescribes ink ladders and badge recipes in prose; nothing
 * checked that the pairs it prescribes actually clear AA. A sweep found several
 * that do not — muted light text at 2.54:1 on every page, the Operations badge
 * at 2.80:1 — so this is the mechanical half the contract was missing: a pure
 * module the token spec can drive, in the same spirit as the raw-colour ratchet.
 *
 * Pure sRGB per WCAG 2.x: linearize each channel, weight, add 0.05 to both.
 * An approximate scan (plain channel averages) reports ratios that are wrong in
 * BOTH directions, which is why the issue's own first numbers were recalculated
 * before it was filed — so the linearization here is not a detail.
 */

/** `#rgb` / `#rrggbb` → [r, g, b] in 0..255. Throws on anything else: a silent
 *  0,0,0 would make every ratio look safe. */
export function parseHex(hex) {
  const raw = String(hex || '').trim().replace(/^#/, '')
  const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw
  if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`not a hex colour: ${hex}`)
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16))
}

/** WCAG relative luminance. */
export function relativeLuminance(hex) {
  const [r, g, b] = parseHex(hex).map((v) => {
    const s = v / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Contrast ratio between two opaque colours, 1..21. Order-independent. */
export function contrastRatio(a, b) {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/** Rounded to 2dp, the form every report and message uses. */
export function ratio(a, b) {
  return Math.round(contrastRatio(a, b) * 100) / 100
}

export const AA_NORMAL = 4.5
export const AA_LARGE = 3

/**
 * Does this pair clear AA?
 *
 * `large` is deliberately opt-in per call and never inferred: the issue's own
 * table shows why — `AUTO` and `Running` pass AA-large and are rendered at badge
 * size, so a checker that guessed "large" from the token name would bless
 * exactly the two the report calls out.
 */
export function meetsAA(fg, bg, { large = false } = {}) {
  return contrastRatio(fg, bg) >= (large ? AA_LARGE : AA_NORMAL)
}

/**
 * Check a table of `{ name, fg, bg, large? }` pairings and return the failures,
 * each with its measured ratio and the threshold it missed — so a spec's message
 * names the number rather than just saying "too low".
 */
export function failures(pairs, { large = false } = {}) {
  return (pairs || [])
    .map((p) => {
      const isLarge = p.large ?? large
      const r = ratio(p.fg, p.bg)
      const need = isLarge ? AA_LARGE : AA_NORMAL
      return r >= need ? null : `${p.name}: ${r}:1 (needs ${need}:1) — ${p.fg} on ${p.bg}`
    })
    .filter(Boolean)
}
