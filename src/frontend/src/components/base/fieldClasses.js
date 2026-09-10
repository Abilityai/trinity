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

// The GHOST recipe (#2662) — the same control wearing chat chrome instead of
// form chrome: no border, no fill, sized to its content, hover tint borrowed
// from the Workspace agent picker it now sits beside.
//
// It exists as a variant of the primitive rather than as a hand-rolled select
// in the composer because `design-system-contract.md` asks for primitives over
// lookalikes, and the ent#403 defect was never that `BaseSelect` was the wrong
// COMPONENT — it was that `FIELD_CLASS`, the settings-form recipe, is the wrong
// dress for a preference sitting under a chat input.
//
// No `w-full`: a ghost select is as wide as the option it is showing. That is
// the property that keeps it out of the composer's width budget.
export const FIELD_GHOST_CLASS =
  // `h-11`: the ghost select is a 44px box, the same box as the composer row's
  // icon buttons (#2259) — a 30px select beside 44px buttons is a 30px tap
  // target on a phone. The native select centres its text in a fixed height.
  // `border` with NO colour here, exactly like FIELD_CLASS: the 1px is reserved
  // so the focus border costs no layout shift, but WHICH colour it takes is the
  // valid/invalid arms' business. Carrying `border-transparent` in this base
  // string made the ghost's error state borderless — `.border-transparent` is
  // emitted AFTER `.border-status-danger-500`, and the two are equal
  // specificity, so the resting keyword silently beat the error colour
  // (measured, not reasoned: transparent on ghost against rgb(239,68,68) on
  // field). Same cascade trap as the shell's, one level down.
  'h-11 w-auto max-w-full rounded-lg border bg-transparent ' +
  'pl-2 py-1 text-[13.5px] text-gray-600 dark:text-gray-300 ' +
  // Hover tints the GROUND only, matching the Workspace agent picker exactly.
  // Deliberately no hover ink change: the sibling "New chat" button spends two
  // more raw-gray classes on one, and the raw-colour ratchet (#2605) is a
  // budget — four classes buy the whole affordance, six buy a nuance.
  // Dark hover is gray-750, the chrome shade (#2a303c): the composer shell it
  // sits on IS gray-800, so a gray-800 tint was invisible in dark mode.
  'hover:bg-gray-100 dark:hover:bg-gray-750 transition ' +
  'focus:outline-none focus:ring-[3px] ' +
  // The disabled reset needs BOTH arms. `disabled:hover:bg-transparent` outranks
  // `hover:bg-gray-100` on specificity (3 vs 2), but `dark:hover:bg-gray-750`
  // compiles to `:hover:is(.dark *)` — also 3 — and is emitted later, so it won:
  // a disabled ghost select still lit up under the cursor in DARK mode while
  // light was correct. Measured on a live render, both themes. Any dark hover
  // tint added here needs its dark disabled reset in the same breath.
  'disabled:opacity-45 disabled:cursor-not-allowed ' +
  'disabled:hover:bg-transparent dark:disabled:hover:bg-transparent'

// Ghost's resting border is transparent — it lives HERE and not in the base
// string so the invalid arm's `border-status-danger-500` is the only border
// colour in play when there is an error (see the base string's note). Focus
// recolours it, as on the field recipe.
export const FIELD_GHOST_VALID_CLASS =
  'border-transparent ' +
  'focus:border-action-primary-600 dark:focus:border-action-primary-500 ' +
  'focus:ring-action-primary-500/40 dark:focus:ring-action-primary-400/40'

export const LABEL_CLASS = 'block text-[13px] font-[550] text-gray-900 dark:text-gray-100'

export const HELP_CLASS = 'text-xs text-gray-500 dark:text-gray-400'

export const ERROR_TEXT_CLASS =
  'flex items-start gap-1.5 text-[12.5px] text-status-danger-700 dark:text-status-danger-300'
