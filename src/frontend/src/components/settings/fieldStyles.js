/**
 * Shared field styling for the Settings panels.
 *
 * Extracted from `Settings.vue`'s `RETENTION_INPUT_CLASS` when ent#375 put a
 * second numeric-window panel (Workspace sessions) on the same tab as Data
 * Retention and the two did not match — different width, different dark
 * background, different focus ring, spinners on one and not the other.
 *
 * A copied class string would have drifted again on the next edit, and "these
 * two forms should look identical" is a property, not a coincidence. One
 * constant, imported by both, makes it true by construction.
 */
export const SETTINGS_NUMBER_INPUT_CLASS =
  'w-24 px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 ' +
  'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 ' +
  'focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:border-transparent ' +
  'disabled:opacity-60 disabled:cursor-not-allowed ' +
  '[appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none ' +
  '[&::-webkit-inner-spin-button]:appearance-none'

/** The primary action button used by every Settings panel. */
export const SETTINGS_PRIMARY_BUTTON_CLASS =
  'inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium ' +
  'rounded-md text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50'

/**
 * The TEXT field used by every Settings panel (#2464).
 *
 * SHAPE ONLY — no width. Callers add their own layout class (`w-full` in a
 * grid, `flex-1` beside a button), because that is the one property the two
 * existing call sites legitimately disagree about and baking either in would
 * force the other to override it.
 *
 * Every property is stated explicitly, and that is the whole point of this
 * constant rather than a stylistic preference. The repo does NOT load
 * `@tailwindcss/forms` (`tailwind.config.js` plugins = typography only), so
 * there are no form-element base styles to inherit:
 *
 *   - `border-gray-300` alone sets a border COLOUR on a border whose width is
 *     0 under Tailwind preflight — nothing renders. The bare `border` is what
 *     makes it visible, and its absence is invisible in review because the
 *     class string reads as if it were complete.
 *   - padding does not exist unless `px-3 py-2` says so.
 *   - the light background is otherwise the UA default rather than a decision.
 *
 * That is exactly how ent#463's intake form shipped as five borderless,
 * unpadded bars next to the Admin sign-in email field it was meant to match.
 */
export const SETTINGS_TEXT_INPUT_CLASS =
  'block px-3 py-2 text-sm rounded-md shadow-sm ' +
  'border border-gray-300 dark:border-gray-600 ' +
  'bg-white dark:bg-gray-700 dark:text-white placeholder-gray-400 ' +
  'focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 ' +
  'disabled:opacity-60 disabled:cursor-not-allowed'

/**
 * The label above a Settings field (#2464).
 *
 * Same argument as the input constants: "the labels on this tab look alike" is
 * a property, not a coincidence. Shape only — a caller that needs a trailing
 * "(optional)" hint styles that span itself.
 */
export const SETTINGS_FIELD_LABEL_CLASS =
  'block text-sm font-medium text-gray-700 dark:text-gray-300'
