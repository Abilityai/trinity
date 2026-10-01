<!--
  The sidebar's Projects link (trinity-enterprise#661): the door to the
  person's Projects list. The sidebar renders it only when the roster says
  this principal has the capability.
-->
<template>
  <button
    type="button"
    data-testid="sidebar-projects"
    :aria-current="active ? 'page' : undefined"
    class="w-full flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition"
    :class="active
      ? 'bg-white text-gray-900 dark:bg-gray-900 dark:text-gray-100'
      : 'text-gray-700 hover:bg-white dark:text-gray-300 dark:hover:bg-gray-900'"
    @click="$emit('open')"
  >
    <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
    Projects
  </button>
</template>

<script setup>
defineProps({ active: { type: Boolean, default: false } })
defineEmits(['open'])
</script>
