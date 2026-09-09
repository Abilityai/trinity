import { computed, nextTick, ref } from 'vue'

/**
 * Stick-to-bottom for a chat transcript (#2624).
 *
 * Both Workspace chat surfaces scrolled to the bottom on EVERY arrival:
 * `scrollDown()` was an unconditional `scrollTop = scrollHeight`, and the room
 * poll, the reply settle and the cancel settle all called it. Scroll up to
 * re-read an answer and the next message yanked you back down — in a room,
 * every 3s, from any participant. Same family as #1927: a background arrival
 * must not move a surface the reader is holding.
 *
 * The distinction the old code did not have is between an ARRIVAL and an
 * INTENT. An arrival follows only if the reader was already following; an
 * intent (sending, opening a thread, clicking jump-to-latest) always pins and
 * re-arms. Hence two entry points rather than one flag threaded through
 * `scrollDown()`:
 *
 *   onArrive(count)  — a message landed by itself. Follows or counts.
 *   pinToBottom()    — the user asked to be at the bottom. Always goes.
 *
 * It lives here, once, because `PortalConversation.vue` and `PortalRoom.vue`
 * both own a `scrollEl` on the same `flex-1 min-h-0 overflow-y-auto` container
 * and had two copies of the bug. A third surface (`chat/ChatMessages.vue`)
 * still carries the unconditional pattern behind its `autoScroll` prop and can
 * adopt this without changing it.
 *
 * @param {import('vue').Ref<HTMLElement|null>} scrollEl the scrolling container
 * @param {{threshold?: number}} [options]
 */
export function useStickToBottom(scrollEl, { threshold = STICK_THRESHOLD_PX } = {}) {
  // A thread opens at the bottom, so following is the starting state — and it
  // must be, because the first `onArrive` can fire before any scroll event has
  // told us where the reader is.
  const following = ref(true)
  const unread = ref(0)

  /** Recompute from where the reader actually is. Bind to the container's `scroll`. */
  function onScroll() {
    const near = isNearBottom(scrollEl.value, threshold)
    following.value = near
    // Re-arming clears the count by construction — there is no detached state
    // left over for the user to dismiss, which is the whole point of keying
    // this on position rather than on a button.
    if (near) unread.value = 0
  }

  /**
   * A message arrived on its own (a poll, a reply settling, a stream ending).
   *
   * `await nextTick()` first: the row goes into a reactive array and Vue
   * patches on the next microtask, so measuring synchronously measures the
   * transcript WITHOUT the arrival and lands a message short of the bottom.
   *
   * @param {number} [count] how many messages landed — 0 is legal and counts nothing
   */
  async function onArrive(count = 1) {
    if (following.value) {
      await scrollToBottomNow()
      return
    }
    if (count > 0) unread.value += count
  }

  /** The user asked to be at the bottom: go there and start following again. */
  async function pinToBottom() {
    await scrollToBottomNow()
    following.value = true
    unread.value = 0
  }

  /** What the jump-to-latest control calls. Same thing, named for the reader. */
  const scrollToLatest = pinToBottom

  /**
   * Re-arm WITHOUT touching the DOM — for a thread/room switch.
   *
   * The outgoing transcript's element is about to be replaced, so scrolling it
   * would move a surface that is being torn down; the incoming thread's own
   * load pins it.
   */
  function reset() {
    following.value = true
    unread.value = 0
  }

  async function scrollToBottomNow() {
    await nextTick()
    pin()
    // A SECOND pass one frame later. `nextTick` covers this component's own
    // patch; a child that patches on a later tick — the loading skeleton
    // swapping out, markdown rendering a long thread — grows the transcript
    // after the first measurement, and the browser clamps the assignment to the
    // height it had THEN. Measured in a real browser: a 40-message thread
    // opened 20px above its newest message, every time, stably.
    await afterFrame()
    pin()
  }

  function pin() {
    const el = scrollEl.value
    // The element can go away between the arrival and the tick (a thread
    // switch, an unmount) — that is a normal race, not a failure.
    if (el) el.scrollTop = el.scrollHeight
  }

  // Detached ALONE is not worth an affordance: a reader scrolled up with
  // nothing new below them has missed nothing, and a control saying otherwise
  // would be the dishonest kind. It appears when there is something to return
  // to and disappears the moment they are caught up.
  const showJumpToLatest = computed(() => !following.value && unread.value > 0)

  return {
    following,
    unread,
    showJumpToLatest,
    onScroll,
    onArrive,
    pinToBottom,
    scrollToLatest,
    reset,
  }
}

/**
 * Resolve after the browser has laid out whatever was just patched in.
 *
 * `requestAnimationFrame` where there is one; a macrotask otherwise, which is
 * what the node test environment gets. Either way this yields AFTER the
 * microtask queue `nextTick` drains, which is the point.
 */
function afterFrame() {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => resolve())
    else setTimeout(resolve, 0)
  })
}

/**
 * How forgiving "at the bottom" is, in px.
 *
 * Not a pixel-exact test, for two independent reasons: `scrollHeight` EXCLUDES
 * the border under `box-sizing: border-box` (the gotcha `PortalRoom.vue`
 * already documents), and fractional layout — zoom, device pixel ratio, a
 * fractional line-height — leaves a sub-pixel gap when the reader IS at the
 * bottom. 64px is also roughly "a line off the bottom", which is what a reader
 * would call still following.
 */
export const STICK_THRESHOLD_PX = 64

/** Pixels of transcript below the viewport. 0 = pinned to the bottom. */
export function distanceFromBottom(el) {
  if (!el) return 0
  const { scrollHeight = 0, scrollTop = 0, clientHeight = 0 } = el
  return scrollHeight - scrollTop - clientHeight
}

/**
 * Is the reader close enough to the bottom to count as following?
 *
 * A missing element answers TRUE. Before first paint, and on a thread too
 * short to overflow, the reader IS at the bottom — answering false would open
 * every thread detached and badge the first reply as unread.
 */
export function isNearBottom(el, threshold = STICK_THRESHOLD_PX) {
  if (!el) return true
  return distanceFromBottom(el) <= threshold
}
