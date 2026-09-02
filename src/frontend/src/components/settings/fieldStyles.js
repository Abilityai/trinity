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
 * The text input used by Settings panels.
 *
 * The shape the admin sign-in email field established, made shared for the
 * same reason as the number input above: ent#463's intake form declared only
 * `rounded-md border-gray-300 dark:bg-gray-700`, and this project does NOT
 * load `@tailwindcss/forms` — so a bare `border-<color>` sets a colour on a
 * zero-width border and the fields rendered as unpadded, borderless bars.
 * The width, the padding and the background have to be stated explicitly.
 */
export const SETTINGS_TEXT_INPUT_CLASS =
  'block w-full px-3 py-2 text-sm rounded-md shadow-sm ' +
  'border border-gray-300 dark:border-gray-600 ' +
  'bg-white dark:bg-gray-700 text-gray-900 dark:text-white ' +
  'placeholder-gray-400 dark:placeholder-gray-500 ' +
  'focus:outline-none focus:ring-action-primary-500 focus:border-action-primary-500 ' +
  'disabled:opacity-60 disabled:cursor-not-allowed'

/** The label above a Settings field. */
export const SETTINGS_FIELD_LABEL_CLASS =
  'block text-sm font-medium text-gray-700 dark:text-gray-300'
