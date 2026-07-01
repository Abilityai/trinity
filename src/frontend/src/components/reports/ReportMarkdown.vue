<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitized by renderMarkdown (DOMPurify, H-005) -->
  <div class="prose prose-sm dark:prose-invert max-w-none" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../../utils/markdown'

// Expected payload shape: { markdown: string }
const props = defineProps({ payload: { type: Object, default: () => ({}) } })

const html = computed(() => renderMarkdown(props.payload?.markdown || ''))
</script>
