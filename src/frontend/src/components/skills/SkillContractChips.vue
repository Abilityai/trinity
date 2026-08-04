<template>
  <!-- The #183 contract chip row (ent#263 shared seam): automation mode,
       invocability, package shape (files/size), optional version short-SHA.
       Interpolation only — skills come from a synced git repo (semi-trusted);
       no v-html, no :href from library-derived strings. -->
  <span class="inline-flex items-center gap-2 flex-wrap">
    <span
      v-if="skill.automation"
      class="text-[11px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300"
    >{{ skill.automation }}</span>
    <span
      v-if="!skill.user_invocable"
      class="text-[11px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
      title="Runs automatically; a user cannot invoke it directly"
    >not user-invocable</span>
    <span v-if="skill.multi_file" class="text-[11px] text-gray-400 whitespace-nowrap">{{ skill.file_count }} files</span>
    <span v-if="skill.size_bytes" class="text-[11px] text-gray-400 whitespace-nowrap">{{ formatBytes(skill.size_bytes) }}</span>
    <span v-if="showVersion && skill.version" class="text-[11px] font-mono text-gray-400">{{ skill.version.slice(0, 7) }}</span>
  </span>
</template>

<script setup>
import { formatBytes } from './contract'

defineProps({
  skill: { type: Object, required: true },
  // Off by default: the per-agent assigned list already renders the version
  // beside the skill name; the Library cards opt in.
  showVersion: { type: Boolean, default: false },
})
</script>
