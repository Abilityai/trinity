<!--
  The sidebar's Build link: the door to the Build page, beside Inbox and Projects.
  A profile someone skipped shows in red under it, wherever they are in the Workspace,
  and opens the onboarding window again.
-->
<template>
  <div>
    <button
      type="button"
      data-testid="sidebar-build"
      :aria-current="active ? 'page' : undefined"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition"
      :class="active
        ? 'bg-white text-gray-900 dark:bg-gray-900 dark:text-gray-100'
        : 'text-gray-700 hover:bg-white dark:text-gray-300 dark:hover:bg-gray-900'"
      @click="$emit('open')"
    >
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" /></svg>
      Build an agent
    </button>
    <button
      v-if="profileIncomplete"
      type="button"
      data-testid="sidebar-profile-incomplete"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-1.5 text-xs font-medium text-status-danger-700 dark:text-status-danger-400 hover:underline underline-offset-2"
      @click="$emit('finish-profile')"
    >
      <span class="w-4 flex justify-center" aria-hidden="true"><span class="w-2 h-2 rounded-full bg-status-danger-600"></span></span>
      Profile incomplete: finish it
    </button>
    <button
      type="button"
      data-testid="sidebar-get-help"
      data-tour="help"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-1.5 text-xs text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
      @click="$emit('get-help')"
    >
      <span class="w-4 flex justify-center" aria-hidden="true">?</span>
      Get help from the team
    </button>
  </div>
</template>

<script setup>
defineProps({
  active: { type: Boolean, default: false },
  profileIncomplete: { type: Boolean, default: false },
})
defineEmits(['open', 'finish-profile', 'get-help'])
</script>
