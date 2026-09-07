<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitized by renderCanvasMarkdown (DOMPurify, H-005, canvas kit allowlist) -->
  <div class="prose prose-sm dark:prose-invert max-w-none" v-html="html"></div>
</template>

<script setup>
/**
 * Markdown prose on a canvas (trinity-enterprise#537) — `ReportMarkdown.vue`
 * with the canvas-mode sanitiser. The one difference is the renderer: canvas
 * markdown may carry raw kit markup (`<div class="ck-card">…`), so it goes
 * through `renderCanvasMarkdown`, which admits the kit's classes and nothing
 * else. Report markdown keeps `renderMarkdown` — a report is the immutable
 * half and its renderer is CI-pinned (#1535); this file exists so the canvas
 * does not need a renderer option threaded through that contract.
 *
 * `.prose` stays on the wrapper so plain `<h2>` / `<p>` / `<ul>` keep their
 * typography; the kit's own rules (`.canvas-kit .ck-*`) out-rank typography's
 * `:where()` selectors and reset what they own (margins, table chrome).
 */
import { computed } from 'vue'
import { renderCanvasMarkdown } from '../../utils/markdown'

const props = defineProps({ markdown: { type: String, default: '' } })

const html = computed(() => renderCanvasMarkdown(props.markdown || ''))
</script>
