<template>
  <!-- trinity#4302: the agents handed the conversation back and forth until the
       chain cap paused them. The stored line says only that (agents read it);
       the next step is said HERE, in the product's words, beside its control. -->
  <div class="flex flex-col items-center gap-2" data-testid="room-continue-offer">
    <div class="flex flex-col sm:flex-row items-center gap-2">
      <p class="text-xs text-center text-gray-600 dark:text-gray-300">
        Continue lets {{ names }} carry on, or send a message.
      </p>
      <BaseButton
        variant="secondary"
        size="sm"
        :loading="continuing"
        loading-label="Continuing…"
        data-testid="room-continue"
        @click="onContinue"
      >Continue</BaseButton>
    </div>
    <InlineError
      v-if="error"
      :message="error"
      data-testid="room-continue-error"
      @dismiss="error = ''"
    />
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import InlineError from '@/components/InlineError.vue'

const props = defineProps({
  roomId: { type: String, required: true },
  // `room.paused_chain` — the server's verdict that an offer stands: {seq, next}.
  offer: { type: Object, required: true },
  // The room's own reload. The control's state follows the PAYLOAD, not the
  // request: the request is held while the woken agents work, and a proxy can
  // cut it long after the offer was consumed.
  reload: { type: Function, default: null },
})

const store = useClientPortalStore()
const continuing = ref(false)
const error = ref('')

// 'a' / 'a and b' / 'a, b and c' — the server's `_names`.
const names = computed(() => {
  const n = props.offer?.next || []
  return n.length <= 1 ? n.join('') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
})

// Refusals the reload already tells the truth about: another click or another
// tab got there first, or the room moved on. Saying them would only add noise.
const SILENT = new Set(['request_in_progress', 'room_moved_on'])

async function onContinue() {
  if (continuing.value) return
  continuing.value = true
  error.value = ''
  try {
    await store.continueRoomChain(props.roomId, props.offer.seq)
  } catch (err) {
    const detail = err?.response?.data?.detail
    // No response is "could not ask" (network, a cut held request) — the
    // reload below shows whether the Continue landed. A coded refusal that
    // still leaves the offer standing (a budget, lost access) is said.
    if (err?.response && !(detail && SILENT.has(detail.code))) {
      error.value = (detail && detail.message) || 'Could not continue the conversation.'
    }
  }
  try { await props.reload?.() } catch { /* the poll retries */ }
  // Still mounted means the offer still stands (PortalRoom drops this control
  // once `paused_chain` is gone), so the person may try again.
  continuing.value = false
}
</script>
