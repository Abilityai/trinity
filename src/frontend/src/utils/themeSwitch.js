/**
 * trinity-enterprise#625 — the decidable half of the Workspace theme switch.
 *
 * The switch reads `useThemeStore` (`theme`: the CHOICE — light | dark | system;
 * `isDark`: the RESOLVED state once `system` is consulted against the OS).
 * Everything a test can decide without a DOM lives here: the option set the
 * NavBar and the Workspace share, what the trigger says, and which icon it
 * wears — so the control cannot claim "System" while showing a sun.
 */

/** The three choices, in display order. `id` is exactly what `setTheme` takes. */
export const THEME_OPTIONS = Object.freeze([
  Object.freeze({ id: 'light', label: 'Light', icon: 'sun' }),
  Object.freeze({ id: 'dark', label: 'Dark', icon: 'moon' }),
  Object.freeze({ id: 'system', label: 'System', icon: 'monitor' }),
])

export const THEME_IDS = Object.freeze(THEME_OPTIONS.map((o) => o.id))

/** An unknown stored value reads as `system` — the store's own default. */
export function normalizeTheme(theme) {
  return THEME_IDS.includes(theme) ? theme : 'system'
}

/** The theme actually on screen: the choice, or the OS answer when `system`. */
export function resolvedTheme(theme, isDark) {
  const t = normalizeTheme(theme)
  if (t === 'system') return isDark ? 'dark' : 'light'
  return t
}

/**
 * What the trigger says. `system` shows the RESOLVED state honestly
 * ("System · dark") — AC #3: a person must be able to tell that "System"
 * currently means dark without opening the menu.
 */
export function themeSwitchLabel(theme, isDark) {
  const t = normalizeTheme(theme)
  if (t === 'system') return `System · ${resolvedTheme(t, isDark)}`
  return THEME_OPTIONS.find((o) => o.id === t).label
}

/** The trigger's icon follows the RESOLVED theme, never the choice. */
export function themeSwitchIcon(theme, isDark) {
  return resolvedTheme(theme, isDark) === 'dark' ? 'moon' : 'sun'
}

/** Accessible name for the trigger — names the control AND the current choice. */
export function themeSwitchAriaLabel(theme, isDark) {
  return `Theme: ${themeSwitchLabel(theme, isDark)}`
}
