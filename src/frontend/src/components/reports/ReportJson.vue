<template>
  <pre class="text-xs bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200 rounded-md p-3 overflow-x-auto whitespace-pre-wrap break-words">{{ pretty }}</pre>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ payload: { type: [Object, Array, String, Number, Boolean], default: () => ({}) } })

const pretty = computed(() => {
  try {
    return JSON.stringify(props.payload, null, 2)
  } catch {
    return String(props.payload)
  }
})
</script>
