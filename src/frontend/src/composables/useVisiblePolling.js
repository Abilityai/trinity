import { onBeforeUnmount, onMounted } from 'vue'

/**
 * trinity-enterprise#957 — re-read something while the page is being looked at.
 *
 * `run` is called every `interval` ms while the document is visible, and at
 * once when the tab comes back (visibility) or the window regains focus —
 * those two fire together on a tab return, so runs closer than `minGap` are
 * collapsed into one. `paused()` returning true skips a run: the caller knows
 * when fresh rows would land under a gesture in progress.
 *
 * The refresh is the caller's to keep invisible (stale-while-revalidate, no
 * skeleton): this only decides WHEN. `doc` / `win` are injectable so a test can
 * drive visibility; everything is torn down with the component.
 */
export function useVisiblePolling(run, {
  interval = 20000, minGap = 1000, paused = () => false,
  doc = globalThis.document, win = globalThis.window,
} = {}) {
  let timer = null
  let last = 0

  function tick() {
    if (!doc || doc.visibilityState === 'hidden' || paused()) return
    const now = Date.now()
    if (now - last < minGap) return
    last = now
    void run()
  }
  const onVisible = () => { if (doc.visibilityState === 'visible') tick() }

  onMounted(() => {
    timer = setInterval(tick, interval)
    doc?.addEventListener?.('visibilitychange', onVisible)
    win?.addEventListener?.('focus', tick)
  })
  onBeforeUnmount(() => {
    if (timer) clearInterval(timer)
    timer = null
    doc?.removeEventListener?.('visibilitychange', onVisible)
    win?.removeEventListener?.('focus', tick)
  })
  return { tick }
}
