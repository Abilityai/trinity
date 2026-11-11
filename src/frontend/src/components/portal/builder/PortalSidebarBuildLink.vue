<!--
  The sidebar's Build link: the door to the Build page, beside Inbox and Projects, until
  the person's own guide is in the Agents list below: then the guide's row is the door, and
  one door is enough. Someone who has the Dashboard but may not build (an operator) gets
  the Dashboard link and help only.
  A profile someone skipped shows in red under it, wherever they are in the Workspace,
  and opens the onboarding window again.
-->
<template>
  <div>
    <button
      v-if="canBuild && !guideShown"
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
    <!-- The way to the Dashboard, for the people who can use it (this whole block is theirs
         only). A new tab, so the chat stays open behind it. A first Show me walk lights it. -->
    <a
      href="/"
      target="_blank"
      rel="noopener"
      data-testid="sidebar-dashboard"
      data-tour="dashboard"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition text-gray-700 hover:bg-white dark:text-gray-300 dark:hover:bg-gray-900"
    >
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h5a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v3a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 16a1 1 0 011-1h5a1 1 0 011 1v3a1 1 0 01-1 1H5a1 1 0 01-1-1v-3zm10-3a1 1 0 011-1h4a1 1 0 011 1v6a1 1 0 01-1 1h-4a1 1 0 01-1-1v-6z" /></svg>
      Dashboard
      <span class="sr-only">(opens in a new tab)</span>
      <svg class="ml-auto w-3.5 h-3.5 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" /></svg>
    </a>
    <button
      v-if="canBuild && profileIncomplete"
      type="button"
      data-testid="sidebar-profile-incomplete"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-1.5 text-xs font-medium text-status-danger-700 dark:text-status-danger-400 hover:underline underline-offset-2"
      @click="$emit('finish-profile')"
    >
      <span class="w-4 flex justify-center" aria-hidden="true"><span class="w-2 h-2 rounded-full bg-status-danger-600"></span></span>
      Profile incomplete: finish it
    </button>
    <!-- Every question answered: the rest fills in as they talk with their guide, so this is
         no alarm. One calm offer to tell it about their work, then only the fact. -->
    <button
      v-else-if="canBuild && profileNote === 'talk'"
      type="button"
      data-testid="sidebar-profile-note"
      class="w-full flex items-center gap-2 rounded-xl px-3 py-1.5 text-xs text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
      @click="$emit('finish-profile')"
    >
      <span class="w-4 flex justify-center" aria-hidden="true"><span class="w-2 h-2 rounded-full border border-gray-500 dark:border-gray-400"></span></span>
      Profile: tell {{ guideLabel }} about your work
    </button>
    <p
      v-else-if="canBuild && profileNote === 'filling'"
      data-testid="sidebar-profile-note"
      class="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300"
    >
      <span class="w-4 flex justify-center" aria-hidden="true"><span class="w-2 h-2 rounded-full border border-gray-500 dark:border-gray-400"></span></span>
      Profile: {{ guideLabel }} fills it in as you talk
    </p>
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
  // '' | 'talk' | 'filling' (`worldView().profileNote`).
  profileNote: { type: String, default: '' },
  guideLabel: { type: String, default: 'Atlas' },
  canBuild: { type: Boolean, default: true },
  guideShown: { type: Boolean, default: false },
})
defineEmits(['open', 'finish-profile', 'get-help'])
</script>
