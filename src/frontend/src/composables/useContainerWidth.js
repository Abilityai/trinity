import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

/**
 * trinity-enterprise#610 §3g A4 — an element's content width, live.
 *
 * The Workspace's columns (sidebar, rail) take width a viewport query cannot
 * see: at 1280px with the rail open the Inbox has ~608px, which a `lg:` rule
 * reads as "wide". Layout decisions that depend on the space a component
 * actually has read it here instead.
 *
 * `ResizeObserver` is injectable (the second argument) so a test can report a
 * width jsdom cannot lay out; by default it is read from the global at MOUNT
 * time, so a test that swaps the global before mounting needs nothing else.
 * No observer (SSR, an old engine) → the width stays 0, which callers treat as
 * "not measured" and fall back on the viewport.
 */
function contentWidth(el) {
  const w = Number(el.clientWidth) || 0
  if (!w) return 0
  const cs = typeof getComputedStyle === 'function' ? getComputedStyle(el) : null
  const pad = cs ? (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0) : 0
  return Math.max(0, w - pad)
}

export function useContainerWidth(elRef, { ResizeObserver: RO } = {}) {
  const width = ref(0)
  let ro = null
  let observed = null
  let stop = null
  // The element may mount LATER than its owner, or come and go (a `v-if`
  // column, round 3): follow the ref, and read 0 while there is no element.
  function attach(el) {
    if (el === observed) return
    ro?.disconnect?.(); ro = null; observed = el || null
    if (!el) { width.value = 0; return }
    const Impl = RO || globalThis.ResizeObserver
    if (typeof Impl !== 'function') return
    // #3060: read the width NOW, at attach — the observer's first report comes
    // after the next layout, so a caller deciding a layout from this ref drew
    // one frame on the "unmeasured" fallback first (the Inbox: a 320px split
    // list for a frame, then stacked, on every mount under 720px). A 0 read
    // (jsdom, a display:none element) leaves "unmeasured" in place.
    // The CONTENT box, as the observer reports it: clientWidth less padding.
    const now = Math.round(contentWidth(el))
    if (now > 0) width.value = now
    ro = new Impl((entries) => {
      const w = entries && entries[0] && entries[0].contentRect ? entries[0].contentRect.width : el.clientWidth
      width.value = Math.round(Number(w) || 0)
    })
    ro.observe(el)
  }
  onMounted(() => {
    attach(elRef.value)
    stop = watch(elRef, (el) => attach(el), { flush: 'post' })
  })
  onBeforeUnmount(() => { stop?.(); stop = null; ro?.disconnect?.(); ro = null; observed = null })
  return width
}
