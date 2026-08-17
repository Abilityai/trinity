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
