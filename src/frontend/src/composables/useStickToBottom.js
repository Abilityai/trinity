import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'

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

  /**
   * Pin to the bottom once the pending patch is in the DOM.
   *
   * `nextTick` covers this component's own patch. It deliberately does NOT try
   * to cover LATER growth by chasing frames — that was tried twice and is the
   * wrong primitive. A fixed two passes fixed a 40-message thread locally and
   * left CI exactly 20px short; a settle loop that stopped when the height
   * repeated ALSO stopped short, because the transcript grew again after the
   * loop had already watched it hold still for a frame. There is no window
   * that is both short enough to be free and long enough to be right.
   *
   * The observer below is what covers late growth, for any cause and at any
   * delay, with nothing to tune.
   */
  async function scrollToBottomNow() {
    await nextTick()
    pin()
  }

  function pin() {
    const el = scrollEl.value
    // The element can go away between the arrival and the tick (a thread
    // switch, an unmount) — a normal race, not a failure.
    if (el) el.scrollTop = el.scrollHeight
  }

  /**
   * Re-pin whenever the transcript changes size AND the reader is following.
   *
   * This is the load-bearing half of "opens at the bottom". Growth arrives
   * after the pin from several directions and on nobody's schedule — the
   * loading skeleton swapping out, markdown rendering, a web font re-flowing
   * every bubble, an image settling, the composer growing — and each one leaves
   * the reader a fraction of a screen above the newest message, silently.
   *
   * It is also what makes a STREAMING reply follow: the bubble grows, the
   * observer fires, and a reader at the bottom stays there. A detached reader
   * is untouched, because the pin is gated on `following` — the same gate as
   * every other arrival path, so there is one rule and not two.
   *
   * Setting `scrollTop` does not change any box's size, so this cannot feed
   * itself.
   */
  let observer = null

  function disconnectObserver() {
    observer?.disconnect()
    observer = null
  }

  function observe(el) {
    disconnectObserver()
    if (!el || typeof ResizeObserver === 'undefined') return
    observer = new ResizeObserver(() => { if (following.value) pin() })
    // The CONTAINER reports a viewport change (a resized window, a dragged
    // column); its first child is the content wrapper and reports the
    // transcript growing. Both matter and they are different events.
    observer.observe(el)
    if (el.firstElementChild) observer.observe(el.firstElementChild)
  }

  watch(scrollEl, (el) => observe(el), { immediate: true, flush: 'post' })
  onScopeDispose(disconnectObserver)

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
