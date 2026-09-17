<template>
  <!-- #2624: shown only while the reader is detached AND behind. The owning
       composable decides that; this component only renders the verdict, so the
       two chat surfaces cannot disagree about when it appears. -->
  <Transition
    enter-active-class="transition duration-150 ease-out motion-reduce:transition-none"
    enter-from-class="opacity-0 translate-y-1"
    leave-active-class="transition duration-100 ease-in motion-reduce:transition-none"
    leave-to-class="opacity-0 translate-y-1"
  >
    <button
      v-if="show"
      type="button"
      class="absolute left-1/2 -translate-x-1/2 bottom-4 z-10 inline-flex items-center gap-1.5
             rounded-full pl-3 pr-3.5 py-1.5 text-xs font-medium shadow-lg
             bg-action-primary-600 text-white hover:bg-action-primary-700
             focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500
             focus-visible:ring-offset-2 focus-visible:ring-offset-white
             dark:focus-visible:ring-offset-gray-900"
      :aria-label="label"
      @click="$emit('jump')"
    >
      <svg class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path fill-rule="evenodd" d="M10 3a1 1 0 0 1 1 1v9.586l3.293-3.293a1 1 0 1 1 1.414 1.414l-5 5a1 1 0 0 1-1.414 0l-5-5a1 1 0 1 1 1.414-1.414L9 13.586V4a1 1 0 0 1 1-1Z" clip-rule="evenodd" />
      </svg>
      {{ label }}
    </button>
  </Transition>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  show: { type: Boolean, default: false },
  // How many messages landed while the reader was away. The control exists to
  // return them to something, so a count of 0 is never rendered.
  count: { type: Number, default: 0 },
})

defineEmits(['jump'])

// Says what actually happened rather than a bare arrow: "jump to latest" alone
// cannot tell a reader whether they missed one reply or twelve, which is the
// thing that decides whether they want to go.
const label = computed(() =>
  props.count === 1 ? '1 new message' : `${props.count} new messages`
)
</script>
