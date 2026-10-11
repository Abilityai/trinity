<!--
  trinity-enterprise#631 — the tagger's marks under their own message: each
  person the message tagged, and how far it got — in their Inbox (delivered,
  in-app only), read, or not delivered. Renders nothing when the message
  tagged no one, so an ordinary message keeps its exact footprint.

  Identity is carried by an icon AND a word, never hue alone (principle 24):
  an envelope for "in their Inbox", a double check for "read", a cross for
  "not delivered". Meta ink (gray-500 light / gray-400 dark) on the surface.
-->
<template>
  <ul
    v-if="list.length"
    class="max-w-[85%] flex flex-wrap justify-end gap-x-3 gap-y-0.5 text-[12.5px] text-gray-500 dark:text-gray-400"
    :title="title"
    aria-label="People this message tagged"
  >
    <li v-for="(t, i) in list" :key="`${t.label}-${i}`" class="inline-flex items-center gap-1" :data-state="t.state">
      <svg class="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path v-if="t.state === 'read'" stroke-linecap="round" stroke-linejoin="round" d="M2 13l4 4L16 7M8 13l4 4L22 7" />
        <path v-else-if="t.state === 'failed'" stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
        <path v-else stroke-linecap="round" stroke-linejoin="round" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
      </svg>
      <span>{{ tagMarkText(t) }}</span>
    </li>
  </ul>
</template>

<script setup>
import { computed } from 'vue'
import { tagMarkText, tagMarksTitle } from './portalMentions'

const props = defineProps({
  tags: { type: Array, default: () => [] },
})
const list = computed(() => (Array.isArray(props.tags) ? props.tags.filter((t) => t && t.label) : []))
const title = computed(() => tagMarksTitle(list.value) || undefined)
</script>
