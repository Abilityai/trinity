/**
 * The shared field recipe for BaseInput / BaseSelect / BaseTextarea
 * (docs/memory/design-system.md §5). One constant, three consumers —
 * "these controls look identical" is a property, not a coincidence
 * (the settings/fieldStyles.js lesson, ent#375).
 */

// Field bg, border-strong, radius 6px, padding 8×11, 13.5 primary ink.
export const FIELD_CLASS =
  'w-full rounded-md border bg-white dark:bg-gray-900 ' +
  'px-[11px] py-2 text-[13.5px] text-gray-900 dark:text-gray-100 ' +
  'placeholder:text-gray-500 dark:placeholder:text-gray-500 ' +
  'focus:outline-none focus:ring-[3px] ' +
  'disabled:opacity-45 disabled:cursor-not-allowed'

// Focus = accent border + ring; the ring replaces the outline.
export const FIELD_VALID_CLASS =
  'border-gray-300 dark:border-gray-700 ' +
  'focus:border-action-primary-600 dark:focus:border-action-primary-500 ' +
  'focus:ring-action-primary-500/40 dark:focus:ring-action-primary-400/40'

// Invalid = danger border, danger ring at 25%.
export const FIELD_INVALID_CLASS =
  'border-status-danger-500 focus:border-status-danger-500 ' +
  'focus:ring-status-danger-500/25'

export const LABEL_CLASS = 'block text-[13px] font-[550] text-gray-900 dark:text-gray-100'

export const HELP_CLASS = 'text-xs text-gray-500 dark:text-gray-400'

export const ERROR_TEXT_CLASS =
  'flex items-start gap-1.5 text-[12.5px] text-status-danger-700 dark:text-status-danger-300'
