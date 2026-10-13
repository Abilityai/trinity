<!--
  The Workspace stage for /workspace/projects and /workspace/projects/:id
  (trinity-enterprise#661): the list, or one project. Owns only the stage
  chrome (the mobile menu button and the theme switch every stage carries);
  the list and the page own their own reads through `stores/projects.js`.
-->
<template>
  <div class="flex h-full min-h-0 flex-col bg-gray-50 dark:bg-gray-900" data-testid="portal-projects">
    <header class="shrink-0 flex items-center gap-2 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <button
        type="button"
        class="sm:hidden -ml-1 p-2 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
        aria-label="Menu"
        @click="$emit('open-menu')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>
      <span class="text-sm font-medium text-gray-900 dark:text-gray-100">Projects</span>
      <span class="flex-1"></span>
      <slot name="header-end" />
    </header>
    <div class="min-h-0 flex-1 overflow-y-auto">
      <ProjectPage
        v-if="projectId"
        :key="projectId"
        :project-id="projectId"
        :my-email="myEmail"
        @back="$emit('navigate', null)"
        @open-thread="$emit('open-thread', $event)"
        @open-room="$emit('open-room', $event)"
      />
      <ProjectsList v-else @open="$emit('navigate', $event)" />
    </div>
  </div>
</template>

<script setup>
import ProjectPage from './ProjectPage.vue'
import ProjectsList from './ProjectsList.vue'

defineProps({
  projectId: { type: String, default: null },
  myEmail: { type: String, default: '' },
})
defineEmits(['open-menu', 'navigate', 'open-thread', 'open-room'])
</script>
