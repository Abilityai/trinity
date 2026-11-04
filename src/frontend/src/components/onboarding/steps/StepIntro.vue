<!--
  Step `intro` (Workspace Build): the value before the setup. Watching it isn't
  required, and it needs no answers.
-->
<template>
  <div data-testid="first-run-step-intro">
    <FirstRunStepHeader
      kicker="Start here"
      title="See what's possible"
      lead="A short look at what an agent is, and what a company run with agents looks like."
      badge="Optional"
    />
    <div class="mt-4"><IntroVideo @ended="$emit('complete')" /></div>
  </div>
</template>

<script setup>
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import IntroVideo from '../IntroVideo.vue'

defineProps({ ctx: { type: Object, default: () => ({}) } })
defineEmits(['complete', 'skip', 'update-intake'])
</script>
