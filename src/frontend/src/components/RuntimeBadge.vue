<template>
  <span
    :class="[
      'inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full',
      badgeClasses
    ]"
    :title="tooltipText"
  >
    <!-- Anthropic/Claude Logo (Official Sunburst) -->
    <svg
      v-if="isClaudeRuntime"
      class="w-3.5 h-3.5"
      overflow="visible"
      viewBox="0 0 100 101"
      fill="currentColor"
    >
      <path d="M96.0000 40.0000 L99.5002 42.0000 L99.5002 43.5000 L98.5000 47.0000 L56.0000 57.0000 L52.0040 47.0708 L96.0000 40.0000" style="transform-origin: 50px 50px; transform: rotate(330deg) scaleY(1.16883) rotate(-330deg);"/>
      <path d="M80.1032 10.5903 L84.9968 11.6171 L86.2958 13.2179 L87.5346 17.0540 L87.0213 19.5007 L58.5000 58.5000 L49.0000 49.0000 L75.3008 14.4873 L80.1032 10.5903" style="transform-origin: 50px 50px; transform: rotate(300deg) scaleY(1.11716) rotate(-300deg);"/>
      <path d="M55.5002 4.5000 L58.5005 2.5000 L61.0002 3.5000 L63.5002 7.0000 L56.6511 48.1620 L52.0005 45.0000 L50.0005 39.5000 L53.5003 8.5000 L55.5002 4.5000" style="transform-origin: 50px 50px; transform: rotate(270deg) scaleY(1.015) rotate(-270deg);"/>
      <path d="M23.4253 5.1588 L26.5075 1.2217 L28.5175 0.7632 L32.5063 1.3458 L34.4748 2.8868 L48.8202 34.6902 L54.0089 49.8008 L47.9378 53.1760 L24.8009 11.1886 L23.4253 5.1588" style="transform-origin: 50px 50px; transform: rotate(240deg) scaleY(1.03) rotate(-240deg);"/>
      <path d="M8.4990 27.0019 L7.4999 23.0001 L10.5003 19.5001 L14.0003 20.0001 L15.0003 20.0001 L36.0000 35.5000 L42.5000 40.5000 L51.5000 47.5000 L46.5000 56.0000 L42.0002 52.5000 L39.0001 49.5000 L10.0000 29.0001 L8.4990 27.0019" style="transform-origin: 50px 50px; transform: rotate(210deg) scaleY(0.925) rotate(-210deg);"/>
      <path d="M2.5003 53.0000 L0.2370 50.5000 L0.2373 48.2759 L2.5003 47.5000 L28.0000 49.0000 L53.0000 51.0000 L52.1885 55.9782 L4.5000 53.5000 L2.5003 53.0000" style="transform-origin: 50px 50px; transform: rotate(180deg) scaleY(1.09) rotate(-180deg);"/>
      <path d="M17.5002 79.0264 L12.5005 79.0264 L10.5124 76.7369 L10.5124 74.0000 L19.0005 68.0000 L53.5082 46.0337 L57.0005 52.0000 L17.5002 79.0264" style="transform-origin: 50px 50px; transform: rotate(150deg) scaleY(0.985) rotate(-150deg);"/>
      <path d="M27.0004 92.9999 L25.0003 93.4999 L22.0003 91.9999 L22.5004 89.4999 L52.0003 50.5000 L56.0004 55.9999 L34.0003 85.0000 L27.0004 92.9999" style="transform-origin: 50px 50px; transform: rotate(120deg) scaleY(0.955) rotate(-120deg);"/>
      <path d="M51.9998 98.0000 L50.5002 100.0000 L47.5002 101.0000 L45.0001 99.0000 L43.5000 96.0000 L51.0003 55.4999 L55.5001 55.9999 L51.9998 98.0000" style="transform-origin: 50px 50px; transform: rotate(90deg) scaleY(1.08451) rotate(-90deg);"/>
      <path d="M77.5007 86.9997 L77.5007 90.9997 L77.0006 92.4997 L75.0004 93.4997 L71.5006 93.0339 L47.4669 57.2642 L56.9998 50.0002 L64.9994 64.5004 L65.7507 69.7497 L77.5007 86.9997" style="transform-origin: 50px 50px; transform: rotate(60deg) scaleY(1.07317) rotate(-60deg);"/>
      <path d="M89.0008 80.9991 L89.5008 83.4991 L88.0008 85.4991 L86.5007 84.9991 L78.0007 78.9991 L65.0007 67.4991 L55.0007 60.4991 L58.0000 51.0000 L62.9999 54.0001 L66.0007 59.4991 L89.0008 80.9991" style="transform-origin: 50px 50px; transform: rotate(30deg) scaleY(1.08284) rotate(-30deg);"/>
      <path d="M82.5003 55.5000 L95.0003 56.5000 L98.0003 58.5000 L100.0000 61.5000 L100.0000 63.6587 L94.5003 66.0000 L66.5005 59.0000 L55.0003 58.5000 L58.0000 48.0000 L66.0005 54.0000 L82.5003 55.5000" style="transform-origin: 50px 50px; transform: rotate(0deg) scaleY(1.16049) rotate(0deg);"/>
    </svg>

    <!-- Google Gemini Logo (Sparkle/Star) -->
    <svg
      v-else-if="isGeminiRuntime"
      class="w-3.5 h-3.5"
      viewBox="0 0 28 28"
      fill="none"
    >
      <!-- Gemini 4-pointed sparkle star -->
      <path
        d="M14 0C14 7.732 7.732 14 0 14C7.732 14 14 20.268 14 28C14 20.268 20.268 14 28 14C20.268 14 14 7.732 14 0Z"
        :fill="gradientId"
      />
      <defs>
        <linearGradient :id="uniqueGradientId" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#4285F4"/>
          <stop offset="33%" stop-color="#9B72CB"/>
          <stop offset="66%" stop-color="#D96570"/>
          <stop offset="100%" stop-color="#D96570"/>
        </linearGradient>
      </defs>
    </svg>

    <!-- Label -->
    <span v-if="showLabel">{{ label }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  runtime: {
    type: String,
    default: 'claude-code'
  },
  showLabel: {
    type: Boolean,
    default: true
  },
  size: {
    type: String,
    default: 'sm', // 'sm' or 'md'
    validator: (v) => ['sm', 'md'].includes(v)
  }
})

// Generate unique gradient ID to avoid SVG conflicts when multiple badges are rendered
const uniqueGradientId = computed(() => `gemini-gradient-${Math.random().toString(36).substr(2, 9)}`)
const gradientId = computed(() => `url(#${uniqueGradientId.value})`)

const isClaudeRuntime = computed(() => {
  return !props.runtime || props.runtime === 'claude-code' || props.runtime === 'claude'
})

const isGeminiRuntime = computed(() => {
  return props.runtime === 'gemini-cli' || props.runtime === 'gemini'
})

const label = computed(() => {
  if (isClaudeRuntime.value) return 'Claude'
  if (isGeminiRuntime.value) return 'Gemini'
  return props.runtime
})

const tooltipText = computed(() => {
  if (isClaudeRuntime.value) return 'Anthropic Claude Code Runtime'
  if (isGeminiRuntime.value) return 'Google Gemini CLI Runtime'
  return `Runtime: ${props.runtime}`
})

const badgeClasses = computed(() => {
  if (isClaudeRuntime.value) {
    return 'bg-brand-claude-50 dark:bg-brand-claude-950/50 text-brand-claude-700 dark:text-brand-claude-300 border border-brand-claude-200 dark:border-brand-claude-800'
  }
  if (isGeminiRuntime.value) {
    return 'bg-brand-gemini-50 dark:bg-brand-gemini-950/50 text-brand-gemini-700 dark:text-brand-gemini-300 border border-brand-gemini-200 dark:border-brand-gemini-800'
  }
  return 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-600'
})
</script>

