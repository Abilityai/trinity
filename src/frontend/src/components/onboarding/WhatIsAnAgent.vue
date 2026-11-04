<!--
  "What's an agent?": a short video, about 25 seconds, of one everyday job done by an
  agent, with captions and the intro's music. Shown to someone who has never built an agent, right after
  they say so, and under Get help for anyone. The transcript (the captions) is always
  there for screen readers, and shows on the page if the video can't play. The words
  are in `whatIsAnAgent.js`.
-->
<template>
  <div data-testid="what-is-an-agent">
    <h2 :id="titleId || undefined" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ copy.title }}</h2>
    <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ copy.lead }}</p>
    <div v-if="!videoFailed" class="relative mt-4">
      <video
        ref="videoEl"
        class="block w-full aspect-video rounded-lg bg-gray-900"
        :src="copy.video.src"
        :poster="copy.video.poster"
        muted
        playsinline
        controls
        preload="metadata"
        :autoplay="!reducedMotion"
        :aria-describedby="transcriptId"
        data-testid="what-is-an-agent-video"
        @error="videoFailed = true"
        @volumechange="muted = $event.target.muted"
      />
      <button
        v-if="muted"
        type="button"
        class="absolute right-3 top-3 rounded-md bg-gray-900/85 px-3 py-1.5 text-[13px] font-medium text-white
               hover:bg-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
        data-testid="what-is-an-agent-sound"
        @click="soundOn"
      >Turn sound on</button>
    </div>
    <ol
      :id="transcriptId"
      :class="videoFailed ? 'mt-4 space-y-1.5' : 'sr-only'"
      data-testid="what-is-an-agent-transcript"
    >
      <li v-for="line in copy.transcript" :key="line" class="text-[13px] leading-snug text-gray-600 dark:text-gray-300">{{ line }}</li>
    </ol>
    <p class="mt-4 text-sm font-medium text-gray-900 dark:text-gray-100" data-testid="what-is-an-agent-summary">{{ copy.summary }}</p>
    <BaseButton v-if="dismissible" class="mt-4" size="sm" data-testid="what-is-an-agent-done" @click="$emit('done')">Got it</BaseButton>
  </div>
</template>

<script setup>
import { computed, ref, useId } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import { agentExplainer } from './whatIsAnAgent'

const props = defineProps({
  software: { type: String, default: '' },
  // Under Get help it's something to watch, not a step to finish.
  dismissible: { type: Boolean, default: true },
  // Where it is a dialog's whole content, its title names the dialog.
  titleId: { type: String, default: '' },
})
defineEmits(['done'])

const copy = computed(() => agentExplainer(props.software))
const videoFailed = ref(false)
const transcriptId = `what-is-an-agent-transcript-${useId()}`
// Browsers only autoplay muted video; someone who asked for less motion presses play.
const reducedMotion = Boolean(typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
// The music is the intro's. It starts muted, as the intro does, and the button or the
// video's own controls turn it on.
const videoEl = ref(null)
const muted = ref(true)

function soundOn() {
  videoEl.value.muted = false
  muted.value = false
}
</script>
