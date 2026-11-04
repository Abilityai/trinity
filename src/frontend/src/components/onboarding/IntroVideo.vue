<!--
  The short intro: what an agent is and what an agentic company looks like. It's a
  motion graphic with its words on screen, made in `intro-video/` with Remotion. It has
  music and sound effects, but starts muted because browsers only autoplay muted video;
  the controls turn the sound on. If the file can't play, which also covers a browser without the codec, the same
  scenes are drawn here instead, with arrows to move through them.
-->
<template>
  <div v-if="!videoFailed" class="relative">
    <video
      ref="videoEl"
      class="w-full aspect-video rounded-[10px] bg-gray-900"
      src="/intro/trinity-intro.mp4"
      poster="/intro/trinity-intro-poster.jpg"
      controls
      muted
      playsinline
      :autoplay="!reducedMotion"
      aria-label="What an AI agent is, and what a company run with agents looks like"
      data-testid="intro-video-file"
      @ended="$emit('ended')"
      @error="videoFailed = true"
      @volumechange="muted = $event.target.muted"
    ></video>
    <button
      v-if="muted"
      type="button"
      class="absolute right-3 top-3 rounded-md bg-gray-900/85 px-3 py-1.5 text-[13px] font-medium text-white
             hover:bg-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
      data-testid="intro-video-sound"
      @click="soundOn"
    >Turn sound on</button>
  </div>
  <div v-else class="rounded-[10px] border border-gray-200 dark:border-gray-750 bg-gray-50 dark:bg-gray-900 p-5" data-testid="intro-video">
    <div class="h-[270px] flex flex-col">
      <p class="text-lg font-semibold text-gray-900 dark:text-gray-100" data-testid="intro-video-title">{{ frame.scene.title }}</p>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-300" aria-live="polite">{{ frame.scene.caption }}</p>

      <div class="mt-4 flex-1 min-h-0">
        <div
          v-if="frame.scene.layout !== 'empty'"
          class="grid gap-2"
          :class="frame.scene.layout === 'grid' ? 'grid-cols-2' : frame.scene.items.length === 4 ? 'grid-cols-4' : 'grid-cols-3'"
        >
          <div
            v-for="(item, i) in frame.scene.items"
            :key="`${frame.scene.id}-${item.title}`"
            class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-2.5 transition-opacity duration-500 motion-reduce:transition-none"
            :class="i < frame.shown ? 'opacity-100' : 'opacity-0'"
          >
            <p class="text-[13px] font-medium text-gray-900 dark:text-gray-100">{{ item.title }}</p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ item.detail }}</p>
          </div>
        </div>
        <div v-else class="h-full rounded-lg border border-dashed border-gray-300 dark:border-gray-700 flex items-center justify-center">
          <p class="text-sm text-gray-500 dark:text-gray-400">Your company goes here</p>
        </div>
      </div>
    </div>

    <div class="mt-3 flex items-center gap-2">
      <BaseButton variant="ghost" size="sm" :disabled="frame.index === 0" data-testid="intro-video-back" aria-label="Previous part" @click="jump(frame.index - 1)">&larr;</BaseButton>
      <BaseButton variant="secondary" size="sm" data-testid="intro-video-play" @click="toggle">{{ ended ? 'Watch again' : playing ? 'Pause' : 'Play' }}</BaseButton>
      <BaseButton variant="ghost" size="sm" :disabled="ended" data-testid="intro-video-next" aria-label="Next part" @click="jump(frame.index + 1)">&rarr;</BaseButton>
      <div class="flex-1 h-1 rounded-full bg-gray-200 dark:bg-gray-750" aria-hidden="true">
        <div class="h-1 rounded-full bg-action-primary-600 dark:bg-action-primary-500" :style="{ width: `${Math.min(100, (elapsed / INTRO_TOTAL_MS) * 100)}%` }"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import BaseButton from '../base/BaseButton.vue'
import { INTRO_SCENES, INTRO_TOTAL_MS, introFrame, introSceneStart } from './introVideo'

const emit = defineEmits(['ended'])
const TICK_MS = 200

const elapsed = ref(0)
const playing = ref(false)
const ended = ref(false)
const frame = computed(() => introFrame(elapsed.value))
let timer = null

function stop() {
  playing.value = false
  clearInterval(timer)
}

function play() {
  playing.value = true
  clearInterval(timer)
  timer = setInterval(() => {
    elapsed.value += TICK_MS
    if (elapsed.value >= INTRO_TOTAL_MS) finish()
  }, TICK_MS)
}

function finish() {
  stop()
  elapsed.value = INTRO_TOTAL_MS
  ended.value = true
  emit('ended')
}

function toggle() {
  if (ended.value) {
    ended.value = false
    elapsed.value = 0
    play()
  } else if (playing.value) stop()
  else play()
}

function jump(index) {
  if (index >= INTRO_SCENES.length) { finish(); return }
  ended.value = false
  elapsed.value = introSceneStart(index)
}

const reducedMotion = Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches)
const videoFailed = ref(false)
const videoEl = ref(null)
const muted = ref(true)

function soundOn() {
  videoEl.value.muted = false
  muted.value = false
}

// The drawn version only runs if the file couldn't.
watch(videoFailed, (failed) => {
  if (!failed) return
  if (reducedMotion) elapsed.value = 1
  else play()
})
onBeforeUnmount(stop)
</script>
