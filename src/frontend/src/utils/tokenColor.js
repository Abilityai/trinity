/**
 * A design-system colour token's value, for surfaces that cannot take a class
 * or a CSS variable — a uPlot canvas strokes with a literal (trinity-enterprise#938).
 *
 * Resolves by rendering a probe carrying the token's text class and reading
 * the computed colour, so the stroke follows `tailwind.config.js` instead of a
 * copied hex. Returns `#rrggbb` (callers append an alpha byte for fills), or
 * `null` where nothing is computed (a test DOM); the caller supplies the
 * fallback, since an undefined uPlot stroke draws nothing.
 */
const cache = new Map()

export function tokenColor(textClass) {
  if (cache.has(textClass)) return cache.get(textClass)
  let hex = null
  if (typeof document !== 'undefined' && document.body) {
    const probe = document.createElement('span')
    probe.className = textClass
    probe.style.display = 'none'
    document.body.appendChild(probe)
    const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(probe).color || '')
    probe.remove()
    if (m) hex = '#' + m.slice(1, 4).map((n) => Number(n).toString(16).padStart(2, '0')).join('')
  }
  if (hex) cache.set(textClass, hex)
  return hex
}
