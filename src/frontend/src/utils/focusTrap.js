/**
 * The decidable half of the modal keyboard contract (#1923, design-system p23).
 *
 * Pure, and separate from `BaseModal.vue`, so every decision is a function over
 * plain data that a node-environment test can drive exhaustively — a focus
 * trap written entirely inside an SFC would be a rule proven only by reading
 * it. The component supplies the DOM: the element list, the key event, the
 * focus() calls.
 *
 * The wiring is NOT left to e2e (an earlier version of this docblock claimed
 * the repo could not mount a component; it can — `vitest.config.js` carries
 * `plugins: [vue()]` + jsdom + `@vue/test-utils` for a per-file opt-in, #2918).
 * `tests/unit/baseModal.spec.js` mounts the shell and proves Esc, the trap,
 * focus return and the scroll lock against a real DOM.
 */

/**
 * Selector for things a user can Tab to. `[tabindex]:not([tabindex="-1"])`
 * carries the custom cases (a div made focusable), and `:not([disabled])`
 * matters because a disabled control still matches the tag selectors.
 */
export const TABBABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * Narrow a candidate list to what can actually receive focus NOW.
 *
 * Takes already-queried elements rather than a root, so it is testable with
 * plain objects. An element is excluded when it is disabled, explicitly
 * removed from the tab order, or hidden — a trap that cycles onto an invisible
 * element strands the keyboard user on nothing, which is worse than no trap.
 *
 * `hidden` is passed in by the caller (the component measures it), because
 * "is this visible" cannot be answered without layout.
 */
export function tabbable(elements) {
  return (Array.isArray(elements) ? elements : []).filter((el) => {
    if (!el || el.disabled) return false
    if (el.hidden === true) return false
    const ti = el.getAttribute ? el.getAttribute('tabindex') : el.tabindex
    if (ti !== null && ti !== undefined && String(ti) === '-1') return false
    return true
  })
}

/**
 * Where Tab should land, given where focus is now.
 *
 * Returns an index into `items`, or null to let the browser do its thing.
 * The trap is a WRAP: Tab past the last goes to the first, Shift+Tab before
 * the first goes to the last. That is the whole contract — anything else and
 * focus escapes behind the overlay, which is the defect #1923 is about.
 *
 * Focus outside the modal entirely (`currentIndex === -1`) pulls back to the
 * first item on Tab and the last on Shift+Tab, so a stray focus is recovered
 * rather than left loose.
 */
export function nextFocusIndex(count, currentIndex, shiftKey) {
  if (!count || count < 1) return null
  if (count === 1) return 0
  if (currentIndex === -1) return shiftKey ? count - 1 : 0
  if (shiftKey) return currentIndex === 0 ? count - 1 : currentIndex - 1
  return currentIndex === count - 1 ? 0 : currentIndex + 1
}

/** Is this key event a dismissal? Esc only, and not when a modifier is held. */
export function isDismissKey(event) {
  if (!event) return false
  if (event.key !== 'Escape' && event.key !== 'Esc') return false
  return !(event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
}

/** Should Tab be intercepted? Only when there is something to cycle between. */
export function isTabKey(event) {
  return Boolean(event) && event.key === 'Tab'
}

/**
 * Which element gets focus when the modal opens.
 *
 * "Initial focus on the SAFE action" (principle 23): never the destructive
 * one. A dialog that opens with Delete focused turns a reflexive Enter into
 * data loss, so an explicitly marked safe target wins, then the first
 * non-destructive tabbable, and only then the first tabbable at all.
 *
 * `destructive` is declared by the caller (`data-destructive`), not guessed
 * from label text — guessing would be wrong in every language but English.
 */
export function initialFocusIndex(items, { explicitIndex = -1 } = {}) {
  const list = Array.isArray(items) ? items : []
  if (!list.length) return null
  if (explicitIndex >= 0 && explicitIndex < list.length) return explicitIndex
  const safe = list.findIndex((el) => !isDestructive(el))
  return safe >= 0 ? safe : 0
}

/** Has the caller marked this control as the destructive one? */
export function isDestructive(el) {
  if (!el) return false
  if (el.dataset && el.dataset.destructive !== undefined) return true
  return Boolean(el.getAttribute && el.getAttribute('data-destructive') !== null)
}

/**
 * Does a click belong to the backdrop rather than the panel?
 *
 * Compared by identity against the overlay node — NOT by checking whether the
 * target is outside some rectangle, which mis-fires for a select popup or a
 * date picker rendered at the document root.
 */
export function isBackdropClick(event, overlayEl) {
  return Boolean(event) && Boolean(overlayEl) && event.target === overlayEl
}

/**
 * A ref-counted scroll lock over one element's `overflow` (#1923 review).
 *
 * The lock is GLOBAL state — `document.body.style.overflow` — and modals nest:
 * a `<ConfirmDialog>` declared inside another modal's slot mounts (closed)
 * while the outer one is opening, and later opens on top of it. A lock written
 * per instance breaks both ways: the inner instance's mount or close would
 * unlock the page while the outer is still open, and an unconditional clear on
 * unmount would clobber a lock some other component holds
 * (`ChannelConfigDialog`, `FirstRunOverlay`). So: the first holder saves the
 * previous value and hides overflow; the last release restores it; every
 * holder in between is a count. `getStyle` is injectable for tests.
 */
export function createScrollLock(getStyle = () => document.body.style) {
  let holders = 0
  let previous = ''
  return {
    acquire() {
      if (holders === 0) {
        const style = getStyle()
        previous = style.overflow
        style.overflow = 'hidden'
      }
      holders += 1
    },
    release() {
      if (holders === 0) return
      holders -= 1
      if (holders === 0) getStyle().overflow = previous
    },
    get holders() { return holders },
  }
}

/** The one lock every `BaseModal` shares. */
export const bodyScrollLock = createScrollLock()
