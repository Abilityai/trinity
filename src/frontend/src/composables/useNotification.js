import { ref } from 'vue'

/**
 * Toast notifications.
 *
 * #1926 (design-system principle 18): **toasts announce completed verbs, not
 * failures.** A 3-second auto-dismissing error is a failure the user is likely
 * to miss entirely — and if they do catch it, it is gone before they can read
 * the detail or act on it. So an `error` notification now persists until it is
 * dismissed; success/info keep the timed behaviour.
 *
 * A failure that belongs *next to a control* should not use this composable at
 * all — use `components/InlineError.vue` (verb failures) or
 * `components/LoadFailed.vue` (failed fetches), which stay anchored to the
 * thing that failed.
 */
export function useNotification() {
  const notification = ref(null)
  let timer = null

  const clearTimer = () => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  const dismissNotification = () => {
    clearTimer()
    notification.value = null
  }

  const showNotification = (message, type = 'success', { timeout = 3000 } = {}) => {
    // Clearing the pending timer matters even on the success path: without it a
    // second toast inherited the first one's countdown and could vanish almost
    // immediately.
    clearTimer()
    notification.value = { message, type }
    if (type === 'error') return // persists until dismissed (principle 18)
    timer = setTimeout(() => {
      notification.value = null
      timer = null
    }, timeout)
  }

  return {
    notification,
    showNotification,
    dismissNotification
  }
}
