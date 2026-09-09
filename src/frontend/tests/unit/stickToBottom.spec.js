/**
 * #2624 — a background arrival must not move a transcript the reader is holding.
 *
 * Both Workspace chat surfaces scrolled to the bottom on EVERY arrival:
 * `scrollDown()` was an unconditional `scrollTop = scrollHeight` and the poll,
 * the reply settle and the cancel settle all called it. Scroll up to re-read an
 * answer and the next message — in a room, any participant's, every 3s — yanked
 * you back down. Same family as #1927: a background poll must not reset a
 * surface the user is holding.
 *
 * The rule lives in ONE composable so the two surfaces cannot drift; these are
 * its decidable parts. `vitest` runs `environment: 'node'` with no mount
 * harness, so the element is a plain object with the three numbers the decision
 * actually reads — which is also the honest scope of the decision.
 */
import { describe, it, expect, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import {
  STICK_THRESHOLD_PX,
  distanceFromBottom,
  isNearBottom,
  useStickToBottom,
} from '@/composables/useStickToBottom'

/** A scroll container as the decision sees it. */
function el({ scrollHeight = 1000, clientHeight = 400, scrollTop = 600 } = {}) {
  return { scrollHeight, clientHeight, scrollTop }
}

describe('#2624 the near-bottom decision', () => {
  it('measures the gap below the viewport', () => {
    expect(distanceFromBottom(el({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 }))).toBe(0)
    expect(distanceFromBottom(el({ scrollTop: 300 }))).toBe(300)
  })

  it('counts pinned-to-the-bottom as following', () => {
    expect(isNearBottom(el({ scrollTop: 600 }))).toBe(true)
  })

  it('is forgiving by a threshold, so a partly-visible last bubble still follows', () => {
    expect(isNearBottom(el({ scrollTop: 600 - STICK_THRESHOLD_PX }))).toBe(true)
    expect(isNearBottom(el({ scrollTop: 600 - STICK_THRESHOLD_PX - 1 }))).toBe(false)
  })

  it('has a threshold wide enough to absorb the border-box rounding', () => {
    // `scrollHeight` EXCLUDES the border under `box-sizing: border-box` — the
    // gotcha already documented in `PortalRoom.vue`. A couple of px must never
    // be able to flip "following" to "detached"; sub-pixel layout maths must
    // not either.
    expect(STICK_THRESHOLD_PX).toBeGreaterThanOrEqual(16)
  })

  it('treats a fractional gap as the bottom', () => {
    // Zoom, a device pixel ratio, or a fractional line-height leaves
    // `scrollHeight - scrollTop - clientHeight` at 0.5 when the user IS at the
    // bottom. A `=== 0` test would report detached forever on those displays.
    expect(isNearBottom(el({ scrollTop: 599.5 }))).toBe(true)
  })

  it('follows when there is nothing to scroll yet', () => {
    // First paint, or a thread short enough not to overflow: there is no
    // element or no overflow, and the honest answer is "you are at the bottom".
    expect(isNearBottom(null)).toBe(true)
    expect(isNearBottom(el({ scrollHeight: 200, clientHeight: 400, scrollTop: 0 }))).toBe(true)
  })
})

describe('#2624 following, detaching and re-arming', () => {
  function setup(initial) {
    const node = el(initial)
    const scrollEl = ref(node)
    return { node, ...useStickToBottom(scrollEl), scrollEl }
  }

  it('starts out following, because a thread opens at the bottom', () => {
    const { following, unread } = setup()
    expect(following.value).toBe(true)
    expect(unread.value).toBe(0)
  })

  it('an arrival while following pins the view to the bottom', async () => {
    const s = setup()
    s.node.scrollHeight = 1400          // the new message made the thread taller
    await s.onArrive(1)
    expect(s.node.scrollTop).toBe(1400)
    expect(s.unread.value).toBe(0)
  })

  it('scrolling away detaches, and the next arrival does not move the viewport', async () => {
    const s = setup()
    s.node.scrollTop = 100              // the reader scrolled up
    s.onScroll()
    expect(s.following.value).toBe(false)

    s.node.scrollHeight = 1400
    await s.onArrive(1)
    expect(s.node.scrollTop).toBe(100)  // the message being read stays put
    expect(s.unread.value).toBe(1)
  })

  it('counts every message that arrives while detached', async () => {
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(2)
    await s.onArrive(3)
    expect(s.unread.value).toBe(5)
  })

  it('an arrival with no new messages counts nothing', async () => {
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(0)
    expect(s.unread.value).toBe(0)
  })

  it('scrolling back to the bottom re-arms following and clears the count', async () => {
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(4)
    expect(s.unread.value).toBe(4)

    s.node.scrollTop = 600              // back at the bottom
    s.onScroll()
    expect(s.following.value).toBe(true)
    expect(s.unread.value).toBe(0)      // no sticky state to clear by hand
  })

  it('jumping to latest returns to the bottom and re-arms', async () => {
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(3)

    await s.scrollToLatest()
    expect(s.node.scrollTop).toBe(s.node.scrollHeight)
    expect(s.following.value).toBe(true)
    expect(s.unread.value).toBe(0)
  })

  it('pinning always goes to the bottom, whatever the prior position', async () => {
    // The send path: an explicit intent to follow, which must override a
    // detached reader rather than queue an unread badge for their own message.
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(2)

    s.node.scrollHeight = 1800
    await s.pinToBottom()
    expect(s.node.scrollTop).toBe(1800)
    expect(s.following.value).toBe(true)
    expect(s.unread.value).toBe(0)
  })

  it('reset re-arms without touching the DOM, for a thread switch', async () => {
    const s = setup()
    s.node.scrollTop = 100
    s.onScroll()
    await s.onArrive(2)

    s.reset()
    expect(s.following.value).toBe(true)
    expect(s.unread.value).toBe(0)
    // The outgoing thread's element is about to be replaced; the incoming
    // thread's load pins it. Moving this one would scroll a transcript that is
    // being torn down.
    expect(s.node.scrollTop).toBe(100)
  })

  it('waits for the DOM to hold the new message before measuring', async () => {
    // The row is pushed into a reactive array; Vue patches on the next
    // microtask. Reading `scrollHeight` synchronously measures the thread
    // WITHOUT the arrival and lands a message short of the bottom.
    const s = setup()
    const node = s.node
    const p = s.onArrive(1)
    node.scrollHeight = 1400            // "Vue patches" after the caller returns
    await p
    expect(node.scrollTop).toBe(1400)
  })

  it('re-pins after a LATE patch, one frame on', async () => {
    // Measured in a real browser (the #2624 e2e): a 40-message thread opened
    // 20px above its newest message and STAYED there. One `nextTick` covers the
    // component's own patch; a child patching on a later tick — the loading
    // skeleton swapping out, markdown rendering a long thread — grows the
    // transcript after the first measurement, and the browser clamps the
    // assignment to the height it had then.
    const s = setup()
    const node = s.node
    let ticks = 0
    Object.defineProperty(node, 'scrollHeight', {
      get: () => (ticks++ === 0 ? 1380 : 1400),   // the late 20px
    })
    await s.onArrive(1)
    expect(node.scrollTop).toBe(1400)
  })

  it('survives an element that has gone away mid-flight', async () => {
    // A thread switch or an unmount between the arrival and the tick.
    const s = setup()
    const p = s.onArrive(1)
    s.scrollEl.value = null
    await expect(p).resolves.toBeUndefined()
  })

  it('a scroll event before the element exists does not detach', () => {
    const scrollEl = ref(null)
    const { following, onScroll } = useStickToBottom(scrollEl)
    onScroll()
    expect(following.value).toBe(true)
  })
})

describe('#2624 the affordance is honest', () => {
  it('shows only while detached AND behind', async () => {
    const scrollEl = ref(el())
    const s = useStickToBottom(scrollEl)
    expect(s.showJumpToLatest.value).toBe(false)   // following

    scrollEl.value.scrollTop = 100
    s.onScroll()
    expect(s.showJumpToLatest.value).toBe(false)   // detached, but nothing missed

    await s.onArrive(1)
    expect(s.showJumpToLatest.value).toBe(true)

    scrollEl.value.scrollTop = 600
    s.onScroll()
    expect(s.showJumpToLatest.value).toBe(false)   // caught up — it goes away
  })

  it('accepts a custom threshold', () => {
    const scrollEl = ref(el({ scrollTop: 300 }))
    const s = useStickToBottom(scrollEl, { threshold: 400 })
    s.onScroll()
    expect(s.following.value).toBe(true)
  })
})
