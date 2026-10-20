<template>
  <!-- trinity-enterprise#657: "this conversation holds unsent text".
       A WORD, not a glyph, and deliberately so: the issue offers "icon or short
       tag", and every icon that would read as a draft is already spoken for on
       these exact surfaces — the pencil is the rename affordance
       (`PortalEditableTitle`, on the header and on hover in the same chat rows),
       the dot is the rail's activity signal in the tab strip, the star is the
       per-viewer pin. Two facts sharing one shape is the failure principle 24
       names, so the fact that has no glyph of its own gets the word.
       Tertiary ink in EACH theme (gray-500 light, gray-400 dark — the ladder's
       two different answers, not one class for both) and the mono-caps overline
       size: quiet beside the unread and ask pills, which are obligations, where
       this is a state of the reader's own work. -->
  <span
    class="inline-flex shrink-0 items-center text-[10px] font-semibold uppercase tracking-wide leading-none text-gray-500 dark:text-gray-400"
    :title="title"
    data-testid="draft-mark"
  >Draft</span>
</template>

<script setup>
defineProps({
  title: { type: String, default: 'Draft — unsent text' },
})
</script>
