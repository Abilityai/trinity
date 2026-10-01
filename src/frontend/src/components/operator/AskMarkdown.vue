<!--
  Agent-authored text in an ask, rendered as markdown (#3115).

  Asks are written by agents, and agents write markdown. Some surfaces showed
  it and others printed `**bold**` verbatim, so the same ask read differently
  depending on where it was answered. Every ask surface — the operator queue
  card and detail, resolved history, the Workspace asks and /m — renders
  through this one component, so they cannot drift again.

  Two modes, both through the app's one sanitiser (`utils/markdown.js`):
    * block (the question): `renderMarkdown`, the policy chat and reports use,
      with one set of prose classes so a list, code and a link look the same
      everywhere;
    * `inline` (title, an option label, the given answer): `renderInlineMarkdown`,
      the #2771 cell policy — bold, italic, code and links only, no block
      element and no `class`, so a field cannot break the layout around it.
-->
<template>
  <component
    :is="tag || (inline ? 'span' : 'div')"
    :class="inline ? 'ask-md-inline' : (prose ? PROSE : 'ask-md-block')"
    v-html="html"
  />
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown, renderInlineMarkdown } from '../../utils/markdown'

// The chat bubble's code convention (ChatBubble.vue): a pill, no typography
// backticks — so an agent's markdown reads the same in its ask as in its chat.
const PROSE = 'prose prose-sm dark:prose-invert max-w-none break-words '
  + 'prose-code:before:content-none prose-code:after:content-none prose-code:bg-gray-100 '
  + 'dark:prose-code:bg-gray-700 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:break-words'

const props = defineProps({
  text: { type: [String, Number], default: '' },
  inline: { type: Boolean, default: false },
  tag: { type: String, default: '' },
  // /m styles its own always-dark card; it opts out of the prose classes and
  // gets the same elements, laid out by `ask-md-block` in currentColor.
  prose: { type: Boolean, default: true },
})

const html = computed(() => {
  const text = props.text === null || props.text === undefined ? '' : String(props.text)
  return props.inline ? renderInlineMarkdown(text) : renderMarkdown(text)
})
</script>

<style scoped>
/* Inline code inside a title or a button reads as code, without a block box.
   A tint of the text's own colour, so it works on any surface — a light card,
   a dark one, /m's always-dark card, a selected button — with no literal. */
.ask-md-inline :deep(code),
.ask-md-block :deep(code) {
  font-size: 0.9em;
  padding: 0 0.25em;
  border-radius: 0.25rem;
  background-color: color-mix(in srgb, currentColor 14%, transparent);
}
.ask-md-inline :deep(a),
.ask-md-block :deep(a) {
  text-decoration: underline;
}
.ask-md-block {
  overflow-wrap: anywhere;
}
.ask-md-block :deep(p),
.ask-md-block :deep(ul),
.ask-md-block :deep(ol),
.ask-md-block :deep(pre) {
  margin: 0 0 0.5em;
}
.ask-md-block :deep(ul) {
  list-style: disc;
  padding-left: 1.25em;
}
.ask-md-block :deep(ol) {
  list-style: decimal;
  padding-left: 1.25em;
}
.ask-md-block :deep(pre) {
  overflow-x: auto;
  white-space: pre;
}
.ask-md-block :deep(:last-child) {
  margin-bottom: 0;
}
</style>
