<template>
  <div
    class="flex flex-col-reverse gap-px"
    :style="{ height: height ? height + 'px' : undefined, width: width + 'px' }"
    :title="capped + '/' + max + ' slots'"
  >
    <div
      v-for="i in max"
      :key="i"
      :class="[
        'flex-1 rounded-sm transition-colors duration-300',
        i <= capped ? fillClass : 'bg-gray-200 dark:bg-gray-700',
        i <= capped && atCapacity ? 'capacity-pulse' : ''
      ]"
    ></div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  active: { type: Number, default: 0 },
  max: { type: Number, default: 1 },
  height: { type: Number, default: 36 },
  width: { type: Number, default: 12 }
})

const capped = computed(() => Math.min(props.active, props.max))

const utilization = computed(() => {
  if (props.max === 0) return 0
  return (capped.value / props.max) * 100
})

const atCapacity = computed(() => utilization.value >= 100)

const fillClass = computed(() => {
  const u = utilization.value
  if (u <= 0) return 'bg-gray-200 dark:bg-gray-700'
  if (u < 50) return 'bg-status-success-500'
  if (u < 80) return 'bg-status-warning-500'
  if (u < 100) return 'bg-status-urgent-500'
  return 'bg-status-danger-500'
})
</script>

<style scoped>
.capacity-pulse {
  animation: capacity-pulse-animation 1.2s ease-in-out infinite;
}

@keyframes capacity-pulse-animation {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
</style>
